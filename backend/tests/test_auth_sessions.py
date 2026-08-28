from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app import main
from backend.app.db import Base
from backend.app.deps import get_db
from backend.app.models import AppSession, AppUser, PriceFormat, UserBranchAssignment
from backend.app.services.auth import SESSION_COOKIE_NAME, hash_password, session_token_hash
from backend.app.timezone import now_kz_naive


def _session_factory(url: str):
    engine = create_engine(url, connect_args={"check_same_thread": False})
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


def _seed_user(
    Session,
    username: str,
    password: str = "strong-password",
    *,
    role: str = "admin",
    display_name: str = "",
    is_active: bool = True,
    branches: list[tuple[str, str]] | None = None,
) -> AppUser:
    with Session() as db:
        user = AppUser(
            username=username,
            display_name=display_name or username,
            role=role,
            is_active=is_active,
            password_hash=hash_password(password),
            password_changed_at=now_kz_naive(),
        )
        db.add(user)
        db.flush()
        for branch_id, branch_name in branches or []:
            db.add(UserBranchAssignment(user_id=user.id, branch_id=branch_id, branch_name=branch_name))
        db.commit()
        db.refresh(user)
        db.expunge(user)
        return user


def _prod_client(Session, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    main.app.dependency_overrides[get_db] = _override_db(Session)
    return TestClient(main.app)


def _login(client: TestClient, username: str, password: str = "strong-password"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_auth_01_valid_username_password_sets_secure_session_cookie(tmp_path, monkeypatch):
    Session = _session_factory(f"sqlite:///{tmp_path / 'auth01.db'}")
    _seed_user(Session, "employee-1", display_name="Employee One")

    client = _prod_client(Session, monkeypatch)
    try:
        response = _login(client, "employee-1")

        assert response.status_code == 200, response.text
        assert response.json()["username"] == "employee-1"
        assert "password_hash" not in response.json()
        assert SESSION_COOKIE_NAME in response.cookies
        cookie = response.headers["set-cookie"].lower()
        assert "httponly" in cookie
        assert "samesite=lax" in cookie
    finally:
        main.app.dependency_overrides.pop(get_db, None)


def test_auth_02_wrong_password_is_rejected(tmp_path, monkeypatch):
    Session = _session_factory(f"sqlite:///{tmp_path / 'auth02.db'}")
    _seed_user(Session, "employee-1")

    client = _prod_client(Session, monkeypatch)
    try:
        response = _login(client, "employee-1", "wrong-password")

        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid username or password"
    finally:
        main.app.dependency_overrides.pop(get_db, None)


def test_auth_03_unknown_user_is_rejected_without_revealing_existence(tmp_path, monkeypatch):
    Session = _session_factory(f"sqlite:///{tmp_path / 'auth03.db'}")

    client = _prod_client(Session, monkeypatch)
    try:
        response = _login(client, "missing-user")

        assert response.status_code == 401
        assert response.json()["detail"] == "Invalid username or password"
    finally:
        main.app.dependency_overrides.pop(get_db, None)


def test_auth_04_inactive_user_cannot_login(tmp_path, monkeypatch):
    Session = _session_factory(f"sqlite:///{tmp_path / 'auth04.db'}")
    _seed_user(Session, "inactive-user", is_active=False)

    client = _prod_client(Session, monkeypatch)
    try:
        response = _login(client, "inactive-user")

        assert response.status_code == 401
    finally:
        main.app.dependency_overrides.pop(get_db, None)


def test_auth_05_current_user_uses_session_cookie(tmp_path, monkeypatch):
    Session = _session_factory(f"sqlite:///{tmp_path / 'auth05.db'}")
    _seed_user(Session, "employee-1", role="pricing_manager")

    client = _prod_client(Session, monkeypatch)
    try:
        assert _login(client, "employee-1").status_code == 200
        response = client.get("/api/current-user")

        assert response.status_code == 200
        payload = response.json()
        assert payload["username"] == "employee-1"
        assert payload["canWrite"] is True
    finally:
        main.app.dependency_overrides.pop(get_db, None)


def test_auth_06_current_user_without_identity_is_401_in_production(tmp_path, monkeypatch):
    Session = _session_factory(f"sqlite:///{tmp_path / 'auth06.db'}")
    _seed_user(Session, "dev-admin")

    client = _prod_client(Session, monkeypatch)
    try:
        response = client.get("/api/current-user")

        assert response.status_code == 401
    finally:
        main.app.dependency_overrides.pop(get_db, None)


def test_auth_07_logout_invalidates_session(tmp_path, monkeypatch):
    Session = _session_factory(f"sqlite:///{tmp_path / 'auth07.db'}")
    _seed_user(Session, "employee-1")

    client = _prod_client(Session, monkeypatch)
    try:
        assert _login(client, "employee-1").status_code == 200
        assert client.get("/api/current-user").status_code == 200

        logout = client.post("/api/auth/logout")
        assert logout.status_code == 200
        assert client.get("/api/current-user").status_code == 401
    finally:
        main.app.dependency_overrides.pop(get_db, None)


def test_auth_08_x_dev_user_cannot_impersonate_in_production(tmp_path, monkeypatch):
    Session = _session_factory(f"sqlite:///{tmp_path / 'auth08.db'}")
    _seed_user(Session, "employee-1")

    client = _prod_client(Session, monkeypatch)
    try:
        response = client.get("/api/current-user", headers={"X-Dev-User": "definitely_fake_test_user"})

        assert response.status_code == 403
    finally:
        main.app.dependency_overrides.pop(get_db, None)


def test_auth_09_x_dev_user_does_not_auto_create_production_user(tmp_path, monkeypatch):
    Session = _session_factory(f"sqlite:///{tmp_path / 'auth09.db'}")

    client = _prod_client(Session, monkeypatch)
    try:
        response = client.get("/api/current-user", headers={"X-Dev-User": "definitely_fake_test_user"})
        assert response.status_code == 403

        with Session() as db:
            user = db.execute(select(AppUser).where(AppUser.username == "definitely_fake_test_user")).scalars().first()
            assert user is None
    finally:
        main.app.dependency_overrides.pop(get_db, None)


def test_auth_10_three_browsers_have_independent_sessions(tmp_path, monkeypatch):
    Session = _session_factory(f"sqlite:///{tmp_path / 'auth10.db'}")
    for username in ["employee-1", "employee-2", "employee-3"]:
        _seed_user(Session, username)

    monkeypatch.setenv("ENVIRONMENT", "prod")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    main.app.dependency_overrides[get_db] = _override_db(Session)
    try:
        def login_and_read(username: str):
            client = TestClient(main.app)
            assert _login(client, username).status_code == 200
            return client.get("/api/current-user").json()["username"]

        with ThreadPoolExecutor(max_workers=3) as pool:
            usernames = list(pool.map(login_and_read, ["employee-1", "employee-2", "employee-3"]))

        assert sorted(usernames) == ["employee-1", "employee-2", "employee-3"]
    finally:
        main.app.dependency_overrides.pop(get_db, None)


def test_auth_11_role_permissions_are_preserved(tmp_path, monkeypatch):
    Session = _session_factory(f"sqlite:///{tmp_path / 'auth11.db'}")
    _seed_user(Session, "viewer-user", role="viewer")

    client = _prod_client(Session, monkeypatch)
    try:
        assert _login(client, "viewer-user").status_code == 200
        current = client.get("/api/current-user")
        assert current.json()["isReadOnly"] is True
        assert current.json()["canWrite"] is False

        response = client.post("/api/price-formats", json={"code": "VIEWER-DENIED", "name": "VIEWER-DENIED", "branch": ""})
        assert response.status_code == 403
    finally:
        main.app.dependency_overrides.pop(get_db, None)


def test_auth_12_branch_assignments_are_preserved(tmp_path, monkeypatch):
    Session = _session_factory(f"sqlite:///{tmp_path / 'auth12.db'}")
    _seed_user(Session, "branch-user", role="pricing_manager", branches=[("astana", "Astana")])
    with Session() as db:
        db.add_all(
            [
                PriceFormat(code="AST", name="Astana", branch="Astana"),
                PriceFormat(code="ALM", name="Almaty", branch="Almaty"),
            ]
        )
        db.commit()

    client = _prod_client(Session, monkeypatch)
    try:
        assert _login(client, "branch-user").status_code == 200
        current = client.get("/api/current-user").json()
        assert current["branches"] == [{"branchId": "astana", "branchName": "Astana"}]

        formats = client.get("/api/price-formats")
        assert formats.status_code == 200
        assert [row["code"] for row in formats.json()] == ["AST"]
    finally:
        main.app.dependency_overrides.pop(get_db, None)


def test_auth_13_password_hash_is_never_returned_from_current_user(tmp_path, monkeypatch):
    Session = _session_factory(f"sqlite:///{tmp_path / 'auth13.db'}")
    _seed_user(Session, "employee-1")

    client = _prod_client(Session, monkeypatch)
    try:
        assert _login(client, "employee-1").status_code == 200
        response = client.get("/api/current-user")

        assert response.status_code == 200
        assert "password_hash" not in response.json()
        assert "passwordHash" not in response.json()
    finally:
        main.app.dependency_overrides.pop(get_db, None)


def test_auth_14_expired_session_is_rejected(tmp_path, monkeypatch):
    Session = _session_factory(f"sqlite:///{tmp_path / 'auth14.db'}")
    user = _seed_user(Session, "employee-1")
    token = "expired-token"
    with Session() as db:
        now = now_kz_naive()
        db.add(
            AppSession(
                session_token_hash=session_token_hash(token),
                user_id=user.id,
                created_at=now - timedelta(hours=2),
                last_seen_at=now - timedelta(hours=2),
                expires_at=now - timedelta(hours=1),
            )
        )
        db.commit()

    client = _prod_client(Session, monkeypatch)
    client.cookies.set(SESSION_COOKIE_NAME, token)
    try:
        response = client.get("/api/current-user")

        assert response.status_code == 401
    finally:
        main.app.dependency_overrides.pop(get_db, None)
