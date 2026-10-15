"""Disabled-by-default Phase 2 matching-cache foundation.

Nothing in this package is imported by source ingestion, legacy matching, or
pricing code.  Phase 2B.2 must explicitly integrate it behind feature flags.
"""

from .config import matching_cache_enabled
from .dependencies import MATCHER_VERSION, NORMALIZATION_VERSION
from .fingerprints import CanonicalSourceContent, canonical_source_content
from .versions import create_or_reuse_pending_source_version
from .dependency_tracking import install_dependency_tracking

install_dependency_tracking()

__all__ = [
    "CanonicalSourceContent",
    "MATCHER_VERSION",
    "NORMALIZATION_VERSION",
    "canonical_source_content",
    "create_or_reuse_pending_source_version",
    "matching_cache_enabled",
]
