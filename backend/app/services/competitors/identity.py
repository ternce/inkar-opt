from __future__ import annotations

import re
from dataclasses import dataclass


_WHITESPACE_RE = re.compile(r"\s+")
_PUNCT_SPACE_RE = re.compile(r"\s*([(),])\s*")
_LEGAL_SUFFIX_RE = re.compile(r"\s+(тоо|too|нпо|npo)\b.*$")


def normalize_regular_competitor_text(value: object) -> str:
    text = str(value or "").strip().casefold()
    if not text:
        return ""
    text = text.replace("ё", "е")
    text = _PUNCT_SPACE_RE.sub(r"\1", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def _mojibake(value: str) -> str:
    try:
        return value.encode("utf-8").decode("cp1251")
    except Exception:
        return value


def _variants(*values: str) -> set[str]:
    out: set[str] = set()
    for value in values:
        normalized = normalize_regular_competitor_text(value)
        if normalized:
            out.add(normalized)
        baked = normalize_regular_competitor_text(_mojibake(value))
        if baked:
            out.add(baked)
    return out


_REGIONS = _variants(
    "Актау",
    "Актобе",
    "Алматы",
    "Астана",
    "Атырау",
    "Караганда",
    "Костанай",
    "Кызылорда",
    "Павлодар",
    "Семей",
    "Тараз",
    "Уральск",
    "Усть-Каменогорск",
    "Шымкент",
)


@dataclass(frozen=True)
class RegularCompetitorAliasFamily:
    canonical: str
    display_name: str
    base_names: frozenset[str]
    legal_prefixes: frozenset[str] = frozenset()
    allow_region_parentheses: bool = True

    def canonical_for(self, identity: str) -> str | None:
        if identity in self.base_names:
            return self.canonical
        if self.allow_region_parentheses:
            for base_name in self.base_names:
                if identity.startswith(f"{base_name}(") and identity.endswith(")"):
                    return self.canonical
        for prefix in self.legal_prefixes:
            if identity == prefix or identity.startswith(f"{prefix}(") or identity.startswith(f"{prefix} "):
                without_legal = _LEGAL_SUFFIX_RE.sub("", identity).strip()
                if without_legal in self.base_names or identity.startswith(prefix):
                    return self.canonical
        return None


def _family(
    canonical: str,
    display_name: str,
    *,
    base_names: tuple[str, ...],
    legal_prefixes: tuple[str, ...] = (),
) -> RegularCompetitorAliasFamily:
    return RegularCompetitorAliasFamily(
        canonical=normalize_regular_competitor_text(canonical),
        display_name=display_name,
        base_names=frozenset(_variants(*base_names)),
        legal_prefixes=frozenset(_variants(*legal_prefixes)),
    )


REGULAR_COMPETITOR_ALIAS_FAMILIES: tuple[RegularCompetitorAliasFamily, ...] = (
    _family("аманат", "Аманат", base_names=("Аманат",)),
    _family("медсервис", "Медсервис", base_names=("Медсервис",)),
    _family("атамирас", "Атамирас", base_names=("Атамирас",), legal_prefixes=("Атамирас ТОО",)),
    _family("зерде", "Зерде", base_names=("Зерде",), legal_prefixes=("Зерде ТОО НПО",)),
    _family("стофарм", "Стофарм", base_names=("Стофарм",)),
)


def canonical_regular_competitor_identity(
    competitor_name: object,
    supplier_name: object | None = None,
    display_name: object | None = None,
) -> str:
    raw_name = competitor_name or supplier_name or display_name or ""
    identity = normalize_regular_competitor_text(raw_name)
    if not identity:
        return ""
    for family in REGULAR_COMPETITOR_ALIAS_FAMILIES:
        canonical = family.canonical_for(identity)
        if canonical:
            return canonical
    return identity


def regular_competitor_display_name(identity: object, fallback: object = "") -> str:
    normalized = normalize_regular_competitor_text(identity)
    for family in REGULAR_COMPETITOR_ALIAS_FAMILIES:
        if normalized == family.canonical:
            return family.display_name
    return str(fallback or identity or "").strip()


def regular_competitor_alias_obsolete_identities(identity: object) -> set[str]:
    normalized = normalize_regular_competitor_text(identity)
    if not normalized:
        return set()
    for family in REGULAR_COMPETITOR_ALIAS_FAMILIES:
        if normalized == family.canonical:
            identities = set(family.base_names) | set(family.legal_prefixes)
            for base_name in family.base_names:
                identities.update(f"{base_name}({region})" for region in _REGIONS)
            return {item for item in identities if item != family.canonical}
    return set()
