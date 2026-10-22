from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Product
from ..sku import normalize_sku


@dataclass(frozen=True)
class CatalogMaterial:
    code: str
    name: str


@dataclass(frozen=True)
class CatalogRegistrationPreview:
    materials: tuple[CatalogMaterial, ...]
    existing: tuple[CatalogMaterial, ...]
    missing: tuple[CatalogMaterial, ...]

    def as_dict(self) -> dict:
        return {
            "materialCount": len(self.materials),
            "existingProductCount": len(self.existing),
            "missingProductCount": len(self.missing),
            "missingProducts": [{"material": row.code, "materialText": row.name} for row in self.missing],
        }


def get_or_create_product(
    db: Session,
    *,
    code: str,
    name: str | None = None,
) -> tuple[Product, bool]:
    """Shared authoritative internal-catalog registration used by manual and SAP imports."""

    normalized = normalize_sku(code)
    if not normalized:
        raise ValueError("empty SKU")
    row = db.execute(select(Product).where(Product.code == normalized)).scalars().first()
    created = row is None
    if row is None:
        row = Product(code=normalized, name=(name or normalized).strip() or normalized, cost=0)
        db.add(row)
        db.flush()
    elif name and not (row.name or "").strip():
        row.name = name.strip()
    return row, created


def preview_catalog_registration(
    db: Session,
    materials: Iterable[CatalogMaterial],
    *,
    require_exact_18_digit_codes: bool = False,
) -> CatalogRegistrationPreview:
    by_normalized: dict[str, CatalogMaterial] = {}
    for item in materials:
        code = str(item.code or "").strip()
        name = str(item.name or "").strip()
        if require_exact_18_digit_codes and (len(code) != 18 or not code.isdigit()):
            raise ValueError(f"SAP Material must be an exact 18-digit string: {code or 'missing'}")
        normalized = normalize_sku(code)
        if not normalized:
            raise ValueError(f"invalid material identifier: {code or 'missing'}")
        previous = by_normalized.get(normalized)
        current = CatalogMaterial(code=code, name=name)
        if previous is not None:
            if previous.code != current.code:
                raise ValueError(
                    "catalog contains ambiguous identifiers after normalization: "
                    f"{previous.code}, {current.code}"
                )
            if previous.name != current.name:
                raise ValueError(f"SAP Material {code} has conflicting materialText values")
            continue
        by_normalized[normalized] = current

    normalized_codes = set(by_normalized)
    product_ids_by_normalized: dict[str, set[int]] = {}
    if normalized_codes:
        for product_id, product_code in db.execute(select(Product.id, Product.code)):
            normalized = normalize_sku(product_code)
            if normalized in normalized_codes:
                product_ids_by_normalized.setdefault(normalized, set()).add(int(product_id))
    collisions = sorted(code for code, ids in product_ids_by_normalized.items() if len(ids) > 1)
    if collisions:
        raise ValueError(
            f"catalog contains {len(collisions)} normalized-code collisions; sample: {', '.join(collisions[:20])}"
        )

    ordered = tuple(by_normalized[key] for key in sorted(by_normalized))
    existing = tuple(row for row in ordered if normalize_sku(row.code) in product_ids_by_normalized)
    missing = tuple(row for row in ordered if normalize_sku(row.code) not in product_ids_by_normalized)
    if require_exact_18_digit_codes:
        unnamed_missing = [row.code for row in missing if not row.name]
        if unnamed_missing:
            raise ValueError(
                f"SAP Material {unnamed_missing[0]} has no materialText; missing Products require authoritative names"
            )
    return CatalogRegistrationPreview(ordered, existing, missing)


def register_catalog_preview(
    db: Session,
    preview: CatalogRegistrationPreview,
    *,
    approved: bool,
) -> list[Product]:
    if preview.missing and not approved:
        raise ValueError(
            f"catalog registration requires explicit approval for {len(preview.missing)} Products"
        )
    created: list[Product] = []
    for item in preview.missing:
        product, was_created = get_or_create_product(db, code=item.code, name=item.name)
        if was_created:
            created.append(product)
    return created
