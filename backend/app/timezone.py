from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .config import get_settings


def app_timezone() -> ZoneInfo:
    tz_name = (
        getattr(get_settings(), "app_timezone", None)
        or get_settings().provisor_auto_refresh_timezone
        or "Asia/Qyzylorda"
    )
    try:
        return ZoneInfo(str(tz_name))
    except Exception:
        return ZoneInfo("Asia/Qyzylorda")


def now_kz() -> datetime:
    return datetime.now(app_timezone())


def now_kz_naive() -> datetime:
    return now_kz().replace(tzinfo=None)


def as_kz(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    tz = app_timezone()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def local_iso(dt: datetime | None) -> str:
    converted = as_kz(dt)
    return converted.isoformat() if converted else ""


def local_display(dt: datetime | None) -> str:
    converted = as_kz(dt)
    return converted.strftime("%d.%m.%Y %H:%M") if converted else ""
