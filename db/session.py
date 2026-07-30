from __future__ import annotations

import os
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
import logging
from sqlalchemy.orm import sessionmaker


DEFAULT_DB_URL = "postgresql+psycopg2://postgres:postgres@127.0.0.1:5433/postgres"


def get_db_url() -> str:
    # По умолчанию коннектимся к локальной БД "postgres" пользователем postgres/postgres
    # Можно переопределить через переменную окружения DATABASE_URL
    return os.getenv("DATABASE_URL", DEFAULT_DB_URL)


def _build_engine(url: str):
    return create_engine(url, future=True, pool_pre_ping=True)


def get_engine():
    """Создаёт engine. Если DATABASE_URL задан, но не доступен, пробуем дефолтный."""
    primary_url = os.getenv("DATABASE_URL")
    default_url = DEFAULT_DB_URL

    if not primary_url:
        print(f"[DB] DATABASE_URL not set, using default: {make_url(default_url).render_as_string(hide_password=True)}", flush=True)
        engine = _build_engine(default_url)
        # Test and announce successful connection
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            safe_url = make_url(default_url).render_as_string(hide_password=True)
            print(f"[DB] Connected successfully to: {safe_url}", flush=True)
        except Exception as e:
            print(f"[DB] Failed to connect to default database: {e}", flush=True)
        return engine

    # Пробуем подключиться к первичному URL
    try:
        safe_primary = make_url(primary_url).render_as_string(hide_password=True)
        print(f"[DB] Attempting connection to: {safe_primary}", flush=True)
        engine = _build_engine(primary_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        # Announce successful connection to primary URL
        print(f"[DB] Connected successfully to: {safe_primary}", flush=True)
        return engine
    except Exception as e:
        try:
            safe_primary = make_url(primary_url).render_as_string(hide_password=True)
        except Exception:
            safe_primary = primary_url.split('@')[-1] if '@' in primary_url else primary_url
        print(f"[DB] Failed to connect to primary database ({safe_primary}): {e}", flush=True)
        # Пытаемся фолбэк на дефолтный URL
        try:
            safe_default = make_url(default_url).render_as_string(hide_password=True)
            print(f"[DB] Attempting fallback connection to: {safe_default}", flush=True)
            fb_engine = _build_engine(default_url)
            with fb_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            print(f"[DB] Connected successfully (fallback) to: {safe_default}", flush=True)
            return fb_engine
        except Exception as fb_e:
            print(f"[DB] Fallback connection also failed: {fb_e}", flush=True)
            # Вернём engine с первичным URL — последующие вызовы покажут ошибку
            return _build_engine(primary_url)


def get_session_maker():
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False, future=True)


## Удалены функции теста подключения и безопасного логирования URL


def ensure_box_columns(engine) -> None:
    """Добавляет недостающие столбцы в таблицу boxes для различных СУБД.

    Работает идемпотентно: сначала проверяет наличие столбцов, затем выполняет ALTER.
    Для SQLite используется PRAGMA table_info и простой ADD COLUMN без IF NOT EXISTS.
    Для PostgreSQL — ALTER ... ADD COLUMN IF NOT EXISTS.
    """
    try:
        needed = (
            ("row", "INTEGER"),
            ("column", "INTEGER"),
            ("rooms", "INTEGER"),
            ("keys_capacity", "INTEGER"),
            ("x", "INTEGER"),
            ("y", "INTEGER"),
            ("key_count", "INTEGER"),
            ("room_count", "INTEGER"),
            ("keys_current", "INTEGER"),
        )
        dialect = getattr(engine, "dialect", None)
        name = getattr(dialect, "name", "") if dialect else ""
        with engine.begin() as conn:
            if name == "sqlite":
                # Соберём существующие имена столбцов
                try:
                    rows = conn.execute(text("PRAGMA table_info(boxes)")).all()
                    existing = {str(r[1]) for r in rows}
                except Exception:
                    existing = set()
                for col, coltype in needed:
                    if col not in existing:
                        try:
                            conn.execute(text(f"ALTER TABLE boxes ADD COLUMN {col} {coltype}"))
                        except Exception:
                            pass
            else:
                # По умолчанию — PostgreSQL и совместимые диалекты
                for col, coltype in needed:
                    try:
                        conn.execute(text(f"ALTER TABLE boxes ADD COLUMN IF NOT EXISTS {col} {coltype}"))
                    except Exception:
                        pass
    except Exception:
        # Тихий режим, чтобы не ломать запуск приложения
        pass


def update_boxes_layout_and_stats(engine, grid_cols: int, grid_rows: int) -> None:
    """Обновляет поля boxes.x, boxes.y, boxes.key_count, boxes.room_count, boxes.keys_current.

    - x, y берём из параметров (текущая конфигурация сетки UI)
    - key_count     = количество ключей в боксе
    - room_count    = количество уникальных rooms, связанных с ключами бокса
    - keys_current  = число ключей, НЕ выданных сейчас (нет записи в issued_keys)
    Так же создаёт одну запись в boxes, если таблица пуста.
    """
    try:
        with engine.begin() as conn:
            # Создать запись по умолчанию, если таблица пустая
            conn.execute(text(
                """
                INSERT INTO boxes (name, comment, x, y, key_count, room_count, keys_current)
                SELECT 'Box 1', NULL, :x, :y, 0, 0, 0
                WHERE NOT EXISTS (SELECT 1 FROM boxes)
                """
            ), {"x": int(grid_cols), "y": int(grid_rows)})

            # Универсальные апдейты (работают и в SQLite, и в PostgreSQL)
            conn.execute(text(
                """
                UPDATE boxes AS b
                SET
                  x = COALESCE(b.x, :x),
                  y = COALESCE(b.y, :y),
                  key_count = (
                    SELECT COUNT(*) FROM keys k WHERE k.box_id = b.id
                  ),
                  room_count = (
                    SELECT COUNT(DISTINCT k.room_id) FROM keys k
                    WHERE k.box_id = b.id AND k.room_id IS NOT NULL
                  ),
                  keys_current = (
                    SELECT COUNT(*)
                    FROM keys k
                    WHERE k.box_id = b.id
                      AND NOT EXISTS (
                        SELECT 1 FROM issued_keys ik WHERE ik.key_id = k.id
                      )
                  )
                """
            ), {"x": int(grid_cols), "y": int(grid_rows)})
    except Exception:
        # Без падения приложения
        pass

