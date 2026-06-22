from __future__ import annotations

import io
import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from openpyxl import load_workbook
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..models import BusinessList, BusinessListItem, ListItem, Product, UniversalList
from ..timezone import now_kz_naive, local_iso
from .sku import normalize_external_sku, normalize_sku_variants


SUPPORTED_LIST_TYPES = {"critical", "markup", "exclusion"}
UNIVERSAL_MARKUP_TYPES = {"critical_markup", "min_markup", "max_markup", "fixed_markup", "markup", "percentile_override"}
UNIVERSAL_FIXED_PRICE_TYPES = {"fixed_price", "min_price", "max_price"}
UNIVERSAL_EXCLUSION_TYPES = {"exclusion", "exclude_from_pricing", "no_bend"}
UNIVERSAL_TYPE_LABELS = {
    "фиксированная цена": "fixed_price",
    "минимальная цена": "min_price",
    "максимальная цена": "max_price",
    "минимальная наценка": "min_markup",
    "критическая наценка": "critical_markup",
    "максимальная наценка": "max_markup",
    "исключить из переоценки": "exclude_from_pricing",
    "без прогиба": "no_bend",
    "fixed markup": "fixed_markup",
    "fixed margin": "fixed_markup",
    "фикс наценка": "fixed_markup",
    "фиксированная наценка": "fixed_markup",
    "фиксированная маржа": "fixed_markup",
}
DEFAULT_MAX_UPLOAD_SIZE_MB = 10
DEFAULT_MAX_ROWS = 50_000


IDENTIFIER_ALIASES: dict[str, tuple[str, ...]] = {
    "material": ("material", "sku", "материал"),
    "article": ("article", "артикул"),
    "product_code": ("product code", "product_code", "productcode", "код товара", "код"),
}
IDENTIFIER_PRIORITY = ("material", "article", "product_code")

VALUE_ALIASES = (
    "value",
    "price",
    "fixed price",
    "markup",
    "percentage",
    "critical",
    "critical flag",
    "наценка",
    "критичка",
    "значение",
    "исключение",
    "exclude",
    "excluded",
)

PRICE_TYPE_VALUE_ALIASES: dict[str, tuple[str, ...]] = {
    "fixed_markup": (
        "фикс наценка",
        "фиксированная наценка",
        "наценка",
        "критичка",
        "критическая наценка",
        "значение",
        "%",
        "процент",
    ),
    "max_price": ("макс цена", "максимальная цена", "цена"),
    "min_price": ("мин цена", "минимальная цена", "цена"),
    "fixed_price": ("фикс цена", "фиксированная цена", "цена"),
}

PRICE_TYPE_VALUE_COLUMN_ERRORS: dict[str, str] = {
    "fixed_markup": "Для списка типа Фиксированная наценка нужна колонка значения",
    "max_price": "Для списка типа Максимальная цена нужна колонка значения:\nМакс цена / Максимальная цена / Цена",
    "min_price": "Для списка типа Минимальная цена нужна колонка значения:\nМин цена / Минимальная цена / Цена",
    "fixed_price": "Для списка типа Фиксированная цена нужна колонка значения:\nФикс цена / Фиксированная цена / Цена",
}


def _missing_value_column_error(list_type: str, headers: list[str]) -> str:
    expected = PRICE_TYPE_VALUE_ALIASES.get(list_type, VALUE_ALIASES)
    prefix = PRICE_TYPE_VALUE_COLUMN_ERRORS.get(list_type, "Не найдена колонка значения")
    detected_text = ", ".join(header or "<пусто>" for header in headers) or "<нет заголовков>"
    expected_text = ", ".join(expected)
    return f"{prefix}. Обнаружены заголовки: {detected_text}. Ожидался один из заголовков: {expected_text}."

MANUFACTURER_ALIASES = ("manufacturer", "producer", "производитель")

TRUE_VALUES = {"1", "true", "yes", "critical", "y", "да", "истина"}
FALSE_VALUES = {"0", "false", "no", "n", "нет", "ложь"}


def max_upload_size_bytes() -> int:
    try:
        mb = int(os.getenv("LIST_IMPORT_MAX_UPLOAD_SIZE_MB", str(DEFAULT_MAX_UPLOAD_SIZE_MB)))
    except ValueError:
        mb = DEFAULT_MAX_UPLOAD_SIZE_MB
    return max(1, mb) * 1024 * 1024


