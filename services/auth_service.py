from __future__ import annotations

from typing import Optional
import logging

import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import User, UserKey, Key


logger = logging.getLogger(__name__)


def hash_password(plain_password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(plain_password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


class AuthService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def authenticate(self, login: str, password: str) -> Optional[User]:
        logger.info("Authenticate attempt: login=%r", login)
        try:
            user = (
                self.session.execute(
                    select(User).where(User.login == login)
                ).scalars().one_or_none()
            )
        except Exception:
            logger.exception("Database error while fetching user by login=%r", login)
            raise

        if user is None:
            logger.warning("Authenticate failed: user not found (login=%r)", login)
            return None

        if verify_password(password, user.password_hash):
            logger.info("Authenticate success: user_id=%s, login=%r", getattr(user, "id", None), login)
            return user

        logger.warning("Authenticate failed: password mismatch for login=%r", login)
        return None

    def authenticate_by_pin(self, pin_code: str, password: str) -> Optional[User]:
        """Authenticate using numeric pin_code instead of textual login."""
        logger.info("Authenticate by PIN attempt: pin=%r", pin_code)
        try:
            user = (
                self.session.execute(
                    select(User).where(User.pin_code == pin_code)
                ).scalars().one_or_none()
            )
        except Exception:
            logger.exception("Database error while fetching user by pin_code=%r", pin_code)
            raise

        if user is None:
            logger.warning("Authenticate failed: user not found (pin=%r)", pin_code)
            return None

        if verify_password(password, user.password_hash):
            logger.info("Authenticate success: user_id=%s, pin=%r", getattr(user, "id", None), pin_code)
            return user

        logger.warning("Authenticate failed: password mismatch for pin=%r", pin_code)
        return None

    def get_user_allowed_keys(self, user: User) -> list[Key]:
        rows = (
            self.session.execute(
                select(Key)
                .join(UserKey, UserKey.key_id == Key.id)
                .join(User, User.id == UserKey.user_id)
                .where(User.id == user.id)
                .distinct()
            )
            .scalars()
            .all()
        )
        return list(rows)


__all__ = ["AuthService", "hash_password", "verify_password"]


