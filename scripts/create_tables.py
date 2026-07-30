from __future__ import annotations

import os
from sqlalchemy import text
from sqlalchemy.orm import Session

from db.session import get_engine
from db.models import Base


def create_all_tables() -> None:
    engine = get_engine()
    Base.metadata.create_all(bind=engine)


def apply_safe_alters() -> None:
    """
    Apply backward-compatible ALTER statements that are also executed at app startup.
    This is optional but keeps parity with runtime bootstrap, useful for existing DBs.
    """
    engine = get_engine()
    try:
        with engine.begin() as conn:
            # issued_keys.issued_at column (if app previously created table without it)
            conn.execute(text(
                "ALTER TABLE issued_keys ADD COLUMN IF NOT EXISTS issued_at TIMESTAMPTZ NOT NULL DEFAULT now()"
            ))
            # users.phone and users.comment columns (optional, if missing)
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(32)"
            ))
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS comment VARCHAR(255)"
            ))
            # users.pin_code 4-digit login code
            conn.execute(text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS pin_code VARCHAR(4)"
            ))
            # boxes grid and aggregates
            conn.execute(text(
                "ALTER TABLE boxes ADD COLUMN IF NOT EXISTS row INTEGER"
            ))
            conn.execute(text(
                "ALTER TABLE boxes ADD COLUMN IF NOT EXISTS column INTEGER"
            ))
            conn.execute(text(
                "ALTER TABLE boxes ADD COLUMN IF NOT EXISTS x INTEGER"
            ))
            conn.execute(text(
                "ALTER TABLE boxes ADD COLUMN IF NOT EXISTS y INTEGER"
            ))
            conn.execute(text(
                "ALTER TABLE boxes ADD COLUMN IF NOT EXISTS rooms INTEGER"
            ))
            conn.execute(text(
                "ALTER TABLE boxes ADD COLUMN IF NOT EXISTS keys_capacity INTEGER"
            ))
            conn.execute(text(
                "ALTER TABLE boxes ADD COLUMN IF NOT EXISTS key_count INTEGER"
            ))
            conn.execute(text(
                "ALTER TABLE boxes ADD COLUMN IF NOT EXISTS room_count INTEGER"
            ))
            conn.execute(text(
                "ALTER TABLE boxes ADD COLUMN IF NOT EXISTS keys_current INTEGER"
            ))
            # keys: placement coordinates inside a box
            conn.execute(text(
                "ALTER TABLE keys ADD COLUMN IF NOT EXISTS pos_x INTEGER"
            ))
            conn.execute(text(
                "ALTER TABLE keys ADD COLUMN IF NOT EXISTS pos_y INTEGER"
            ))
            # images: session metadata and references
            conn.execute(text(
                "ALTER TABLE images ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"
            ))
            conn.execute(text(
                "ALTER TABLE images ADD COLUMN IF NOT EXISTS box_id INTEGER REFERENCES boxes(id) ON DELETE SET NULL"
            ))
            conn.execute(text(
                "ALTER TABLE images ADD COLUMN IF NOT EXISTS session_started_at TIMESTAMPTZ"
            ))
            conn.execute(text(
                "ALTER TABLE images ADD COLUMN IF NOT EXISTS session_stopped_at TIMESTAMPTZ"
            ))
    except Exception:
        # Best effort; keep script idempotent even if DB dialect lacks IF NOT EXISTS
        pass


def main() -> int:
    create_all_tables()
    apply_safe_alters()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