def max_rows() -> int:
    try:
        value = int(os.getenv("LIST_IMPORT_MAX_ROWS", str(DEFAULT_MAX_ROWS)))
    except ValueError:
        value = DEFAULT_MAX_ROWS
    return max(1, value)


@dataclass
class ImportIssue:
    row: int
    code: str
    message: str
    identifier: str = ""
    field: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "row": self.row,
            "code": self.code,
            "message": self.message,
            "identifier": self.identifier,
            "field": self.field,
        }


@dataclass
class ParsedListItem:
    product: Product
    sku: str
    manufacturer: str
    value_json: dict[str, Any]
    value_decimal: Decimal | None
    value_bool: bool | None
    source_row: int
    source_identifier: str


@dataclass
class ParsedUniversalListItem:
    product: Product
    value: Decimal
    source_row: int
    source_identifier: str


def normalize_header(value: object) -> str:
    text = str(value or "").strip().casefold()
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _is_empty_row(row: tuple[Any, ...]) -> bool:
    return all(_cell_text(value) == "" for value in row)


def _find_header_indexes(
    headers: list[str],
    *,
    additional_value_aliases: tuple[str, ...] = (),
) -> tuple[dict[str, int], int | None, int | None]:
    identifier_indexes: dict[str, int] = {}
    for identifier_type, aliases in IDENTIFIER_ALIASES.items():
        alias_set = {normalize_header(alias) for alias in aliases}
        for idx, header in enumerate(headers):
            if header in alias_set:
                identifier_indexes[identifier_type] = idx
                break

    value_idx = None
    value_aliases = {normalize_header(alias) for alias in (*VALUE_ALIASES, *additional_value_aliases)}
    for idx, header in enumerate(headers):
        if header in value_aliases:
            value_idx = idx
            break

    manufacturer_idx = None
    manufacturer_aliases = {normalize_header(alias) for alias in MANUFACTURER_ALIASES}
    for idx, header in enumerate(headers):
        if header in manufacturer_aliases:
            manufacturer_idx = idx
            break
    return identifier_indexes, value_idx, manufacturer_idx


def _identifier_for_row(row: tuple[Any, ...], identifier_indexes: dict[str, int]) -> tuple[str, str]:
    for identifier_type in IDENTIFIER_PRIORITY:
        idx = identifier_indexes.get(identifier_type)
        if idx is None or idx >= len(row):
            continue
        value = _cell_text(row[idx])
        if value:
            return identifier_type, value
    return "", ""


def _product_lookup_keys(value: object) -> list[str]:
    raw = normalize_external_sku(value)
    keys: list[str] = []
    for key in [raw, *normalize_sku_variants(value)]:
        if key and key not in keys:
            keys.append(key)
    return keys


def _find_product(db: Session, identifier: object) -> Product | None:
    keys = _product_lookup_keys(identifier)
    if not keys:
        return None
    return db.execute(select(Product).where(Product.code.in_(keys)).limit(1)).scalars().first()


def _parse_bool(value: object) -> bool | None:
    text = _cell_text(value).casefold()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return None


def _parse_markup_percent(value: object) -> Decimal | None:
    text = _cell_text(value).replace(" ", "").replace(",", ".")
    if not text:
        return None
    is_percent = text.endswith("%")
    if is_percent:
        text = text[:-1]
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not is_percent and Decimal("0") < number < Decimal("1"):
        number *= Decimal("100")
    return number.quantize(Decimal("0.000001"))


def _parse_decimal_value(value: object) -> Decimal | None:
    text = _cell_text(value).replace(" ", "").replace(",", ".")
    if not text:
        return None
    try:
        return Decimal(text).quantize(Decimal("0.000001"))
    except (InvalidOperation, ValueError):
        return None


def parse_list_decimal(value: object) -> Decimal | None:
    return _parse_decimal_value(value)


def _format_percent(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text}%"


