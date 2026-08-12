from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.prod.yml"


def _compose_text() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def _service_block(name: str) -> str:
    text = _compose_text()
    marker = f"\n  {name}:\n"
    start = text.index(marker) + 1
    next_start = len(text)
    for candidate in ("\n  db:\n", "\n  web:\n", "\n  worker:\n", "\n  app:\n", "\nvolumes:\n"):
        idx = text.find(candidate, start + len(marker))
        if idx != -1:
            next_start = min(next_start, idx)
    return text[start:next_start]


def test_prod_web_service_uses_web_role_and_uvicorn_command():
    web = _service_block("web")
    assert "container_name: inkar_opt_web" in web
    assert "PROCESS_ROLE: web" in web
    assert "python -m uvicorn app.main:app --host 0.0.0.0 --port 8000" in web
    assert '"${APP_PORT:-8010}:8000"' in web


def test_prod_worker_service_uses_worker_role_and_no_published_port():
    worker = _service_block("worker")
    assert "container_name: inkar_opt_worker" in worker
    assert "PROCESS_ROLE: worker" in worker
    assert 'command: ["python", "-m", "app.worker"]' in worker
    assert "\n    ports:" not in worker


def test_emit_tmp_volume_belongs_only_to_worker():
    web = _service_block("web")
    worker = _service_block("worker")
    assert "emit_tmp:/tmp/emit" in worker
    assert "emit_tmp:/tmp/emit" not in web


def test_web_and_worker_share_backend_image_and_db_dependency():
    text = _compose_text()
    web = _service_block("web")
    worker = _service_block("worker")
    assert "image: inkar_opt_backend:latest" in text
    assert "<<: *backend-image" in web
    assert "<<: *backend-image" in worker
    assert "<<: *backend-env" in web
    assert "<<: *backend-env" in worker
    assert "DATABASE_URL: ${DATABASE_URL:-postgresql://${POSTGRES_USER:-inkar}:${POSTGRES_PASSWORD:-CHANGE_ME}@db:5432/${POSTGRES_DB:-inkar_opt}}" in text
    assert "condition: service_healthy" in web
    assert "condition: service_healthy" in worker


def test_rollback_single_app_service_is_profile_gated_all_mode():
    app = _service_block("app")
    assert 'profiles: ["rollback"]' in app
    assert "container_name: inkar_opt_app" in app
    assert "PROCESS_ROLE: all" in app
