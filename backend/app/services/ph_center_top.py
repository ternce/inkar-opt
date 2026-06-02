from __future__ import annotations

import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _token_value(token: str | None) -> str:
    raw = str(token or "").strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    return raw


def _as_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    text = re.sub(r"[^\d-]+", "", str(value))
    if not text:
        return None
    try:
        number = int(text)
    except Exception:
        return None
    return number if number > 0 else None


def normalize_farmcenter_sku(sku: object) -> str:
    text = str(sku or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s+", "", text)
    text = text.lstrip("0")
    return text or "0"


def extract_goods(data: Any) -> list[dict[str, Any]]:
    rows: Any = None
    if isinstance(data, dict):
        root = data.get("root")
        if isinstance(root, dict):
            goods = root.get("goods")
            if isinstance(goods, dict):
                rows = goods.get("good")
        if rows is None:
            rows = data.get("good")
        if rows is None:
            nested = data.get("data")
            if isinstance(nested, dict):
                rows = nested.get("good")

    if isinstance(rows, dict):
        rows = [rows]
    if isinstance(rows, list):
        out = [row for row in rows if isinstance(row, dict)]
        logger.info("[FARMCENTER_TOP] loaded=%s", len(out))
        return out

    logger.warning("[FARMCENTER_TOP] goods path not found")
    logger.info("[FARMCENTER_TOP] loaded=0")
    return []


class FarmcenterTopService:
    def __init__(self, *, base_url: str, token: str | None, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = _token_value(token)
        self.timeout = timeout

    async def load_top_by_sku(
        self,
        *,
        region: int,
        price_mode: int,
        distributors: list[int],
    ) -> dict[str, int]:
        if not self.token:
            raise RuntimeError("PHCENTER_TOKEN is not configured")
        distributors_param = ",".join(str(x) for x in dict.fromkeys(distributors) if int(x) > 0)
        if not distributors_param:
            raise ValueError("distributors is required")

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            res = await client.get(
                f"{self.base_url}/api/Report/PricesAnalysis",
                params={
                    "region": int(region),
                    "price_mode": int(price_mode),
                    "distributors": distributors_param,
                },
                headers={"Authorization": f"Bearer {self.token}"},
            )
        res.raise_for_status()
        payload = res.json()

        goods = extract_goods(payload)
        top_by_sku: dict[str, int] = {}
        for row in goods:
            if not isinstance(row, dict):
                continue
            sku = normalize_farmcenter_sku(row.get("id"))
            rank = _as_int(row.get("is_top"))
            if sku and rank is not None:
                top_by_sku[sku] = rank

        logger.info("[FARMCENTER_TOP] unique_sku=%s", len(top_by_sku))
        return top_by_sku