def _normalize_value(list_type: str, raw_value: object) -> tuple[dict[str, Any], Decimal | None, bool | None, str | None]:
    if list_type == "critical":
        parsed = _parse_bool(raw_value)
        if parsed is None:
            return {}, None, None, "invalid critical value"
        return {"is_critical": parsed}, None, parsed, None
    if list_type == "markup":
        percent = _parse_markup_percent(raw_value)
        if percent is None:
            return {}, None, None, "invalid markup value"
        return {"markup_percent": float(percent), "display": _format_percent(percent)}, percent, None, None
    if list_type == "exclusion":
        parsed = _parse_bool(raw_value)
        if parsed is None and _cell_text(raw_value) == "":
            parsed = True
        if parsed is None:
            return {}, None, None, "invalid exclusion value"
        return {"is_excluded": parsed}, None, parsed, None
    return {}, None, None, "unsupported list type"


def _universal_import_behavior(list_type: str) -> str:
    normalized = str(list_type or "").strip().casefold()
    normalized = UNIVERSAL_TYPE_LABELS.get(normalized, normalized)
    if normalized in UNIVERSAL_MARKUP_TYPES:
        return "markup"
    if normalized in UNIVERSAL_FIXED_PRICE_TYPES:
        return "decimal"
    if normalized in UNIVERSAL_EXCLUSION_TYPES:
        return "exclusion"
    return "decimal"


def is_exclude_from_pricing_type(list_type: str) -> bool:
    normalized = str(list_type or "").strip().casefold()
    normalized = UNIVERSAL_TYPE_LABELS.get(normalized, normalized)
    return normalized in {"exclude_from_pricing", "exclusion"}


def _normalize_universal_value(list_type: str, raw_value: object) -> tuple[Decimal | None, str | None]:
    behavior = _universal_import_behavior(list_type)
    if behavior == "markup":
        value = _parse_markup_percent(raw_value)
        if value is None:
            return None, "invalid markup value"
        return value, None
    if behavior == "exclusion":
        parsed = _parse_bool(raw_value)
        if parsed is None and _cell_text(raw_value) == "":
            parsed = True
        if parsed is None:
            return None, "invalid exclusion value"
        return Decimal("1") if parsed else Decimal("0"), None
    value = _parse_decimal_value(raw_value)
    if value is None:
        return None, "invalid numeric value"
    return value, None


def normalize_universal_list_value(list_type: str, raw_value: object) -> tuple[Decimal | None, str | None]:
    return _normalize_universal_value(list_type, raw_value)


def _summary(total_rows: int = 0) -> dict[str, int]:
    return {
        "total_rows": total_rows,
        "processed": 0,
        "not_found": 0,
        "duplicates": 0,
        "empty_rows": 0,
        "invalid_rows": 0,
        "errors": 0,
    }


