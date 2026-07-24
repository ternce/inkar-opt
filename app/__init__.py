from __future__ import annotations

from pathlib import Path

_backend_app = Path(__file__).resolve().parents[1] / "backend" / "app"
if _backend_app.exists():
    __path__.append(str(_backend_app))  # type: ignore[name-defined]
