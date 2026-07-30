from __future__ import annotations

import os
import sys
from pathlib import Path
from configparser import ConfigParser
from sqlalchemy.engine import make_url


class AppConfig:
    def __init__(self, database_url: str | None, box_id: int | None, rfid_host: str | None, rfid_port: int | None, rfid_mode: str | None, camera_device_index: int | None, rfid_feedback_host: str | None, rfid_feedback_port: int | None, device_host: str | None, device_port: int | None, idle_timeout: int | None) -> None:
        self.database_url = database_url
        self.box_id = box_id
        self.rfid_host = rfid_host
        self.rfid_port = rfid_port
        self.rfid_mode = rfid_mode
        self.camera_device_index = camera_device_index
        # Optional feedback channel (app -> simulator)
        self.rfid_feedback_host = rfid_feedback_host
        self.rfid_feedback_port = rfid_feedback_port
        # Equipment device command target (app -> device)
        self.device_host = device_host
        self.device_port = device_port
        # Idle timeout in seconds
        self.idle_timeout = idle_timeout


def _compose_db_url(host: str | None, port: str | None, user: str | None, password: str | None, db_name: str | None) -> str | None:
    # Store original values for logging
    orig_host, orig_port, orig_user, orig_password, orig_dbname = host, port, user, password, db_name
    
    # Apply defaults if None or empty
    host = host or "127.0.0.1"
    port = port or "5433"
    user = user or "postgres"
    password = password or "postgres"
    db_name = db_name or "postgres"
    
    # Log what values are being used (showing if defaults were applied)
    print(f"[CONFIG] Composing DB URL:", flush=True)
    print(f"[CONFIG]   host: {repr(orig_host)} -> {host} {'(default)' if not orig_host else ''}", flush=True)
    print(f"[CONFIG]   port: {repr(orig_port)} -> {port} {'(default)' if not orig_port else ''}", flush=True)
    print(f"[CONFIG]   user: {repr(orig_user)} -> {user} {'(default)' if not orig_user else ''}", flush=True)
    print(f"[CONFIG]   password: {'***' if orig_password else None} -> {'***' if password else None} {'(default)' if not orig_password else ''}", flush=True)
    print(f"[CONFIG]   dbname: {repr(orig_dbname)} -> {db_name} {'(default)' if not orig_dbname else ''}", flush=True)
    
    # Only PostgreSQL is supported in current app stack
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{db_name}"


def load_config(cfg_path: str | os.PathLike[str] | None = None) -> AppConfig:
    # Determine path: prefer provided, else project root/config.cfg
    if cfg_path is None:
        root = Path(__file__).resolve().parents[1]
        cfg_path = root / "config.cfg"
    
    # Convert to string for logging
    cfg_path_str = str(cfg_path)
    print(f"[CONFIG] Loading config from: {cfg_path_str}", flush=True)
    print(f"[CONFIG] Config file exists: {os.path.exists(cfg_path_str)}", flush=True)

    parser = ConfigParser()
    database_url: str | None = None
    box_id: int | None = None
    rfid_host: str | None = None
    rfid_port: int | None = None
    rfid_mode: str | None = None
    camera_device_index: int | None = None
    device_host: str | None = None
    device_port: int | None = None
    rfid_feedback_host: str | None = None
    rfid_feedback_port: int | None = None
    idle_timeout: int | None = None

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            parser.read_file(f)
        if parser.has_section("database"):
            host = parser.get("database", "host", fallback=None)
            port = parser.get("database", "port", fallback=None)
            user = parser.get("database", "user", fallback=None)
            password = parser.get("database", "password", fallback=None)
            dbname = parser.get("database", "name", fallback=None)
            database_url = _compose_db_url(host, port, user, password, dbname)
        if parser.has_section("device"):
            raw_box_id = parser.get("device", "box_id", fallback=None)
            try:
                if raw_box_id:
                    box_id = int(raw_box_id)
            except Exception:
                box_id = None
            # optional camera index in this section
            raw_cam = parser.get("device", "camera_index", fallback=None)
            try:
                if raw_cam is not None and raw_cam != "":
                    camera_device_index = int(raw_cam)
            except Exception:
                camera_device_index = None
            # device command endpoint (equipment)
            device_host = parser.get("device", "device_host", fallback=None)
            # Normalize empty strings to None
            if device_host is not None and (not device_host or not device_host.strip()):
                device_host = None
            raw_dev_port = parser.get("device", "device_port", fallback=None)
            try:
                if raw_dev_port:
                    device_port = int(raw_dev_port)
            except Exception:
                device_port = None
            # Debug logging
            print(f"[CONFIG] device_host from config: {repr(device_host)}", flush=True)
            print(f"[CONFIG] device_port from config: {repr(device_port)}", flush=True)
            # Idle timeout
            raw_idle_timeout = parser.get("device", "idle_timeout", fallback=None)
            try:
                if raw_idle_timeout:
                    idle_timeout = int(raw_idle_timeout)
            except Exception:
                idle_timeout = None
        if parser.has_section("rfid"):
            rfid_host = parser.get("rfid", "host", fallback=None)
            raw_port = parser.get("rfid", "port", fallback=None)
            rfid_mode = parser.get("rfid", "mode", fallback=None)
            try:
                if raw_port:
                    rfid_port = int(raw_port)
            except Exception:
                rfid_port = None
            # Optional feedback target (app -> simulator)
            rfid_feedback_host = parser.get("rfid", "feedback_host", fallback=None)
            raw_fb_port = parser.get("rfid", "feedback_port", fallback=None)
            try:
                if raw_fb_port:
                    rfid_feedback_port = int(raw_fb_port)
            except Exception:
                rfid_feedback_port = None
    except Exception:
        # Use defaults silently when config is missing or invalid
        pass

    # Export database URL into env so db.session.get_engine picks it up
    if database_url:
        safe_url = database_url
        try:
            from sqlalchemy.engine import make_url
            safe_url = make_url(database_url).render_as_string(hide_password=True)
        except Exception:
            pass
        print(f"[CONFIG] Setting DATABASE_URL from config: {safe_url}", flush=True)
        os.environ.setdefault("DATABASE_URL", database_url)
    else:
        print(f"[CONFIG] No DATABASE_URL in config, will use default", flush=True)

    return AppConfig(database_url=database_url, box_id=box_id, rfid_host=rfid_host, rfid_port=rfid_port, rfid_mode=rfid_mode, camera_device_index=camera_device_index, rfid_feedback_host=rfid_feedback_host, rfid_feedback_port=rfid_feedback_port, device_host=device_host, device_port=device_port, idle_timeout=idle_timeout)



