from __future__ import annotations

from datetime import datetime, timedelta
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.db import Base
from backend.app.models import (
    CompetitorMatchSnapshot,
    CompetitorPriceList,
    CompetitorSourcePublication,
    CompetitorSourceVersion,
    PriceFormat,
    PriceFormatCompetitorMaterialization,
    PricingContext,
    PricingWorkflowRun,
)
from backend.app.services.matching_cache.retention import (
    RetentionPolicy,
    build_retention_manifest,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def _fixture(db: Session):
    price_format = PriceFormat(code="RETENTION", name="Retention", branch="Test")
    db.add(price_format)
    db.flush()
    source = CompetitorPriceList(
        price_format_id=price_format.id,
        source_type="manual",
        source_key="manual:retention",
        display_name="Retention",
    )
    db.add(source)
    db.flush()
    old = datetime(2025, 1, 1)
    version = CompetitorSourceVersion(
        id=1,
        competitor_price_list_id=source.id,
        generation=1,
        content_sha256="a" * 64,
        matching_input_sha256="b" * 64,
        row_count=0,
        status="superseded",
        created_at=old,
    )
    db.add(version)
    db.flush()
    snapshot = CompetitorMatchSnapshot(
        id=1,
        source_version_id=version.id,
        dependency_fingerprint="c" * 64,
        matcher_version="m",
        normalization_version="n",
        status="superseded",
        created_at=old,
    )
    publication = CompetitorSourcePublication(
        id=1,
        competitor_price_list_id=source.id,
        source_version_id=version.id,
        row_count=0,
        status="superseded",
        created_at=old,
    )
    db.add_all([snapshot, publication])
    db.flush()
    return price_format, version, snapshot, publication


def test_retention_manifest_marks_complete_unreferenced_dependency_set(db):
    _, version, snapshot, publication = _fixture(db)
    manifest = build_retention_manifest(
        db=db,
        policy=RetentionPolicy(30, 30, 30, 30),
        now=datetime(2026, 1, 1),
    )
    assert manifest.dry_run is True
    assert manifest.blocked is False
    assert manifest.candidates["source_version_ids"] == [version.id]
    assert manifest.candidates["match_snapshot_ids"] == [snapshot.id]
    assert manifest.candidates["source_publication_ids"] == [publication.id]
    assert "physical_deletion_not_implemented; manifest is dry-run only" in manifest.blockers


def test_retention_protects_historical_materialization_and_workflow_references(db):
    price_format, version, snapshot, _ = _fixture(db)
    db.add(PriceFormatCompetitorMaterialization(
        id=1,
        price_format_id=price_format.id,
        materialization_key="retention",
        assignment_fingerprint="fingerprint",
        source_snapshot_ids_json=json.dumps([snapshot.id]),
        status="superseded",
        is_current=False,
    ))
    context = PricingContext(branch_id="1", region="Test", sales_channel="", name="Retention")
    db.add(context)
    db.flush()
    db.add(PricingWorkflowRun(
        pricing_context_id=context.id,
        price_format_id=price_format.id,
        run_snapshot_json=json.dumps({"sourceVersionId": version.id}),
    ))
    db.flush()
    manifest = build_retention_manifest(db=db, now=datetime(2026, 1, 1))
    assert snapshot.id in manifest.protected["match_snapshot_ids"]
    assert version.id in manifest.protected["source_version_ids"]
    assert snapshot.id not in manifest.candidates["match_snapshot_ids"]
    assert version.id not in manifest.candidates["source_version_ids"]


def test_retention_fails_closed_on_malformed_audit_reference(db):
    price_format, _, _, _ = _fixture(db)
    context = PricingContext(branch_id="2", region="Test", sales_channel="", name="Broken")
    db.add(context)
    db.flush()
    run = PricingWorkflowRun(
        pricing_context_id=context.id,
        price_format_id=price_format.id,
        run_snapshot_json="{not-json",
    )
    db.add(run)
    db.flush()
    manifest = build_retention_manifest(db=db, now=datetime(2026, 1, 1))
    assert manifest.blocked is True
    assert manifest.candidates["source_version_ids"] == []
    assert manifest.candidates["match_snapshot_ids"] == []
    assert f"malformed_workflow_reference:{run.id}" in manifest.blockers


def test_retention_policy_rejects_zero_day_window(monkeypatch):
    monkeypatch.setenv("MATCH_CACHE_RETENTION_SOURCE_VERSION_DAYS", "0")
    with pytest.raises(ValueError, match="at least one day"):
        RetentionPolicy.from_environment()
