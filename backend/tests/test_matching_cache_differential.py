from decimal import Decimal
from pathlib import Path
import subprocess
import sys

import pytest

from backend.app.services.matching_cache.differential import compare_records
from backend.app.services.matching_cache.shadow import cleanup_stale_shadow_spools


def test_differential_comparator_preserves_duplicates_and_reports_every_field():
    report = compare_records(
        artifact="competitors_prices",
        legacy=[
            {"source": "A", "sku": "P1", "price": Decimal("10.00"), "coefficient": Decimal("1.2")},
            {"source": "A", "sku": "P1", "price": Decimal("11.00"), "coefficient": Decimal("1.2")},
        ],
        shadow=[
            {"source": "A", "sku": "P1", "price": Decimal("10"), "coefficient": Decimal("1.3")},
        ],
        identity_fields=("source", "sku"),
        compare_fields=("price", "coefficient"),
    )

    assert not report.exact
    assert [(row.field, row.legacy_value, row.shadow_value) for row in report.mismatches] == [
        ("__multiplicity__", 2, 1),
        ("coefficient", "1.2", "1.3"),
    ]
    with pytest.raises(AssertionError, match="competitors_prices differential mismatch"):
        report.assert_exact()


def test_differential_comparator_ignores_only_representation_not_business_values():
    report = compare_records(
        artifact="calculated_prices",
        legacy=[{"productCode": "X", "calculatedPrice": Decimal("12.0000"), "analytics": {"b": 2, "a": 1}}],
        shadow=[{"productCode": "X", "calculatedPrice": Decimal("12"), "analytics": {"a": 1, "b": 2}}],
        identity_fields=("productCode",),
    )
    report.assert_exact()

    numeric_representation = compare_records(
        artifact="matching_decisions",
        legacy=[{"source": "X", "matchScore": 100.0}],
        shadow=[{"source": "X", "matchScore": Decimal("100.0000")}],
        identity_fields=("source",),
    )
    numeric_representation.assert_exact()

    changed_value = compare_records(
        artifact="matching_decisions",
        legacy=[{"source": "X", "matchScore": 99.9999}],
        shadow=[{"source": "X", "matchScore": Decimal("100.0000")}],
        identity_fields=("source",),
    )
    assert [(row.field, row.legacy_value, row.shadow_value) for row in changed_value.mismatches] == [
        ("matchScore", "99.9999", "100")
    ]


def test_abandoned_shadow_spool_is_reclaimed_after_process_termination():
    code = (
        "import os; "
        "from backend.app.services.matching_cache.shadow import _DecisionSpool; "
        "spool=_DecisionSpool(); print(spool.path, flush=True); os._exit(23)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 23
    path = Path(result.stdout.strip().splitlines()[-1])
    assert path.exists()
    removed = cleanup_stale_shadow_spools(
        directory=path.parent,
        older_than_seconds=0,
    )
    assert str(path) in removed
    assert not path.exists()
