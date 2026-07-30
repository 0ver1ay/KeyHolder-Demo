import os
import sys
os.environ.setdefault("KIVY_CAMERA", "opencv")
os.environ.setdefault("KIVY_LOG_LEVEL", "info")
os.environ.setdefault("KIVY_LOG_MODE", "PYTHON")
os.environ.setdefault("KIVY_NO_FILELOG", "1")
# Fix clipboard and input issues on Linux
if sys.platform.startswith('linux'):
    # Disable clipboard if xclip/xsel not available (prevents "ccutbuffer provider" error)
    os.environ.setdefault("KIVY_CLIPBOARD", "dummy")
    # Ensure proper input providers
    os.environ.setdefault("KIVY_WINDOW", "sdl2")
from kivymd.app import MDApp
import logging
from kivy.lang import Builder
from sqlalchemy import text
from sqlalchemy.orm import Session

from controllers import AppController
from db.session import get_engine
from db.session import ensure_box_columns
from db.session import update_boxes_layout_and_stats
from db.models import Base
from services.auth_service import hash_password
from views.behaviors import HoverBehavior  # ensure class is imported so kv can resolve it
from views.widgets.ios_widgets import IOSGrayButton, IOSFilledButton, IOSOutlineButton
from views.widgets.ios_widgets import IOSMenuButton, GlassPanel, IOSCardButton, IOSCardPanel
from kivy.factory import Factory
from views.widgets.fonts import register_roboto
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.screenmanager import Screen
from views.widgets.permission_tile import PermissionTile
from views.widgets.admin_menu import AdminMenuGrid, MenuTile
from config.loader import load_config
from services.rfid_server import RfidServer
from services.device_client import start_device_client, stop_device_client

import sys

def app_dir():
    return os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(app_dir(), "config.cfg")
# Early logging to verify file is being executed
print(f"[MAIN_ADMIN] Module loaded, CONFIG_PATH={CONFIG_PATH}", flush=True)

def require_config(path: str) -> None:
    if os.path.exists(path):
        return
    msg = f"Не найден config.cfg по пути:\n{path}"
    # Кроссплатформенное отображение ошибки
    try:
        import sys
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "Ошибка конфигурации", 0x10)
        else:
            # На Linux просто выводим в консоль
            print(f"ОШИБКА: {msg}", file=sys.stderr)
    except Exception:
        pass
    raise SystemExit(2)

require_config(CONFIG_PATH)

# Ensure working directory is the app directory so relative KV paths work
try:
    os.chdir(app_dir())
except Exception:
    pass

# DATABASE_URL from config.cfg must be set before first get_engine() call.
try:
    load_config(CONFIG_PATH)
except Exception as e:
    print(f"[MAIN_ADMIN] Config preload failed: {e}", flush=True)

