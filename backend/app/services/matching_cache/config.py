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


def matching_cache_worker_enabled() -> bool:
    return matching_cache_enabled() and _enabled("MATCH_CACHE_WORKER_ENABLED")


def matching_cache_shadow_enabled() -> bool:
    return matching_cache_enabled() and _enabled("MATCH_CACHE_SHADOW_ENABLED")


def matching_cache_source_noop_enabled() -> bool:
    """Conservative source-import no-op optimization; disabled by default."""

    return (
        matching_cache_dual_write_enabled()
        and _enabled("MATCH_CACHE_SOURCE_NOOP_ENABLED")
    )
