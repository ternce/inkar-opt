from __future__ import annotations

import hashlib
import os
import secrets
from datetime import timedelta

from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AppSession, AppUser
from ..timezone import now_kz_naive


PASSWORD_CONTEXT = CryptContext(schemes=["bcrypt"], deprecated="auto")
SESSION_COOKIE_NAME = os.getenv("AUTH_SESSION_COOKIE_NAME", "inkar_opt_session")
SESSION_TOKEN_BYTES = 32


def session_lifetime() -> timedelta:
    try:
        hours = int(os.getenv("AUTH_SESSION_LIFETIME_HOURS", "12"))
    except ValueError:
        hours = 12
    return timedelta(hours=max(1, hours))


def is_production_environment() -> bool:
    return (os.getenv("ENVIRONMENT") or "").strip().casefold() in {"prod", "production"}


def session_cookie_secure() -> bool:
    raw = os.getenv("AUTH_COOKIE_SECURE")
    if raw is not None:
        return raw.strip().casefold() in {"1", "true", "yes", "on"}
    return is_production_environment()


def hash_password(password: str) -> str:
    return PASSWORD_CONTEXT.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    return PASSWORD_CONTEXT.verify(password, password_hash)


def session_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(db: Session, user: AppUser) -> tuple[str, AppSession]:
    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    now = now_kz_naive()
    row = AppSession(
        session_token_hash=session_token_hash(token),
        user_id=int(user.id),
        created_at=now,
        last_seen_at=now,
        expires_at=now + session_lifetime(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return token, row


def get_user_for_session(db: Session, token: str | None) -> AppUser | None:
    if not token:
        return None
    now = now_kz_naive()
    row = (
        db.execute(
            select(AppSession)
            .where(AppSession.session_token_hash == session_token_hash(token))
            .where(AppSession.revoked_at.is_(None))
            .where(AppSession.expires_at > now)
        )
        .scalars()
        .first()
    )
    if row is None:
        return None
    user = db.get(AppUser, row.user_id)
    if user is None or not user.is_active:
        return None
    row.last_seen_at = now
    db.commit()
    return user


def revoke_session(db: Session, token: str | None) -> bool:
    if not token:
        return False
    row = db.execute(select(AppSession).where(AppSession.session_token_hash == session_token_hash(token))).scalars().first()
    if row is None or row.revoked_at is not None:
        return False
    row.revoked_at = now_kz_naive()
    db.commit()
    return True
