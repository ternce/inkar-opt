from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from backend.app.models import InternalProductNormalized, Product, ProductExtra
from backend.app.services.vidman_normalization import ParsedVidmanItem, parse_vidman_product
from backend.app.timezone import now_kz_naive


DEFAULT_BATCH_SIZE = 500


@dataclass(frozen=True)
class InternalProductRow:
    product: Product
    manufacturer: str
    source_updated_at: object | None


@dataclass
class InternalProductAudit:
    total_products: int = 0
    products_with_manufacturer: int = 0
    products_without_manufacturer: int = 0
    products_with_provisor_goods_id: int = 0
    products_without_provisor_goods_id: int = 0
    pattern_counts: dict[str, int] = field(default_factory=dict)
    sample_names: list[dict[str, object]] = field(default_factory=list)


@dataclass
class InternalNormalizationSummary:
    total_products: int = 0
    processed: int = 0
    normalized_products: int = 0
    unresolved: int = 0
    warning_rows: int = 0
    unique_normalized_signatures: int = 0
    duplicate_signature_groups: int = 0
    products_in_duplicate_signature_groups: int = 0
    missing_manufacturers: int = 0
    parse_warning_distribution: dict[str, int] = field(default_factory=dict)
    suspicious_numeric_conflicts: int = 0
    elapsed_seconds: float = 0
    rows_per_sec: float = 0
    sample_rows: list[dict[str, object]] = field(default_factory=list)

    def metrics(self) -> dict[str, object]:
        return {
            "TOTAL_PRODUCTS": self.total_products,
            "NORMALIZED_PRODUCTS": self.normalized_products,
            "UNRESOLVED": self.unresolved,
            "WARNING_ROWS": self.warning_rows,
            "UNIQUE_NORMALIZED_SIGNATURES": self.unique_normalized_signatures,
            "DUPLICATE_SIGNATURE_GROUPS": self.duplicate_signature_groups,
            "PRODUCTS_IN_DUPLICATE_SIGNATURE_GROUPS": self.products_in_duplicate_signature_groups,
            "MISSING_MANUFACTURERS": self.missing_manufacturers,
            "SUSPICIOUS_NUMERIC_CONFLICTS": self.suspicious_numeric_conflicts,
            "ROWS_PER_SEC": round(self.rows_per_sec, 2),
            "PARSE_WARNING_DISTRIBUTION": self.parse_warning_distribution,
        }


