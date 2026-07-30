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
from config.loader import load_config
from services.rfid_server import RfidServer
from services.device_client import start_device_client, stop_device_client
from db.session import get_engine
from db.session import ensure_box_columns
from db.session import update_boxes_layout_and_stats
from db.models import Base
from services.auth_service import hash_password
from views.behaviors import HoverBehavior
from views.widgets.ios_widgets import IOSGrayButton, IOSFilledButton, IOSOutlineButton
from views.widgets.ios_widgets import IOSMenuButton, GlassPanel, IOSCardButton, IOSCardPanel
from kivy.factory import Factory
from views.widgets.fonts import register_roboto
from kivy.uix.behaviors import ButtonBehavior
from views.widgets.permission_tile import PermissionTile
from views.widgets.user_tile import UserKeyTile

import sys

def app_dir():
    return os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(app_dir(), "config.cfg")
# Early logging to verify file is being executed
print(f"[MAIN_USER] Module loaded, CONFIG_PATH={CONFIG_PATH}", flush=True)

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

# Early fail if config is missing
require_config(CONFIG_PATH)

# Ensure working directory is the app directory so relative KV paths work
try:
    os.chdir(app_dir())
except Exception:
    pass

try:
    load_config(CONFIG_PATH)
except Exception as e:
    print(f"[MAIN_USER] Config preload failed: {e}", flush=True)

