from __future__ import annotations

from sqlalchemy import Column, Integer, String, Table, ForeignKey, DateTime, LargeBinary, func
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.schema import UniqueConstraint


Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    login = Column(String(64), unique=True, nullable=False, index=True)
    # 4-значный цифровой код-логин для PIN-авторизации (может быть NULL для старых записей)
    pin_code = Column(String(4), nullable=True, index=True)
    # Пароль хранится в виде хеша bcrypt
    password_hash = Column(String(128), nullable=False)
    phone = Column(String(32), nullable=True)
    comment = Column(String(255), nullable=True)
    # RFID-токен пользователя для быстрой авторизации (тест/производство)
    rfid = Column(String(128), nullable=True, unique=True, index=True)
    # Привязка пользователя к конкретному боксу (ящику). Может быть NULL для старых записей.
    box_id = Column(Integer, ForeignKey("boxes.id", ondelete="SET NULL"), nullable=True, index=True)

    keys = relationship("UserKey", back_populates="user", cascade="all, delete-orphan")
    box = relationship("Box", back_populates="users")


class Key(Base):
    __tablename__ = "keys"

    id = Column(Integer, primary_key=True)
    code = Column(String(64), unique=True, nullable=False, index=True)
    description = Column(String(255), nullable=True)
    rfid = Column(String(128), nullable=True, unique=True)
    secret_code = Column(String(128), nullable=True, unique=True)
    # Привязка ключа к боксу и помещению (комнате)
    box_id = Column(Integer, ForeignKey("boxes.id", ondelete="SET NULL"), nullable=True, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id", ondelete="SET NULL"), nullable=True, index=True)
    # Позиция ключа в ящике (координаты сетки)
    pos_x = Column(Integer, nullable=True)
    pos_y = Column(Integer, nullable=True)

    users = relationship("UserKey", back_populates="key", cascade="all, delete-orphan")
    room = relationship("Room", back_populates="keys")
    box = relationship("Box", back_populates="keys")


class Box(Base):
    __tablename__ = "boxes"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False, index=True)
    comment = Column(String(255), nullable=True)
    # Optional grid position info for a key slot (if box represents a single slot)
    row = Column(Integer, nullable=True)
    column = Column(Integer, nullable=True)
    # Параметры выкладки тайлов (размер сетки):
    # x — количество столбцов; y — количество строк
    x = Column(Integer, nullable=True)
    y = Column(Integer, nullable=True)
    # Capacity info
    rooms = Column(Integer, nullable=True)  # количество помещений (мест)
    keys_capacity = Column(Integer, nullable=True)  # сколько ключей может храниться
    # Агрегированные показатели по боксу:
    # key_count — общее количество ключей в этом боксе
    # room_count — общее количество помещений/комнат в этом боксе
    # keys_current — фактическое число ключей, находящихся в боксе сейчас
    key_count = Column(Integer, nullable=True)
    room_count = Column(Integer, nullable=True)
    keys_current = Column(Integer, nullable=True)

    users = relationship("User", back_populates="box")
    keys = relationship("Key", back_populates="box")


class Room(Base):
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)
    comment = Column(String(255), nullable=True)
    keys = relationship("Key", back_populates="room")

    __table_args__ = (
        UniqueConstraint("name", name="uq_rooms_name"),
    )


class UserKey(Base):
    __tablename__ = "user_keys"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    key_id = Column(Integer, ForeignKey("keys.id", ondelete="CASCADE"), nullable=False)
    # Привязка допуска к конкретному боксу (если допуск зависит от ящика)
    box_id = Column(Integer, ForeignKey("boxes.id", ondelete="SET NULL"), nullable=True, index=True)
    # Сводное состояние ключа для удобной выборки
    # state: 'выдан' | 'не выдан'
    state = Column(String(16), nullable=False, default='не выдан')
    # state_user_id: 0 — не выдан; -1 — выдан по секретному коду; >0 — id пользователя
    state_user_id = Column(Integer, nullable=False, default=0)
    state_updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User", back_populates="keys")
    key = relationship("Key", back_populates="users")


class IssuedKey(Base):
    __tablename__ = "issued_keys"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    key_id = Column(Integer, ForeignKey("keys.id", ondelete="CASCADE"), nullable=False, unique=True)
    issued_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User")
    key = relationship("Key")


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    key_id = Column(Integer, ForeignKey("keys.id", ondelete="CASCADE"), nullable=False)
    action = Column(String(16), nullable=False)  # 'issue' | 'return'
    event_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User")
    key = relationship("Key")


class Image(Base):
    __tablename__ = "images"

    id = Column(Integer, primary_key=True)
    issued_id = Column(Integer, ForeignKey("issued_keys.id", ondelete="CASCADE"), nullable=True, index=True)
    # Дополнительные ссылки и временные метки сессии снимков
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    box_id = Column(Integer, ForeignKey("boxes.id", ondelete="SET NULL"), nullable=True, index=True)
    session_started_at = Column(DateTime(timezone=True), nullable=True)
    session_stopped_at = Column(DateTime(timezone=True), nullable=True)
    mime_type = Column(String(64), nullable=True)
    data = Column(LargeBinary, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)



class ErrorLog(Base):
    __tablename__ = "errors"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    key_id = Column(Integer, ForeignKey("keys.id", ondelete="SET NULL"), nullable=True, index=True)
    message = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User")
    key = relationship("Key")


__all__ = ["Base", "User", "Key", "Box", "Room", "UserKey", "IssuedKey", "Event", "Image", "ErrorLog"]


