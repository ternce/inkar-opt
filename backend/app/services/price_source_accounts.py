from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import PriceSourceAccount
from .price_source_security import decrypt_secret, encrypt_secret
from .competitor_price_lists import upsert_unified_price_list
from .price_sources import (
    PriceSourceAccountCredentials,
    ProvisorPriceService,
    VidmanPriceService,
    account_config,
)

logger = logging.getLogger(__name__)


def _timing(operation: str, step: str, started_at: float) -> None:
    logger.info("[TIMING] operation=%s step=%s elapsed_ms=%s", operation, step, round((time.perf_counter() - started_at) * 1000, 2))


def account_to_dict(row: PriceSourceAccount) -> dict[str, Any]:
    config = account_config(row.config_json)
    return {
        "id": row.id,
        "name": str(config.get("name") or config.get("title") or row.login),
        "sourceType": row.source_type,
        "login": row.login,
        "status": row.status,
        "statusMessage": row.status_message,
        "lastSuccessAt": row.last_success_at.isoformat() if row.last_success_at else "",
        "priceListsCount": int(row.price_lists_count or 0),
        "isActive": bool(row.is_active),
        "createdAt": row.created_at.isoformat() if row.created_at else "",
        "updatedAt": row.updated_at.isoformat() if row.updated_at else "",
        "config": config,
    }


def credentials_from_row(row: PriceSourceAccount) -> PriceSourceAccountCredentials:
    return PriceSourceAccountCredentials(
        id=int(row.id),
        source_type=row.source_type,
        login=row.login,
        password=decrypt_secret(row.encrypted_password, get_settings().price_source_secret),
        config=account_config(row.config_json),
    )


def adapter_for_source(source_type: str):
    settings = get_settings()
    if source_type == "provisor":
        return ProvisorPriceService(base_url=settings.provisor_base_url)
    if source_type == "vidman":
        return VidmanPriceService(
            base_url=settings.vidman_base_url,
            login_path=settings.vidman_login_path,
            price_base_url=settings.vidman_price_base_url,
        )
    raise ValueError(f"unsupported source type: {source_type}")


def upsert_account(
    *,
    db: Session,
    source_type: str,
    login: str,
    password: str,
    config: dict[str, Any] | None = None,
) -> PriceSourceAccount:
    source_type = source_type.strip().lower()
    if source_type not in {"provisor", "vidman"}:
        raise ValueError("source_type must be provisor or vidman")
    login = login.strip()
    if not login:
        raise ValueError("login is required")
    if not password:
        raise ValueError("password is required")

    row = (
        db.execute(
            select(PriceSourceAccount)
            .where(PriceSourceAccount.source_type == source_type)
            .where(PriceSourceAccount.login == login)
        )
        .scalars()
        .first()
    )
    if row is None:
        row = PriceSourceAccount(source_type=source_type, login=login)
        db.add(row)
        db.flush()

    row.encrypted_password = encrypt_secret(password, get_settings().price_source_secret)
    row.config_json = json.dumps(config or {}, ensure_ascii=False)
    row.is_active = True
    row.updated_at = datetime.utcnow()
    if not row.status:
        row.status = "not_checked"
    db.commit()
    return row


async def test_account_connection(*, db: Session, account_id: int, price_format_code: str | None = None) -> PriceSourceAccount:
    operation = f"price_source_account_test:{account_id}"
    started_at = time.perf_counter()
    _timing(operation, "start", started_at)
    row = db.get(PriceSourceAccount, account_id)
    if row is None:
        _timing(operation, "load_account_from_db", started_at)
        raise ValueError("account not found")
    _timing(operation, "load_account_from_db", started_at)
    adapter = adapter_for_source(row.source_type)
    credentials = credentials_from_row(row)
    auth_mode = str(credentials.config.get("authMode") or ("playwright" if row.source_type == "vidman" else "auto"))
    logger.info(
        "Testing price source account start: account_id=%s sourceType=%s authMode=%s login=%s format_code=%s",
        row.id,
        row.source_type,
        auth_mode,
        row.login,
        price_format_code,
    )
    try:
        ok, message = await adapter.test_connection(credentials)
    except Exception:
        logger.exception("[TIMING] operation=%s step=exception elapsed_ms=%s", operation, round((time.perf_counter() - started_at) * 1000, 2))
        raise
    _timing(operation, "client.login", started_at)
    logger.info(
        "Price source test_connection result: account_id=%s sourceType=%s authMode=%s ok=%s message=%s elapsed=%.2fs",
        row.id,
        row.source_type,
        auth_mode,
        ok,
        message,
        time.perf_counter() - started_at,
    )
    price_lists_count = 0
    if ok and row.source_type == "vidman":
        logger.info(
            "Skipping Vidman fetch_price_lists during connection test: account_id=%s elapsed=%.2fs",
            row.id,
            time.perf_counter() - started_at,
        )
    elif ok:
        try:
            price_lists = await adapter.fetch_price_lists(credentials)
            price_lists_count = len(price_lists)
            logger.info(
                "Price source fetch_price_lists result: account_id=%s sourceType=%s authMode=%s priceListsCount=%s",
                row.id,
                row.source_type,
                auth_mode,
                price_lists_count,
            )
            if price_format_code and price_lists:
                saved_count = 0
                for price_list in price_lists:
                    try:
                        upsert_unified_price_list(
                            db=db,
                            price_format_code=price_format_code,
                            price_list=price_list,
                            items=[],
                            status="listed",
                            run_matching=False,
                        )
                        saved_count += 1
                    except Exception:
                        db.rollback()
                        logger.exception(
                            "Failed to save source price list during connection test: source=%s account_id=%s payload=%s",
                            row.source_type,
                            row.id,
                            {
                                "source": price_list.source,
                                "accountId": price_list.account_id,
                                "priceListId": price_list.price_list_id,
                                "priceListName": price_list.price_list_name,
                            },
                        )
                logger.info(
                    "%s saved %s price list headers for format=%s account_id=%s",
                    row.source_type.capitalize(),
                    saved_count,
                    price_format_code,
                    row.id,
                )
            if price_lists_count <= 0:
                ok = False
                message = "Авторизация успешна, но прайсы не найдены"
        except Exception as e:
            logger.exception(
                "Price source fetch_price_lists failed during connection test: account_id=%s sourceType=%s authMode=%s",
                row.id,
                row.source_type,
                auth_mode,
            )
            ok = False
            message = str(e)
    row.status = "connected" if ok else _status_from_message(message)
    row.status_message = message[:512]
    row.price_lists_count = price_lists_count
    row.updated_at = datetime.utcnow()
    if ok:
        row.last_success_at = datetime.utcnow()
    db.commit()
    _timing(operation, "finish", started_at)
    _timing(operation, "total_ms", started_at)
    logger.info(
        "Price source account test saved: account_id=%s sourceType=%s authMode=%s status=%s priceListsCount=%s statusMessage=%s elapsed=%.2fs",
        row.id,
        row.source_type,
        auth_mode,
        row.status,
        row.price_lists_count,
        row.status_message,
        time.monotonic() - started_at,
    )
    return row


def _status_from_message(message: str) -> str:
    msg = (message or "").lower()
    if "login" in msg or "password" in msg or "парол" in msg:
        return "invalid_credentials"
    if "сесс" in msg or "session" in msg:
        return "session_expired"
    if "timeout" in msg or "connect" in msg:
        return "source_unavailable"
    return "auth_error"