def import_business_list_excel(
    *,
    db: Session,
    content: bytes,
    filename: str,
    list_type: str,
) -> dict[str, Any]:
    list_type = str(list_type or "").strip().casefold()
    if list_type not in SUPPORTED_LIST_TYPES:
        raise ValueError("list_type must be one of: critical, markup, exclusion")
    if not filename.lower().endswith(".xlsx"):
        raise ValueError(".xlsx files are supported; .xls support requires an installed reader library")
    if len(content) > max_upload_size_bytes():
        limit_mb = max_upload_size_bytes() // (1024 * 1024)
        raise ValueError(f"file exceeds LIST_IMPORT_MAX_UPLOAD_SIZE_MB={limit_mb}")

    try:
        workbook = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as exc:
        raise ValueError(f"invalid Excel file: {exc}") from exc

    worksheet = workbook.active
    rows_iter = worksheet.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        raise ValueError("empty file")
    if _is_empty_row(header_row):
        raise ValueError("file without headers")

    headers = [normalize_header(value) for value in header_row]
    identifier_indexes, value_idx, manufacturer_idx = _find_header_indexes(headers)
    if not identifier_indexes:
        raise ValueError("missing product identifier column")
    if value_idx is None:
        raise ValueError("missing value column")

    summary = _summary()
    issues: list[ImportIssue] = []
    items_by_product: dict[int, ParsedListItem] = {}

    row_limit = max_rows()
    for excel_row_number, row in enumerate(rows_iter, start=2):
        summary["total_rows"] += 1
        if summary["total_rows"] > row_limit:
            raise ValueError(f"row count exceeds LIST_IMPORT_MAX_ROWS={row_limit}")
        if _is_empty_row(row):
            summary["empty_rows"] += 1
            continue

        identifier_type, identifier = _identifier_for_row(row, identifier_indexes)
        if not identifier:
            summary["invalid_rows"] += 1
            summary["errors"] += 1
            issues.append(ImportIssue(excel_row_number, "missing_required_field", "missing product identifier", field="identifier"))
            continue

        raw_value = row[value_idx] if value_idx is not None and value_idx < len(row) else None
        value_json, value_decimal, value_bool, error = _normalize_value(list_type, raw_value)
        if error:
            summary["invalid_rows"] += 1
            summary["errors"] += 1
            issues.append(ImportIssue(excel_row_number, "invalid_value", error, identifier=identifier, field="value"))
            continue

        product = _find_product(db, identifier)
        if product is None:
            summary["not_found"] += 1
            issues.append(
                ImportIssue(
                    excel_row_number,
                    "product_not_found",
                    f"product not found by {identifier_type}",
                    identifier=identifier,
                    field=identifier_type,
                )
            )
            continue

        if product.id in items_by_product:
            summary["duplicates"] += 1
            issues.append(ImportIssue(excel_row_number, "duplicate_row", "duplicate product row; last row wins", identifier=identifier))

        manufacturer = _cell_text(row[manufacturer_idx]) if manufacturer_idx is not None and manufacturer_idx < len(row) else ""
        items_by_product[product.id] = ParsedListItem(
            product=product,
            sku=product.code,
            manufacturer=manufacturer,
            value_json=value_json,
            value_decimal=value_decimal,
            value_bool=value_bool,
            source_row=excel_row_number,
            source_identifier=identifier,
        )

    summary["processed"] = len(items_by_product)

    try:
        business_list = BusinessList(
            list_type=list_type,
            name=f"{list_type} import {now_kz_naive().strftime('%Y-%m-%d %H:%M:%S')}",
            original_filename=filename,
            status="imported",
            summary_json=json.dumps(summary, ensure_ascii=False),
            errors_json=json.dumps([issue.to_dict() for issue in issues], ensure_ascii=False),
            item_count=len(items_by_product),
        )
        db.add(business_list)
        db.flush()

        for item in items_by_product.values():
            db.add(
                BusinessListItem(
                    business_list_id=business_list.id,
                    product_id=item.product.id,
                    sku=item.sku,
                    product_name=item.product.name,
                    manufacturer=item.manufacturer,
                    value_json=json.dumps(item.value_json, ensure_ascii=False),
                    value_decimal=item.value_decimal,
                    value_bool=item.value_bool,
                    source_row=item.source_row,
                    source_identifier=item.source_identifier,
                )
            )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    db.refresh(business_list)
    return business_list_to_dict(business_list, include_errors=True)


