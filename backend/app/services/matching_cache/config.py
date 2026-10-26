from __future__ import annotations

import os


def _enabled(name: str) -> bool:
    return str(os.getenv(name, "")).strip().casefold() in {"1", "true", "yes", "on"}


def matching_cache_enabled() -> bool:
    """Master kill switch.  The foundation is inert unless explicitly enabled."""

    return _enabled("MATCH_CACHE_ENABLED")


def matching_cache_dual_write_enabled() -> bool:
    return matching_cache_enabled() and _enabled("MATCH_CACHE_DUAL_WRITE")


def matching_cache_read_enabled() -> bool:
    return matching_cache_enabled() and _enabled("MATCH_CACHE_READ")


def matching_cache_read_mode() -> str:
    """Return disabled, fallback, or required without enabling reads implicitly."""

    if not matching_cache_read_enabled():
        return "disabled"
    value = str(os.getenv("MATCH_CACHE_READ_MODE", "fallback")).strip().casefold()
    if value not in {"fallback", "required"}:
        raise ValueError(
            "MATCH_CACHE_READ_MODE must be 'fallback' or 'required' when cache reads are enabled"
        )
    return value


def matching_cache_read_price_format_ids() -> frozenset[int]:
    """Return the cache-read allowlist, empty when it is not configured.

    Global reads require the separate, explicit ``MATCH_CACHE_READ_ALLOW_ALL``
    gate. This prevents enabling the master/read flags from silently changing
    every production pricing format.
    """

    raw = os.getenv("MATCH_CACHE_READ_PRICE_FORMAT_IDS")
    if raw is None or not raw.strip():
        return frozenset()
    values: set[int] = set()
    for token in raw.split(","):
        value = token.strip()
        if not value:
            continue
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(
                "MATCH_CACHE_READ_PRICE_FORMAT_IDS must be a comma-separated list of positive integers"
            ) from exc
        if parsed <= 0:
            raise ValueError(
                "MATCH_CACHE_READ_PRICE_FORMAT_IDS must contain only positive integers"
            )
        values.add(parsed)
    if not values:
        raise ValueError("MATCH_CACHE_READ_PRICE_FORMAT_IDS must not be empty when configured")
    return frozenset(values)


def matching_cache_read_allowed_for_price_format(price_format_id: int) -> bool:
    if _enabled("MATCH_CACHE_READ_ALLOW_ALL"):
        return True
    allowlist = matching_cache_read_price_format_ids()
    return int(price_format_id) in allowlist


def matching_cache_worker_enabled() -> bool:
    return matching_cache_enabled() and _enabled("MATCH_CACHE_WORKER_ENABLED")


def matching_cache_auto_prepare_enabled() -> bool:
    """Worker-side lifecycle reconciliation; never enables cache reads."""

    return matching_cache_worker_enabled() and _enabled(
        "MATCH_CACHE_AUTO_PREPARE_ENABLED"
    )


def _positive_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = str(os.getenv(name, default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def matching_cache_worker_poll_interval_seconds() -> int:
    return _positive_int(
        "MATCH_CACHE_WORKER_POLL_INTERVAL_SECONDS", 5, minimum=1, maximum=300
    )


def matching_cache_worker_lease_seconds() -> int:
    return _positive_int(
        "MATCH_CACHE_WORKER_LEASE_SECONDS", 900, minimum=60, maximum=7200
    )


def matching_cache_auto_prepare_interval_seconds() -> int:
    return _positive_int(
        "MATCH_CACHE_AUTO_PREPARE_INTERVAL_SECONDS",
        300,
        minimum=10,
        maximum=3600,
    )


def matching_cache_shadow_enabled() -> bool:
    return matching_cache_enabled() and _enabled("MATCH_CACHE_SHADOW_ENABLED")


def matching_cache_source_noop_enabled() -> bool:
    """Conservative source-import no-op optimization; disabled by default."""

    return (
        matching_cache_dual_write_enabled()
        and _enabled("MATCH_CACHE_SOURCE_NOOP_ENABLED")
    )
