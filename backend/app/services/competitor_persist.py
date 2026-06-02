from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import data
from ..models import CompetitorPrice, PriceFormat, Product
from .sku import normalize_external_sku, normalize_sku, normalize_sku_variants


@dataclass(frozen=True)
class PersistStats:
    sources: int
    incoming_rows: int
    matched_products: int
    inserted_prices: int
    missing_products: int

    def to_dict(self) -> dict[str, int]:
        return {
            "sources": int(self.sources),
            "incoming_rows": int(self.incoming_rows),
            "matched_products": int(self.matched_products),
            "inserted_prices": int(self.inserted_prices),
            "missing_products": int(self.missing_products),
        }


def _as_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except Exception:
            return None

    s = str(value).strip()
    if not s:
        return None

    # common: "1 234,56" / "1234,56"
    s = s.replace(" ", "").replace(",", ".")
    try:
        return Decimal(s)
    except Exception:
        return None


def _chunks(items: list[str], chunk_size: int = 900) -> Iterable[list[str]]:
    # Keep below typical SQL parameter limits.
    for i in range(0, len(items), chunk_size):
        yield items[i : i + chunk_size]


def _ensure_price_format(db: Session, price_format_code: str) -> PriceFormat:
    pf = db.execute(select(PriceFormat).where(PriceFormat.code == price_format_code)).scalars().first()
    if pf:
        return pf

    meta = next((x for x in data.PRICE_FORMATS if x.get("code") == price_format_code), None)
    pf = PriceFormat(
        code=price_format_code,
        name=(meta.get("name") if meta else None) or price_format_code,
        branch=(meta.get("branch") if meta else "") or "",
    )
    db.add(pf)
    db.flush()
    return pf


def persist_phcenter_report(
    *,
    db: Session,
    price_format_code: str,
    report: dict[str, Any],
    distributor_codes: list[int],
    as_of: date | None = None,
) -> PersistStats:
    pf = _ensure_price_format(db, price_format_code)
    today = as_of or date.today()

    # 1) ensure sources configs exist
    sources = [f"phcenter:{code}" for code in distributor_codes]

    # Drop phcenter:* sources that are no longer selected (and their price rows)
    existing_phcenter = (
        db.execute(
            select(CompetitorPrice.source_name)
            .where(CompetitorPrice.price_format_id == pf.id)
            .where(CompetitorPrice.product_id.is_(None))
            .where(CompetitorPrice.source_name.like("phcenter:%"))
        )
        .scalars()
        .all()
    )
    to_delete = [s for s in existing_phcenter if s not in set(sources)]
    if to_delete:
        db.execute(
            delete(CompetitorPrice)
            .where(CompetitorPrice.price_format_id == pf.id)
            .where(CompetitorPrice.source_name.in_(to_delete))
        )

    for src in sources:
        cfg = (
            db.execute(
                select(CompetitorPrice)
                .where(CompetitorPrice.price_format_id == pf.id)
                .where(CompetitorPrice.product_id.is_(None))
                .where(CompetitorPrice.source_name == src)
            )
            .scalars()
            .first()
        )
        if cfg is None:
            db.add(
                CompetitorPrice(
                    price_format_id=pf.id,
                    product_id=None,
                    source_name=src,
                    supplier="ph.center",
                    coefficient=1.0,
                )
            )

    db.flush()

    # 2) wipe previous prices for these sources (keep configs)
    if sources:
        db.execute(
            delete(CompetitorPrice)
            .where(CompetitorPrice.price_format_id == pf.id)
            .where(CompetitorPrice.product_id.is_not(None))
            .where(CompetitorPrice.source_name.in_(sources))
        )

    # 3) extract rows
    raw = None
    try:
        raw = (
            report.get("root", {}).get("goods", {}).get("good")
            or report.get("goods", {}).get("good")
            or report.get("good")
        )
    except Exception:
        raw = None

    if raw is None:
        rows: list[dict[str, Any]] = []
    elif isinstance(raw, list):
        rows = [x for x in raw if isinstance(x, dict)]
    elif isinstance(raw, dict):
        rows = [raw]
    else:
        rows = []

    # 4) map Product.code -> id
    codes: list[str] = []
    for x in rows:
        # Prefer "id" from report, fallback to ink_id
        c_raw = x.get("id") or x.get("ink_id")
        c = normalize_sku(c_raw) or ""
        if c:
            codes.append(c)

    codes = list(dict.fromkeys(codes))

    code_to_id: dict[str, int] = {}
    if codes:
        for part in _chunks(codes):
            found = db.execute(select(Product.code, Product.id).where(Product.code.in_(part))).all()
            for code, pid in found:
                code_to_id[str(code)] = int(pid)

    inserted = 0
    matched = 0

    for x in rows:
        code = normalize_sku(x.get("id") or x.get("ink_id")) or ""
        if not code:
            continue
        pid = code_to_id.get(code)
        if pid is None:
            continue
        matched += 1

        # ph.center returns columns as distr1_price, distr2_price, ...
        # where the index corresponds to the order of requested distributors.
        # Keep compatibility with both styles:
        # - old/assumed: distr{code}_price
        # - actual:      distr{index}_price
        for idx, d in enumerate(distributor_codes, start=1):
            src = f"phcenter:{d}"

            price_val = x.get(f"distr{idx}_price")
            if price_val is None:
                price_val = x.get(f"distr{idx}_notdisc")

            if price_val is None:
                price_val = x.get(f"distr{d}_price")
            if price_val is None:
                price_val = x.get(f"distr{d}_notdisc")

            dec = _as_decimal(price_val)
            if dec is None or dec <= 0:
                continue

            db.add(
                CompetitorPrice(
                    price_format_id=pf.id,
                    product_id=pid,
                    source_name=src,
                    supplier="ph.center",
                    price_date=today,
                    coefficient=1.0,
                    source_price=float(dec),
                )
            )
            inserted += 1

    db.commit()

    missing_products = len(codes) - len(code_to_id)
    return PersistStats(
        sources=len(sources),
        incoming_rows=len(rows),
        matched_products=matched,
        inserted_prices=inserted,
        missing_products=missing_products if missing_products >= 0 else 0,
    )