def import_universal_list_excel(
    *,
    db: Session,
    universal_list: UniversalList,
    content: bytes,
    filename: str,
) -> dict[str, Any]:
    if not filename.lower().endswith(".xlsx"):
        raise ValueError(".xlsx files are supported; .xls support requires an installed reader library")
    if len(content) > max_upload_size_bytes():
        limit_mb = max_upload_size_bytes() // (1024 * 1024)
        raise ValueError(f"file exceeds LIST_IMPORT_MAX_UPLOAD_SIZE_MB={limit_mb}")

    try:
        workbook = load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    except Exception as exc:
        raise ValueError(f"invalid Excel file: {exc}") from exc

    worksheet = workbook.active
    rows_iter = worksheet.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        raise ValueError("empty file")
    if _is_empty_row(header_row):
        raise ValueError("file without headers")

    headers = [normalize_header(value) for value in header_row]
    normalized_list_type = str(universal_list.type or "").strip().casefold()
    normalized_list_type = UNIVERSAL_TYPE_LABELS.get(normalized_list_type, normalized_list_type)
    identifier_indexes, value_idx, _manufacturer_idx = _find_header_indexes(
        headers,
        additional_value_aliases=PRICE_TYPE_VALUE_ALIASES.get(normalized_list_type, ()),
    )
    if not identifier_indexes:
        raise ValueError("missing product identifier column")
    exclusion_by_presence = is_exclude_from_pricing_type(str(universal_list.type or ""))
    if value_idx is None and not exclusion_by_presence:
        raise ValueError(_missing_value_column_error(normalized_list_type, headers))

    summary = _summary()
    issues: list[ImportIssue] = []
    items_by_product: dict[int, ParsedUniversalListItem] = {}

    row_limit = max_rows()
    for excel_row_number, row in enumerate(rows_iter, start=2):
        summary["total_rows"] += 1
        if summary["total_rows"] > row_limit:
            raise ValueError(f"row count exceeds LIST_IMPORT_MAX_ROWS={row_limit}")
        if _is_empty_row(row):
            summary["empty_rows"] += 1
            continue

        identifier_type, identifier = _identifier_for_row(row, identifier_indexes)
        if not identifier:
            summary["invalid_rows"] += 1
            summary["errors"] += 1
            issues.append(ImportIssue(excel_row_number, "missing_required_field", "missing product identifier", field="identifier"))
            continue

        raw_value = row[value_idx] if value_idx is not None and value_idx < len(row) else None
        value, error = _normalize_universal_value(str(universal_list.type or ""), raw_value)
        if error or value is None:
            summary["invalid_rows"] += 1
            summary["errors"] += 1
            issues.append(ImportIssue(excel_row_number, "invalid_value", error or "invalid value", identifier=identifier, field="value"))
            continue

        product = _find_product(db, identifier)
        if product is None:
            summary["not_found"] += 1
            issues.append(
                ImportIssue(
                    excel_row_number,
                    "product_not_found",
                    f"product not found by {identifier_type}",
                    identifier=identifier,
                    field=identifier_type,
                )
            )
            continue

        if product.id in items_by_product:
            summary["duplicates"] += 1
            issues.append(ImportIssue(excel_row_number, "duplicate_row", "duplicate product row; last row wins", identifier=identifier))

        items_by_product[product.id] = ParsedUniversalListItem(
            product=product,
            value=value,
            source_row=excel_row_number,
            source_identifier=identifier,
        )

    invalid_value_issues = [issue for issue in issues if issue.code == "invalid_value"]
    if invalid_value_issues:
        first = invalid_value_issues[0]
        raise ValueError(
            f"Некорректное значение в строке {first.row}: {first.message}. "
            "Допустимы числа вида 10, 10,5 или 10.5."
        )

    summary["processed"] = len(items_by_product)

    try:
        for item in items_by_product.values():
            existing = db.execute(
                select(ListItem)
                .where(ListItem.universal_list_id == universal_list.id)
                .where(ListItem.product_id == item.product.id)
            ).scalars().first()
            if existing:
                existing.value = item.value
            else:
                db.add(ListItem(universal_list_id=universal_list.id, product_id=item.product.id, value=item.value))
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

    item_count = db.scalar(select(func.count(ListItem.id)).where(ListItem.universal_list_id == universal_list.id))
    if item_count is None:
        item_count = 0
    return {
        "status": "ok",
        "list_id": universal_list.id,
        "list_type": str(universal_list.type or ""),
        "filename": filename,
        "item_count": int(item_count),
        "summary": summary,
        "errors": [issue.to_dict() for issue in issues],
    }


def business_list_to_dict(row: BusinessList, *, include_errors: bool = False, item_preview: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload = {
        "id": row.id,
        "list_type": row.list_type,
        "name": row.name,
        "filename": row.original_filename,
        "status": row.status,
        "item_count": row.item_count,
        "summary": json.loads(row.summary_json or "{}"),
        "created_at": local_iso(row.created_at) if row.created_at else None,
        "updated_at": local_iso(row.updated_at) if row.updated_at else None,
    }
    if include_errors:
        payload["errors"] = json.loads(row.errors_json or "[]")
    if item_preview is not None:
        payload["items"] = item_preview
    return payload


def business_list_item_to_dict(row: BusinessListItem) -> dict[str, Any]:
    return {
        "id": row.id,
        "product_id": row.product_id,
        "sku": row.sku,
        "product_name": row.product_name,
        "manufacturer": row.manufacturer,
        "value": json.loads(row.value_json or "{}"),
        "value_decimal": float(row.value_decimal) if row.value_decimal is not None else None,
        "value_bool": row.value_bool,
        "source_row": row.source_row,
        "source_identifier": row.source_identifier,
    }