class KeyHolderUserApp(MDApp):
    def build(self):
        # Console logging
        try:
            logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
            logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)
        except Exception:
            pass
        # Load config (DATABASE_URL, box_id, camera device index)
        try:
            # Use CONFIG_PATH defined at module level
            print(f"[MAIN_USER] Loading config from CONFIG_PATH: {CONFIG_PATH}", flush=True)
            app_cfg = load_config(CONFIG_PATH)
            self.app_cfg = app_cfg  # Сохраняем для использования в тайм-ауте
            self.box_id = getattr(app_cfg, 'box_id', None)
            self.camera_device_index = getattr(app_cfg, 'camera_device_index', None)
            print(f"[MAIN_USER] Config loaded: device_host={getattr(app_cfg, 'device_host', None)}, device_port={getattr(app_cfg, 'device_port', None)}", flush=True)
        except Exception as e:
            print(f"[MAIN_USER] Error loading config: {e}", flush=True, file=sys.stderr)
            self.app_cfg = None
            self.box_id = None
            self.camera_device_index = None
        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "BlueGray"
        try:
            from kivy.core.window import Window
            Window.clearcolor = (0.18, 0.19, 0.20, 1)
            try:
                # Set startup window size to 1200x800
                Window.size = (1280, 800)
            except Exception:
                pass
        except Exception:
            pass

        # Ensure DB schema
        try:
            engine = get_engine()
            Base.metadata.create_all(bind=engine)
            try:
                # Suppress verbose Pillow plugin debug spam
                try:
                    logging.getLogger("PIL").setLevel(logging.WARNING)
                    logging.getLogger("PIL.Image").setLevel(logging.WARNING)
                except Exception:
                    pass
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
                    # keys: position inside the box grid
                    conn.execute(text("ALTER TABLE keys ADD COLUMN IF NOT EXISTS pos_x INTEGER"))
                    conn.execute(text("ALTER TABLE keys ADD COLUMN IF NOT EXISTS pos_y INTEGER"))
                    # user_keys: add box_id for box-scoped permissions
                    conn.execute(text("ALTER TABLE user_keys ADD COLUMN IF NOT EXISTS box_id INTEGER REFERENCES boxes(id) ON DELETE SET NULL"))
                    # user_keys: state columns for unified key state
                    conn.execute(text("ALTER TABLE user_keys ADD COLUMN IF NOT EXISTS state VARCHAR(16) NOT NULL DEFAULT 'не выдан'"))
                    conn.execute(text("ALTER TABLE user_keys ADD COLUMN IF NOT EXISTS state_user_id INTEGER NOT NULL DEFAULT 0"))
                    conn.execute(text("ALTER TABLE user_keys ADD COLUMN IF NOT EXISTS state_updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"))
                    # events rename and images table
                    conn.execute(text("CREATE TABLE IF NOT EXISTS events (id SERIAL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, key_id INTEGER NOT NULL REFERENCES keys(id) ON DELETE CASCADE, action VARCHAR(16) NOT NULL, event_at TIMESTAMPTZ NOT NULL DEFAULT now())"))
                    conn.execute(text("CREATE TABLE IF NOT EXISTS images (id SERIAL PRIMARY KEY, issued_id INTEGER NOT NULL REFERENCES issued_keys(id) ON DELETE CASCADE, user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, box_id INTEGER REFERENCES boxes(id) ON DELETE SET NULL, session_started_at TIMESTAMPTZ, session_stopped_at TIMESTAMPTZ, mime_type VARCHAR(64), data BYTEA NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())"))
                    # images: ensure new columns on existing DBs
                    conn.execute(text("ALTER TABLE images ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"))
                    conn.execute(text("ALTER TABLE images ADD COLUMN IF NOT EXISTS box_id INTEGER REFERENCES boxes(id) ON DELETE SET NULL"))
                    conn.execute(text("ALTER TABLE images ADD COLUMN IF NOT EXISTS session_started_at TIMESTAMPTZ"))
                    conn.execute(text("ALTER TABLE images ADD COLUMN IF NOT EXISTS session_stopped_at TIMESTAMPTZ"))
                    # images: allow capture without issued_id and add helpful indexes
                    conn.execute(text("ALTER TABLE images ALTER COLUMN issued_id DROP NOT NULL"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_images_user_created ON images(user_id, created_at)"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_images_session_started ON images(session_started_at, created_at)"))
            except Exception:
                pass
            # Гарантируем наличие столбцов в boxes для любого диалекта
            try:
                ensure_box_columns(engine)
            except Exception:
                pass
            # Обновим значения полей boxes на основе текущей конфигурации сетки
            try:
                update_boxes_layout_and_stats(engine, self.controller.grid_cols, self.controller.grid_rows)
            except Exception:
                pass
        except Exception:
            pass

        # Controller — увеличенные плитки и отступы только для user-приложения
        from kivy.metrics import dp
        self.controller = AppController(
            grid_rows=4,
            grid_cols=6,
            cell_min_w=dp(240),
            cell_min_h=dp(140),
            cell_max_w=dp(320),
            cell_max_h=dp(180),
            grid_spacing=dp(16),
            camera_device_index=self.camera_device_index,
        )
        # Equipment device host/port (commands -> device)
        try:
            app_cfg = getattr(self, 'app_cfg', None)
            if app_cfg:
                device_host = getattr(app_cfg, 'device_host', None)
                device_port = getattr(app_cfg, 'device_port', None)
                # Normalize empty strings to None
                if device_host is not None and (not device_host or not str(device_host).strip()):
                    device_host = None
                setattr(self.controller, 'device_host', device_host)
                setattr(self.controller, 'device_port', device_port)
                if device_host and device_port:
                    print(f"[MAIN_USER] Controller device config: {device_host}:{device_port}")
        except Exception as e:
            print(f"[MAIN_USER] Error setting device config in controller: {e}")
            pass
        # Provide current box context to controller if available
        try:
            if hasattr(self.controller, 'set_current_box_id'):
                self.controller.set_current_box_id(self.box_id)
            else:
                # fallback: set attribute used later in controller
                setattr(self.controller, 'current_box_id', self.box_id)
        except Exception:
            pass
        # Wire simulator feedback (optional)
        try:
            app_cfg = getattr(self, 'app_cfg', None)
            setattr(self.controller, 'sim_feedback_host', getattr(app_cfg, 'rfid_feedback_host', None) if app_cfg else None)
            setattr(self.controller, 'sim_feedback_port', getattr(app_cfg, 'rfid_feedback_port', None) if app_cfg else None)
        except Exception:
            pass

        # Load KV
        try:
            register_roboto()
            Factory.register('HoverBehavior', cls=HoverBehavior)
            Factory.register('ButtonBehavior', cls=ButtonBehavior)
            Factory.register('IOSGrayButton', cls=IOSGrayButton)
            Factory.register('IOSFilledButton', cls=IOSFilledButton)
            Factory.register('IOSOutlineButton', cls=IOSOutlineButton)
            Factory.register('IOSMenuButton', cls=IOSMenuButton)
            Factory.register('GlassPanel', cls=GlassPanel)
            Factory.register('IOSCardButton', cls=IOSCardButton)
            Factory.register('IOSCardPanel', cls=IOSCardPanel)
            Factory.register('PermissionTile', cls=PermissionTile)
            Factory.register('UserKeyTile', cls=UserKeyTile)
        except Exception:
            pass
        # Ensure ActionTileButton is available before loading screens
        Builder.load_file("views/widgets/actions.kv")
        Builder.load_file("views/widgets/permission_tile.kv")
        Builder.load_file("views/widgets/user_tile.kv")
        Builder.load_file("views/widgets/buttons.kv")
        Builder.load_file("views/widgets/panels.kv")
        Builder.load_file("views/widgets/admin_forms.kv")
        Builder.load_file("views/screens/mismatch_screen.kv")
        Builder.load_file("views/widgets/key_placeholder.kv")
        Builder.load_file("views/screens/guest_screen_user.kv")
        Builder.load_file("views/screens/main_screen_user.kv")
        Builder.load_file("views/screens/issued_keys_screen.kv")
        Builder.load_file("views/screens/take_keys_screen.kv")
        Builder.load_file("views/screens/return_keys_screen.kv")
        # Load PIN-based auth screen for user app
        Builder.load_file("views/screens/auth_screen_user.kv")
        root = Builder.load_file("views/root_user.kv")
        try:
            from kivy.clock import Clock
            root.do_layout()
            Clock.schedule_once(lambda dt: root.do_layout(), 0)
        except Exception:
            pass

        try:
            self.controller.initialize_keys_grid(root)
        except Exception:
            pass

        return root

    def on_start(self):
        app_cfg = getattr(self, 'app_cfg', None)
        # Запуск RFID TCP-сервера, если указан в конфиге
        try:
            if app_cfg and getattr(app_cfg, 'rfid_host', None) and getattr(app_cfg, 'rfid_port', None):
                old_srv = getattr(self, '_rfid_server', None)
                if old_srv is not None:
                    old_srv.stop()
                self._rfid_server = RfidServer(
                    host=str(app_cfg.rfid_host),
                    port=int(app_cfg.rfid_port),
                    mode=str(getattr(app_cfg, 'rfid_mode', 'both') or 'both'),
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
        # Подключение к серверу оборудования
        try:
            if app_cfg and getattr(app_cfg, 'device_host', None) and getattr(app_cfg, 'device_port', None):
                device_host = str(app_cfg.device_host).strip()
                device_port = int(app_cfg.device_port)
                if not device_host:
                    print("[MAIN_USER] Device connection skipped: empty host")
                    stop_device_client(self)
                else:
                    print(f"[MAIN_USER] Connecting to device at {device_host}:{device_port}")
                    start_device_client(
                        self,
                        device_host,
                        device_port,
                        on_user=self.controller.handle_rfid_user,
                        on_key=self.controller.handle_rfid_key,
                        on_slot_status=getattr(self.controller, 'handle_slot_status', None),
                    )
            else:
                print(f"[MAIN_USER] Device connection skipped: cfg={app_cfg is not None}, device_host={getattr(app_cfg, 'device_host', None) if app_cfg else None}, device_port={getattr(app_cfg, 'device_port', None) if app_cfg else None}")
                stop_device_client(self)
        except Exception as e:
            print(f"[MAIN_USER] Error creating device client: {e}")
            stop_device_client(self)

        # Check connections after full screen load
        try:
            from kivy.clock import Clock
            Clock.schedule_once(lambda dt: self._check_connections(), 0.5)
        except Exception:
            pass

        # === Тайм-ауты экранов (перенесены из build) ===
        try:
            from kivy.clock import Clock
            # Читаем idle_timeout из конфига
            try:
                app_cfg = getattr(self, 'app_cfg', None)
                idle_timeout = getattr(app_cfg, 'idle_timeout', None) if app_cfg else None
                if idle_timeout is None or idle_timeout <= 0:
                    idle_timeout = 20  # Значение по умолчанию
                self._idle_seconds = idle_timeout
            except Exception:
                self._idle_seconds = 20  # Значение по умолчанию
            self._idle_ev = None
            self._idle_popup = None  # Храним ссылку на popup
            self._idle_logout_ev = None  # Таймер для автоматического разлогинивания

            def _on_current_change(inst, value):
                # При смене экрана отменяем автоматическое разлогинивание и закрываем popup
                try:
                    if getattr(self, '_idle_logout_ev', None) is not None:
                        from kivy.clock import Clock
                        Clock.unschedule(self._idle_logout_ev)
                        self._idle_logout_ev = None
                    if self._idle_popup:
                        self._idle_popup.dismiss()
                        self._idle_popup = None
                except Exception:
                    pass
                self._schedule_idle_timeout()

            try:
                self.root.bind(current=_on_current_change)
            except Exception:
                pass
            # стартовый таймер
            self._schedule_idle_timeout()
            # Сбрасывать таймер при любом пользовательском действии
            def _activity(*args, **kwargs):
                try:
                    # Отменяем автоматическое разлогинивание при активности
                    if getattr(self, '_idle_logout_ev', None) is not None:
                        from kivy.clock import Clock
                        Clock.unschedule(self._idle_logout_ev)
                        self._idle_logout_ev = None
                    # Закрываем popup при активности
                    if self._idle_popup:
                        self._idle_popup.dismiss()
                        self._idle_popup = None
                    self._schedule_idle_timeout()
                except Exception:
                    pass
            try:
                from kivy.core.window import Window
                try:
                    Window.bind(on_key_down=lambda *a: _activity())
                except Exception:
                    pass
                # На разных платформах события могут отличаться — оборачиваем в try
                for evt in ("on_touch_down", "on_mouse_down"):
                    try:
                        Window.bind(**{evt: lambda *a, **k: _activity()})
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                self.root.bind(on_touch_down=lambda *a: _activity())
            except Exception:
                pass
        except Exception:
            pass

    def _check_connections(self) -> None:
        """Проверка соединений с БД и оборудованием, показ popup при ошибках."""
        db_error = None
        device_error = None
        
        # Проверка БД
        try:
            engine = get_engine()
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as e:
            db_error = str(e)
        
        # Проверка оборудования (только если настроено в конфиге)
        try:
            app_cfg = load_config(CONFIG_PATH)
            device_host = getattr(app_cfg, 'device_host', None) if app_cfg else None
            device_port = getattr(app_cfg, 'device_port', None) if app_cfg else None
            
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

    # ---- Idle timeout helpers ----
    def _cancel_idle_timeout(self):
        try:
            if getattr(self, '_idle_ev', None) is not None:
                from kivy.clock import Clock
                Clock.unschedule(self._idle_ev)
                self._idle_ev = None
            if getattr(self, '_idle_logout_ev', None) is not None:
                from kivy.clock import Clock
                Clock.unschedule(self._idle_logout_ev)
                self._idle_logout_ev = None
        except Exception:
            pass

    def _schedule_idle_timeout(self):
        try:
            self._cancel_idle_timeout()
            from kivy.clock import Clock
            # Popup появляется за 5 секунд до разлогинивания
            idle_seconds = float(getattr(self, '_idle_seconds', 10))
            popup_delay = max(0, idle_seconds - 5)
            self._idle_ev = Clock.schedule_once(self._on_idle_timeout, popup_delay)
        except Exception:
            pass

    def _on_idle_timeout(self, dt):
        """Обработка тайм-аута бездействия: показ popup за 5 секунд до разлогинивания."""
        try:
            sm = getattr(self, 'root', None)
            if sm is None:
                return
            
            # Если popup уже открыт, не показываем новый
            if self._idle_popup:
                return
            
            # Не показываем popup на guest экране (пользователь уже разлогинен)
            current = getattr(sm, 'current', None)
            if current == 'guest':
                return
            
            # Показываем popup
            try:
                from kivy.uix.popup import Popup
                from kivy.uix.boxlayout import BoxLayout
                from kivy.uix.label import Label
                from kivy.uix.button import Button
                from kivy.metrics import dp, sp
                from kivy.clock import Clock
                
                content = BoxLayout(orientation='vertical', spacing=dp(10), padding=dp(15))
                
                # Заголовок
                title_label = Label(
                    text="Обнаружено бездействие.",
                    size_hint_y=None,
                    height=dp(40),
                    font_size=sp(18),
                    color=(1, 1, 1, 1)
                )
                content.add_widget(title_label)
                
                # Сообщение
                message_label = Label(
                    text="Ваша пользовательская сессия\n автоматически завершится через 5 секунд.\n\nНажмите 'ОК', чтобы остаться в системе.",
                    text_size=(None, None),
                    halign='center',
                    valign='middle',
                    color=(1, 1, 1, 1),
                    font_size=sp(14)
                )
                content.add_widget(message_label)
                
                # Кнопка OK
                btn_ok = Button(
                    text='ОК',
                    size_hint_y=None,
                    height=dp(40),
                    background_color=(0.2, 0.6, 0.9, 1)
                )
                
                def on_ok(instance):
                    try:
                        # Отменяем автоматическое разлогинивание
                        if self._idle_logout_ev:
                            Clock.unschedule(self._idle_logout_ev)
                            self._idle_logout_ev = None
                        if self._idle_popup:
                            self._idle_popup.dismiss()
                            self._idle_popup = None
                        # Сбрасываем таймер
                        self._schedule_idle_timeout()
                    except Exception:
                        pass
                
                btn_ok.bind(on_release=on_ok)
                content.add_widget(btn_ok)
                
                popup = Popup(
                    title='',
                    content=content,
                    size_hint=(0.6, 0.4),
                    auto_dismiss=False
                )
                
                def on_popup_dismiss(instance):
                    try:
                        # Отменяем автоматическое разлогинивание при закрытии popup
                        if getattr(self, '_idle_logout_ev', None) is not None:
                            from kivy.clock import Clock
                            Clock.unschedule(self._idle_logout_ev)
                            self._idle_logout_ev = None
                        self._idle_popup = None
                    except Exception:
                        pass
                
                popup.bind(on_dismiss=on_popup_dismiss)
                self._idle_popup = popup
                popup.open()
                
                # Планируем автоматическое разлогинивание через 5 секунд
                def _auto_logout(dt):
                    try:
                        if self._idle_popup:
                            self._idle_popup.dismiss()
                            self._idle_popup = None
                        # Разлогиниваем пользователя
                        try:
                            self.controller.logout()
                        except Exception:
                            pass
                        # Сбрасываем таймер
                        self._schedule_idle_timeout()
                    except Exception:
                        pass
                
                self._idle_logout_ev = Clock.schedule_once(_auto_logout, 5.0)
            except Exception:
                pass
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
        KeyHolderUserApp().run()
    except Exception:
        import traceback
        traceback.print_exc()
        raise