def persist_provisor_prices(
    *,
    db: Session,
    price_format_code: str,
    filial_id: int,
    items: list[dict[str, Any]],
    as_of: date | None = None,
) -> PersistStats:
    pf = _ensure_price_format(db, price_format_code)
    today = as_of or date.today()

    src = f"provisor:{filial_id}"

    cfg = (
        db.execute(
            select(CompetitorPrice)
            .where(CompetitorPrice.price_format_id == pf.id)
            .where(CompetitorPrice.product_id.is_(None))
            .where(CompetitorPrice.source_name == src)
        )
        .scalars()
        .first()
    )
    if cfg is None:
        db.add(
            CompetitorPrice(
                price_format_id=pf.id,
                product_id=None,
                source_name=src,
                supplier="provisor",
                coefficient=1.0,
            )
        )
        db.flush()

    db.execute(
        delete(CompetitorPrice)
        .where(CompetitorPrice.price_format_id == pf.id)
        .where(CompetitorPrice.product_id.is_not(None))
        .where(CompetitorPrice.source_name == src)
    )

    incoming_skus: list[str] = []
    for x in items:
        c = normalize_external_sku(x.get("distributorGoodsId"))
        if c:
            incoming_skus.append(c)

    uniq_skus = list(dict.fromkeys(incoming_skus))

    code_to_id: dict[str, int] = {}
    product_rows = db.execute(select(Product.code, Product.id)).all()
    for code, pid in product_rows:
        for variant in normalize_sku_variants(code):
            code_to_id[variant] = int(pid)

    inserted = 0
    matched = 0
    matched_skus: set[str] = set()

    for x in items:
        raw_sku = normalize_external_sku(x.get("distributorGoodsId"))
        sku = next((v for v in normalize_sku_variants(raw_sku) if v in code_to_id), None) or raw_sku
        if not sku:
            continue

        pid: int | None = None
        direct_pid = code_to_id.get(sku)
        if direct_pid is not None:
            pid = int(direct_pid)
            matched_skus.add(sku)

        if pid is None:
            continue

        matched += 1

        # Provisor часто возвращает goodsPriceWithUserDiscount=0.0,
        # когда скидка не применена — в этом случае нужно брать goodsPrice.
        disc = _as_decimal(x.get("goodsPriceWithUserDiscount"))
        base = _as_decimal(x.get("goodsPrice"))

        dec = None
        if disc is not None and disc > 0:
            dec = disc
        elif base is not None and base > 0:
            dec = base

        if dec is None:
            continue

        db.add(
            CompetitorPrice(
                price_format_id=pf.id,
                product_id=pid,
                source_name=src,
                supplier="provisor",
                price_date=today,
                coefficient=1.0,
                source_price=float(dec),
            )
        )
        inserted += 1

    db.commit()

    missing_products = len(set(uniq_skus)) - len(matched_skus)
    return PersistStats(
        sources=1,
        incoming_rows=len(items),
        matched_products=matched,
        inserted_prices=inserted,
        missing_products=missing_products if missing_products >= 0 else 0,
    )
