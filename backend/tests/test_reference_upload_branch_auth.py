from __future__ import annotations

import io

from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import main
from backend.app.db import Base
from backend.app.deps import ROLE_ADMIN, ROLE_PRICING_LEAD, ROLE_PRICING_MANAGER
from backend.app.models import AppUser, UserBranchAssignment
from backend.app.services.references.types import BRANCHES


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _override_db(Session):
    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    return override


def _user(*, role: str, branch_ids: list[str] | None = None) -> AppUser:
    user = AppUser(id=1, username=f"{role}-user", role=role, is_active=True)
    user.branches = [
        UserBranchAssignment(user_id=1, branch_id=branch_id, branch_name=next((row["name"] for row in BRANCHES if row["id"] == branch_id), branch_id))
        for branch_id in (branch_ids or [])
    ]
    return user


def _client(user: AppUser):
    Session = _session_factory()
    main.app.dependency_overrides[main.get_db] = _override_db(Session)
    main.app.dependency_overrides[main.get_current_user] = lambda: user
    main.app.dependency_overrides[main.require_write_access] = lambda: user
    return TestClient(main.app)


def _stock_xlsx() -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["sku", "stock"])
    worksheet.append(["SKU-REF-AUTH", 10])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _post_single(client: TestClient, branch_value: str):
    return client.post(
        f"/api/references/import?data_type=stock&branch_ids={branch_value}",
        files={"file": ("stock.xlsx", _stock_xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )


def _post_batch(client: TestClient, branch_value: str):
    return client.post(
        "/api/references/import/batch",
        data={"data_types": "stock", "branch_ids": branch_value, "source_type": "excel"},
        files={"files": ("stock.xlsx", _stock_xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )


def teardown_function():
    main.app.dependency_overrides.clear()


def test_reference_upload_admin_can_upload_to_supported_branch_from_picker():
    client = _client(_user(role=ROLE_ADMIN))
    branch_value = client.get("/api/references/branches").json()[0]["id"]

    response = _post_single(client, branch_value)

    assert response.status_code == 200, response.text
    assert response.json()["branchIds"] == '["1"]'


def test_reference_upload_pricing_lead_without_assignments_can_upload_to_any_supported_branch():
    client = _client(_user(role=ROLE_PRICING_LEAD))
    branch_value = client.get("/api/references/branches").json()[1]["id"]

    response = _post_single(client, branch_value)

    assert response.status_code == 200, response.text
    assert response.json()["branchIds"] == '["2"]'


def test_reference_upload_pricing_manager_with_numeric_assignment_accepts_frontend_branch_name():
    client = _client(_user(role=ROLE_PRICING_MANAGER, branch_ids=["1"]))
    branch_value = client.get("/api/references/branches").json()[0]["id"]

    response = _post_single(client, branch_value)

    assert response.status_code == 200, response.text
    assert response.json()["branchIds"] == '["1"]'


def test_reference_upload_branch_scoped_user_is_limited_to_assigned_branch():
    client = _client(_user(role=ROLE_PRICING_MANAGER, branch_ids=["1"]))
    allowed = client.get("/api/references/branches").json()[0]["id"]
    denied = BRANCHES[1]["name"]

    allowed_response = _post_single(client, allowed)
    denied_response = _post_single(client, denied)

    assert allowed_response.status_code == 200, allowed_response.text
    assert denied_response.status_code == 403
    assert denied_response.json()["detail"] == "branch is not assigned to current user"


def test_reference_upload_batch_uses_same_canonical_branch_authorization():
    client = _client(_user(role=ROLE_PRICING_MANAGER, branch_ids=["1"]))
    branch_value = client.get("/api/references/branches").json()[0]["id"]

    response = _post_batch(client, branch_value)

    assert response.status_code == 200, response.text
    assert response.json()["jobs"][0]["branchIds"] == '["1"]'


def test_reference_upload_invalid_branch_value_fails_safely_for_branch_scoped_user():
    client = _client(_user(role=ROLE_PRICING_MANAGER, branch_ids=["1"]))

    response = _post_single(client, "not-a-supported-branch")

    assert response.status_code == 403
    assert response.json()["detail"] == "branch is not assigned to current user"
