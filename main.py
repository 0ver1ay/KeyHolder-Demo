import os
import sys
os.environ.setdefault("KIVY_CAMERA", "opencv")
os.environ.setdefault("KIVY_LOG_LEVEL", "debug")
# Fix clipboard and input issues on Linux
if sys.platform.startswith('linux'):
    # Disable clipboard if xclip/xsel not available (prevents "ccutbuffer provider" error)
    os.environ.setdefault("KIVY_CLIPBOARD", "dummy")
    # Ensure proper input providers
    os.environ.setdefault("KIVY_WINDOW", "sdl2")
from kivymd.app import MDApp
import logging
from kivy.core.window import Window
from kivy.lang import Builder
from sqlalchemy import text, select
from sqlalchemy.orm import Session

from controllers import AppController
from config.loader import load_config
from db.session import get_engine
from db.models import Base


class KeyHolderApp(MDApp):
    def build(self):
        # Console logging
        try:
            logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
            logging.getLogger("sqlalchemy.engine.Engine").setLevel(logging.WARNING)
        except Exception:
            pass
        # Тёмная тема по умолчанию
        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "BlueGray"

        # Проверка доступности БД до загрузки интерфейса убрана

        # Автоматическое создание схемы (таблиц) при старте
        try:
            engine = get_engine()
            Base.metadata.create_all(bind=engine)
            try:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE issued_keys ADD COLUMN IF NOT EXISTS issued_at TIMESTAMPTZ NOT NULL DEFAULT now()"))
                    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(32)"))
                    conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS comment VARCHAR(255)"))
                    # Ensure box grid dimensions exist
                    conn.execute(text("ALTER TABLE boxes ADD COLUMN IF NOT EXISTS x INTEGER"))
                    conn.execute(text("ALTER TABLE boxes ADD COLUMN IF NOT EXISTS y INTEGER"))
                    # images: allow capture without issued_id and add helpful indexes
                    conn.execute(text("ALTER TABLE images ALTER COLUMN issued_id DROP NOT NULL"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_images_user_created ON images(user_id, created_at)"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_images_session_started ON images(session_started_at, created_at)"))
            except Exception:
                pass
            print("Database schema ensured (tables created if missing)")
            # Ensure default admin user exists for admin panel access
            try:
                with Session(engine, future=True) as session:
                    from db.models import User
                    from services.auth_service import hash_password
                    admin = session.execute(select(User).where(User.login == 'admin')).scalar_one_or_none()
                    if admin is None:
                        session.add(User(login='admin', password_hash=hash_password('admin')))
                        session.commit()
                        print("Admin user created with default password 'admin'")
            except Exception as _:
                pass
        except Exception as exc:
            print(f"Failed to ensure database schema: {exc}")

        # Инициализация контроллера и хранение ссылки в app.controller
        try:
            cfg = load_config()
            cam_idx = getattr(cfg, 'camera_device_index', None)
        except Exception:
            cam_idx = None
        self.controller = AppController(grid_rows=4, grid_cols=6, camera_device_index=cam_idx)


        # Загрузка KV-файлов (виджеты, экраны, корневой менеджер)
        Builder.load_file("views/widgets/buttons.kv")
        Builder.load_file("views/widgets/admin_forms.kv")
        Builder.load_file("views/widgets/key_placeholder.kv")
        Builder.load_file("views/widgets/actions.kv")
        Builder.load_file("views/widgets/panels.kv")
        Builder.load_file("views/screens/main_screen.kv")
        Builder.load_file("views/screens/issued_keys_screen.kv")
        Builder.load_file("views/screens/auth_screen.kv")
        Builder.load_file("views/screens/admin_menu.kv")
        Builder.load_file("views/screens/admin_permissions.kv")
        Builder.load_file("views/screens/admin_add_user.kv")
        Builder.load_file("views/screens/admin_add_room.kv")
        Builder.load_file("views/screens/admin_assign_box.kv")
        Builder.load_file("views/screens/admin_delete_user.kv")
        Builder.load_file("views/screens/admin_delete_room.kv")
        Builder.load_file("views/screens/admin_export.kv")
        Builder.load_file("views/screens/admin_secret_codes.kv")
        # optional user-focused screens
        try:
            Builder.load_file("views/screens/main_screen_user.kv")
        except Exception:
            pass
        try:
            Builder.load_file("views/screens/auth_screen_user.kv")
        except Exception:
            pass
        try:
            Builder.load_file("views/screens/take_keys_screen.kv")
        except Exception:
            pass
        try:
            Builder.load_file("views/screens/return_keys_screen.kv")
        except Exception:
            pass
        root = Builder.load_file("views/root.kv")
        # Привязываем коды ключей к плейсхолдерам и обновляем цвета
        try:
            self.controller.initialize_keys_grid(root)
        except Exception:
            pass
        return root

    def on_stop(self):
        pass


if __name__ == "__main__":
    KeyHolderApp().run()



