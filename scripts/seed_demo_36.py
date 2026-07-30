"""Seed demo data: 1 box, 2 users, 36 rooms, 36 keys, split permissions.

Run directly or via deploy/seed_demo_36.sh on Linux Mint.

Environment variables (all optional):
  BOX_NAME          default: "Box Demo"
  BOX_X, BOX_Y      grid size, default: 6 x 6
  USER1_LOGIN       default: "user1"
  USER1_PASSWORD    default: "user1"
  USER2_LOGIN       default: "user2"
  USER2_PASSWORD    default: "user2"
  ROOM_COUNT        default: 36
  DATABASE_URL      PostgreSQL connection string
"""
from __future__ import annotations

import math
import os
import sys

from sqlalchemy import select, text
from sqlalchemy.orm import Session

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from db.models import Base, Box, Key, Room, User, UserKey
from db.session import ensure_box_columns, get_engine, update_boxes_layout_and_stats
from services.auth_service import hash_password


def ensure_schema(engine) -> None:
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE IF NOT EXISTS boxes (id SERIAL PRIMARY KEY, name VARCHAR(128) NOT NULL, comment VARCHAR(255), row INTEGER, "column" INTEGER, UNIQUE(name))'))
        conn.execute(text("CREATE TABLE IF NOT EXISTS rooms (id SERIAL PRIMARY KEY, name VARCHAR(128) NOT NULL, comment VARCHAR(255), CONSTRAINT uq_rooms_name UNIQUE(name))"))
        conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS box_id INTEGER REFERENCES boxes(id) ON DELETE SET NULL"))
        conn.execute(text("ALTER TABLE keys ADD COLUMN IF NOT EXISTS box_id INTEGER REFERENCES boxes(id) ON DELETE SET NULL"))
        conn.execute(text("ALTER TABLE keys ADD COLUMN IF NOT EXISTS room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL"))
        conn.execute(text("ALTER TABLE keys ADD COLUMN IF NOT EXISTS pos_x INTEGER"))
        conn.execute(text("ALTER TABLE keys ADD COLUMN IF NOT EXISTS pos_y INTEGER"))
        conn.execute(text("ALTER TABLE user_keys ADD COLUMN IF NOT EXISTS box_id INTEGER REFERENCES boxes(id) ON DELETE SET NULL"))
    ensure_box_columns(engine)


def get_or_create_box(session: Session, name: str, *, x: int, y: int, comment: str | None = None) -> Box:
    box = session.execute(select(Box).where(Box.name == name)).scalar_one_or_none()
    if box is None:
        box = Box(name=name, comment=comment, x=x, y=y)
        session.add(box)
        session.flush()
    else:
        box.x = x
        box.y = y
        if comment:
            box.comment = comment
    session.commit()
    session.refresh(box)
    return box


def get_or_create_user(
    session: Session,
    login: str,
    password: str,
    *,
    box_id: int,
    comment: str | None = None,
) -> User:
    user = session.execute(select(User).where(User.login == login)).scalar_one_or_none()
    if user is None:
        user = User(
            login=login,
            password_hash=hash_password(password),
            comment=comment,
            box_id=box_id,
        )
        session.add(user)
    else:
        user.box_id = box_id
        if comment:
            user.comment = comment
        if password:
            user.password_hash = hash_password(password)
    session.commit()
    session.refresh(user)
    return user


def get_or_create_room(session: Session, name: str, comment: str | None = None) -> Room:
    room = session.execute(select(Room).where(Room.name == name)).scalar_one_or_none()
    if room is None:
        room = Room(name=name, comment=comment)
        session.add(room)
        session.commit()
        session.refresh(room)
    elif comment and room.comment != comment:
        room.comment = comment
        session.commit()
        session.refresh(room)
    return room


def get_or_create_key(
    session: Session,
    code: str,
    *,
    description: str | None,
    box_id: int,
    room_id: int,
    pos_x: int,
    pos_y: int,
) -> Key:
    key = session.execute(select(Key).where(Key.code == code)).scalar_one_or_none()
    if key is None:
        key = Key(
            code=code,
            description=description,
            box_id=box_id,
            room_id=room_id,
            pos_x=pos_x,
            pos_y=pos_y,
        )
        session.add(key)
    else:
        key.description = description
        key.box_id = box_id
        key.room_id = room_id
        key.pos_x = pos_x
        key.pos_y = pos_y
    session.commit()
    session.refresh(key)
    return key


def assign_key(session: Session, user: User, key: Key, *, box_id: int) -> None:
    link = session.execute(
        select(UserKey).where(
            UserKey.user_id == user.id,
            UserKey.key_id == key.id,
            UserKey.box_id == box_id,
        )
    ).scalar_one_or_none()
    if link is None:
        session.add(UserKey(user_id=user.id, key_id=key.id, box_id=box_id))
        session.commit()


def main() -> int:
    room_count = int(os.getenv("ROOM_COUNT", "36"))
    if room_count < 2:
        print("ROOM_COUNT must be >= 2", file=sys.stderr)
        return 1

    box_name = os.getenv("BOX_NAME", "Box Demo")
    grid_x = int(os.getenv("BOX_X", "6"))
    grid_y = int(os.getenv("BOX_Y", "6"))
    if grid_x * grid_y < room_count:
        grid_y = math.ceil(room_count / grid_x)
        print(f"[seed] Grid expanded to {grid_x}x{grid_y} for {room_count} keys")

    user1_login = os.getenv("USER1_LOGIN", "user1")
    user1_password = os.getenv("USER1_PASSWORD", "user1")
    user2_login = os.getenv("USER2_LOGIN", "user2")
    user2_password = os.getenv("USER2_PASSWORD", "user2")

    engine = get_engine()
    ensure_schema(engine)

    keys: list[Key] = []

    with Session(engine, future=True) as session:
        box = get_or_create_box(
            session,
            box_name,
            x=grid_x,
            y=grid_y,
            comment=f"Demo box: {room_count} rooms",
        )

        user1 = get_or_create_user(
            session,
            user1_login,
            user1_password,
            box_id=box.id,
            comment="Demo user (first half of keys)",
        )
        user2 = get_or_create_user(
            session,
            user2_login,
            user2_password,
            box_id=box.id,
            comment="Demo user (second half of keys)",
        )

        for i in range(1, room_count + 1):
            room_name = f"Помещение {i:03d}"
            room = get_or_create_room(session, room_name, comment=f"Demo room #{i}")
            key_code = f"K-{i:03d}"
            pos_x = (i - 1) // grid_x
            pos_y = (i - 1) % grid_x
            key = get_or_create_key(
                session,
                key_code,
                description=room_name,
                box_id=box.id,
                room_id=room.id,
                pos_x=pos_x,
                pos_y=pos_y,
            )
            keys.append(key)

        split = room_count // 2
        for key in keys[:split]:
            assign_key(session, user1, key, box_id=box.id)
        for key in keys[split:]:
            assign_key(session, user2, key, box_id=box.id)

    update_boxes_layout_and_stats(engine, grid_x, grid_y)

    print("Seed complete:")
    print(f"  Box:      {box_name} ({grid_x}x{grid_y})")
    print(f"  Rooms:    {room_count}")
    print(f"  Keys:     {room_count} (K-001 .. K-{room_count:03d})")
    print(f"  User 1:   {user1_login} / {user1_password} -> {split} keys")
    print(f"  User 2:   {user2_login} / {user2_password} -> {room_count - split} keys")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