class KeyHolderAdminApp(MDApp):
    def build(self):
        # Console logging
        try:
            logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
            logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)
            # Suppress verbose Pillow plugin debug spam
            try:
                logging.getLogger("PIL").setLevel(logging.WARNING)
                logging.getLogger("PIL.Image").setLevel(logging.WARNING)
            except Exception:
                pass
        except Exception:
            pass
        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "BlueGray"
        try:
            from kivy.core.window import Window
            # Match app background to IOSCardPanel flat fill
            Window.clearcolor = (0.4, 0.4, 0.4, 1)
            # Ensure a minimum window size so admin grid fits without overflow
            try:
                Window.minimum_width = 640
                Window.minimum_height = 480
                try:
                    Window.size = (1200, 800)
                except Exception:
                    pass
            except Exception:
                pass
        except Exception:
            pass

        # Ensure DB schema
        try:
            engine = get_engine()
            Base.metadata.create_all(bind=engine)
            try:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE issued_keys ADD COLUMN IF NOT EXISTS issued_at TIMESTAMPTZ NOT NULL DEFAULT now()"))
                    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS pin_code VARCHAR(4)"))
                    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(32)"))
                    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS comment VARCHAR(255)"))
                    # keys: RFID
                    conn.execute(text("ALTER TABLE keys ADD COLUMN IF NOT EXISTS rfid VARCHAR(128) UNIQUE"))
                    # keys: secret code issuance
                    conn.execute(text("ALTER TABLE keys ADD COLUMN IF NOT EXISTS secret_code VARCHAR(128) UNIQUE"))
                    # New structure: boxes, rooms and FKs
                    conn.execute(text("CREATE TABLE IF NOT EXISTS boxes (id SERIAL PRIMARY KEY, name VARCHAR(128) NOT NULL, comment VARCHAR(255), row INTEGER, column INTEGER, UNIQUE(name))"))
                    # boxes: ensure columns exist on already-created tables
                    conn.execute(text("ALTER TABLE boxes ADD COLUMN IF NOT EXISTS row INTEGER"))
                    conn.execute(text("ALTER TABLE boxes ADD COLUMN IF NOT EXISTS column INTEGER"))
                    conn.execute(text("ALTER TABLE boxes ADD COLUMN IF NOT EXISTS rooms INTEGER"))
                    conn.execute(text("ALTER TABLE boxes ADD COLUMN IF NOT EXISTS keys_capacity INTEGER"))
                    conn.execute(text("ALTER TABLE boxes ADD COLUMN IF NOT EXISTS x INTEGER"))
                    conn.execute(text("ALTER TABLE boxes ADD COLUMN IF NOT EXISTS y INTEGER"))
                    conn.execute(text("ALTER TABLE boxes ADD COLUMN IF NOT EXISTS key_count INTEGER"))
                    conn.execute(text("ALTER TABLE boxes ADD COLUMN IF NOT EXISTS room_count INTEGER"))
                    conn.execute(text("ALTER TABLE boxes ADD COLUMN IF NOT EXISTS keys_current INTEGER"))
                    conn.execute(text("CREATE TABLE IF NOT EXISTS rooms (id SERIAL PRIMARY KEY, name VARCHAR(128) NOT NULL, comment VARCHAR(255), CONSTRAINT uq_rooms_name UNIQUE(name))"))
                    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS box_id INTEGER REFERENCES boxes(id) ON DELETE SET NULL"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_box_id ON users(box_id)"))
                    conn.execute(text("ALTER TABLE keys ADD COLUMN IF NOT EXISTS box_id INTEGER REFERENCES boxes(id) ON DELETE SET NULL"))
                    conn.execute(text("ALTER TABLE keys ADD COLUMN IF NOT EXISTS room_id INTEGER REFERENCES rooms(id) ON DELETE SET NULL"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_keys_box_id ON keys(box_id)"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_keys_room_id ON keys(room_id)"))
                    # user_keys: add box_id for box-scoped permissions
                    conn.execute(text("ALTER TABLE user_keys ADD COLUMN IF NOT EXISTS box_id INTEGER REFERENCES boxes(id) ON DELETE SET NULL"))
                    # user_keys: state columns for unified key state
                    conn.execute(text("ALTER TABLE user_keys ADD COLUMN IF NOT EXISTS state VARCHAR(16) NOT NULL DEFAULT 'не выдан'"))
                    conn.execute(text("ALTER TABLE user_keys ADD COLUMN IF NOT EXISTS state_user_id INTEGER NOT NULL DEFAULT 0"))
                    conn.execute(text("ALTER TABLE user_keys ADD COLUMN IF NOT EXISTS state_updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"))
                    # events rename and images table
                    conn.execute(text("CREATE TABLE IF NOT EXISTS events (id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, key_id INTEGER NOT NULL REFERENCES keys(id) ON DELETE CASCADE, action VARCHAR(16) NOT NULL, event_at TIMESTAMPTZ NOT NULL DEFAULT now())"))
                    conn.execute(text("CREATE TABLE IF NOT EXISTS images (id SERIAL PRIMARY KEY, issued_id INTEGER NOT NULL REFERENCES issued_keys(id) ON DELETE CASCADE, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, box_id INTEGER REFERENCES boxes(id) ON DELETE SET NULL, session_started_at TIMESTAMPTZ, session_stopped_at TIMESTAMPTZ, mime_type VARCHAR(64), data BYTEA NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())"))
                    # keys: position inside the box grid
                    conn.execute(text("ALTER TABLE keys ADD COLUMN IF NOT EXISTS pos_x INTEGER"))
                    conn.execute(text("ALTER TABLE keys ADD COLUMN IF NOT EXISTS pos_y INTEGER"))
                    # images: ensure new columns on existing DBs
                    conn.execute(text("ALTER TABLE images ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"))
                    conn.execute(text("ALTER TABLE images ADD COLUMN IF NOT EXISTS box_id INTEGER REFERENCES boxes(id) ON DELETE SET NULL"))
                    conn.execute(text("ALTER TABLE images ADD COLUMN IF NOT EXISTS session_started_at TIMESTAMPTZ"))
                    conn.execute(text("ALTER TABLE images ADD COLUMN IF NOT EXISTS session_stopped_at TIMESTAMPTZ"))
                    # images: allow capture without issued_id and add helpful indexes
                    conn.execute(text("ALTER TABLE images ALTER COLUMN issued_id DROP NOT NULL"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_images_user_created ON images(user_id, created_at)"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_images_session_started ON images(session_started_at, created_at)"))
                # Обновить пароли всех пользователей на '1111' (уникальная соль для каждого)
                try:
                    from db.models import User
                    with Session(engine, future=True) as session:
                        users = session.query(User).all()
                        for u in users:
                            u.password_hash = hash_password('1111')
                        session.commit()
                except Exception:
                    pass
            except Exception:
                pass
            # Гарантируем наличие столбцов в boxes для любого диалекта
            try:
                ensure_box_columns(engine)
            except Exception:
                pass
            # Обновим значения полей boxes на основе конфигурации сетки админ-приложения
            try:
                update_boxes_layout_and_stats(engine, 6, 4)
            except Exception:
                pass
        except Exception:
            pass

        # Controller
        try:
            # Use CONFIG_PATH defined at module level
            cfg = load_config(CONFIG_PATH)
            cam_idx = getattr(cfg, 'camera_device_index', None)
        except Exception:
            cam_idx = None
        self.controller = AppController(grid_rows=4, grid_cols=6, camera_device_index=cam_idx)
        # Equipment device host/port (commands -> device)
        try:
            if cfg:
                device_host = getattr(cfg, 'device_host', None)
                device_port = getattr(cfg, 'device_port', None)
                # Normalize empty strings to None
                if device_host is not None and (not device_host or not str(device_host).strip()):
                    device_host = None
                setattr(self.controller, 'device_host', device_host)
                setattr(self.controller, 'device_port', device_port)
                if device_host and device_port:
                    print(f"[MAIN_ADMIN] Controller device config: {device_host}:{device_port}")
        except Exception as e:
            print(f"[MAIN_ADMIN] Error setting device config in controller: {e}")
            pass

        # Load KV (admin only)
        try:
            # Register fonts before loading kv so font_name: 'Roboto' works
            register_roboto()
            # make HoverBehavior available for kv baseclass resolution
            Factory.register('HoverBehavior', cls=HoverBehavior)
            # expose ButtonBehavior for KV dynamic classes (e.g., PermissionTile@ButtonBehavior+MDCard)
            Factory.register('ButtonBehavior', cls=ButtonBehavior)
            Factory.register('IOSGrayButton', cls=IOSGrayButton)
            Factory.register('IOSFilledButton', cls=IOSFilledButton)
            Factory.register('IOSOutlineButton', cls=IOSOutlineButton)
            Factory.register('IOSMenuButton', cls=IOSMenuButton)
            Factory.register('GlassPanel', cls=GlassPanel)
            Factory.register('IOSCardButton', cls=IOSCardButton)
            Factory.register('IOSCardPanel', cls=IOSCardPanel)
            # Register custom PermissionTile so Factory.PermissionTile() works
            Factory.register('PermissionTile', cls=PermissionTile)
            Factory.register('AdminMenuGrid', cls=AdminMenuGrid)
            Factory.register('MenuTile', cls=MenuTile)
        except Exception:
            pass
        Builder.load_file("views/widgets/neumorphism.kv")
        Builder.load_file("views/widgets/buttons.kv")
        Builder.load_file("views/widgets/admin_forms.kv")
        Builder.load_file("views/widgets/key_placeholder.kv")
        Builder.load_file("views/widgets/panels.kv")
        Builder.load_file("views/widgets/permission_tile.kv")
        Builder.load_file("views/widgets/user_tile.kv")
        Builder.load_file("views/widgets/actions.kv")
        Builder.load_file("views/screens/admin_menu.kv")
        Builder.load_file("views/screens/admin_secret_codes.kv")
        Builder.load_file("views/screens/admin_permissions.kv")
        Builder.load_file("views/screens/admin_add_user.kv")
        Builder.load_file("views/screens/admin_add_room.kv")
        Builder.load_file("views/screens/admin_add_box.kv")
        Builder.load_file("views/screens/admin_delete_user.kv")
        Builder.load_file("views/screens/admin_delete_room.kv")
        Builder.load_file("views/screens/admin_delete_box.kv")
        Builder.load_file("views/screens/admin_export.kv")
        Builder.load_file("views/screens/issued_keys_screen.kv")
        Builder.load_file("views/screens/admin_assign_box.kv")
        Builder.load_file("views/screens/admin_register_rfid.kv")
        Builder.load_file("views/screens/admin_register_user_rfid.kv")
        Builder.load_file("views/screens/admin_reassign_user_rfid.kv")
        Builder.load_file("views/screens/admin_images.kv")
        root = Builder.load_file("views/root_admin.kv")
        # Trigger initial layout twice (after next frame) and fire a size event on the admin screen
        try:
            from kivy.clock import Clock
            root.do_layout()
            def _post(dt):
                root.do_layout()
                try:
                    scr = root.get_screen("admin_menu")
                    if hasattr(scr, 'dispatch'):
                        scr.dispatch('on_size')
                except Exception:
                    pass
                # ensure admin permissions grid populates if spinner already has a value
                try:
                    admin_scr = root.get_screen("admin_permissions")
                    sp = admin_scr.ids.get("admin_user_spinner") if hasattr(admin_scr, "ids") else None
                    if sp and getattr(sp, 'text', '') and sp.text != 'Выберите пользователя':
                        self.controller.on_admin_user_selected(sp.text)
                except Exception:
                    pass
            Clock.schedule_once(_post, 0)
        except Exception:
            pass
        return root

    def on_start(self):
        # Загрузить конфигурацию один раз для всех блоков
        cfg = None
        try:
            # Use CONFIG_PATH defined at module level
            print(f"[MAIN_ADMIN] Loading config from CONFIG_PATH: {CONFIG_PATH}", flush=True)
            cfg = load_config(CONFIG_PATH)
            print(f"[MAIN_ADMIN] Config loaded: device_host={getattr(cfg, 'device_host', None)}, device_port={getattr(cfg, 'device_port', None)}", flush=True)
        except Exception as e:
            print(f"[MAIN_ADMIN] Error loading config: {e}", flush=True, file=sys.stderr)
            cfg = None
        # Поднять RFID-сервер, если задан в конфиге
        try:
            # Wire simulator feedback (optional)
            try:
                if cfg:
                    setattr(self.controller, 'sim_feedback_host', getattr(cfg, 'rfid_feedback_host', None))
                    setattr(self.controller, 'sim_feedback_port', getattr(cfg, 'rfid_feedback_port', None))
            except Exception:
                pass
            if cfg and getattr(cfg, 'rfid_host', None) and getattr(cfg, 'rfid_port', None):
                old_srv = getattr(self, '_rfid_server', None)
                if old_srv is not None:
                    old_srv.stop()
                self._rfid_server = RfidServer(
                    host=str(cfg.rfid_host),
                    port=int(cfg.rfid_port),
                    mode=str(getattr(cfg, 'rfid_mode', 'both') or 'both'),
                    on_user=self.controller.handle_rfid_user,
                    on_key=self.controller.handle_rfid_key,
                    on_lock_open=getattr(self.controller, 'handle_lock_open', None),
                    on_slot_status=getattr(self.controller, 'handle_slot_status', None),
                )
                self._rfid_server.start()
            else:
                self._rfid_server = None
        except Exception:
            self._rfid_server = None
        # Подключение к серверу оборудования (если оно не делает исходящих подключений к нам)
        try:
            if cfg and getattr(cfg, 'device_host', None) and getattr(cfg, 'device_port', None):
                device_host = str(cfg.device_host).strip()
                device_port = int(cfg.device_port)
                if device_host:
                    print(f"[MAIN_ADMIN] Connecting to device at {device_host}:{device_port}")
                    start_device_client(
                        self,
                        device_host,
                        device_port,
                        on_user=self.controller.handle_rfid_user,
                        on_key=self.controller.handle_rfid_key,
                        on_slot_status=getattr(self.controller, 'handle_slot_status', None),
                    )
                else:
                    print("[MAIN_ADMIN] Device connection skipped: empty host")
                    stop_device_client(self)
            else:
                print(f"[MAIN_ADMIN] Device connection skipped: cfg={cfg is not None}, device_host={getattr(cfg, 'device_host', None) if cfg else None}, device_port={getattr(cfg, 'device_port', None) if cfg else None}")
                stop_device_client(self)
        except Exception as e:
            print(f"[MAIN_ADMIN] Error creating device client: {e}")
            stop_device_client(self)
        
        # Check connections after full screen load
        try:
            from kivy.clock import Clock
            Clock.schedule_once(lambda dt: self._check_connections(cfg), 0.5)
        except Exception:
            pass

    def _check_connections(self, cfg) -> None:
        """Проверка соединений с БД и оборудованием, показ popup при ошибках."""
        db_error = None
        device_error = None
        
        # Проверка БД
        try:
            from db.session import get_engine
            from sqlalchemy import text
            engine = get_engine()
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as e:
            db_error = str(e)
        
        # Проверка оборудования (только если настроено в конфиге)
        try:
            device_host = getattr(cfg, 'device_host', None) if cfg else None
            device_port = getattr(cfg, 'device_port', None) if cfg else None
            
            if device_host and device_port:
                device_host = str(device_host).strip()
                if device_host:
                    dc = getattr(self, '_device_client', None)
                    if dc is not None and not dc.is_connected:
                        device_error = f"Не удалось подключиться к {device_host}:{device_port}"
                    elif dc is None:
                        import socket
                        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        s.settimeout(2.0)
                        try:
                            result = s.connect_ex((device_host, int(device_port)))
                            if result != 0:
                                device_error = f"Не удалось подключиться к {device_host}:{device_port}"
                        except Exception as e:
                            device_error = f"Ошибка подключения к оборудованию: {str(e)}"
                        finally:
                            s.close()
        except Exception as e:
            device_error = f"Ошибка проверки оборудования: {str(e)}"
        
        # Показ popup, если есть ошибки
        if db_error or device_error:
            try:
                from kivy.uix.popup import Popup
                from kivy.uix.boxlayout import BoxLayout
                from kivy.uix.label import Label
                from kivy.uix.button import Button
                from kivy.metrics import dp, sp
                
                content = BoxLayout(orientation='vertical', spacing=dp(10), padding=dp(15))
                
                # Заголовок
                title_label = Label(
                    text='Предупреждение о соединениях',
                    size_hint_y=None,
                    height=dp(40),
                    font_size=sp(18),
                    color=(1, 1, 1, 1)
                )
                content.add_widget(title_label)
                
                # Сообщения об ошибках
                error_text = []
                if db_error:
                    error_text.append(
                        "База данных:\n"
                        "  Не удалось подключиться. Проверьте config.cfg (адрес, порт, логин, пароль)."
                    )
                if device_error:
                    error_text.append(f"Оборудование:\n  {device_error}")

                error_label = Label(
                    text='\n\n'.join(error_text),
                    text_size=(dp(420), None),
                    size_hint_y=None,
                    height=dp(100),
                    halign='left',
                    valign='top',
                    color=(1, 0.85, 0.85, 1),
                    font_size=sp(14)
                )
                content.add_widget(error_label)
                
                # Кнопка закрытия
                btn_close = Button(
                    text='Закрыть',
                    size_hint_y=None,
                    height=dp(40),
                    background_color=(0.2, 0.6, 0.9, 1)
                )
                content.add_widget(btn_close)
                
                popup = Popup(
                    title='',
                    content=content,
                    size_hint=(0.7, 0.5),
                    auto_dismiss=False,
                    background='atlas://data/images/defaulttheme/button_pressed'
                )
                
                btn_close.bind(on_release=popup.dismiss)
                popup.open()
            except Exception:
                pass

    def on_stop(self):
        try:
            srv = getattr(self, '_rfid_server', None)
            if srv is not None:
                srv.stop()
        except Exception:
            pass
        try:
            stop_device_client(self)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        KeyHolderAdminApp().run()
    except Exception:
        import traceback
        traceback.print_exc()
        raise