def _json_list(values: list[str] | tuple[str, ...]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def _token_with_prefix(parsed: ParsedVidmanItem, prefix: str) -> str:
    return "|".join(token for token in parsed.identity_tokens if token.startswith(prefix))


def parse_internal_product(raw_name: str | None, raw_manufacturer: str | None = "") -> ParsedVidmanItem:
    parsed = parse_vidman_product(raw_name, raw_manufacturer)
    warnings = list(parsed.parse_warnings)
    if not parsed.normalized_manufacturer:
        warnings.append("missing_manufacturer")
    if not parsed.normalized_signature:
        warnings.append("missing_normalized_signature")
    return ParsedVidmanItem(**{**parsed.__dict__, "parse_warnings": list(dict.fromkeys(warnings))})


def _row_to_payload(row: InternalProductRow, parsed: ParsedVidmanItem) -> dict[str, object]:
    return {
        "product_id": row.product.id,
        "raw_name": row.product.name or "",
        "raw_manufacturer": row.manufacturer or "",
        "normalized_name": parsed.normalized_name,
        "base_name": parsed.base_name,
        "normalized_manufacturer": parsed.normalized_manufacturer,
        "dosage_value": parsed.dosage_value,
        "dosage_unit": parsed.dosage_unit,
        "strength_components": _token_with_prefix(parsed, "strengths:"),
        "concentration_value": parsed.concentration_value,
        "concentration_unit": parsed.concentration_unit,
        "volume_value": parsed.volume_value,
        "volume_unit": parsed.volume_unit,
        "weight_value": parsed.weight_value,
        "weight_unit": parsed.weight_unit,
        "package_volume": _token_with_prefix(parsed, "package_volume:"),
        "package_weight": _token_with_prefix(parsed, "package_weight:"),
        "pack_count": parsed.pack_count,
        "dosage_form": parsed.dosage_form,
        "variant_text": parsed.variant_text,
        "identity_tokens_json": _json_list(parsed.identity_tokens),
        "normalized_signature": parsed.normalized_signature,
        "parse_warnings_json": _json_list(parsed.parse_warnings),
        "parse_confidence": parsed.parse_confidence,
        "source_updated_at": row.source_updated_at,
        "normalized_at": now_kz_naive(),
    }


def _apply_payload(target: InternalProductNormalized, payload: dict[str, object]) -> None:
    for key, value in payload.items():
        setattr(target, key, value)


def _query_internal_rows(db: Session):
    return (
        select(Product, ProductExtra.manufacturer, ProductExtra.updated_at)
        .outerjoin(ProductExtra, ProductExtra.product_id == Product.id)
        .order_by(Product.id)
    )


def audit_internal_products(db: Session, *, sample_limit: int = 80) -> InternalProductAudit:
    audit = InternalProductAudit()
    audit.total_products = int(db.scalar(select(func.count(Product.id))) or 0)
    audit.products_with_manufacturer = int(
        db.scalar(
            select(func.count(Product.id))
            .join(ProductExtra, ProductExtra.product_id == Product.id)
            .where(func.coalesce(func.nullif(func.trim(ProductExtra.manufacturer), ""), "") != "")
        )
        or 0
    )
    audit.products_without_manufacturer = audit.total_products - audit.products_with_manufacturer
    audit.products_with_provisor_goods_id = int(
        db.scalar(select(func.count(Product.id)).where(Product.provisor_goods_id.is_not(None))) or 0
    )
    audit.products_without_provisor_goods_id = audit.total_products - audit.products_with_provisor_goods_id

    rows = [
        InternalProductRow(product=product, manufacturer=manufacturer or "", source_updated_at=updated_at)
        for product, manufacturer, updated_at in db.execute(_query_internal_rows(db))
    ]
    patterns = {
        "dosage_mg": lambda p: p.dosage_value is not None and p.dosage_unit == "mg",
        "dosage_mcg": lambda p: p.dosage_value is not None and p.dosage_unit == "mcg",
        "dosage_iu": lambda p: p.dosage_value is not None and p.dosage_unit == "iu",
        "percentage": lambda p: p.concentration_unit == "percent",
        "ratio_concentration": lambda p: any(t.startswith("ratio:") for t in p.identity_tokens),
        "multi_strength": lambda p: any(t.startswith("strengths:") for t in p.identity_tokens),
        "package_volume": lambda p: p.volume_value is not None or any(t.startswith("package_volume:") for t in p.identity_tokens),
        "package_weight": lambda p: p.weight_value is not None or any(t.startswith("package_weight:") for t in p.identity_tokens),
        "pack_count": lambda p: p.pack_count is not None,
        "dosage_form": lambda p: bool(p.dosage_form),
        "unparsed_numeric_warning": lambda p: "unparsed_numeric_token" in p.parse_warnings,
        "missing_manufacturer": lambda p: not p.normalized_manufacturer,
    }
    counts = {name: 0 for name in patterns}
    samples: list[dict[str, object]] = []
    for row in rows:
        parsed = parse_internal_product(row.product.name, row.manufacturer)
        for name, predicate in patterns.items():
            if predicate(parsed):
                counts[name] += 1
        if len(samples) < sample_limit:
            samples.append(
                {
                    "product_id": row.product.id,
                    "name": row.product.name,
                    "manufacturer": row.manufacturer,
                    "base_name": parsed.base_name,
                    "signature": parsed.normalized_signature,
                    "warnings": parsed.parse_warnings,
                }
            )
    audit.pattern_counts = counts
    audit.sample_names = samples
    return audit


def _post_rebuild_metrics(db: Session, summary: InternalNormalizationSummary) -> None:
    summary.normalized_products = int(db.scalar(select(func.count(InternalProductNormalized.id))) or 0)
    summary.unresolved = int(
        db.scalar(
            select(func.count(InternalProductNormalized.id)).where(
                InternalProductNormalized.normalized_signature == ""
            )
        )
        or 0
    )
    summary.warning_rows = int(
        db.scalar(
            select(func.count(InternalProductNormalized.id)).where(
                InternalProductNormalized.parse_warnings_json != "[]"
            )
        )
        or 0
    )
    summary.unique_normalized_signatures = int(
        db.scalar(
            select(func.count(func.distinct(InternalProductNormalized.normalized_signature))).where(
                InternalProductNormalized.normalized_signature != ""
            )
        )
        or 0
    )
    duplicate_rows = db.execute(
        select(InternalProductNormalized.normalized_signature, func.count(InternalProductNormalized.id))
        .where(InternalProductNormalized.normalized_signature != "")
        .group_by(InternalProductNormalized.normalized_signature)
        .having(func.count(InternalProductNormalized.id) > 1)
    ).all()
    summary.duplicate_signature_groups = len(duplicate_rows)
    summary.products_in_duplicate_signature_groups = sum(int(count) for _signature, count in duplicate_rows)
    summary.missing_manufacturers = int(
        db.scalar(
            select(func.count(InternalProductNormalized.id)).where(
                InternalProductNormalized.normalized_manufacturer == ""
            )
        )
        or 0
    )

    warning_distribution: dict[str, int] = {}
    for raw_warnings in db.scalars(select(InternalProductNormalized.parse_warnings_json)):
        for warning in json.loads(raw_warnings or "[]"):
            warning_distribution[warning] = warning_distribution.get(warning, 0) + 1
    summary.parse_warning_distribution = dict(sorted(warning_distribution.items()))

    summary.suspicious_numeric_conflicts = int(
        db.scalar(
            select(func.count()).select_from(
                select(
                    InternalProductNormalized.base_name,
                    InternalProductNormalized.normalized_manufacturer,
                )
                .where(InternalProductNormalized.base_name != "")
                .group_by(
                    InternalProductNormalized.base_name,
                    InternalProductNormalized.normalized_manufacturer,
                )
                .having(func.count(func.distinct(InternalProductNormalized.normalized_signature)) > 1)
                .subquery()
            )
        )
        or 0
    )


def process_internal_product_normalization(
    db: Session,
    *,
    dry_run: bool = True,
    rebuild: bool = False,
    limit: int | None = None,
    product_id: int | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    verbose: bool = False,
) -> InternalNormalizationSummary:
    started = time.monotonic()
    summary = InternalNormalizationSummary()
    query = _query_internal_rows(db)
    if product_id is not None:
        query = query.where(Product.id == product_id)
    summary.total_products = int(db.scalar(select(func.count()).select_from(query.subquery())) or 0)

    if rebuild and not dry_run:
        delete_query = delete(InternalProductNormalized)
        if product_id is not None:
            delete_query = delete_query.where(InternalProductNormalized.product_id == product_id)
        db.execute(delete_query)
        db.flush()

    existing: dict[int, InternalProductNormalized] = {}
    if not dry_run:
        existing = {
            row.product_id: row
            for row in db.scalars(select(InternalProductNormalized))
        }

    remaining = limit
    offset = 0
    signature_counts: dict[str, int] = {}
    while remaining is None or remaining > 0:
        current_limit = batch_size if remaining is None else min(batch_size, remaining)
        rows = [
            InternalProductRow(product=product, manufacturer=manufacturer or "", source_updated_at=updated_at)
            for product, manufacturer, updated_at in db.execute(query.offset(offset).limit(current_limit))
        ]
        if not rows:
            break
        for row in rows:
            parsed = parse_internal_product(row.product.name, row.manufacturer)
            payload = _row_to_payload(row, parsed)
            if not dry_run:
                target = existing.get(row.product.id)
                if target is None:
                    target = InternalProductNormalized(product_id=row.product.id)
                    db.add(target)
                    existing[row.product.id] = target
                _apply_payload(target, payload)
            if parsed.parse_warnings:
                summary.warning_rows += 1
                for warning in parsed.parse_warnings:
                    summary.parse_warning_distribution[warning] = summary.parse_warning_distribution.get(warning, 0) + 1
            if not parsed.normalized_signature:
                summary.unresolved += 1
            else:
                signature_counts[parsed.normalized_signature] = signature_counts.get(parsed.normalized_signature, 0) + 1
            if len(summary.sample_rows) < 50:
                summary.sample_rows.append(
                    {
                        "product_id": row.product.id,
                        "name": row.product.name,
                        "manufacturer": row.manufacturer,
                        "base_name": parsed.base_name,
                        "signature": parsed.normalized_signature,
                        "warnings": parsed.parse_warnings,
                    }
                )
            if verbose:
                print(f"product_id={row.product.id} signature={parsed.normalized_signature} warnings={parsed.parse_warnings}")
            summary.processed += 1
        offset += len(rows)
        if remaining is not None:
            remaining -= len(rows)
        if not dry_run:
            db.flush()

    if dry_run:
        summary.normalized_products = summary.processed
        summary.unique_normalized_signatures = len(signature_counts)
        duplicate_counts = [count for count in signature_counts.values() if count > 1]
        summary.duplicate_signature_groups = len(duplicate_counts)
        summary.products_in_duplicate_signature_groups = sum(duplicate_counts)
        summary.missing_manufacturers = summary.parse_warning_distribution.get("missing_manufacturer", 0)
        db.rollback()
    else:
        db.commit()
        _post_rebuild_metrics(db, summary)

    summary.elapsed_seconds = time.monotonic() - started
    summary.rows_per_sec = summary.processed / summary.elapsed_seconds if summary.elapsed_seconds else 0
    return summary
