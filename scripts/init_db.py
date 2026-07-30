from __future__ import annotations

import os
import sys
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.session import get_engine
from db.models import Base, User
from services.auth_service import hash_password


def create_schema() -> None:
    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    # safe alters to keep parity
    try:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE issued_keys ADD COLUMN IF NOT EXISTS issued_at TIMESTAMPTZ NOT NULL DEFAULT now()"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(32)"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS comment VARCHAR(255)"))
            conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS pin_code VARCHAR(4)"))
    except Exception:
        pass


def ensure_admin_user(session: Session, login: str, password: str) -> Optional[User]:
    existing = session.execute(select(User).where(User.login == login)).scalar_one_or_none()
    if existing:
        return existing
    user = User(login=login, password_hash=hash_password(password))
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def main() -> int:
    # Optional seeding via env vars
    admin_login = os.getenv("ADMIN_LOGIN")
    admin_password = os.getenv("ADMIN_PASSWORD")

    create_schema()

    # Seed only if both provided
    if admin_login and admin_password:
        engine = get_engine()
        with Session(engine, future=True) as session:
            ensure_admin_user(session, admin_login, admin_password)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())



