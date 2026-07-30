from __future__ import annotations

from typing import Optional
import math
import socket
import subprocess
import sys
from datetime import datetime, timezone

from utils.datetime_local import (
    format_local_date,
    format_local_datetime,
    local_day_start,
    now_local,
)
from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.checkbox import CheckBox
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.textinput import TextInput
from kivy.uix.button import Button
from kivy.clock import Clock

from config.minmax import (
    DEFAULT_GRID_ROWS,
    DEFAULT_GRID_COLS,
    CELL_MIN_W,
    CELL_MIN_H,
    CELL_MAX_W,
    CELL_MAX_H,
    GRID_SPACING,
)
from db import get_session_maker, User, Key, Room, UserKey, IssuedKey, Event, ErrorLog, Box, Image
from services.auth_service import AuthService, hash_password
from sqlalchemy import select, text, func
from kivymd.uix.datatables import MDDataTable
from kivy.metrics import dp
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.modalview import ModalView
from services.camera_service import CameraService
from services.slot_poller import MockSlotRfidPoller
from kivy.logger import Logger as KivyLogger
from kivy.factory import Factory
from kivy.core.image import Image as CoreImage
from io import BytesIO


class AppController:
    """Главный контроллер: обработчики UI и управление раскладкой."""

    def __init__(
        self,
        grid_rows: int = DEFAULT_GRID_ROWS,
        grid_cols: int = DEFAULT_GRID_COLS,
        *,
        cell_min_w: float | None = None,
        cell_min_h: float | None = None,
        cell_max_w: float | None = None,
        cell_max_h: float | None = None,
        grid_spacing: float | None = None,
        camera_device_index: int | None = None,
    ) -> None:
        self.grid_rows = grid_rows
        self.grid_cols = grid_cols
        # Значения из централизованного конфига (с возможностью переопределения для конкретного приложения)
        self.cell_min_w = CELL_MIN_W if cell_min_w is None else float(cell_min_w)
        self.cell_min_h = CELL_MIN_H if cell_min_h is None else float(cell_min_h)
        self.cell_max_w = CELL_MAX_W if cell_max_w is None else float(cell_max_w)
        self.cell_max_h = CELL_MAX_H if cell_max_h is None else float(cell_max_h)
        self.grid_spacing = GRID_SPACING if grid_spacing is None else float(grid_spacing)
        # Инициализация слоя доступа к данным
        self._SessionLocal = get_session_maker()
        self._auth_service: AuthService | None = None
        self._current_user: User | None = None
        # Последний удачный логин (для восстановления сессии, если _current_user потерян)
        self._last_login: str | None = None
        # Состояние PIN-авторизации (логин/пароль по 4 цифры)
        self._pin_login: str = ""
        self._pin_password: str = ""

        # Пагинация для экранов пользователя (5x3 сетка)
        self._issued_tiles_all = []
        self._issued_page = 0
        self._take_tiles_all = []
        self._take_page = 0
        self._return_tiles_all = []
        self._return_page = 0
        # Мастер-списки для фильтрации и текущее значение фильтра
        self._issued_tiles_master = []
        self._take_tiles_master = []
        self._return_tiles_master = []
        self._filter_text_map = {"issued": "", "take": "", "return": ""}
        # Флаг ожидания срабатывания RFID-заглушки (только после нажатия "Авторизация")
        self._auth_stub_pending: bool = False
        # Флаг успешной авторизации (устойчивее, чем проверка _current_user на None)
        self._is_authenticated: bool = False
        # Box context
        self.current_box_id: int | None = None
        self._box_name_to_id: dict[str, int] = {}
        self._box_spinner_to_name: dict[str, str] = {}
        # Assign-to-box pagination state
        self._assign_page: int = 0
        self._assign_page_size: int = 9
        # Admin: ожидание RFID для привязки к выбранному ключу / пользователю
        self._rfid_bind_target_key_id: int | None = None
        self._rfid_bind_target_user_id: int | None = None
        # Экран, на котором показываем статус привязки RFID пользователя
        # ("admin_register_user_rfid" — авто-привязка после создания,
        #  "admin_reassign_user_rfid" — переназначение существующему).
        self._rfid_bind_ui_screen: str = "admin_register_user_rfid"
        # Сдача: экран «Сдать ключ» / секретный код / popup ожидания RFID
        self._on_return_screen: bool = False
        self._await_return_key_id: int | None = None
        self._await_secret_return_key_id: int | None = None
        self._return_rfid_popup: Popup | None = None
        # --- Camera capture session state ---
        self._camera: CameraService | None = None
        self._capture_ev = None
        self._capture_session_started_at: datetime | None = None
        self._capture_last_image_id: int | None = None
        self._camera_device_index: int | None = (int(camera_device_index) if camera_device_index is not None else None)
        self._capture_frames: int = 0
        self._images_schema_fixed: bool = False
        # --- Admin images viewer state ---
        self._img_sessions = []
        self._img_current = None  # (user_id, session_started_at)
        self._img_images = []
        self._img_index = 0
        self._img_filter_date_from: datetime | None = None
        self._img_filter_date_to: datetime | None = None
        # --- Admin export date filters ---
        self._export_filter_date_from: datetime | None = None
        self._export_filter_date_to: datetime | None = None

    # ---- Camera capture helpers ----
    def _start_capture_session(self) -> None:
        """Start periodic capture (1s) while a user is authenticated."""
        # If already running, do nothing
        try:
            from kivy.clock import Clock as _Clock
            if getattr(self, '_capture_ev', None) is not None:
                try:
                    KivyLogger.debug("capture: already running")
                except Exception:
                    pass
                return
            if self._camera is None:
                # respect configured device index if provided
                idx = int(self._camera_device_index) if self._camera_device_index is not None else 0
                self._camera = CameraService(device_index=idx)
            # Open camera best-effort
            try:
                ok = self._camera.open()
                try:
                    KivyLogger.info("capture: open camera ok=%s device_index=%s" % (ok, getattr(self, '_camera_device_index', None)))
                except Exception:
                    pass
            except Exception:
                pass
            # Session start timestamp (UTC)
            self._capture_session_started_at = datetime.now(timezone.utc)
            try:
                KivyLogger.info("capture: session started at %s" % self._capture_session_started_at)
            except Exception:
                pass
            # reset counters
            try:
                self._capture_frames = 0
            except Exception:
                pass
        except Exception:
            pass

        # Storage for device-reported slot values (keyed by 1-based (x,y))
        try:
            self._device_slot_values = {}
            self._slot_poll_ev = None
            self._rfid_key_close_evs = {}
            self._slot_poll_target = None  # (x1,y1, expect_present)
        except Exception:
            pass

        # Ensure DB allows NULL issued_id (fallback migration)
        try:
            if not getattr(self, '_images_schema_fixed', False):
                with self._SessionLocal() as session:
                    try:
                        session.execute(text("ALTER TABLE images ALTER COLUMN issued_id DROP NOT NULL"))
                        session.commit()
                        self._images_schema_fixed = True
                        KivyLogger.info("capture: adjusted schema (images.issued_id set to NULLABLE)")
                    except Exception:
                        session.rollback()
                        # Ignore if already nullable or lacks permissions
                        KivyLogger.debug("capture: schema adjust skipped or failed")
        except Exception:
            pass

        def _tick(dt):
            try:
                # Ensure still authenticated
                if not getattr(self, '_current_user', None):
                    try:
                        KivyLogger.info("capture.tick: no current user -> stopping")
                    except Exception:
                        pass
                    self._stop_capture_session()
                    return
                # Grab frame
                data = None
                try:
                    if self._camera is not None:
                        data = self._camera.read_jpeg_bytes()
                except Exception:
                    data = None
                if not data:
                    try:
                        KivyLogger.info("capture.tick: no frame data")
                    except Exception:
                        pass
                    return
                with self._SessionLocal() as session:
                    img = Image(
                        issued_id=None,  # session-based, not tied to specific issuance
                        user_id=getattr(self._current_user, 'id', None),
                        box_id=getattr(self, 'current_box_id', None),
                        session_started_at=self._capture_session_started_at,
                        session_stopped_at=None,
                        mime_type='image/jpeg',
                        data=data,
                    )
                    try:
                        session.add(img)
                        session.commit()
                        try:
                            self._capture_last_image_id = getattr(img, 'id', None)
                            self._capture_frames += 1
                            KivyLogger.info("capture.tick: insert OK id=%s bytes=%s total_frames=%s" % (self._capture_last_image_id, len(data) if data else 0, self._capture_frames))
                        except Exception:
                            pass
                    except Exception as exc:
                        session.rollback()
                        try:
                            KivyLogger.exception("capture.tick: DB insert failed: %s" % exc)
                        except Exception:
                            pass
            except Exception:
                pass

        try:
            KivyLogger.info("capture: scheduling timer...")
            self._capture_ev = _Clock.schedule_interval(_tick, 1.0)
            KivyLogger.info("capture: timer scheduled each 1.0s")
        except Exception as exc:
            try:
                KivyLogger.exception("capture: timer schedule failed: %s" % exc)
            except Exception:
                pass

    # ---- Admin: Images viewer ----
    def admin_open_images_view(self) -> None:
        try:
            app = App.get_running_app()
            app.root.current = "admin_images"
        except Exception:
            pass
        try:
            # Инициализация спиннеров выполняется через on_pre_enter экрана
            self.admin_refresh_image_sessions()
        except Exception:
            pass

    def admin_refresh_image_sessions(self) -> None:
        """Load unique sessions (user_id, session_started_at) with counts."""
        try:
            from sqlalchemy import text as _text
            # Формируем условия фильтрации по датам
            date_conditions = []
            params = {}
            if self._img_filter_date_from is not None:
                date_conditions.append("i.session_started_at >= :date_from")
                params["date_from"] = self._img_filter_date_from
            if self._img_filter_date_to is not None:
                # Добавляем один день к конечной дате, чтобы включить весь день
                from datetime import timedelta
                date_to_end = self._img_filter_date_to + timedelta(days=1)
                date_conditions.append("i.session_started_at < :date_to")
                params["date_to"] = date_to_end
            
            where_clause = "i.session_started_at IS NOT NULL"
            if date_conditions:
                where_clause += " AND " + " AND ".join(date_conditions)
            
            query = f"""
                    SELECT i.user_id, u.login, i.box_id, i.session_started_at,
                           COUNT(*) AS n,
                           MIN(i.created_at) AS first_at,
                           MAX(i.created_at) AS last_at
                    FROM images i
                    LEFT JOIN users u ON u.id = i.user_id
                    WHERE {where_clause}
                    GROUP BY i.user_id, u.login, i.box_id, i.session_started_at
                    ORDER BY i.session_started_at DESC
                    LIMIT 200
                    """
            with self._SessionLocal() as s:
                rows = s.execute(_text(query), params).all()
                self._img_sessions = []
                for r in rows:
                    self._img_sessions.append({
                        "user_id": r[0],
                        "login": r[1] or "",
                        "box_id": r[2],
                        "session_started_at": r[3],
                        "count": int(r[4] or 0),
                        "first_at": r[5],
                        "last_at": r[6],
                    })
        except Exception:
            self._img_sessions = []
        # Populate UI list
        try:
            app = App.get_running_app()
            scr = app.root.get_screen("admin_images")
            lst = scr.ids.get("sessions_list")
            if hasattr(lst, "clear_widgets"):
                lst.clear_widgets()
            for sess in self._img_sessions:
                try:
                    ts = sess["session_started_at"]
                    ts_str = format_local_datetime(ts)
                    login = sess["login"] or f"id={sess['user_id']}"
                    box = sess["box_id"]
                    n = sess["count"]
                    txt = f"{ts_str}  user: {login}  box: {box}  n={n}"
                    item = Factory.OneLineListItem(text=txt)
                    try:
                        item.theme_bg_color = "Custom"
                        item.md_bg_color = (0.26, 0.27, 0.29, 1)
                        item.theme_text_color = "Custom"
                        item.text_color = (0.92, 0.93, 0.95, 1)
                    except Exception:
                        pass
                    def _bind_select(_inst, _sess=sess):
                        try:
                            self.on_admin_select_session(_sess["user_id"], _sess["session_started_at"])
                        except Exception:
                            pass
                    try:
                        item.bind(on_release=_bind_select)
                    except Exception:
                        pass
                    lst.add_widget(item)
                except Exception:
                    pass
        except Exception:
            pass

    def on_admin_select_session(self, user_id: int | None, session_started_at) -> None:
        """Load images for selected session and show the first one."""
        try:
            self._img_current = (user_id, session_started_at)
            from sqlalchemy import text as _text
            with self._SessionLocal() as s:
                rows = s.execute(_text(
                    """
                    SELECT id, mime_type, data, created_at
                    FROM images
                    WHERE user_id IS NOT DISTINCT FROM :uid
                      AND session_started_at = :ss
                    ORDER BY created_at ASC, id ASC
                    """
                ), {"uid": user_id, "ss": session_started_at}).all()
                self._img_images = [(r[0], r[1], r[2], r[3]) for r in rows]
                self._img_index = 0
        except Exception:
            self._img_images = []
            self._img_index = 0
        # Update UI controls
        try:
            app = App.get_running_app()
            scr = app.root.get_screen("admin_images")
            slider = scr.ids.get("img_slider")
            lbl = scr.ids.get("img_counter")
            title = scr.ids.get("session_title")
            total = max(1, len(self._img_images))
            if slider:
                slider.min = 1
                slider.max = total
                slider.value = 1
            if lbl:
                lbl.text = f"1 / {len(self._img_images)}"
            if title:
                try:
                    login = ""
                    for s in self._img_sessions:
                        if s["user_id"] == user_id and s["session_started_at"] == session_started_at:
                            login = s.get("login") or f"id={user_id}"
                            break
                    ts_str = format_local_datetime(session_started_at)
                    title.text = f"Просмотр сессии {ts_str} ({login})"
                except Exception:
                    pass
        except Exception:
            pass
        # Show first image
        try:
            self._admin_show_image_at(0)
        except Exception:
            pass

    def _admin_show_image_at(self, index: int) -> None:
        """Render image by index into the img_view texture."""
        try:
            if not self._img_images:
                return
            index = max(0, min(index, len(self._img_images) - 1))
            self._img_index = index
            _id, mime, data, _created = self._img_images[index]
            if not data:
                return
            ext = "jpg"
            if mime and "png" in str(mime).lower():
                ext = "png"
            tex = CoreImage(BytesIO(data), ext=ext).texture
            app = App.get_running_app()
            scr = app.root.get_screen("admin_images")
            img_w = scr.ids.get("img_view")
            if img_w:
                img_w.texture = tex
            lbl = scr.ids.get("img_counter")
            if lbl:
                lbl.text = f"{index+1} / {len(self._img_images)}"
        except Exception:
            pass

    def on_admin_image_slider(self, value: int) -> None:
        try:
            self._admin_show_image_at(int(value) - 1)
        except Exception:
            pass

    def _admin_images_init_date_spinners(self) -> None:
        """Initialize date spinners with years, months, and days."""
        try:
            from datetime import datetime, timedelta, timezone
            import calendar
            app = App.get_running_app()
            if not app or not hasattr(app, 'root') or app.root is None:
                # Повторная попытка через небольшую задержку
                Clock.schedule_once(lambda dt: self._admin_images_init_date_spinners(), 0.1)
                return
            scr = app.root.get_screen("admin_images")
            if not scr or not hasattr(scr, 'ids'):
                # Повторная попытка через небольшую задержку
                Clock.schedule_once(lambda dt: self._admin_images_init_date_spinners(), 0.1)
                return
            
            # Годы: от текущего года до 5 лет назад
            current_year = datetime.now().year
            years = [str(y) for y in range(current_year, current_year - 6, -1)]
            
            # Месяцы
            months = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12']
            
            # Инициализируем спиннеры для "от"
            year_from = scr.ids.get("filter_date_from_year")
            month_from = scr.ids.get("filter_date_from_month")
            day_from = scr.ids.get("filter_date_from_day")
            
            if year_from:
                year_from.values = ['Год'] + years
            if month_from:
                month_from.values = ['Месяц'] + months
            if day_from:
                day_from.values = ['День']
            
            # Инициализируем спиннеры для "до"
            year_to = scr.ids.get("filter_date_to_year")
            month_to = scr.ids.get("filter_date_to_month")
            day_to = scr.ids.get("filter_date_to_day")
            
            if year_to:
                year_to.values = ['Год'] + years
            if month_to:
                month_to.values = ['Месяц'] + months
            if day_to:
                day_to.values = ['День']
            
            # Устанавливаем фильтр по умолчанию на последнюю неделю только если фильтры еще не установлены
            if self._img_filter_date_from is None and self._img_filter_date_to is None:
                now = now_local()
                week_ago = now - timedelta(days=7)
                
                # Устанавливаем значения по умолчанию (неделю назад)
                if year_from and month_from and day_from:
                    year_from.text = str(week_ago.year)
                    month_from.text = str(week_ago.month)
                    # Обновляем список дней для выбранного месяца
                    days_in_month = calendar.monthrange(week_ago.year, week_ago.month)[1]
                    days = [str(d) for d in range(1, days_in_month + 1)]
                    day_from.values = ['День'] + days
                    day_from.text = str(week_ago.day)
                    # Устанавливаем фильтр
                    self._img_filter_date_from = local_day_start(week_ago.year, week_ago.month, week_ago.day)
                
                # Устанавливаем значения по умолчанию (сегодня)
                if year_to and month_to and day_to:
                    year_to.text = str(now.year)
                    month_to.text = str(now.month)
                    # Обновляем список дней для выбранного месяца
                    days_in_month = calendar.monthrange(now.year, now.month)[1]
                    days = [str(d) for d in range(1, days_in_month + 1)]
                    day_to.values = ['День'] + days
                    day_to.text = str(now.day)
                    # Устанавливаем фильтр
                    self._img_filter_date_to = local_day_start(now.year, now.month, now.day)
                
                # Обновляем список сессий с установленными фильтрами
                Clock.schedule_once(lambda dt: self.admin_refresh_image_sessions(), 0.2)
        except Exception as e:
            try:
                KivyLogger.error(f"_admin_images_init_date_spinners error: {e}")
            except:
                pass

    def admin_images_update_date(self, field: str) -> None:
        """Update date filter when spinner values change."""
        try:
            from datetime import datetime, timezone, time as dt_time
            import calendar
            
            app = App.get_running_app()
            scr = app.root.get_screen("admin_images")
            
            if field == 'from':
                year_sp = scr.ids.get("filter_date_from_year")
                month_sp = scr.ids.get("filter_date_from_month")
                day_sp = scr.ids.get("filter_date_from_day")
            else:
                year_sp = scr.ids.get("filter_date_to_year")
                month_sp = scr.ids.get("filter_date_to_month")
                day_sp = scr.ids.get("filter_date_to_day")
            
            if not year_sp or not month_sp or not day_sp:
                return
            
            year_str = year_sp.text
            month_str = month_sp.text
            day_str = day_sp.text
            
            # Если выбран год, обновляем список месяцев (если месяц еще не выбран)
            if year_str != 'Год':
                months_list = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12']
                if month_sp.text == 'Месяц' or not month_sp.values or len(month_sp.values) <= 1:
                    month_sp.values = ['Месяц'] + months_list
            
            # Если выбран год и месяц, обновляем список дней
            if year_str != 'Год' and month_str != 'Месяц':
                try:
                    year = int(year_str)
                    month = int(month_str)
                    days_in_month = calendar.monthrange(year, month)[1]
                    days = [str(d) for d in range(1, days_in_month + 1)]
                    day_sp.values = ['День'] + days
                except Exception:
                    pass
            elif year_str == 'Год' or month_str == 'Месяц':
                # Если год или месяц не выбраны, сбрасываем список дней
                if day_sp.values != ['День']:
                    day_sp.values = ['День']
                    day_sp.text = 'День'
            
            # Если все три значения выбраны, устанавливаем дату
            if year_str != 'Год' and month_str != 'Месяц' and day_str != 'День':
                try:
                    year = int(year_str)
                    month = int(month_str)
                    day = int(day_str)
                    selected_date = local_day_start(year, month, day)
                    
                    if field == 'from':
                        self._img_filter_date_from = selected_date
                    else:
                        self._img_filter_date_to = selected_date
                    
                    self.admin_refresh_image_sessions()
                except Exception:
                    pass
            else:
                # Если не все значения выбраны, сбрасываем фильтр
                if field == 'from':
                    self._img_filter_date_from = None
                else:
                    self._img_filter_date_to = None
                self.admin_refresh_image_sessions()
        except Exception:
            pass

    def admin_images_clear_date_filter(self) -> None:
        """Clear date filters and refresh sessions list."""
        try:
            self._img_filter_date_from = None
            self._img_filter_date_to = None
            
            app = App.get_running_app()
            scr = app.root.get_screen("admin_images")
            
            # Сбрасываем спиннеры "от"
            year_from = scr.ids.get("filter_date_from_year")
            month_from = scr.ids.get("filter_date_from_month")
            day_from = scr.ids.get("filter_date_from_day")
            if year_from:
                year_from.text = 'Год'
            if month_from:
                month_from.text = 'Месяц'
            if day_from:
                day_from.text = 'День'
                day_from.values = ['День']
            
            # Сбрасываем спиннеры "до"
            year_to = scr.ids.get("filter_date_to_year")
            month_to = scr.ids.get("filter_date_to_month")
            day_to = scr.ids.get("filter_date_to_day")
            if year_to:
                year_to.text = 'Год'
            if month_to:
                month_to.text = 'Месяц'
            if day_to:
                day_to.text = 'День'
                day_to.values = ['День']
            
            # Обновляем список сессий
            self.admin_refresh_image_sessions()
        except Exception:
            pass

    def _init_period_spinners_in_popup(self, year_from_sp, month_from_sp, day_from_sp, 
                                       year_to_sp, month_to_sp, day_to_sp) -> None:
        """Initialize date spinners in popup with years, months, and days."""
        try:
            from datetime import datetime
            current_year = datetime.now().year
            years = [str(y) for y in range(current_year, current_year - 6, -1)]
            months = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12']
            
            if year_from_sp:
                year_from_sp.values = ['Год'] + years
                if year_from_sp.values:
                    year_from_sp.text = 'Год'
            if month_from_sp:
                month_from_sp.values = ['Месяц'] + months
                if month_from_sp.values:
                    month_from_sp.text = 'Месяц'
            if day_from_sp:
                day_from_sp.values = ['День']
                if day_from_sp.values:
                    day_from_sp.text = 'День'
            
            if year_to_sp:
                year_to_sp.values = ['Год'] + years
                if year_to_sp.values:
                    year_to_sp.text = 'Год'
            if month_to_sp:
                month_to_sp.values = ['Месяц'] + months
                if month_to_sp.values:
                    month_to_sp.text = 'Месяц'
            if day_to_sp:
                day_to_sp.values = ['День']
                if day_to_sp.values:
                    day_to_sp.text = 'День'
        except Exception:
            pass

    def _update_day_spinner_in_popup(self, year_sp, month_sp, day_sp) -> None:
        """Update day spinner based on selected year and month in popup."""
        try:
            import calendar
            year_str = year_sp.text if year_sp else 'Год'
            month_str = month_sp.text if month_sp else 'Месяц'
            
            if year_str != 'Год' and month_str != 'Месяц':
                try:
                    year = int(year_str)
                    month = int(month_str)
                    days_in_month = calendar.monthrange(year, month)[1]
                    days = [str(d) for d in range(1, days_in_month + 1)]
                    if day_sp:
                        day_sp.values = ['День'] + days
                except Exception:
                    pass
            else:
                if day_sp:
                    day_sp.values = ['День']
                    day_sp.text = 'День'
        except Exception:
            pass

    def _get_date_from_spinners(self, year_sp, month_sp, day_sp) -> datetime | None:
        """Get datetime from spinner selections."""
        try:
            from datetime import datetime, timezone, time as dt_time
            year_str = year_sp.text if year_sp else 'Год'
            month_str = month_sp.text if month_sp else 'Месяц'
            day_str = day_sp.text if day_sp else 'День'
            
            if year_str != 'Год' and month_str != 'Месяц' and day_str != 'День':
                year = int(year_str)
                month = int(month_str)
                day = int(day_str)
                return local_day_start(year, month, day)
        except Exception:
            pass
        return None

    def admin_images_delete_by_period(self) -> None:
        """Open popup to select period for deleting image records."""
        self._show_delete_period_popup_images()

    def _show_delete_period_popup_images(self) -> None:
        """Show popup with period selection for deleting images."""
        try:
            from kivy.uix.spinner import Spinner
            from datetime import timedelta
            
            layout = BoxLayout(orientation='vertical', spacing=10, padding=10)
            
            # Label
            title_label = Label(text='Выберите период для удаления записей', color=(1,1,1,1), size_hint_y=None, height=30)
            layout.add_widget(title_label)
            
            # Period selection - "От"
            period_from_layout = BoxLayout(orientation='horizontal', size_hint_y=None, height=44, spacing=5)
            period_from_layout.add_widget(Label(text='От:', color=(1,1,1,1), size_hint_x=None, width=40))
            
            year_from_sp = Spinner(text='Год', size_hint_x=0.3, size_hint_y=None, height=44,
                                   background_normal='', background_color=(0.30, 0.31, 0.32, 1), color=(1,1,1,1))
            month_from_sp = Spinner(text='Месяц', size_hint_x=0.3, size_hint_y=None, height=44,
                                    background_normal='', background_color=(0.30, 0.31, 0.32, 1), color=(1,1,1,1))
            day_from_sp = Spinner(text='День', size_hint_x=0.3, size_hint_y=None, height=44,
                                  background_normal='', background_color=(0.30, 0.31, 0.32, 1), color=(1,1,1,1))
            
            def update_from_days(dt):
                self._update_day_spinner_in_popup(year_from_sp, month_from_sp, day_from_sp)
            year_from_sp.bind(text=lambda inst, val: update_from_days(None))
            month_from_sp.bind(text=lambda inst, val: update_from_days(None))
            
            period_from_layout.add_widget(year_from_sp)
            period_from_layout.add_widget(month_from_sp)
            period_from_layout.add_widget(day_from_sp)
            layout.add_widget(period_from_layout)
            
            # Period selection - "До"
            period_to_layout = BoxLayout(orientation='horizontal', size_hint_y=None, height=44, spacing=5)
            period_to_layout.add_widget(Label(text='До:', color=(1,1,1,1), size_hint_x=None, width=40))
            
            year_to_sp = Spinner(text='Год', size_hint_x=0.3, size_hint_y=None, height=44,
                                background_normal='', background_color=(0.30, 0.31, 0.32, 1), color=(1,1,1,1))
            month_to_sp = Spinner(text='Месяц', size_hint_x=0.3, size_hint_y=None, height=44,
                                 background_normal='', background_color=(0.30, 0.31, 0.32, 1), color=(1,1,1,1))
            day_to_sp = Spinner(text='День', size_hint_x=0.3, size_hint_y=None, height=44,
                               background_normal='', background_color=(0.30, 0.31, 0.32, 1), color=(1,1,1,1))
            
            def update_to_days(dt):
                self._update_day_spinner_in_popup(year_to_sp, month_to_sp, day_to_sp)
            year_to_sp.bind(text=lambda inst, val: update_to_days(None))
            month_to_sp.bind(text=lambda inst, val: update_to_days(None))
            
            period_to_layout.add_widget(year_to_sp)
            period_to_layout.add_widget(month_to_sp)
            period_to_layout.add_widget(day_to_sp)
            layout.add_widget(period_to_layout)
            
            # Buttons
            btns = BoxLayout(size_hint_y=None, height=44, spacing=10)
            btn_delete = Button(text='Удалить')
            btn_cancel = Button(text='Отмена')
            btns.add_widget(btn_delete)
            btns.add_widget(btn_cancel)
            layout.add_widget(btns)
            
            popup = Popup(title='Удаление записей за период', content=layout, size_hint=(0.7, 0.5), auto_dismiss=False)
            
            # Initialize spinners
            Clock.schedule_once(lambda dt: self._init_period_spinners_in_popup(
                year_from_sp, month_from_sp, day_from_sp, year_to_sp, month_to_sp, day_to_sp), 0.1)
            
            def on_delete(instance):
                try:
                    date_from = self._get_date_from_spinners(year_from_sp, month_from_sp, day_from_sp)
                    date_to = self._get_date_from_spinners(year_to_sp, month_to_sp, day_to_sp)
                    
                    if date_from is None and date_to is None:
                        self._show_error_popup("Выберите период для удаления")
                        return
                    
                    popup.dismiss()
                    self._perform_images_delete(date_from, date_to)
                except Exception as e:
                    try:
                        self._show_error_popup(f"Ошибка: {str(e)}")
                    except Exception:
                        pass
            
            def on_cancel(instance):
                popup.dismiss()
            
            btn_delete.bind(on_release=on_delete)
            btn_cancel.bind(on_release=on_cancel)
            popup.open()
        except Exception as e:
            try:
                self._show_error_popup(f"Ошибка при открытии окна: {str(e)}")
            except Exception:
                pass

    def _perform_images_delete(self, date_from: datetime | None, date_to: datetime | None) -> None:
        """Perform deletion of image records for the selected period."""
        try:
            from datetime import timedelta
            from sqlalchemy import text as _text
            
            # Формируем сообщение подтверждения
            period_str = ""
            if date_from is not None:
                period_str += f"От: {format_local_date(date_from)}"
            if date_to is not None:
                if period_str:
                    period_str += " "
                period_str += f"До: {format_local_date(date_to)}"
            
            def do_delete():
                try:
                    deleted_count = 0
                    with self._SessionLocal() as session:
                        # Формируем условия фильтрации по датам
                        date_conditions = []
                        params = {}
                        if date_from is not None:
                            date_conditions.append("session_started_at >= :date_from")
                            params["date_from"] = date_from
                        if date_to is not None:
                            date_to_end = date_to + timedelta(days=1)
                            date_conditions.append("session_started_at < :date_to")
                            params["date_to"] = date_to_end
                        
                        where_clause = "session_started_at IS NOT NULL"
                        if date_conditions:
                            where_clause += " AND " + " AND ".join(date_conditions)
                        
                        # Сначала подсчитываем количество записей для удаления
                        count_query = f"SELECT COUNT(*) FROM images WHERE {where_clause}"
                        result = session.execute(_text(count_query), params).scalar()
                        deleted_count = result or 0
                        
                        # Удаляем записи
                        delete_query = f"DELETE FROM images WHERE {where_clause}"
                        session.execute(_text(delete_query), params)
                        session.commit()
                    
                    # Обновляем список сессий
                    self.admin_refresh_image_sessions()
                    
                    # Показываем сообщение об успехе
                    try:
                        self._show_error_popup(f"Удалено записей: {deleted_count}")
                    except Exception:
                        pass
                except Exception as e:
                    try:
                        self._show_error_popup(f"Ошибка при удалении: {str(e)}")
                    except Exception:
                        pass
            
            self._confirm_action("Подтверждение удаления", f"Удалить записи за период: {period_str}?", do_delete)
        except Exception as e:
            try:
                self._show_error_popup(f"Ошибка: {str(e)}")
            except Exception:
                pass

    def _stop_capture_session(self) -> None:
        """Stop periodic capture and mark session_stopped_at for last image if any."""
        try:
            from kivy.clock import Clock as _Clock
            if getattr(self, '_capture_ev', None) is not None:
                try:
                    _Clock.unschedule(self._capture_ev)
                except Exception:
                    pass
                self._capture_ev = None
                try:
                    KivyLogger.info("capture: timer unscheduled")
                except Exception:
                    pass
            # mark session_stopped_at for all open images in this session for this user
            try:
                if self._capture_session_started_at is not None and getattr(self, '_current_user', None):
                    with self._SessionLocal() as session:
                        from sqlalchemy import update
                        session_ts = self._capture_session_started_at
                        try:
                            session.execute(
                                update(Image)
                                .where(Image.user_id == getattr(self._current_user, 'id', None))
                                .where(Image.session_started_at == session_ts)
                                .where(Image.session_stopped_at.is_(None))
                                .values(session_stopped_at=datetime.now(timezone.utc))
                            )
                            session.commit()
                        except Exception as exc:
                            session.rollback()
                            try:
                                KivyLogger.exception("capture.stop: DB update failed: %s" % exc)
                            except Exception:
                                pass
            except Exception:
                pass
            # Close camera
            try:
                if self._camera is not None:
                    self._camera.close()
            except Exception:
                pass
            self._camera = None
            self._capture_session_started_at = None
            self._capture_last_image_id = None
            try:
                KivyLogger.info("capture: session stopped")
            except Exception:
                pass
        except Exception:
            pass

    # ---- Box helpers ----
    def set_current_box_id(self, box_id: int | None) -> None:
        self.current_box_id = box_id
        try:
            # refresh main grid if present
            app = App.get_running_app()
            try:
                screen = app.root.get_screen("main")
                grid = screen.ids.get("keys_grid") if hasattr(screen, "ids") else None
                if grid:
                    self.initialize_keys_grid()
            except Exception:
                pass
            # refresh admin permissions if user selected
            try:
                perm = app.root.get_screen("admin_permissions")
                sp = perm.ids.get("admin_user_spinner") if hasattr(perm, "ids") else None
                if sp and sp.text and sp.text != 'Выберите пользователя':
                    self.on_admin_user_selected(sp.text)
            except Exception:
                pass
            try:
                self._show_issued_page()
            except Exception:
                pass
        except Exception:
            pass

    @staticmethod
    def _format_box_spinner_label(box) -> str:
        name = str(getattr(box, "name", "") or "").strip()
        box_id = getattr(box, "id", None)
        if not name:
            return f"id={box_id}" if box_id is not None else ""
        if box_id is None:
            return name
        return f"{name} (id={box_id})"

    def _load_boxes_ordered(self) -> list:
        with self._SessionLocal() as session:
            return session.execute(select(Box).order_by(Box.id.asc())).scalars().all()

    def _apply_box_spinner_values(
        self,
        sp,
        boxes,
        *,
        placeholder: str = "Выберите бокс",
        select_box_id: int | None = None,
    ) -> None:
        labels = [self._format_box_spinner_label(b) for b in boxes]
        self._box_name_to_id = {label: int(b.id) for label, b in zip(labels, boxes)}
        self._box_spinner_to_name = {label: str(b.name) for label, b in zip(labels, boxes)}
        sp.values = labels
        if select_box_id is not None:
            for label, box_id in self._box_name_to_id.items():
                if box_id == int(select_box_id):
                    sp.text = label
                    return
        try:
            current = (getattr(sp, "text", None) or "").strip()
            if current and current in labels:
                return
            if labels and (not current or current == placeholder):
                sp.text = labels[0]
            elif not labels:
                sp.text = placeholder
        except Exception:
            pass

    def _resolve_box_from_spinner(self, text: str) -> tuple[int | None, str]:
        """Вернуть (box_id, box_name) по тексту спиннера «Имя (id=N)»."""
        text = (text or "").strip()
        if not text or text == "Выберите бокс":
            return None, ""
        cached_name = getattr(self, "_box_spinner_to_name", {}).get(text)
        cached_id = getattr(self, "_box_name_to_id", {}).get(text)
        if cached_name:
            return (int(cached_id) if cached_id is not None else None), cached_name
        import re
        m = re.match(r"^(.*)\s+\(id=(\d+)\)\s*$", text)
        if m:
            return int(m.group(2)), m.group(1).strip()
        return cached_id, text

    def _box_name_from_spinner(self, text: str) -> str:
        return self._resolve_box_from_spinner(text)[1]

    def on_admin_box_changed(self, box_name: str) -> None:
        try:
            box_id, _ = self._resolve_box_from_spinner(box_name)
            self.set_current_box_id(box_id)
        except Exception:
            pass

    def _populate_admin_box_spinner(self) -> None:
        try:
            app = App.get_running_app()
            screen = app.root.get_screen("admin_permissions")
            sp = screen.ids.get("admin_box_spinner") if hasattr(screen, "ids") else None
            if sp is None:
                return
            boxes = self._load_boxes_ordered()
            self._apply_box_spinner_values(sp, boxes, select_box_id=self.current_box_id)
            if self.current_box_id is None and sp.values:
                self.set_current_box_id(self._box_name_to_id.get(sp.text))
        except Exception:
            pass

    def _populate_admin_box_spinner_for_create_key(self) -> None:
        try:
            app = App.get_running_app()
            screen = app.root.get_screen("admin_add_room")
            sp = screen.ids.get("admin_box_spinner_create_key") if hasattr(screen, "ids") else None
            if sp is None:
                return
            boxes = self._load_boxes_ordered()
            self._apply_box_spinner_values(sp, boxes)
        except Exception:
            pass

    # Навигация / действия
    def open_admin_panel(self) -> None:
        # Требуем ввод пароля админа каждый раз
        self._show_admin_password_prompt()

    def _show_admin_password_prompt(self) -> None:
        layout = BoxLayout(orientation='vertical', spacing=10, padding=10)
        input_pw = TextInput(password=True, multiline=False, size_hint_y=None, height=40)
        btns = BoxLayout(size_hint_y=None, height=44, spacing=10)
        btn_ok = Button(text='OK')
        btn_cancel = Button(text='Отмена')
        btns.add_widget(btn_ok)
        btns.add_widget(btn_cancel)
        layout.add_widget(Label(text='Пароль администратора:', color=(1,1,1,1), size_hint_y=None, height=24))
        layout.add_widget(input_pw)
        layout.add_widget(btns)
        popup = Popup(title='Вход в админ-панель', content=layout, size_hint=(0.6, 0.4))

        def on_ok(instance):
            pw = input_pw.text or ''
            try:
                with self._SessionLocal() as session:
                    svc = AuthService(session)
                    # Пользователь admin должен существовать, проверяем пароль
                    user = svc.authenticate('admin', pw)
                    if user is None:
                        popup.title = 'Неверный пароль'
                        return
            except Exception:
                popup.dismiss()
                return
            # Успешный вход администратора: переключаем текущего пользователя на admin
            try:
                self._current_user = user
                try:
                    self._is_authenticated = True
                except Exception:
                    pass
                try:
                    self._last_login = getattr(user, 'login', None)
                except Exception:
                    pass
                app = App.get_running_app()
                # Обновить панель кнопок как после авторизации
                try:
                    main_screen = app.root.get_screen("main")
                    if hasattr(main_screen, "ids"):
                        pre = main_screen.ids.get("pre_auth_bar")
                        post = main_screen.ids.get("post_auth_bar")
                        if pre and post:
                            pre.size_hint_x = None
                            pre.width = 0
                            pre.disabled = True
                            pre.opacity = 0
                            post.size_hint_x = 1
                            post.disabled = False
                            try:
                                post.width = post.minimum_width
                            except Exception:
                                pass
                            post.opacity = 1
                        # Показать/скрыть кнопку админки
                        try:
                            admin_btn = post.ids.get('btn_admin_post') if hasattr(post, 'ids') else None
                            if admin_btn is not None:
                                admin_btn.disabled = False
                                admin_btn.opacity = 1
                                admin_btn.size_hint_x = 1
                        except Exception:
                            pass
                        # Обновить цвета грида как для админа
                        try:
                            grid = main_screen.ids.get("keys_grid")
                            if grid:
                                self._refresh_key_colors(grid)
                        except Exception:
                            pass
                except Exception:
                    pass
            except Exception:
                pass
            popup.dismiss()
            # Переходим в админ-панель
            self._enter_admin()
            # Старт съёмки после успешного входа администратора
            try:
                self._start_capture_session()
            except Exception:
                pass

        def on_cancel(instance):
            popup.dismiss()

        btn_ok.bind(on_release=on_ok)
        btn_cancel.bind(on_release=on_cancel)
        popup.open()

    def _enter_admin(self) -> None:
        app = App.get_running_app()
        try:
            # Сначала меню админки; сами пункты обновят своё состояние при входе
            app.root.current = "admin_menu"
        except Exception:
            pass

    def enter_as_selected_user(self) -> None:
        """Переключиться на выбранного в админке пользователя и скрыть админ-кнопку."""
        app = App.get_running_app()
        try:
            screen = app.root.get_screen("admin_permissions")
            screen = app.root.get_screen("admin_permissions")
            spinner = screen.ids.get("admin_user_spinner") if hasattr(screen, "ids") else None
            login = spinner.text if spinner else None
            if not login:
                return
            with self._SessionLocal() as session:
                user = session.execute(select(User).where(User.login == login)).scalar_one_or_none()
                if user is None:
                    return
                # Показать комментарий пользователя
                try:
                    comment_label = screen.ids.get("admin_user_comment") if hasattr(screen, "ids") else None
                    if comment_label is not None:
                        comment_label.text = f"Комментарий: {user.comment or ''}"
                except Exception:
                    pass
                # Устанавливаем выбранного пользователя как текущего
                self._current_user = user
                try:
                    self._last_login = getattr(user, 'login', None)
                except Exception:
                    pass
                # Стартуем съёмку, если пользователь установлен
                try:
                    self._is_authenticated = True
                    self._start_capture_session()
                except Exception:
                    pass
        except Exception:
            return

        # Вернуться на главный экран и скрыть кнопку админки
        try:
            app.root.current = "main"
            main_screen = app.root.get_screen("main")
            if hasattr(main_screen, "ids"):
                post = main_screen.ids.get("post_auth_bar")
                if post and hasattr(post, 'ids'):
                    admin_btn = post.ids.get('btn_admin_post')
                    if admin_btn is not None:
                        admin_btn.disabled = (self._current_user.login != 'admin')
                        admin_btn.opacity = 1 if self._current_user.login == 'admin' else 0
                        admin_btn.size_hint_x = 1 if self._current_user.login == 'admin' else None
                        if self._current_user.login != 'admin':
                            admin_btn.width = 0
            # Обновить цвета доступа
            grid = main_screen.ids.get("keys_grid") if hasattr(main_screen, "ids") else None
            if grid:
                self._refresh_key_colors(grid)
        except Exception:
            pass

    def on_admin_user_selected(self, login: str) -> None:
        """Строит сетку помещений, как на главном экране, с кликом для выдачи/отзыва прав."""
        app = App.get_running_app()
        try:
            screen = app.root.get_screen("admin_permissions")
            grid = screen.ids.get("admin_keys_grid") if hasattr(screen, "ids") else None
            if grid is None:
                return
            # Очистим и перестроим сетку
            grid.clear_widgets()

            with self._SessionLocal() as session:
                # Ключи текущего бокса (или все, если бокс не выбран)
                q = select(Key).order_by(Key.description.asc(), Key.code.asc())
                if self.current_box_id is not None:
                    q = q.where(Key.box_id == self.current_box_id)
                keys_rows = session.execute(q).scalars().all()

                user = session.execute(select(User).where(User.login == login)).scalar_one_or_none()
                if user is None:
                    allowed_ids = set()
                else:
                    uq = select(UserKey.key_id).where(UserKey.user_id == user.id)
                    if self.current_box_id is not None:
                        uq = uq.where(UserKey.box_id == self.current_box_id)
                    allowed_ids = set(session.execute(uq).scalars().all())

            # Создаём набор PermissionTile с хендлером для прав
            for idx, key_row in enumerate(keys_rows):
                try:
                    from kivy.factory import Factory
                    tile = Factory.PermissionTile()
                    tile.key_code = key_row.code
                    tile.room_name = key_row.description or key_row.code
                    tile.allowed = key_row.id in allowed_ids
                    grid.add_widget(tile)
                except Exception:
                    continue
            # Ensure grid cell sizes are computed immediately and after next frame
            try:
                from kivy.clock import Clock
                container = screen.ids.get("admin_keys_container") if hasattr(screen, "ids") else None
                card = screen.ids.get("admin_perm_card") if hasattr(screen, "ids") else None
                if container is not None:
                    self.on_admin_container_resize(container, grid)
                    # bind once to keep sizes consistent on first render and future resizes
                    if not getattr(container, "_admin_bound", False):
                        try:
                            container.bind(size=lambda inst, val: self.on_admin_container_resize(inst, grid))
                            if card is not None:
                                # react to both width and height changes of the card
                                card.bind(size=lambda inst, val: self.on_admin_container_resize(container, grid))
                        except Exception:
                            pass
                        try:
                            setattr(container, "_admin_bound", True)
                        except Exception:
                            pass
                    # schedule multiple recalculations over next frames to avoid clipped edges when sizes settle
                    Clock.schedule_once(lambda dt: self.on_admin_container_resize(container, grid), 0)
                    Clock.schedule_once(lambda dt: self.on_admin_container_resize(container, grid), 0.05)
                    Clock.schedule_once(lambda dt: self.on_admin_container_resize(container, grid), 0.15)
            except Exception:
                pass
        except Exception:
            pass

    def reset_all_issued_keys(self) -> None:
        """Удаляет все записи о выданных ключах и обновляет цвета в гриде."""
        try:
            with self._SessionLocal() as session:
                rows = session.execute(select(IssuedKey)).scalars().all()
                for row in rows:
                    session.delete(row)
                session.commit()
        except Exception:
            return
        # Обновляем цвета на главном экране
        app = App.get_running_app()
        try:
            screen = app.root.get_screen("main")
            grid = screen.ids.get("keys_grid") if hasattr(screen, "ids") else None
            if grid:
                self._refresh_key_colors(grid)
        except Exception:
            pass

    def _toggle_user_permission(self, login: str, key_id: int) -> None:
        try:
            with self._SessionLocal() as session:
                user = session.execute(select(User).where(User.login == login)).scalar_one_or_none()
                if user is None:
                    return
                exists = session.execute(
                    select(UserKey).where(UserKey.user_id == user.id, UserKey.key_id == key_id)
                ).scalar_one_or_none()
                if exists:
                    session.delete(exists)
                else:
                    session.add(UserKey(user_id=user.id, key_id=key_id))
                session.commit()
        except Exception:
            return

    def on_admin_switch(self, placeholder, is_active: bool) -> None:
        """Обработчик переключателя на плитке в админ-сетке.
        Тогглит право выбранного в спиннере пользователя для соответствующего ключа.
        """
        try:
            app = App.get_running_app()
            screen = app.root.get_screen("admin_permissions")
            spinner = screen.ids.get("admin_user_spinner") if hasattr(screen, "ids") else None
            login = spinner.text if spinner else None
            key_code = getattr(placeholder, "key_code", None)
            if not login or not key_code:
                return
            # Разрешаем/запрещаем право
            with self._SessionLocal() as session:
                user = session.execute(select(User).where(User.login == login)).scalar_one_or_none()
                key = session.execute(select(Key).where(Key.code == key_code)).scalar_one_or_none()
                if not user or not key:
                    return
                q = select(UserKey).where(UserKey.user_id == user.id, UserKey.key_id == key.id)
                if self.current_box_id is not None:
                    q = q.where(UserKey.box_id == self.current_box_id)
                link = session.execute(q).scalar_one_or_none()
                if is_active:
                    if link is None:
                        session.add(UserKey(user_id=user.id, key_id=key.id, box_id=self.current_box_id))
                else:
                    if link is not None:
                        session.delete(link)
                session.commit()
            # Обновим визуализацию текущей плитки: статус и индикаторы
            try:
                placeholder.allowed = is_active
                # перерисовка текста статуса выполняется в самой плитке; здесь ничего больше не нужно
            except Exception:
                pass
        except Exception:
            pass

    def view_issued_keys(self) -> None:
        """Открыть экран с плитками всех ключей, подсвечивая выданные. RFID/права не требуются."""
        app = App.get_running_app()
        try:
            screen = app.root.get_screen("issued")
        except Exception:
            return

        grid = getattr(screen.ids, "get", lambda k: None)("issued_keys_grid") if hasattr(screen, "ids") else None
        if grid is None:
            try:
                app.root.current = "issued"
            except Exception:
                pass
            return

        # Admin app: show box spinner and populate values; user app: hide it and stick to current_box_id
        try:
            issued_box_row = screen.ids.get("issued_box_row") if hasattr(screen, "ids") else None
            issued_box_spinner = screen.ids.get("issued_box_spinner") if hasattr(screen, "ids") else None
            # Heuristic: admin app has screen 'admin_menu'
            is_admin = bool(getattr(app.root, 'has_screen', lambda *_: False)('admin_menu'))
            if issued_box_row is not None:
                if is_admin:
                    issued_box_row.height = dp(48)
                    issued_box_row.opacity = 1
                    # populate spinner
                    try:
                        with self._SessionLocal() as session:
                            boxes = session.execute(select(Box).order_by(Box.name.asc())).scalars().all()
                        names = [b.name for b in boxes]
                        name_to_id = {b.name: b.id for b in boxes}
                        setattr(self, '_issued_name_to_id', name_to_id)
                        if issued_box_spinner is not None:
                            issued_box_spinner.values = names
                            if self.current_box_id is not None:
                                sel = next((n for n,i in name_to_id.items() if i == self.current_box_id), None)
                                issued_box_spinner.text = sel or (names[0] if names else '')
                            elif names and not getattr(issued_box_spinner, 'text', None):
                                issued_box_spinner.text = names[0]
                                self.on_issued_box_changed(names[0])
                    except Exception:
                        pass
                else:
                    issued_box_row.height = 0
                    issued_box_row.opacity = 0
        except Exception:
            pass

        # Построим сетку UserKeyTile только для ВЫДАННЫХ ключей (всем пользователям)
        try:
            with self._SessionLocal() as session:
                q = (
                    session.query(Key, User.login, IssuedKey.issued_at)
                    .join(IssuedKey, IssuedKey.key_id == Key.id)
                    .join(User, User.id == IssuedKey.user_id)
                    .order_by(Key.description.asc(), Key.code.asc())
                )
                # use explicit issued filter for admin spinner override
                selected_box_id = getattr(self, '_issued_selected_box_id', None)
                box_id_for_filter = selected_box_id if selected_box_id is not None else self.current_box_id
                if box_id_for_filter is not None:
                    q = q.filter(Key.box_id == box_id_for_filter)
                rows = q.all()
        except Exception:
            rows = []

        from kivy.factory import Factory
        # Построим полный список плиток и отрисуем постранично (5x3)
        all_tiles = []
        for key_row, user_login, issued_at in rows:
            try:
                tile = Factory.UserKeyTile()
                tile.key_code = key_row.code
                tile.room_name = key_row.description or key_row.code
                try:
                    ts = format_local_datetime(issued_at)
                except Exception:
                    ts = ''
                tile.status_text = f"выдан: {user_login}"
                tile.sub_status_text = ts
                try:
                    tile.md_bg_color = (0.1, 0.4, 1.0, 1.0)
                except Exception:
                    pass
                all_tiles.append(tile)
            except Exception:
                continue
        # Сохраняем мастер-список и применяем фильтр, если он активен
        self._issued_tiles_master = all_tiles
        self._issued_tiles_all = self._apply_filter_to_tiles('issued', all_tiles)
        self._issued_page = 0
        try:
            grid.clear_widgets()
        except Exception:
            pass

        try:
            self._active_filtered = None
        except Exception:
            pass

        # Отобразим первую страницу
        try:
            self._show_issued_page()
        except Exception:
            pass
        try:
            app.root.current = "issued"
        except Exception:
            pass

    def logout(self) -> None:
        """Сброс авторизации и возврат на гостевой экран."""
        # Остановить съёмку и закрыть камеру
        try:
            self._stop_capture_session()
        except Exception:
            pass
        # Сброс флагов взаимодействия с оборудованием
        try:
            self._device_lock_open_sent = False
            self._last_led_pos = None
        except Exception:
            pass
        # При необходимости можно очищать кэшированные сервисы/пользователя
        self._auth_service = None
        self._current_user = None
        try:
            self._is_authenticated = False
        except Exception:
            pass
        # Переключаем панели кнопок в состояние до авторизации (для совместимости с старым main)
        app = App.get_running_app()
        try:
            # Установим слайд-переход при возврате на главный экран
            try:
                sm = getattr(app, 'root', None)
                if sm is not None:
                    from kivy.uix.screenmanager import SlideTransition
                    sm.transition = SlideTransition(direction='right', duration=0.5)
            except Exception:
                pass
            screen = app.root.get_screen("main")
            if hasattr(screen, "ids"):
                pre = screen.ids.get("pre_auth_bar")
                post = screen.ids.get("post_auth_bar")
                # Вернуть заголовок
                try:
                    title = screen.ids.get("main_title")
                    if title is not None:
                        title.text = "Ключница"
                except Exception:
                    pass
                if pre and post:
                    # показать pre, скрыть post
                    try:
                        pre.size_hint_x = 1
                        pre.disabled = False
                        pre.opacity = 1
                    except Exception:
                        pass
                    try:
                        post.size_hint_x = None
                        post.width = 0
                        post.disabled = True
                        post.opacity = 0
                    except Exception:
                        pass
                # Обновить цвета ячеек после выхода
                try:
                    keys_grid = screen.ids.get("keys_grid")
                    if keys_grid:
                        self._refresh_key_colors(keys_grid)
                except Exception:
                    pass
            # Переход на гостевой экран
            try:
                sm = getattr(app, 'root', None)
                if sm is not None:
                    from kivy.uix.screenmanager import SlideTransition
                    sm.transition = SlideTransition(direction='right', duration=0.5)
                    if getattr(sm, 'has_screen', lambda *_: False)('guest'):
                        sm.current = 'guest'
                    else:
                        sm.current = 'main'
            except Exception:
                pass
        except Exception:
            pass

    # === Тестовые системные кнопки (главное меню UserApp) ===
    def _run_detached(self, cmd: list[str], *, label: str) -> None:
        try:
            print(f"[TEST] {label}: {' '.join(cmd)}", flush=True)
            subprocess.Popen(cmd, start_new_session=True)
        except Exception as exc:
            try:
                print(f"[TEST] {label} failed: {exc}", flush=True)
            except Exception:
                pass

    def test_reboot_system(self) -> None:
        """Тест: перезагрузка компьютера (Linux Mint / Windows)."""
        if sys.platform.startswith('win'):
            self._run_detached(['shutdown', '/r', '/t', '0'], label='reboot')
        else:
            self._run_detached(['systemctl', 'reboot'], label='reboot')

    def test_shutdown_system(self) -> None:
        """Тест: выключение компьютера."""
        if sys.platform.startswith('win'):
            self._run_detached(['shutdown', '/s', '/t', '0'], label='shutdown')
        else:
            self._run_detached(['systemctl', 'poweroff'], label='shutdown')

    def test_open_terminal(self) -> None:
        """Тест: открыть терминал в графической сессии."""
        if sys.platform.startswith('win'):
            self._run_detached(['cmd', '/c', 'start', 'cmd'], label='terminal')
            return
        for cmd in (
            ['x-terminal-emulator'],
            ['gnome-terminal'],
            ['xfce4-terminal'],
            ['konsole'],
            ['xterm'],
        ):
            try:
                subprocess.Popen(cmd, start_new_session=True)
                print(f"[TEST] terminal: {' '.join(cmd)}", flush=True)
                return
            except Exception:
                continue
        print("[TEST] terminal: no suitable emulator found", flush=True)

    # === Пользовательские экраны: фильтрованные списки для взятия/сдачи ключей ===
    def open_take_keys(self) -> None:
        """Открывает экран с плитками только тех ключей, которые доступны текущему пользователю для взятия."""
        app = App.get_running_app()
        # Заглушка автологина отключена; переходим сразу, если пользователь авторизован
        # Требуем авторизацию для корректной фильтрации
        if not getattr(self, "_current_user", None) and not getattr(self, "_is_authenticated", False):
            # Попробуем восстановить пользователя по последнему логину (надежно перед переходом)
            try:
                last = getattr(self, "_last_login", None)
                if last:
                    with self._SessionLocal() as session:
                        user = session.execute(select(User).where(User.login == last)).scalar_one_or_none()
                        if user is not None:
                            self._current_user = user
                            try:
                                self._is_authenticated = True
                            except Exception:
                                pass
            except Exception:
                pass
        if not getattr(self, "_current_user", None) and not getattr(self, "_is_authenticated", False):
            # Доп. эвристика: если на главном экране в заголовке указан логин — используем его
            try:
                main_screen = app.root.get_screen("main")
                if hasattr(main_screen, 'ids'):
                    title = main_screen.ids.get('main_title')
                    login_hint = getattr(title, 'text', '') if title is not None else ''
                    if login_hint and login_hint != 'Ключница':
                        with self._SessionLocal() as session:
                            user = session.execute(select(User).where(User.login == login_hint)).scalar_one_or_none()
                            if user is not None:
                                self._current_user = user
                                try:
                                    self._last_login = getattr(user, 'login', None)
                                    self._is_authenticated = True
                                except Exception:
                                    pass
            except Exception:
                pass
        if not getattr(self, "_current_user", None) and not getattr(self, "_is_authenticated", False):
            # Если всё ещё нет пользователя — не уходим на авторизацию из этого handler,
            # а мягко переключаемся на auth только если сейчас не main, чтобы избежать "отскока".
            try:
                sm = getattr(app, 'root', None)
                if sm is not None and getattr(sm, 'current', None) != 'auth_login':
                    sm.current = 'auth_login'
            except Exception:
                pass
            return
        try:
            screen = app.root.get_screen("take")
        except Exception:
            return

        # Гарантированно переходим на экран взятия до сборки грида
        try:
            sm = getattr(app, 'root', None)
            if sm is not None:
                from kivy.uix.screenmanager import SlideTransition
                sm.transition = SlideTransition(direction='left', duration=0.4)
                # Защита: если каким-то образом current будет изменён "сбоку",
                # то мы закрепим нужный экран ещё раз на следующем кадре.
                sm.current = 'take'
                try:
                    from kivy.clock import Clock
                    Clock.schedule_once(lambda dt: setattr(sm, 'current', 'take'), 0)
                except Exception:
                    pass
        except Exception:
            pass

        # При входе в сценарий взятия — сразу открыть общий замок (пока локальная имитация)
        try:
            self.handle_lock_open()
        except Exception:
            pass

        grid = getattr(screen.ids, "get", lambda k: None)("take_keys_grid") if hasattr(screen, "ids") else None
        if grid is None:
            # экрана хватает — уже перешли на 'take'; выходим
            return

        # Сбросим флаг активности (для логирования отмены без действий)
        try:
            self._take_action_performed = False
        except Exception:
            pass

        # Построим сетку из UserKeyTile с фильтром: у пользователя есть допуск и ключ сейчас никому не выдан
        try:
            with self._SessionLocal() as session:
                # Допуски текущего пользователя
                allowed_ids = set()
                if getattr(self, "_current_user", None) and self._current_user.login != "admin":
                    allowed_ids = set(
                        session.execute(select(UserKey.key_id).where(UserKey.user_id == self._current_user.id)).scalars().all()
                    )
                # Выданные ключи исключаем
                issued_ids = set(
                    session.execute(select(IssuedKey.key_id)).scalars().all()
                )
                # Список ключей для показа
                q = select(Key).order_by(Key.description.asc(), Key.code.asc())
                if self.current_box_id is not None:
                    q = q.where(Key.box_id == self.current_box_id)
                keys_rows = session.execute(q).scalars().all()
                keys_rows = [k for k in keys_rows if (k.id in allowed_ids and k.id not in issued_ids)]

        except Exception:
            keys_rows = []

        # Заполняем грид
        from kivy.factory import Factory
        all_tiles = []
        for key_row in keys_rows:
            try:
                tile = Factory.UserKeyTile()
                tile.key_code = key_row.code
                tile.room_name = key_row.description or key_row.code
                # Начальный статус на экране взятия — допуск есть (галочка)
                try:
                    tile.status_text = 'допуск есть'
                    tile.sub_status_text = ''
                except Exception:
                    pass
                all_tiles.append(tile)
            except Exception:
                continue
        self._take_tiles_master = all_tiles
        self._take_tiles_all = self._apply_filter_to_tiles('take', all_tiles)
        self._take_page = 0
        try:
            grid.clear_widgets()
        except Exception:
            pass
        # сохранить ссылку для корректной окраски
        try:
            self._active_take_grid = grid
            self._active_filtered = 'take'
        except Exception:
            pass

        # Сохраняем ссылку на активный фильтрованный грид (для корректной окраски)
        try:
            self._active_take_grid = grid
            self._active_filtered = 'take'
        except Exception:
            pass

        # Показать первую страницу
        try:
            self._show_take_page()
        except Exception:
            pass
        # Повторно зафиксируем нужный экран после сборки, на случай гонок событий
        try:
            from kivy.clock import Clock
            Clock.schedule_once(lambda dt: setattr(app.root, 'current', 'take'), 0)
        except Exception:
            pass
        # Открыть общий замок на первое действие в сессии
        try:
            if not getattr(self, '_device_lock_open_sent', False):
                self._device_lock_open()
                self._device_lock_open_sent = True
        except Exception:
            pass

    def open_return_keys(self) -> None:
        """Открывает экран с плитками только тех ключей, которые сейчас выданы текущему пользователю (для сдачи)."""
        app = App.get_running_app()
        # Требуем авторизацию
        if not getattr(self, "_current_user", None):
            # Попробуем восстановить пользователя по последнему логину
            try:
                if getattr(self, "_last_login", None):
                    with self._SessionLocal() as session:
                        user = session.execute(select(User).where(User.login == self._last_login)).scalar_one_or_none()
                        if user is not None:
                            self._current_user = user
            except Exception:
                pass
        if not getattr(self, "_current_user", None):
            try:
                # Запускаем общий поток авторизации (включая RFID-заглушку)
                self.open_auth()
            except Exception:
                try:
                    app.root.current = "auth_login"
                except Exception:
                    pass
            return
        # Сбросим флаг активности (для логирования отмены без действий)
        try:
            self._return_action_performed = False
            self._on_return_screen = True
            self._await_return_key_id = None
        except Exception:
            pass

        try:
            screen = app.root.get_screen("return")
        except Exception:
            return

        # При входе в сценарий сдачи — сразу открыть общий замок (пока локальная имитация)
        try:
            self.handle_lock_open()
        except Exception:
            pass

        grid = getattr(screen.ids, "get", lambda k: None)("return_keys_grid") if hasattr(screen, "ids") else None
        if grid is None:
            return

        try:
            with self._SessionLocal() as session:
                issued_key_ids = set()
                if getattr(self, "_current_user", None):
                    issued_key_ids = set(
                        session.execute(
                            select(IssuedKey.key_id).where(IssuedKey.user_id == self._current_user.id)
                        ).scalars().all()
                    )
                q = select(Key).order_by(Key.description.asc(), Key.code.asc())
                if self.current_box_id is not None:
                    q = q.where(Key.box_id == self.current_box_id)
                keys_rows = session.execute(q).scalars().all()
                keys_rows = [k for k in keys_rows if k.id in issued_key_ids]
        except Exception:
            keys_rows = []

        from kivy.factory import Factory
        all_tiles = []
        for key_row in keys_rows:
            try:
                tile = Factory.UserKeyTile()
                tile.key_code = key_row.code
                tile.room_name = key_row.description or key_row.code
                # Начальный статус на экране сдачи — ключ выдан
                try:
                    tile.status_text = 'ключ выдан'
                    tile.sub_status_text = ''
                except Exception:
                    pass
                all_tiles.append(tile)
            except Exception:
                continue
        self._return_tiles_master = all_tiles
        self._return_tiles_all = self._apply_filter_to_tiles('return', all_tiles)
        self._return_page = 0
        try:
            grid.clear_widgets()
        except Exception:
            pass
        # сохранить ссылку для корректной окраски
        try:
            self._active_return_grid = grid
            self._active_filtered = 'return'
        except Exception:
            pass

        try:
            self._active_return_grid = grid
            self._active_filtered = 'return'
        except Exception:
            pass

        try:
            self._show_return_page()
        except Exception:
            pass
        try:
            app.root.current = "return"
        except Exception:
            pass
        # Открыть общий замок на первое действие в сессии
        try:
            if not getattr(self, '_device_lock_open_sent', False):
                self._device_lock_open()
                self._device_lock_open_sent = True
        except Exception:
            pass

    def on_take_cancel(self) -> None:
        """Назад с экрана взятия: если действий не было — залогировать отмену без действия."""
        try:
            if getattr(self, '_blocked_due_to_mismatch', False):
                return
        except Exception:
            pass
        app = App.get_running_app()
        # Погасить подсветку выбранной ячейки, если была включена
        try:
            lp = getattr(self, '_last_led_pos', None)
            if isinstance(lp, tuple) and len(lp) == 2 and lp[0] is not None and lp[1] is not None:
                self._device_led_set(lp[0], lp[1], False)
            self._last_led_pos = None
        except Exception:
            pass
        # Остановить опрос слота
        try:
            from kivy.clock import Clock
            if getattr(self, '_slot_poll_ev', None) is not None:
                try:
                    Clock.unschedule(self._slot_poll_ev)
                except Exception:
                    pass
                self._slot_poll_ev = None
                self._slot_poll_target = None
        except Exception:
            pass
        try:
            if not getattr(self, "_take_action_performed", False) and getattr(self, "_current_user", None):
                with self._SessionLocal() as session:
                    try:
                        session.add(ErrorLog(user_id=self._current_user.id, key_id=None, message='take: cancel without action'))
                        session.commit()
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            app.root.current = 'main'
        except Exception:
            pass
        try:
            self._active_filtered = None
            self._active_take_grid = None
        except Exception:
            pass

    def on_return_cancel(self) -> None:
        """Назад с экрана сдачи: если действий не было — залогировать отмену без действия."""
        try:
            if getattr(self, '_blocked_due_to_mismatch', False):
                return
        except Exception:
            pass
        app = App.get_running_app()
        # Погасить подсветку выбранной ячейки, если была включена
        try:
            lp = getattr(self, '_last_led_pos', None)
            if isinstance(lp, tuple) and len(lp) == 2 and lp[0] is not None and lp[1] is not None:
                self._device_led_set(lp[0], lp[1], False)
            self._last_led_pos = None
        except Exception:
            pass
        # Остановить опрос слота
        try:
            from kivy.clock import Clock
            if getattr(self, '_slot_poll_ev', None) is not None:
                try:
                    Clock.unschedule(self._slot_poll_ev)
                except Exception:
                    pass
                self._slot_poll_ev = None
                self._slot_poll_target = None
        except Exception:
            pass
        try:
            self._on_return_screen = False
            self._await_return_key_id = None
            self._await_secret_return_key_id = None
            self._dismiss_return_rfid_popup()
        except Exception:
            pass
        try:
            if not getattr(self, "_return_action_performed", False) and getattr(self, "_current_user", None):
                with self._SessionLocal() as session:
                    try:
                        session.add(ErrorLog(user_id=self._current_user.id, key_id=None, message='return: cancel without action'))
                        session.commit()
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            app.root.current = 'main'
        except Exception:
            pass
        try:
            self._active_filtered = None
            self._active_return_grid = None
        except Exception:
            pass

    def open_auth(self) -> None:
        # Переключаемся на первый экран авторизации (логин)
        app = App.get_running_app()
        if hasattr(app, "root") and app.root:
            try:
                # Переход на экран авторизации: слайд вперёд (влево)
                try:
                    sm = getattr(app, 'root', None)
                    if sm is not None:
                        from kivy.uix.screenmanager import SlideTransition
                        sm.transition = SlideTransition(direction='left', duration=0.5)
                except Exception:
                    pass
                app.root.current = "auth_login"
                # Заполнить список пользователей и очистить поля/ошибку
                try:
                    screen = app.root.get_screen("auth_login")
                    if hasattr(screen, "ids"):
                        # Ошибка
                        if "auth_error_login" in screen.ids:
                            screen.ids["auth_error_login"].text = ""
                        # Сброс PIN-полей и обновление визуальных меток (для PIN-экрана)
                        self._pin_login = ""
                        self._pin_password = ""
                        try:
                            self._update_auth_pin_labels()
                        except Exception:
                            pass
                except Exception:
                    pass
                # Приём RFID выполняется через TCP-сервер; заглушка отключена
            except Exception:
                pass
        print("Open Auth clicked")

    # === Общая авторизация по выпадающему списку + пароль (старый экран) ===
    def on_auth_submit(self, login_label: str, password: str) -> None:
        """Аутентификация по логину/паролю с экрана auth_screen.kv.

        При успешном входе: устанавливаем текущего пользователя, обновляем UI
        и запускаем сессию съёмки с камеры раз в секунду.
        """
        app = App.get_running_app()
        login = (login_label or '').strip()
        if not login or not password:
            try:
                scr = app.root.get_screen("auth")
                if hasattr(scr, "ids") and "auth_error" in scr.ids:
                    scr.ids["auth_error"].text = "Выберите пользователя и введите пароль"
            except Exception:
                pass
            return
        try:
            with self._SessionLocal() as session:
                svc = AuthService(session)
                user = svc.authenticate(login, password)
        except Exception as exc:
            try:
                scr = app.root.get_screen("auth")
                if hasattr(scr, "ids") and "auth_error" in scr.ids:
                    scr.ids["auth_error"].text = f"Ошибка БД: {exc}"
            except Exception:
                pass
            return

        if not user:
            try:
                scr = app.root.get_screen("auth")
                if hasattr(scr, "ids") and "auth_error" in scr.ids:
                    scr.ids["auth_error"].text = "Неверный логин или пароль"
            except Exception:
                pass
            return

        # Успешный вход
        self._current_user = user
        try:
            self._is_authenticated = True
            self._last_login = getattr(user, 'login', None)
        except Exception:
            pass

        try:
            app.root.current = "main"
            screen = app.root.get_screen("main")
            if hasattr(screen, "ids"):
                try:
                    title = screen.ids.get("main_title")
                    if title is not None:
                        title.text = getattr(user, 'login', '') or 'Ключница'
                except Exception:
                    pass
                pre = screen.ids.get("pre_auth_bar")
                post = screen.ids.get("post_auth_bar")
                if pre and post:
                    pre.size_hint_x = None
                    pre.width = 0
                    pre.disabled = True
                    pre.opacity = 0
                    post.size_hint_x = 1
                    post.disabled = False
                    try:
                        post.width = post.minimum_width
                    except Exception:
                        pass
                    post.opacity = 1
        except Exception:
            pass

        # Запустить сессию съёмки
        try:
            self._start_capture_session()
        except Exception:
            pass

    def _rfid_match_variants(self, code: str) -> set[str]:
        raw = (code or "").strip()
        if not raw:
            return set()
        upper = raw.upper()
        compact = upper.replace("-", "").replace(":", "").replace(" ", "")
        return {v for v in (raw, upper, compact) if v}

    def _find_key_by_rfid(self, session, code: str):
        variants = self._rfid_match_variants(code)
        if not variants:
            return None
        q = select(Key).where(Key.rfid.in_(list(variants)))  # type: ignore[attr-defined]
        if getattr(self, "current_box_id", None) is not None:
            q = q.where(Key.box_id == self.current_box_id)
        key_row = session.execute(q).scalars().first()
        if key_row is not None:
            return key_row
        compact_in = {v.replace("-", "").replace(":", "").replace(" ", "").upper() for v in variants}
        keys = session.execute(select(Key).where(Key.rfid.isnot(None))).scalars().all()  # type: ignore[attr-defined]
        for key_row in keys:
            stored = str(getattr(key_row, "rfid", "") or "")
            stored_compact = stored.replace("-", "").replace(":", "").replace(" ", "").upper()
            if stored_compact not in compact_in:
                continue
            if getattr(self, "current_box_id", None) is not None and key_row.box_id != self.current_box_id:
                continue
            return key_row
        return None

    def _try_return_by_rfid_code(self, code: str, *, via_secret: bool = False) -> bool:
        """Сдача ключа по RFID-метке (экран «Сдать ключ» или секретный код)."""
        code = (code or "").strip()
        if not code:
            return False
        secret_await = getattr(self, "_await_secret_return_key_id", None)
        on_return = getattr(self, "_on_return_screen", False)
        if not via_secret and secret_await is None and not on_return:
            return False
        if not via_secret and secret_await is None and not getattr(self, "_current_user", None):
            return False
        try:
            with self._SessionLocal() as session:
                key_row = self._find_key_by_rfid(session, code)
                if key_row is None:
                    try:
                        print(f"[RFID-RETURN] skip: unknown key rfid={code}")
                    except Exception:
                        pass
                    if on_return or secret_await is not None:
                        self._show_error_popup(
                            f"Ключ с RFID не найден в базе:\n{code}\n"
                            "Проверьте keys.rfid в админке."
                        )
                    return False
                key_id = int(key_row.id)
                if secret_await is not None:
                    try:
                        target_id = int(secret_await)
                    except Exception:
                        target_id = None
                    if target_id is not None and key_id != target_id:
                        self._show_error_popup("Отсканирован не тот ключ")
                        return False
                issued = session.execute(
                    select(IssuedKey).where(IssuedKey.key_id == key_id)
                ).scalar_one_or_none()
                if issued is None:
                    try:
                        print(f"[RFID-RETURN] skip: key not issued rfid={code}")
                    except Exception:
                        pass
                    return False
                if not via_secret and secret_await is None:
                    if issued.user_id != self._current_user.id:
                        self._show_error_popup("Ключ выдан другому пользователю")
                        return False
                try:
                    print(
                        f"[RFID-RETURN] return rfid={code} key_id={key_id} "
                        f"user={getattr(self._current_user, 'login', '')}"
                    )
                except Exception:
                    pass
            if self._complete_return_after_shared_rfid(
                key_id,
                via_secret=bool(via_secret or secret_await is not None),
            ):
                self._show_success_popup("Ключ сдан")
                return True
            self._show_error_popup("Не удалось сдать ключ")
            return False
        except Exception as exc:
            try:
                print(f"[RFID-RETURN] error: {exc}")
            except Exception:
                pass
            return False

    def open_auth_rfid_popup(self) -> None:
        """Отключено: ввод RFID вручную заменён приёмом по TCP."""
        return

    def handle_rfid_user(self, code: str) -> None:
        """Обработка RFID-пользователя: авторизация по users.rfid == code с обновлением UI."""
        try:
            try:
                print(f"[RFID] handle_rfid_user code={code}")
            except Exception:
                pass
            code = (code or "").strip()
            if not code:
                return
            # Ожидание привязки к ключу: считыватель может прислать USER: или код без префикса
            if getattr(self, "_rfid_bind_target_key_id", None):
                self.handle_rfid_key(code)
                return
            # Режим админ-привязки: записать RFID новому/выбранному пользователю
            if getattr(self, "_rfid_bind_target_user_id", None):
                try:
                    target_id = int(self._rfid_bind_target_user_id)  # type: ignore[arg-type]
                except Exception:
                    target_id = None
                if target_id is None:
                    return
                from kivy.clock import Clock
                bound_login = ""
                with self._SessionLocal() as session:
                    user_row = session.execute(select(User).where(User.id == target_id)).scalar_one_or_none()
                    if user_row is None:
                        self._rfid_bind_target_user_id = None
                        return
                    bound_login = str(getattr(user_row, "login", "") or "")
                    try:
                        exists = session.execute(
                            select(User).where(User.rfid == code, User.id != user_row.id)
                        ).scalar_one_or_none()
                        if exists is not None:
                            ui_screen = getattr(self, "_rfid_bind_ui_screen", "admin_register_user_rfid")

                            def _show_err(dt):
                                try:
                                    app = App.get_running_app()
                                    scr = app.root.get_screen(ui_screen)
                                    lbl = scr.ids.get("user_rfid_status") if hasattr(scr, "ids") else None
                                    if lbl is not None:
                                        lbl.text = f"RFID {code} уже привязан к другому пользователю"
                                except Exception:
                                    pass
                            Clock.schedule_once(_show_err, 0)
                            return
                    except Exception:
                        pass
                    user_row.rfid = code  # type: ignore[attr-defined]
                    session.add(user_row)
                    session.commit()
                self._rfid_bind_target_user_id = None
                try:
                    print(f"[RFID] user bind ok login={bound_login} rfid={code}")
                except Exception:
                    pass

                ui_screen = getattr(self, "_rfid_bind_ui_screen", "admin_register_user_rfid")

                def _ok(dt):
                    try:
                        app = App.get_running_app()
                        scr = app.root.get_screen(ui_screen)
                        if hasattr(scr, "ids"):
                            lbl = scr.ids.get("user_rfid_status")
                            if lbl is not None:
                                lbl.text = f"RFID {code} привязан к пользователю {bound_login}"
                    except Exception:
                        pass
                    try:
                        self._show_success_popup(f"RFID привязан к пользователю {bound_login}")
                    except Exception:
                        pass
                    try:
                        app = App.get_running_app()
                        if hasattr(app, "root") and app.root:
                            if ui_screen == "admin_reassign_user_rfid":
                                # Остаёмся на экране переназначения: обновим текущую метку
                                self._update_reassign_current_rfid()
                            else:
                                app.root.current = "admin_menu"
                    except Exception:
                        pass

                Clock.schedule_once(_ok, 0)
                return
            # Сдача: оборудование часто шлёт метку ключа как OBJECT:RFID_USER / SET_STATE / VALUE
            if getattr(self, "_await_secret_return_key_id", None) is not None:
                if self._try_return_by_rfid_code(code, via_secret=True):
                    return
            if getattr(self, "_on_return_screen", False) and getattr(self, "_current_user", None):
                if self._try_return_by_rfid_code(code, via_secret=False):
                    return
            with self._SessionLocal() as session:
                # Авторизация строго по users.rfid == code
                user = session.execute(select(User).where(User.rfid == code)).scalar_one_or_none()  # type: ignore[attr-defined]
            if user is None:
                return
            # Успешная авторизация: все UI-операции выполняем на главном потоке
            try:
                from kivy.clock import Clock
                self._current_user = user
                try:
                    self._last_login = getattr(user, 'login', None)
                except Exception:
                    pass
                app = App.get_running_app()

                def _apply_login_ui(dt):
                    try:
                        setattr(app.root, 'current', 'main')
                        try:
                            screen = app.root.get_screen('main')
                            if hasattr(screen, 'ids'):
                                try:
                                    title = screen.ids.get('main_title')
                                    if title is not None:
                                        title.text = getattr(user, 'login', '') or 'Ключница'
                                except Exception:
                                    pass
                                pre = screen.ids.get('pre_auth_bar')
                                post = screen.ids.get('post_auth_bar')
                                try:
                                    admin_btn = post.ids.get('btn_admin_post') if hasattr(post, 'ids') else None
                                    if admin_btn is not None:
                                        admin_btn.disabled = (user.login != 'admin')
                                        admin_btn.opacity = 1 if user.login == 'admin' else 0
                                        admin_btn.size_hint_x = 1 if user.login == 'admin' else None
                                        if user.login != 'admin':
                                            admin_btn.width = 0
                                except Exception:
                                    pass
                                if pre and post:
                                    pre.size_hint_x = None
                                    pre.width = 0
                                    pre.disabled = True
                                    pre.opacity = 0
                                    post.size_hint_x = 1
                                    post.disabled = False
                                    try:
                                        post.width = post.minimum_width
                                    except Exception:
                                        pass
                                    post.opacity = 1
                                    try:
                                        keys_grid = screen.ids.get('keys_grid')
                                        if keys_grid:
                                            self._refresh_key_colors(keys_grid)
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                    except Exception:
                        pass

                Clock.schedule_once(_apply_login_ui, 0)
                # Старт съёмки после успешной RFID-авторизации
                try:
                    self._start_capture_session()
                except Exception:
                    pass
            except Exception:
                pass

        except Exception:
            pass

    def handle_rfid_key(self, code: str) -> None:
        """Обработка KEY: с общего RFID-считывателя (один на устройство).

        Сдача: на экране «Сдать ключ» (_on_return_screen) или после секретного кода
        (_await_secret_return_key_id). С главного меню сдача по KEY: не выполняется.
        Выдача: после авторизации и проверки допуска.
        """
        try:
            try:
                print(f"[RFID] handle_rfid_key code={code}")
            except Exception:
                pass
            code = (code or "").strip()
            if not code:
                return
            # Ожидание привязки к пользователю: считыватель может прислать KEY:
            if getattr(self, "_rfid_bind_target_user_id", None):
                self.handle_rfid_user(code)
                return
            # Режим админ-привязки: если установлен целевой key_id — просто записываем RFID в этот ключ
            if getattr(self, "_rfid_bind_target_key_id", None):
                try:
                    target_id = int(self._rfid_bind_target_key_id)  # type: ignore[arg-type]
                except Exception:
                    target_id = None
                if target_id is None:
                    return
                from kivy.clock import Clock
                with self._SessionLocal() as session:
                    key_row = session.execute(select(Key).where(Key.id == target_id)).scalar_one_or_none()
                    if key_row is None:
                        # сбросить режим ожидания
                        self._rfid_bind_target_key_id = None
                        return
                    # Проверим уникальность: тот же RFID не должен быть на другом ключе
                    try:
                        exists = session.execute(select(Key).where(Key.rfid == code, Key.id != key_row.id)).scalar_one_or_none()
                        if exists is not None:
                            # отобразим ошибку на экране регистрации
                            def _show_err(dt):
                                try:
                                    app = App.get_running_app()
                                    scr = app.root.get_screen("admin_register_rfid")
                                    lbl = scr.ids.get("rfid_status") if hasattr(scr, "ids") else None
                                    if lbl is not None:
                                        lbl.text = f"RFID {code} уже привязан к другому ключу"
                                except Exception:
                                    pass
                            Clock.schedule_once(_show_err, 0)
                            return
                    except Exception:
                        pass
                    # Записываем RFID и сохраняем
                    key_row.rfid = code  # type: ignore[attr-defined]
                    session.add(key_row)
                    session.commit()
                    bound_key_code = str(getattr(key_row, "code", "") or "")
                    # Feedback to simulator: RFID bound to key
                    try:
                        self._send_sim_feedback(
                            f"EVENT:BIND key_code={bound_key_code} key_rfid={code} x={getattr(key_row, 'pos_x', '')} y={getattr(key_row, 'pos_y', '')}"
                        )
                    except Exception:
                        pass
                # Сброс режима и обновление UI
                self._rfid_bind_target_key_id = None
                try:
                    print(f"[RFID] key bind ok key={bound_key_code} rfid={code}")
                except Exception:
                    pass
                try:
                    from kivy.clock import Clock
                    def _ok(dt):
                        try:
                            app = App.get_running_app()
                            scr = app.root.get_screen("admin_register_rfid")
                            if hasattr(scr, "ids"):
                                lbl = scr.ids.get("rfid_status")
                                if lbl is not None:
                                    lbl.text = f"RFID {code} привязан к ключу {bound_key_code}"
                        except Exception:
                            pass
                        try:
                            self._show_success_popup(f"RFID привязан к ключу {bound_key_code}")
                        except Exception:
                            pass
                    Clock.schedule_once(_ok, 0)
                except Exception:
                    pass
                return

            # Сдача по секретному коду: ждём RFID на общем считывателе (авторизация не нужна)
            secret_await = getattr(self, '_await_secret_return_key_id', None)
            if secret_await is not None:
                if self._try_return_by_rfid_code(code, via_secret=True):
                    return
                return

            if not getattr(self, "_current_user", None):
                try:
                    print("[RFID-KEY] skip: user not authenticated")
                except Exception:
                    pass
                return
            with self._SessionLocal() as session:
                key_row = self._find_key_by_rfid(session, code)
                if key_row is None:
                    try:
                        print(f"[RFID-KEY] skip: unknown key rfid={code}")
                    except Exception:
                        pass
                    return
                key_id = int(key_row.id)

                issued = session.execute(select(IssuedKey).where(IssuedKey.key_id == key_id)).scalar_one_or_none()

                # Сдача на экране «Сдать ключ»
                if getattr(self, '_on_return_screen', False):
                    if self._try_return_by_rfid_code(code, via_secret=False):
                        return
                    return

                # Ключ уже у текущего пользователя, но не на экране сдачи — игнорируем KEY:
                if issued and issued.user_id == self._current_user.id:
                    try:
                        print("[RFID-KEY] skip return: choose key on Return screen first")
                    except Exception:
                        pass
                    return

                # Уже выдан другому
                if issued and issued.user_id != self._current_user.id:
                    try:
                        session.add(ErrorLog(user_id=self._current_user.id, key_id=key_row.id, message='взять: уже выдан другому'))
                        session.commit()
                    except Exception:
                        pass
                    return

                # Выдача: проверка допуска и запрета для admin
                if getattr(self._current_user, "login", None) == "admin":
                    try:
                        session.add(ErrorLog(user_id=self._current_user.id, key_id=key_row.id, message='взять: запрещено для admin'))
                        session.commit()
                    except Exception:
                        pass
                    return

                if not self._is_user_allowed_for_key(session, key_id):
                    try:
                        session.add(ErrorLog(user_id=self._current_user.id, key_id=key_id, message='взять: нет допуска'))
                        session.commit()
                    except Exception:
                        pass
                    return

                # Взятие
                try:
                    print(
                        f"[RFID-KEY] issue requested code={code} "
                        f"user={getattr(self._current_user, 'login', '')}"
                    )
                except Exception:
                    pass
                self._begin_key_slot_flow(key_id, 'issue')

        except Exception:
            pass

    def handle_lock_open(self) -> None:
        """Общий замок открыт: зафиксировать состояние, запустить опрос слотов и показать индикатор в UI.

        Пока что реализован черновой обработчик: включает флаг, запускает таймер-таймаут и
        вызывает заглушки _start_slot_polling/_stop_slot_polling. Индикатор отображается,
        если в экране 'main' существует соответствующий элемент с id 'lock_indicator'.
        """
        try:
            try:
                print("[LOCK] handle_lock_open: start window")
            except Exception:
                pass
            # таймаут окна опроса (секунды)
            timeout_sec = float(getattr(self, '_lock_open_timeout_sec', 10))
            setattr(self, '_lock_is_open', True)
            # отменить предыдущий таймер, если есть
            try:
                if getattr(self, '_lock_open_ev', None) is not None:
                    from kivy.clock import Clock
                    Clock.unschedule(self._lock_open_ev)
            except Exception:
                pass
            # запустить опрос (пока заглушка)
            try:
                self._start_slot_polling()
            except Exception:
                pass
            # показать индикатор в UI (если есть)
            try:
                from kivy.clock import Clock
                app = App.get_running_app()
                def _show(dt):
                    try:
                        scr = app.root.get_screen('main') if hasattr(app, 'root') else None
                        if scr and hasattr(scr, 'ids'):
                            ind = scr.ids.get('lock_indicator')
                            if ind is not None:
                                try:
                                    ind.text = 'Замок открыт: идёт опрос'
                                except Exception:
                                    pass
                                try:
                                    ind.opacity = 1
                                except Exception:
                                    pass
                    except Exception:
                        pass
                Clock.schedule_once(_show, 0)
            except Exception:
                pass
            # запланировать закрытие окна опроса по таймауту
            try:
                from kivy.clock import Clock
                def _timeout(dt):
                    try:
                        self._on_lock_window_timeout()
                    except Exception:
                        pass
                self._lock_open_ev = Clock.schedule_once(_timeout, timeout_sec)
            except Exception:
                pass
        except Exception:
            pass

    def _on_lock_window_timeout(self) -> None:
        try:
            try:
                print("[LOCK] window timeout: stopping slot polling")
            except Exception:
                pass
            setattr(self, '_lock_is_open', False)
            # остановить опрос (заглушка)
            try:
                self._stop_slot_polling()
            except Exception:
                pass
            # скрыть индикатор в UI, если есть
            try:
                from kivy.clock import Clock
                app = App.get_running_app()
                def _hide(dt):
                    try:
                        scr = app.root.get_screen('main') if hasattr(app, 'root') else None
                        if scr and hasattr(scr, 'ids'):
                            ind = scr.ids.get('lock_indicator')
                            if ind is not None:
                                try:
                                    ind.opacity = 0
                                except Exception:
                                    pass
                    except Exception:
                        pass
                Clock.schedule_once(_hide, 0)
            except Exception:
                pass
        except Exception:
            pass

    def _start_slot_polling(self) -> None:
        try:
            # Создать (лениво) мок‑опросчик, который читает текущее состояние слотов из БД
            if getattr(self, '_slot_poller', None) is None:
                def _provider():
                    return self._get_current_slot_presence()
                poll_interval = float(getattr(self, '_slot_poll_interval_sec', 0.5))
                self._slot_poller = MockSlotRfidPoller(provider=_provider, interval_sec=poll_interval)
                self._slot_poller.set_on_presence_change(self._on_slot_presence_change)
            self._slot_poller.start()
            setattr(self, '_slot_polling_active', True)
            try:
                print("[LOCK] slot polling started")
            except Exception:
                pass
        except Exception:
            pass

    def _stop_slot_polling(self) -> None:
        try:
            if getattr(self, '_slot_poller', None) is not None:
                try:
                    self._slot_poller.stop()
                except Exception:
                    pass
            setattr(self, '_slot_polling_active', False)
            try:
                print("[LOCK] slot polling stopped")
            except Exception:
                pass
        except Exception:
            pass

    def _get_current_slot_presence(self) -> dict[tuple[int, int], bool]:
        """Мок‑снимок присутствия в слотах для текущего бокса: present = ключ в шкафу.

        Алгоритм: ключ считается присутствующим, если для него нет записи в issued_keys.
        Используются координаты keys.pos_x / keys.pos_y (только если заданы) и фильтр по текущему box.
        """
        snapshot: dict[tuple[int, int], bool] = {}
        try:
            with self._SessionLocal() as session:
                q = select(Key.id, Key.pos_x, Key.pos_y)
                if getattr(self, 'current_box_id', None) is not None:
                    q = q.where(Key.box_id == self.current_box_id)
                rows = session.execute(q).all()
                key_ids = [r[0] for r in rows]
                issued_ids = set()
                if key_ids:
                    issued_ids = set(session.execute(select(IssuedKey.key_id).where(IssuedKey.key_id.in_(key_ids))).scalars().all())
                for key_id, pos_x, pos_y in rows:
                    try:
                        if pos_x is None or pos_y is None:
                            continue
                        present = key_id not in issued_ids
                        snapshot[(int(pos_x), int(pos_y))] = bool(present)
                    except Exception:
                        continue
        except Exception:
            pass
        return snapshot

    def _on_slot_presence_change(self, xy: tuple[int, int], present: bool) -> None:
        """Обработка изменения присутствия ключа в слоте xy=(x,y).

        present=False => ключ изъят (взятие), present=True => ключ положен (сдача).
        """
        try:
            if not getattr(self, '_lock_is_open', False):
                return
            if not getattr(self, '_current_user', None):
                return
            # Найдём ключ по текущему боксу и координатам слота
            with self._SessionLocal() as session:
                q = select(Key).where(Key.pos_x == xy[0], Key.pos_y == xy[1])
                if getattr(self, 'current_box_id', None) is not None:
                    q = q.where(Key.box_id == self.current_box_id)
                key_row = session.execute(q).scalar_one_or_none()
                if key_row is None:
                    return
            # Переиспользуем бизнес‑правила из RFID‑обработчика: см. handle_rfid_key
            if present:
                # Сдача: ключ помещён в слот
                self._perform_key_return_by_id(key_row.id)
            else:
                # Взятие: ключ извлечён из слота
                self._perform_key_issue_by_id(key_row.id)
        except Exception:
            pass

    def _is_user_allowed_for_key(self, session, key_id: int) -> bool:
        """Проверка допуска: сначала по текущему боксу, затем legacy без box_id."""
        try:
            if not getattr(self, '_current_user', None):
                return False
            base_allowed = select(UserKey).where(
                UserKey.user_id == self._current_user.id,
                UserKey.key_id == key_id,
            )
            allowed = None
            if getattr(self, 'current_box_id', None) is not None:
                allowed = session.execute(
                    base_allowed.where(UserKey.box_id == self.current_box_id)
                ).scalars().first()
                if allowed is None:
                    allowed = session.execute(base_allowed).scalars().first()
            else:
                allowed = session.execute(base_allowed).scalars().first()
            return allowed is not None
        except Exception:
            return False

    def _perform_key_return_by_id(self, key_id: int) -> None:
        try:
            try:
                print(f"[RETURN] enter key_id={key_id}")
            except Exception:
                pass
            if not getattr(self, '_current_user', None):
                return
            with self._SessionLocal() as session:
                key_row = session.execute(select(Key).where(Key.id == key_id)).scalars().first()
                if key_row is None:
                    return
                issued = session.execute(select(IssuedKey).where(IssuedKey.key_id == key_row.id)).scalars().first()
                if issued and issued.user_id == self._current_user.id:
                    session.delete(issued)
                    try:
                        session.add(Event(user_id=self._current_user.id, key_id=key_row.id, action='return'))
                    except Exception:
                        pass
                    try:
                        uk = session.execute(select(UserKey).where(UserKey.user_id == self._current_user.id, UserKey.key_id == key_row.id)).scalars().first()
                        if uk is not None:
                            uk.state = 'не выдан'
                            uk.state_user_id = 0
                            uk.state_updated_at = func.now()
                            session.add(uk)
                    except Exception:
                        pass
                    session.commit()
                    try:
                        print(f"[RETURN] success key_id={key_id} user={getattr(self._current_user, 'login', None)}")
                    except Exception:
                        pass
                    # Feedback to simulator: RETURN (slot)
                    try:
                        self._send_sim_feedback(
                            f"EVENT:RETURN key_code={getattr(key_row, 'code', '')} key_rfid={getattr(key_row, 'rfid', '')} x={getattr(key_row, 'pos_x', '')} y={getattr(key_row, 'pos_y', '')} user={getattr(self._current_user, 'login', '')}"
                        )
                    except Exception:
                        pass
                    # Обновить UI
                    try:
                        app = App.get_running_app()
                        from kivy.clock import Clock
                        def _refresh(dt):
                            # Перестроить данные экранов (сдача/взятие), затем обновить раскраску
                            try:
                                self._rebuild_return_data_and_refresh()
                            except Exception:
                                pass
                            try:
                                self._rebuild_take_data_and_refresh()
                            except Exception:
                                pass
                            try:
                                main_screen = app.root.get_screen("main")
                                main_grid = main_screen.ids.get("keys_grid") if hasattr(main_screen, "ids") else None
                                if main_grid:
                                    self._refresh_key_colors(main_grid)
                            except Exception:
                                pass
                            # Также обновим экраны сдачи/взятия, если открыты
                            try:
                                if getattr(app.root, "current", None) == "return":
                                    ret_scr = app.root.get_screen("return")
                                    ret_grid = ret_scr.ids.get("return_keys_grid") if hasattr(ret_scr, "ids") else None
                                    if ret_grid:
                                        self._refresh_key_colors(ret_grid)
                            except Exception:
                                pass
                            try:
                                if getattr(app.root, "current", None) == "take":
                                    tk_scr = app.root.get_screen("take")
                                    tk_grid = tk_scr.ids.get("take_keys_grid") if hasattr(tk_scr, "ids") else None
                                    if tk_grid:
                                        self._refresh_key_colors(tk_grid)
                            except Exception:
                                pass
                        Clock.schedule_once(_refresh, 0)
                    except Exception:
                        pass
        except Exception as e:
            try:
                print(f"[RETURN] error key_id={key_id}: {e}")
            except Exception:
                pass

    def _perform_key_issue_by_id(self, key_id: int) -> None:
        try:
            try:
                print(f"[ISSUE] enter key_id={key_id}")
            except Exception:
                pass
            if not getattr(self, '_current_user', None):
                try:
                    print(f"[ISSUE] skip: no current_user key_id={key_id}")
                except Exception:
                    pass
                return
            with self._SessionLocal() as session:
                key_row = session.execute(select(Key).where(Key.id == key_id)).scalars().first()
                if key_row is None:
                    try:
                        print(f"[ISSUE] skip: key not found key_id={key_id}")
                    except Exception:
                        pass
                    return
                issued = session.execute(select(IssuedKey).where(IssuedKey.key_id == key_row.id)).scalars().first()
                if issued:
                    # Уже выдан: или другому, или этому же — в обоих случаях повторной выдачи нет
                    try:
                        print(f"[ISSUE] skip: already issued key_id={key_id}")
                    except Exception:
                        pass
                    return
                # Запрет для admin
                if getattr(self._current_user, 'login', None) == 'admin':
                    try:
                        print(f"[ISSUE] skip: admin user key_id={key_id}")
                    except Exception:
                        pass
                    return
                if not self._is_user_allowed_for_key(session, key_row.id):
                    try:
                        print(f"[ISSUE] skip: no permission key_id={key_id} user={getattr(self._current_user, 'login', None)}")
                    except Exception:
                        pass
                    return
                # Выдать
                session.add(IssuedKey(user_id=self._current_user.id, key_id=key_row.id))
                try:
                    session.add(Event(user_id=self._current_user.id, key_id=key_row.id, action='issue'))
                except Exception:
                    pass
                try:
                    uk = session.execute(select(UserKey).where(UserKey.user_id == self._current_user.id, UserKey.key_id == key_row.id)).scalars().first()
                    if uk is not None:
                        uk.state = 'выдан'
                        uk.state_user_id = int(getattr(self._current_user, 'id', 0) or 0)
                        uk.state_updated_at = func.now()
                        session.add(uk)
                except Exception:
                    pass
                session.commit()
                try:
                    print(f"[ISSUE] success key_id={key_id} user={getattr(self._current_user, 'login', None)}")
                except Exception:
                    pass
                # Feedback to simulator: ISSUE (slot)
                try:
                    self._send_sim_feedback(
                        f"EVENT:ISSUE key_code={getattr(key_row, 'code', '')} key_rfid={getattr(key_row, 'rfid', '')} x={getattr(key_row, 'pos_x', '')} y={getattr(key_row, 'pos_y', '')} user={getattr(self._current_user, 'login', '')}"
                    )
                except Exception:
                    pass
                # Обновить UI
                try:
                    app = App.get_running_app()
                    from kivy.clock import Clock
                    def _refresh(dt):
                        # Перестроить данные экрана сдачи и обновить страницу
                        try:
                            self._rebuild_return_data_and_refresh()
                        except Exception:
                            pass
                        # Перестроить данные экрана взятия и обновить страницу
                        try:
                            self._rebuild_take_data_and_refresh()
                        except Exception:
                            pass
                        try:
                            main_screen = app.root.get_screen("main")
                            main_grid = main_screen.ids.get("keys_grid") if hasattr(main_screen, "ids") else None
                            if main_grid:
                                self._refresh_key_colors(main_grid)
                        except Exception:
                            pass
                        # Также обновим экраны сдачи/взятия, если открыты
                        try:
                            if getattr(app.root, "current", None) == "return":
                                ret_scr = app.root.get_screen("return")
                                ret_grid = ret_scr.ids.get("return_keys_grid") if hasattr(ret_scr, "ids") else None
                                if ret_grid:
                                    self._refresh_key_colors(ret_grid)
                        except Exception:
                            pass
                        try:
                            if getattr(app.root, "current", None) == "take":
                                tk_scr = app.root.get_screen("take")
                                tk_grid = tk_scr.ids.get("take_keys_grid") if hasattr(tk_scr, "ids") else None
                                if tk_grid:
                                    self._refresh_key_colors(tk_grid)
                        except Exception:
                            pass
                    Clock.schedule_once(_refresh, 0)
                except Exception:
                    pass
        except Exception as e:
            try:
                print(f"[ISSUE] error key_id={key_id}: {e}")
            except Exception:
                pass

    # ---- Simulator feedback sender ----
    def _send_sim_feedback(self, line: str) -> None:
        try:
            host = getattr(self, 'sim_feedback_host', None)
            port = getattr(self, 'sim_feedback_port', None)
            if not host or not port:
                return
            try:
                with socket.create_connection((str(host), int(port)), timeout=1.0) as s:
                    s.sendall((str(line).strip() + "\n").encode("utf-8"))
            except Exception:
                pass
        except Exception:
            pass

    # ---- Equipment device command sender (app -> device over TCP) ----
    def _send_device_kv(self, pairs: dict) -> None:
        try:
            host = getattr(self, 'device_host', None)
            port = getattr(self, 'device_port', None)
            if not host or not port:
                return
            try:
                lines = []
                for k, v in pairs.items():
                    lines.append(f"{str(k)}:{str(v)}")
                payload = ("\r\n".join(lines) + "\r\n").encode("utf-8")
            except Exception:
                return
            try:
                from kivy.app import App
                app = App.get_running_app()
                dc = getattr(app, '_device_client', None) if app else None
                if dc is not None and dc.send_payload(payload):
                    return
            except Exception:
                pass
            # Fallback: отдельное соединение только если DeviceClient не используется
            try:
                import socket
                with socket.create_connection((str(host), int(port)), timeout=1.0) as s:
                    s.sendall(payload)
                    try:
                        preview = payload.decode("utf-8", errors="ignore").strip().replace("\r", " ")
                        print(f"[DEVICE] TX (direct): {preview}")
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception:
            pass

    def _device_lock_open(self) -> None:
        try:
            self._send_device_kv({
                "OBJECT": "KEYLOCK",
                "COMMAND": "SET_STATUS",
                "VALUE": "ON",
            })
        except Exception:
            pass

    def _device_led_set(self, pos_x: int | None, pos_y: int | None, on: bool) -> None:
        try:
            if pos_x is None or pos_y is None:
                return
            row = int(pos_x)
            col = int(pos_y)
            self._send_device_kv({
                "OBJECT": "LED_KEY",
                "COMMAND": "SET_STATE",
                "ROWS": str(row),
                "COLS": str(col),
                "VALUE": "ON" if on else "OFF",
            })
        except Exception:
            pass

    def _device_slot_lock_open(self, pos_x: int | None, pos_y: int | None) -> None:
        """Открыть замок конкретной ячейки (RFID_KEY SET_STATUS OPEN)."""
        try:
            if pos_x is None or pos_y is None:
                try:
                    print("[RFID-KEY] lock open skipped: no slot position")
                except Exception:
                    pass
                return
            row = int(pos_x)
            col = int(pos_y)
            try:
                print(f"[RFID-KEY] TX lock open OBJECT=RFID_KEY COMMAND=SET_STATUS ROWS={row} COLS={col} VALUE=OPEN")
            except Exception:
                pass
            self._send_device_kv({
                "OBJECT": "RFID_KEY",
                "COMMAND": "SET_STATUS",
                "ROWS": str(row),
                "COLS": str(col),
                "VALUE": "OPEN",
            })
        except Exception as exc:
            try:
                print(f"[RFID-KEY] lock open error: {exc}")
            except Exception:
                pass

    def _device_slot_lock_close(self, pos_x: int | None, pos_y: int | None) -> None:
        """Закрыть замок конкретной ячейки (RFID_KEY SET_STATUS CLOSE)."""
        try:
            if pos_x is None or pos_y is None:
                try:
                    print("[RFID-KEY] lock close skipped: no slot position")
                except Exception:
                    pass
                return
            row = int(pos_x)
            col = int(pos_y)
            try:
                print(f"[RFID-KEY] TX lock close OBJECT=RFID_KEY COMMAND=SET_STATUS ROWS={row} COLS={col} VALUE=CLOSE")
            except Exception:
                pass
            self._send_device_kv({
                "OBJECT": "RFID_KEY",
                "COMMAND": "SET_STATUS",
                "ROWS": str(row),
                "COLS": str(col),
                "VALUE": "CLOSE",
            })
        except Exception as exc:
            try:
                print(f"[RFID-KEY] lock close error: {exc}")
            except Exception:
                pass

    # ---- Device slot status handling and polling ----
    def handle_slot_status(self, rows: int, cols: int, value: str) -> None:
        """Вызывается TCP-сервером при получении ответа RFID_KEY GET_STATUS.
        rows/cols — 1-based координаты; value — строковое значение (EMPTY при отсутствии метки,
        либо RFID метки при наличии).
        """
        try:
            try:
                print(f"[SLOT] handle_slot_status rows={rows} cols={cols} value={value} pending={getattr(self, '_pending_action', None)}")
            except Exception:
                pass
            if rows is None or cols is None:
                return
            key = (int(rows), int(cols))
            # Предыдущее значение для детекции переходов
            prev_val = self._device_slot_values.get(key, None)
            # Если есть ожидаемое действие — сперва проверяем УСПЕХ по целевой ячейке
            pend = getattr(self, '_pending_action', None)
            if pend and isinstance(pend, tuple) and len(pend) >= 5:
                prow, pcol, exp_val, act, kid = pend
                # 1) Сначала обработать успех на целевой ячейке, чтобы не показывать mismatch на том же тике
                if int(rows) == int(prow) and int(cols) == int(pcol):
                    ok = False
                    exp = str(exp_val or "")
                    got = str(value or "")
                    if exp.upper() == "EMPTY":
                        ok = (got.upper() == "EMPTY")
                    else:
                        ok = (got == exp)
                    if ok:
                        try:
                            print(f"[SLOT] match OK -> finalize action={act} key_id={kid} value={got}")
                        except Exception:
                            pass
                        # завершаем действие и гасим подсветку
                        try:
                            self._finalize_pending_action(str(act or ''), int(kid) if kid is not None else None)
                        except Exception:
                            pass
                        # Гасим LED
                        try:
                            self._device_led_set(int(prow), int(pcol), False)
                            self._last_led_pos = None
                        except Exception:
                            pass
                        # очистим ожидание
                        try:
                            if getattr(self, '_slot_poll_ev', None) is not None:
                                try:
                                    Clock.unschedule(self._slot_poll_ev)
                                except Exception:
                                    pass
                                self._slot_poll_ev = None
                        except Exception:
                            pass
                        self._slot_poll_target = None
                        self._pending_action = None
                        # Снять блокировку и закрыть mismatch‑экран, если открыт
                        try:
                            self._cancel_mismatch_screen_pending()
                            self._dismiss_mismatch_screen()
                            setattr(self, '_blocked_due_to_mismatch', False)
                        except Exception:
                            pass
                        # Обновить сохранённое значение слота и выйти
                        try:
                            self._device_slot_values[key] = value or ""
                        except Exception:
                            pass
                        return
                # Проверка неверного действия пользователя: блокировка до корректного события
                try:
                    blocked = bool(getattr(self, '_blocked_due_to_mismatch', False))
                except Exception:
                    blocked = False
                try:
                    exp_str = str(exp_val or "")
                    act_str = str(act or "").lower()
                    got_str = str(value or "")
                    prev_str = str(prev_val or "")
                    # Мисматчи:
                    # 1) Выдача (issue): если другая ячейка стала EMPTY
                    if not blocked and act_str == "issue":
                        # Блокируем только при переходе в EMPTY (раньше было не EMPTY), чтобы не ловить статический фон
                        if (int(rows) != int(prow) or int(cols) != int(pcol)) and got_str.upper() == "EMPTY" and prev_str.upper() != "EMPTY":
                            # Заблокировать: пользователь снял не тот ключ
                            self._log_and_block_mismatch(user_key_id=int(kid) if kid is not None else None,
                                                         message=f"взять: неверная ячейка EMPTY rows={rows} cols={cols}, ожидали rows={int(prow)} cols={int(pcol)}")
                    # 2) Сдача (return):
                    elif not blocked and act_str == "return":
                        expected_rfid = exp_str
                        # RFID пришёл в ДРУГОЙ ячейке (переход с EMPTY/другого на RFID) — это ошибка
                        if (int(rows) != int(prow) or int(cols) != int(pcol)) and got_str and got_str.upper() != "EMPTY" and got_str != prev_str:
                            self._log_and_block_mismatch(user_key_id=int(kid) if kid is not None else None,
                                                         message=f"сдать: RFID в другой ячейке rows={rows} cols={cols}, ожидали rows={int(prow)} cols={int(pcol)} (получили {got_str})")
                        # В ожидаемой ячейке пришло не то значение (EMPTY или чужой RFID) — это ошибка
                        if int(rows) == int(prow) and int(cols) == int(pcol):
                            # Неправильный RFID при переходе
                            if expected_rfid and got_str and got_str != expected_rfid and got_str != prev_str:
                                self._log_and_block_mismatch(user_key_id=int(kid) if kid is not None else None,
                                                             message=f"сдать: неверный RFID в ячейке rows={rows} cols={cols}, ожидали={expected_rfid} получили={got_str}")
                            # Переход к EMPTY — ошибка (ожидали RFID)
                            if got_str.upper() == "EMPTY" and prev_str.upper() != "EMPTY":
                                self._log_and_block_mismatch(user_key_id=int(kid) if kid is not None else None,
                                                             message=f"сдать: ячейка пуста rows={rows} cols={cols}, ожидали RFID")
                except Exception:
                    pass
            # Обновить сохранённое значение слота после обработки
            try:
                self._device_slot_values[key] = value or ""
            except Exception:
                pass
        except Exception:
            pass

    def _cancel_rfid_key_close_timer(self, key_id: int | None = None) -> None:
        try:
            evs = getattr(self, '_rfid_key_close_evs', None)
            if not isinstance(evs, dict):
                self._rfid_key_close_evs = {}
                return
            if key_id is not None:
                ev = evs.pop(int(key_id), None)
                if ev is not None:
                    try:
                        Clock.unschedule(ev)
                    except Exception:
                        pass
                return
            for ev in list(evs.values()):
                try:
                    Clock.unschedule(ev)
                except Exception:
                    pass
            evs.clear()
        except Exception:
            pass

    def _schedule_rfid_key_lock_close(
        self,
        pos_x: int,
        pos_y: int,
        action_type: str,
        key_id: int,
        *,
        delay_sec: float = 15.0,
    ) -> None:
        """Через delay_sec после открытия — закрыть ячейку (без опроса)."""
        self._cancel_rfid_key_close_timer(int(key_id))
        act = str(action_type or "")
        try:
            print(
                f"[RFID-KEY] schedule close in {delay_sec}s action={act} "
                f"key_id={key_id} pos=({pos_x},{pos_y})"
            )
        except Exception:
            pass

        def _close(_dt):
            try:
                evs = getattr(self, '_rfid_key_close_evs', None)
                if isinstance(evs, dict):
                    evs.pop(int(key_id), None)
            except Exception:
                pass
            try:
                print(f"[RFID-KEY] close timer fired action={act} key_id={key_id}")
            except Exception:
                pass
            self._device_slot_lock_close(pos_x, pos_y)
            try:
                print(f"[RFID-KEY] done action={act} key_id={key_id}")
            except Exception:
                pass

        if not isinstance(getattr(self, '_rfid_key_close_evs', None), dict):
            self._rfid_key_close_evs = {}
        self._rfid_key_close_evs[int(key_id)] = Clock.schedule_once(_close, float(delay_sec))

    def _prompt_shared_rfid_for_return(self, key_id: int, *, secret: bool = False) -> None:
        """Запросить прикладывание ключа к общему RFID-считывателю перед открытием ячейки."""
        try:
            key_id = int(key_id)
        except Exception:
            return
        if secret:
            self._await_secret_return_key_id = key_id
            self._await_return_key_id = None
        else:
            self._await_return_key_id = key_id
            self._await_secret_return_key_id = None
        self._show_return_rfid_popup(
            'Поднесите ключ к общему RFID-считывателю.\n'
            'После проверки метки откроется нужная ячейка.',
            title='Сдача ключа',
        )

    def _dismiss_return_rfid_popup(self) -> None:
        popup = getattr(self, '_return_rfid_popup', None)
        if popup is None:
            return
        try:
            popup.dismiss()
        except Exception:
            pass
        self._return_rfid_popup = None

    def _show_return_rfid_popup(self, message: str, title: str = 'Сдача ключа') -> None:
        """Popup «поднесите ключ к считывателю» — закрывается при успешной сдаче."""
        self._dismiss_return_rfid_popup()
        try:
            box = BoxLayout(orientation='vertical', spacing=10, padding=10)
            lbl = Label(text=message, color=(1, 1, 1, 1))
            box.add_widget(lbl)
            btn = Button(text='OK', size_hint_y=None, height=44)
            box.add_widget(btn)
            popup = Popup(title=title, content=box, size_hint=(0.55, 0.35), auto_dismiss=False)
            btn.bind(on_release=lambda *_: self._dismiss_return_rfid_popup())
            popup.bind(on_dismiss=lambda *_: setattr(self, '_return_rfid_popup', None))
            self._return_rfid_popup = popup
            popup.open()
        except Exception:
            self._return_rfid_popup = None

    def _complete_return_after_shared_rfid(self, key_id: int, *, via_secret: bool = False) -> bool:
        """После KEY: с общего считывателя — открыть ячейку и зафиксировать сдачу в БД."""
        try:
            key_id = int(key_id)
        except Exception:
            return False
        try:
            with self._SessionLocal() as session:
                key_row = session.execute(select(Key).where(Key.id == key_id)).scalars().first()
                if key_row is None:
                    return False
                pos_x = getattr(key_row, 'pos_x', None)
                pos_y = getattr(key_row, 'pos_y', None)
            if pos_x is None or pos_y is None:
                self._show_error_popup('У ключа не задана ячейка в боксе')
                return False
            try:
                print(
                    f"[RFID-KEY] return open cell key_id={key_id} "
                    f"pos0=({pos_x},{pos_y}) via_secret={via_secret}"
                )
            except Exception:
                pass
            self._device_led_set(int(pos_x), int(pos_y), True)
            self._last_led_pos = (int(pos_x), int(pos_y))
            self._device_slot_lock_open(pos_x, pos_y)
            if via_secret:
                ok = self._perform_secret_return_by_key_id(key_id)
            else:
                self._perform_key_return_by_id(key_id)
                ok = True
            if ok:
                try:
                    setattr(self, '_return_action_performed', True)
                except Exception:
                    pass
                self._dismiss_return_rfid_popup()
                self._schedule_rfid_key_lock_close(int(pos_x), int(pos_y), 'return', key_id)
                self._await_return_key_id = None
                self._await_secret_return_key_id = None
            return bool(ok)
        except Exception as exc:
            try:
                print(f"[RFID-KEY] return flow error: {exc}")
            except Exception:
                pass
            return False

    def _begin_key_slot_flow(self, key_id: int, action_type: str) -> None:
        """Открыть ячейку для выдачи (сдача — только через общий RFID, см. _complete_return_after_shared_rfid)."""
        try:
            act = str(action_type or '').lower()
            with self._SessionLocal() as session:
                key_row = session.execute(select(Key).where(Key.id == key_id)).scalars().first()
                if key_row is None:
                    try:
                        print(f"[KEY-SLOT] flow blocked: key_id={key_id} not found")
                    except Exception:
                        pass
                    return
                pos_x = getattr(key_row, 'pos_x', None)
                pos_y = getattr(key_row, 'pos_y', None)
                key_code = getattr(key_row, 'code', None)
                rfid_code = getattr(key_row, 'rfid', None)
            try:
                print(
                    f"[KEY-SLOT] flow action={act} key_id={key_id} "
                    f"code={key_code} rfid={rfid_code} pos0=({pos_x},{pos_y})"
                )
            except Exception:
                pass
            if pos_x is None or pos_y is None:
                try:
                    print(f"[KEY-SLOT] flow blocked: key_id={key_id} has no slot position")
                except Exception:
                    pass
                return
            if act == 'return':
                try:
                    print(f"[KEY-SLOT] return must use shared RFID reader on Return screen, key_id={key_id}")
                except Exception:
                    pass
                return
            self._device_slot_lock_open(pos_x, pos_y)
            if act == 'issue':
                self._perform_key_issue_by_id(key_id)
                try:
                    setattr(self, '_take_action_performed', True)
                except Exception:
                    pass
            else:
                try:
                    print(f"[KEY-SLOT] unknown action={act} key_id={key_id}")
                except Exception:
                    pass
                return
            self._schedule_rfid_key_lock_close(int(pos_x), int(pos_y), act, key_id)
        except Exception as exc:
            try:
                print(f"[KEY-SLOT] flow error: {exc}")
            except Exception:
                pass

    def _finalize_pending_action(self, action_type: str, key_id: int | None) -> None:
        try:
            if not action_type or key_id is None:
                return
            act = action_type.lower()
            try:
                print(f"[SLOT] finalize action={act} key_id={key_id}")
            except Exception:
                pass
            if act == 'return':
                try:
                    print(f"[SLOT] calling perform RETURN key_id={key_id}")
                except Exception:
                    pass
                try:
                    self._perform_key_return_by_id(int(key_id))
                except Exception as e:
                    try:
                        print(f"[SLOT] perform RETURN raised: {e}")
                    except Exception:
                        pass
            elif act == 'issue':
                try:
                    print(f"[SLOT] calling perform ISSUE key_id={key_id}")
                except Exception:
                    pass
                try:
                    self._perform_key_issue_by_id(int(key_id))
                except Exception as e:
                    try:
                        print(f"[SLOT] perform ISSUE raised: {e}")
                    except Exception:
                        pass
        except Exception:
            pass

    # ---- Mismatch handling (blocking + popup) ----
    def _log_and_block_mismatch(self, user_key_id: int | None, message: str) -> None:
        try:
            # Already blocked — do not spam
            if getattr(self, '_blocked_due_to_mismatch', False):
                # Обновим сообщение и перезапланируем показ
                try:
                    setattr(self, '_mismatch_last_message', str(message))
                    self._schedule_mismatch_screen(str(message))
                except Exception:
                    pass
                return
            try:
                print(f"[MISMATCH] {message} (key_id={user_key_id})")
            except Exception:
                pass
            # Persist error
            try:
                with self._SessionLocal() as session:
                    try:
                        uid = getattr(self._current_user, 'id', None) if getattr(self, '_current_user', None) else None
                    except Exception:
                        uid = None
                    try:
                        from db.models import ErrorLog  # local import to avoid top-level cycles
                        session.add(ErrorLog(user_id=uid, key_id=user_key_id, message=str(message)[:255]))
                        session.commit()
                    except Exception:
                        pass
            except Exception:
                pass
            # Block and show popup
            setattr(self, '_blocked_due_to_mismatch', True)
            setattr(self, '_mismatch_last_message', str(message))
            # Отложенный показ экрана: если быстро придёт корректный сигнал — успеем отменить
            self._schedule_mismatch_screen(str(message))
        except Exception:
            pass

    def _show_mismatch_screen(self, message: str) -> None:
        try:
            app = App.get_running_app()
            sm = getattr(app, 'root', None)
            if sm is None:
                return
            # Save previous screen to return to
            try:
                prev = getattr(sm, 'current', None)
                setattr(self, '_mismatch_prev_screen', prev)
            except Exception:
                pass
            # Set message on screen if present
            try:
                scr = sm.get_screen('mismatch')
                if hasattr(scr, 'ids'):
                    msg = scr.ids.get('mismatch_message')
                    if msg is not None:
                        msg.text = f"Снят/положен ключ в неверную ячейку.\n{message}\n\nПожалуйста, выполните действие в указанной ячейке."
            except Exception:
                pass
            # Switch to mismatch screen
            sm.current = 'mismatch'
            # Start timeout to auto-exit
            from kivy.clock import Clock
            try:
                tout = float(getattr(self, '_mismatch_timeout_sec', 10))
            except Exception:
                tout = 10.0
            # cancel previous
            try:
                if getattr(self, '_mismatch_timeout_ev', None) is not None:
                    Clock.unschedule(self._mismatch_timeout_ev)
            except Exception:
                pass
            self._mismatch_timeout_ev = Clock.schedule_once(lambda dt: self._on_mismatch_timeout(), tout)
        except Exception:
            pass

    def _schedule_mismatch_screen(self, message: str) -> None:
        """Показывает экран mismatch с небольшой задержкой, чтобы избежать мигания,
        если корректный сигнал приходит сразу следом."""
        try:
            from kivy.clock import Clock
            # cancel previous planned show
            try:
                if getattr(self, '_mismatch_show_ev', None) is not None:
                    Clock.unschedule(self._mismatch_show_ev)
            except Exception:
                pass
            delay = float(getattr(self, '_mismatch_show_delay_sec', 0.2))
            self._mismatch_show_ev = Clock.schedule_once(lambda dt: self._show_mismatch_screen(message), delay)
        except Exception:
            pass

    def _cancel_mismatch_screen_pending(self) -> None:
        try:
            from kivy.clock import Clock
            try:
                if getattr(self, '_mismatch_show_ev', None) is not None:
                    Clock.unschedule(self._mismatch_show_ev)
            except Exception:
                pass
            self._mismatch_show_ev = None
        except Exception:
            pass

    def _dismiss_mismatch_screen(self) -> None:
        try:
            # cancel timeout
            from kivy.clock import Clock
            try:
                if getattr(self, '_mismatch_timeout_ev', None) is not None:
                    Clock.unschedule(self._mismatch_timeout_ev)
            except Exception:
                pass
            self._mismatch_timeout_ev = None
            # navigate back to previous screen if exists
            app = App.get_running_app()
            sm = getattr(app, 'root', None)
            prev = getattr(self, '_mismatch_prev_screen', None)
            # Возвращаемся только если сейчас действительно экран mismatch
            if sm is not None and getattr(sm, 'current', None) == 'mismatch' and prev:
                try:
                    # смена экрана на следующем кадре — исключает артефакты UI
                    Clock.schedule_once(lambda dt: setattr(sm, 'current', prev), 0)
                except Exception:
                    pass
            # очистим текст сообщения для будущих показов
            try:
                scr = sm.get_screen('mismatch') if sm is not None else None
                if scr and hasattr(scr, 'ids'):
                    msg = scr.ids.get('mismatch_message')
                    if msg is not None:
                        msg.text = 'Снят/положен ключ в неверную ячейку.\n\nПожалуйста, выполните действие в указанной ячейке.'
            except Exception:
                pass
            self._mismatch_prev_screen = None
        except Exception:
            pass

    def _on_mismatch_timeout(self) -> None:
        try:
            # On timeout: clear pending action and LED, unblock and return to previous or main
            from kivy.clock import Clock
            # stop polling
            try:
                if getattr(self, '_slot_poll_ev', None) is not None:
                    Clock.unschedule(self._slot_poll_ev)
            except Exception:
                pass
            self._slot_poll_ev = None
            self._slot_poll_target = None
            self._pending_action = None
            # turn off LED if any
            try:
                lp = getattr(self, '_last_led_pos', None)
                if isinstance(lp, tuple) and len(lp) == 2 and lp[0] is not None and lp[1] is not None:
                    self._device_led_set(lp[0], lp[1], False)
            except Exception:
                pass
            self._last_led_pos = None
            # navigate back
            self._dismiss_mismatch_screen()
            setattr(self, '_blocked_due_to_mismatch', False)
        except Exception:
            pass

    # === PIN-авторизация (экран AuthScreen из auth_screen_user.kv) ===
    def _update_auth_pin_labels(self) -> None:
        """Обновляет визуальные метки для PIN-логина и пароля на экране авторизации."""
        try:
            app = App.get_running_app()
            # Обновляем оба экрана, если они существуют
            screens = []
            try:
                screens.append(app.root.get_screen("auth_login"))
            except Exception:
                pass
            try:
                screens.append(app.root.get_screen("auth_pin"))
            except Exception:
                pass
            if not screens:
                return
            # Логин: показываем набранные цифры и подчеркивания для оставшихся
            for scr in screens:
                if hasattr(scr, "ids"):
                    login_view = scr.ids.get("login_value")
                    if login_view is not None:
                        filled_login = list(self._pin_login[:4])
                        while len(filled_login) < 4:
                            filled_login.append("_")
                        login_view.text = " ".join(filled_login)
                    password_view = scr.ids.get("password_value")
                    if password_view is not None:
                        dots = ["•" for _ in self._pin_password[:4]]
                        while len(dots) < 4:
                            dots.append("_")
                        password_view.text = " ".join(dots)
        except Exception:
            pass

    def auth_pin_digit(self, digit: str) -> None:
        """Обработка нажатия цифровой кнопки на PIN-клавиатуре.
        Сначала заполняется логин (4 цифры), затем пароль (4 цифры)."""
        if not isinstance(digit, str) or not digit.isdigit() or len(digit) != 1:
            return
        try:
            if len(self._pin_login) < 4:
                self._pin_login += digit
            elif len(self._pin_password) < 4:
                self._pin_password += digit
            self._update_auth_pin_labels()
        except Exception:
            pass

    def auth_pin_backspace(self) -> None:
        """Удаляет последний введённый символ (сначала из пароля, затем из логина)."""
        try:
            if len(self._pin_password) > 0:
                self._pin_password = self._pin_password[:-1]
            elif len(self._pin_login) > 0:
                self._pin_login = self._pin_login[:-1]
            self._update_auth_pin_labels()
        except Exception:
            pass

    def auth_pin_clear(self) -> None:
        """Сбрасывает обе PIN-строки и обновляет визуализацию."""
        try:
            self._pin_login = ""
            self._pin_password = ""
            self._update_auth_pin_labels()
        except Exception:
            pass

    def on_auth_login_next(self) -> None:
        """Переход со ввода логина (4 цифры) на экран ввода PIN."""
        try:
            app = App.get_running_app()
            if len(self._pin_login) != 4:
                try:
                    scr = app.root.get_screen("auth_login")
                    if hasattr(scr, "ids") and "auth_error_login" in scr.ids:
                        scr.ids["auth_error_login"].text = "Введите 4 цифры логина"
                except Exception:
                    pass
                return
            # Очистим возможную ошибку и пароль, перейдём на экран PIN
            try:
                scr = app.root.get_screen("auth_login")
                if hasattr(scr, "ids") and "auth_error_login" in scr.ids:
                    scr.ids["auth_error_login"].text = ""
            except Exception:
                pass
            self._pin_password = ""
            app.root.current = "auth_pin"
            # Очистим ошибку PIN-экрана
            try:
                scrp = app.root.get_screen("auth_pin")
                if hasattr(scrp, "ids") and "auth_error_pin" in scrp.ids:
                    scrp.ids["auth_error_pin"].text = ""
            except Exception:
                pass
            self._update_auth_pin_labels()
        except Exception:
            pass

    def on_auth_pin_back(self) -> None:
        """Возврат на экран логина без очистки введённого логина."""
        try:
            app = App.get_running_app()
            app.root.current = "auth_login"
            # Очистим сообщение об ошибке на экране логина
            try:
                scr = app.root.get_screen("auth_login")
                if hasattr(scr, "ids") and "auth_error_login" in scr.ids:
                    scr.ids["auth_error_login"].text = ""
            except Exception:
                pass
            self._update_auth_pin_labels()
        except Exception:
            pass

    def on_auth_submit_pin(self) -> None:
        """Нажатие кнопки "Войти" на PIN-экране.
        Сначала проверяем пользователя по pin_code (4 цифры), затем сверяем пароль (4 цифры)."""
        try:
            app = App.get_running_app()
            if len(self._pin_login) != 4 or len(self._pin_password) != 4:
                try:
                    scr = app.root.get_screen("auth_pin")
                    if hasattr(scr, "ids") and "auth_error_pin" in scr.ids:
                        scr.ids["auth_error_pin"].text = "Введите 4 цифры PIN и 4 цифры пароля"
                except Exception:
                    pass
                return

            # Аутентификация по PIN
            try:
                if self._auth_service is None:
                    with self._SessionLocal() as session:
                        self._auth_service = AuthService(session)
                        user = self._auth_service.authenticate_by_pin(self._pin_login, self._pin_password)
                else:
                    with self._SessionLocal() as session:
                        svc = AuthService(session)
                        user = svc.authenticate_by_pin(self._pin_login, self._pin_password)
            except Exception as exc:
                # Ошибка БД
                try:
                    scr = app.root.get_screen("auth_pin")
                    if hasattr(scr, "ids") and "auth_error_pin" in scr.ids:
                        scr.ids["auth_error_pin"].text = f"Ошибка БД: {exc}"
                except Exception:
                    pass
                return

            if user:
                # Успешный вход — копируем поведение on_auth_submit
                self._current_user = user
                try:
                    self._is_authenticated = True
                except Exception:
                    pass
                try:
                    self._last_login = getattr(user, 'login', None)
                except Exception:
                    pass
                try:
                    app.root.current = "main"
                    # Переключить панели кнопок в состояние после авторизации
                    try:
                        screen = app.root.get_screen("main")
                        if hasattr(screen, "ids"):
                            # Обновить заголовок
                            try:
                                title = screen.ids.get("main_title")
                                if title is not None:
                                    title.text = getattr(user, 'login', '') or 'Ключница'
                            except Exception:
                                pass
                            pre = screen.ids.get("pre_auth_bar")
                            post = screen.ids.get("post_auth_bar")
                            # Скрыть кнопку админки, если вошёл не admin
                            try:
                                admin_btn = post.ids.get('btn_admin_post') if hasattr(post, 'ids') else None
                                if admin_btn is not None:
                                    admin_btn.disabled = (user.login != 'admin')
                                    admin_btn.opacity = 1 if user.login == 'admin' else 0
                                    admin_btn.size_hint_x = 1 if user.login == 'admin' else None
                                    if user.login != 'admin':
                                        admin_btn.width = 0
                            except Exception:
                                pass
                            if pre and post:
                                pre.size_hint_x = None
                                pre.width = 0
                                pre.disabled = True
                                pre.opacity = 0
                                post.size_hint_x = 1
                                post.disabled = False
                                # Автоматически подстроить ширину
                                try:
                                    post.width = post.minimum_width
                                except Exception:
                                    pass
                                post.opacity = 1
                                # Обновить цвета ячеек после входа
                                try:
                                    keys_grid = screen.ids.get("keys_grid")
                                    if keys_grid:
                                        self._refresh_key_colors(keys_grid)
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    # Очистить ошибки на auth-экранах
                    try:
                        for scr_name, err_id in (("auth_login", "auth_error_login"), ("auth_pin", "auth_error_pin")):
                            try:
                                scr = app.root.get_screen(scr_name)
                                if hasattr(scr, "ids") and err_id in scr.ids:
                                    scr.ids[err_id].text = ""
                            except Exception:
                                pass
                    except Exception:
                        pass
                except Exception:
                    pass
            else:
                # Ошибка логина/пароля
                try:
                    scr = app.root.get_screen("auth_pin")
                    if hasattr(scr, "ids") and "auth_error_pin" in scr.ids:
                        scr.ids["auth_error_pin"].text = "Неверный PIN или пароль"
                except Exception:
                    pass
            # При успешном входе (user): старт сессии съёмки
            try:
                if getattr(self, "_is_authenticated", False) and getattr(self, "_current_user", None):
                    self._start_capture_session()
            except Exception:
                pass
        except Exception:
            pass

    def _simulate_rfid_stub(self) -> None:
        """Отключено: заглушка RFID заменена приёмом по TCP."""
        return

    # Навигация по пунктам меню админки
    def admin_open_add_user(self) -> None:
        app = App.get_running_app()
        try:
            app.root.current = "admin_add_user"
        except Exception:
            pass

    def admin_open_add_room(self) -> None:
        app = App.get_running_app()
        try:
            app.root.current = "admin_add_room"
            try:
                self._refresh_admin_add_room_form()
            except Exception:
                pass
        except Exception:
            pass

    def _refresh_admin_add_room_form(self) -> None:
        """Обновить списки помещений и боксов на экране создания ключа."""
        self._populate_admin_rooms_spinner()
        self._populate_admin_box_spinner_for_create_key()

    def admin_open_add_box(self) -> None:
        app = App.get_running_app()
        try:
            app.root.current = "admin_add_box"
        except Exception:
            pass

    def admin_open_register_rfid(self) -> None:
        app = App.get_running_app()
        try:
            app.root.current = "admin_register_rfid"
            # заполнить список ключей
            try:
                screen = app.root.get_screen("admin_register_rfid")
                sp = screen.ids.get("key_spinner") if hasattr(screen, "ids") else None
                if sp is not None:
                    with self._SessionLocal() as session:
                        rows = session.execute(select(Key).order_by(Key.code.asc())).scalars().all()
                    sp.values = [k.code for k in rows]
                    if sp.values and not getattr(sp, 'text', None):
                        sp.text = sp.values[0]
            except Exception:
                pass
            # сбросить ожидание RFID
            self._rfid_bind_target_key_id = None
            self._rfid_bind_target_user_id = None
        except Exception:
            pass

    def admin_begin_user_rfid_bind(self, user_id: int, login: str) -> None:
        """После создания пользователя — экран ожидания RFID метки."""
        try:
            self._rfid_bind_target_user_id = int(user_id)
            self._rfid_bind_target_key_id = None
            self._rfid_bind_ui_screen = "admin_register_user_rfid"
            app = App.get_running_app()
            app.root.current = "admin_register_user_rfid"
            login_label = (login or "").strip() or f"id={user_id}"

            def _upd(_dt):
                try:
                    scr = app.root.get_screen("admin_register_user_rfid")
                    if hasattr(scr, "ids"):
                        prompt = scr.ids.get("user_rfid_prompt")
                        if prompt is not None:
                            prompt.text = (
                                f'Пользователь "{login_label}" создан.\n'
                                "Приложите RFID-метку к считывателю."
                            )
                        status = scr.ids.get("user_rfid_status")
                        if status is not None:
                            status.text = "Ожидание RFID..."
                except Exception:
                    pass

            from kivy.clock import Clock
            Clock.schedule_once(_upd, 0)
        except Exception:
            pass

    def admin_cancel_user_rfid_bind(self) -> None:
        try:
            self._rfid_bind_target_user_id = None
            app = App.get_running_app()
            if hasattr(app, "root") and app.root:
                app.root.current = "admin_menu"
        except Exception:
            pass

    # --- Переназначение RFID существующему пользователю -------------------
    def admin_open_reassign_user_rfid(self) -> None:
        """Открыть экран переназначения RFID и заполнить список пользователей."""
        try:
            self._rfid_bind_target_user_id = None
            self._rfid_bind_target_key_id = None
            self._rfid_bind_ui_screen = "admin_reassign_user_rfid"
            self._populate_reassign_user_spinner()
            app = App.get_running_app()
            if hasattr(app, "root") and app.root:
                app.root.current = "admin_reassign_user_rfid"
        except Exception:
            pass

    def _populate_reassign_user_spinner(self) -> None:
        try:
            app = App.get_running_app()
            screen = app.root.get_screen("admin_reassign_user_rfid")
            sp = screen.ids.get("reassign_user_spinner") if hasattr(screen, "ids") else None
            if sp is None:
                return
            with self._SessionLocal() as session:
                users = session.execute(select(User).order_by(User.login.asc())).scalars().all()
                logins = [u.login for u in users if getattr(u, "login", None) != "admin"]
            sp.values = logins
            if logins:
                if not getattr(sp, "text", "") or sp.text not in logins:
                    sp.text = logins[0]
            else:
                sp.text = "Выберите пользователя"
            # сбросить статус и показать текущую метку
            try:
                lbl = screen.ids.get("user_rfid_status")
                if lbl is not None:
                    lbl.text = ""
            except Exception:
                pass
            self._update_reassign_current_rfid()
        except Exception:
            pass

    def _update_reassign_current_rfid(self) -> None:
        """Показать текущую RFID-метку выбранного пользователя."""
        try:
            app = App.get_running_app()
            screen = app.root.get_screen("admin_reassign_user_rfid")
            sp = screen.ids.get("reassign_user_spinner") if hasattr(screen, "ids") else None
            cur = screen.ids.get("reassign_current_rfid") if hasattr(screen, "ids") else None
            if sp is None or cur is None:
                return
            login = (getattr(sp, "text", "") or "").strip()
            rfid_txt = "—"
            if login and login != "Выберите пользователя":
                with self._SessionLocal() as session:
                    u = session.execute(select(User).where(User.login == login)).scalar_one_or_none()
                    if u is not None:
                        rfid_txt = getattr(u, "rfid", None) or "не привязана"
            cur.text = f"Текущая метка: {rfid_txt}"
        except Exception:
            pass

    def admin_reassign_user_rfid_on_select(self, login: str) -> None:
        """Смена пользователя в списке: обновить метку и снять ожидание RFID."""
        try:
            self._rfid_bind_target_user_id = None
            self._update_reassign_current_rfid()
            app = App.get_running_app()
            screen = app.root.get_screen("admin_reassign_user_rfid")
            lbl = screen.ids.get("user_rfid_status") if hasattr(screen, "ids") else None
            if lbl is not None:
                lbl.text = ""
        except Exception:
            pass

    def admin_reassign_user_rfid_arm(self) -> None:
        """Включить ожидание RFID для выбранного пользователя."""
        try:
            app = App.get_running_app()
            screen = app.root.get_screen("admin_reassign_user_rfid")
            sp = screen.ids.get("reassign_user_spinner") if hasattr(screen, "ids") else None
            login = (getattr(sp, "text", "") or "").strip() if sp is not None else ""
            if not login or login == "Выберите пользователя":
                self._set_reassign_status("Сначала выберите пользователя")
                return
            with self._SessionLocal() as session:
                u = session.execute(select(User).where(User.login == login)).scalar_one_or_none()
                if u is None:
                    self._set_reassign_status("Пользователь не найден")
                    return
                self._rfid_bind_target_user_id = int(u.id)
            self._rfid_bind_target_key_id = None
            self._rfid_bind_ui_screen = "admin_reassign_user_rfid"
            self._set_reassign_status(f'Ожидание RFID для "{login}"... приложите метку к считывателю')
        except Exception:
            pass

    def _set_reassign_status(self, text: str) -> None:
        try:
            app = App.get_running_app()
            screen = app.root.get_screen("admin_reassign_user_rfid")
            lbl = screen.ids.get("user_rfid_status") if hasattr(screen, "ids") else None
            if lbl is not None:
                lbl.text = text
        except Exception:
            pass

    def admin_register_rfid_arm(self, selected_key_code: str) -> None:
        """Включает режим ожидания следующего RFID для привязки к выбранному ключу."""
        try:
            code = (selected_key_code or '').strip()
            if not code:
                return
            with self._SessionLocal() as session:
                key_row = session.execute(select(Key).where(Key.code == code)).scalar_one_or_none()
                if key_row is None:
                    return
                self._rfid_bind_target_key_id = key_row.id
                self._rfid_bind_target_user_id = None
            # Обновить статус на экране
            try:
                from kivy.clock import Clock
                def _upd(dt):
                    try:
                        app = App.get_running_app()
                        scr = app.root.get_screen("admin_register_rfid")
                        lbl = scr.ids.get("rfid_status") if hasattr(scr, "ids") else None
                        if lbl is not None:
                            lbl.text = f"Ожидание RFID для ключа {code}..."
                    except Exception:
                        pass
                Clock.schedule_once(_upd, 0)
            except Exception:
                pass
        except Exception:
            pass

    def _populate_admin_rooms_spinner(self) -> None:
        try:
            app = App.get_running_app()
            screen = app.root.get_screen("admin_add_room")
            sp = screen.ids.get("admin_room_spinner") if hasattr(screen, "ids") else None
            if sp is None:
                return
            with self._SessionLocal() as session:
                rooms = session.execute(select(Room).order_by(Room.name.asc())).scalars().all()
            room_names = [r.name for r in rooms]
            sp.values = room_names
            try:
                if room_names and (not getattr(sp, 'text', None) or sp.text == 'Выберите помещение'):
                    sp.text = room_names[0]
                elif not room_names:
                    sp.text = 'Выберите помещение'
            except Exception:
                pass
        except Exception:
            pass

    def admin_open_permissions(self) -> None:
        app = App.get_running_app()
        try:
            # Обновим список пользователей при входе
            screen = app.root.get_screen("admin_permissions")
            with self._SessionLocal() as session:
                users = session.execute(select(User)).scalars().all()
                user_logins = [u.login for u in users if getattr(u, 'login', None) != 'admin']
            spinner = screen.ids.get("admin_user_spinner") if hasattr(screen, "ids") else None
            if spinner:
                spinner.values = user_logins
                if user_logins:
                    spinner.text = user_logins[0]
                    self.on_admin_user_selected(user_logins[0])
                else:
                    spinner.text = 'Выберите пользователя'
            # Обновим спиннер боксов при входе и синхронизируем текущий выбор
            try:
                self._populate_admin_box_spinner()
            except Exception:
                pass
            app.root.current = "admin_permissions"
        except Exception:
            pass

    def admin_open_export(self) -> None:
        app = App.get_running_app()
        try:
            # Обновим список пользователей (с пунктом 'Все')
            screen = app.root.get_screen("admin_export")
            with self._SessionLocal() as session:
                users = session.execute(select(User)).scalars().all()
                user_logins = ["Все"] + [u.login for u in users]
            # Выпадающий список пользователя удалён; оставляем фильтрацию текстом в строке фильтров
            # Установим период по умолчанию и очистим пер-колоночные фильтры
            # Инициализация спиннеров выполняется через on_pre_enter экрана
            try:
                for fid in ("export_filter_ts", "export_filter_user", "export_filter_key", "export_filter_action"):
                    try:
                        w = screen.ids.get(fid)
                        if w is not None:
                            w.text = ""
                    except Exception:
                        pass
            except Exception:
                pass
            # Построим/обновим таблицу сразу с выбранным периодом
            try:
                # Инициализируем фильтры по колонкам
                self._export_col_filters = {'ts': '', 'user': '', 'key': '', 'action': '', 'box': '', 'code': '', 'room': '', 'when': ''}
            except Exception:
                pass
            app.root.current = "admin_export"
            # Построить таблицу после переключения экрана (гарантия наличия ids)
            try:
                from kivy.clock import Clock
                def _init_build(dt):
                    try:
                        scr = app.root.get_screen("admin_export")
                        sp = scr.ids.get("export_period_spinner") if hasattr(scr, "ids") else None
                        if sp is not None:
                            sp.text = 'Неделя'
                        self._build_admin_export_table()
                    except Exception:
                        pass
                # Несколько попыток на ближайшие кадры, чтобы поймать момент, когда ids готовы
                #Clock.schedule_once(_init_build, 0)
                #Clock.schedule_once(_init_build, 0.05)
                Clock.schedule_once(_init_build, 0.15)
            except Exception:
                pass
        except Exception:
            pass

    def admin_open_secret_codes(self) -> None:
        app = App.get_running_app()
        try:
            screen = app.root.get_screen("admin_secret_codes")
            # заполнить список ключей
            with self._SessionLocal() as session:
                q = select(Key)
                if self.current_box_id is not None:
                    q = q.where(Key.box_id == self.current_box_id)
                keys = session.execute(q).scalars().all()
                titles = [f"{(k.description or k.code)} ({k.code})" for k in keys]
                # map for later lookup
                self._secret_codes_title_to_code = { (f"{(k.description or k.code)} ({k.code})"): k.code for k in keys }
                self._secret_codes_code_to_secret = { k.code: (k.secret_code or '') for k in keys }
            sp = screen.ids.get("sc_key_spinner") if hasattr(screen, "ids") else None
            if sp:
                sp.values = titles
                if not sp.text or sp.text == 'Выберите ключ':
                    sp.text = titles[0] if titles else 'Выберите ключ'
            self.admin_secret_code_key_selected(sp.text if sp else '')
            app.root.current = "admin_secret_codes"
        except Exception:
            pass

    def admin_secret_code_key_selected(self, key_title: str) -> None:
        """Подставить текущий секретный код выбранного ключа в поле ввода."""
        key_title = (key_title or '').strip()
        if not key_title or key_title == 'Выберите ключ':
            return
        try:
            app = App.get_running_app()
            screen = app.root.get_screen("admin_secret_codes")
            ti = screen.ids.get("sc_code_input") if hasattr(screen, "ids") else None
            if ti is None:
                return
            code = None
            if hasattr(self, '_secret_codes_title_to_code'):
                code = self._secret_codes_title_to_code.get(key_title)
            if code is None and '(' in key_title and key_title.endswith(')'):
                code = key_title[key_title.rfind('(') + 1:-1]
            secret = ''
            if code and hasattr(self, '_secret_codes_code_to_secret'):
                secret = self._secret_codes_code_to_secret.get(code, '')
            if not secret and code:
                with self._SessionLocal() as session:
                    k = session.execute(select(Key).where(Key.code == code)).scalar_one_or_none()
                    if k is not None:
                        secret = k.secret_code or ''
            ti.text = secret
        except Exception:
            pass

    def _secret_code_is_taken(self, session, secret_code: str, *, exclude_key_id: int | None = None) -> bool:
        secret_code = (secret_code or '').strip()
        if not secret_code:
            return False
        q = select(Key.id).where(Key.secret_code == secret_code)
        if exclude_key_id is not None:
            q = q.where(Key.id != exclude_key_id)
        return session.execute(q).scalar_one_or_none() is not None

    def admin_open_assign_keys_to_box(self) -> None:
        app = App.get_running_app()
        try:
            screen = app.root.get_screen("admin_assign_box")
            self._populate_assign_box_spinner()
            self._assign_page = 0
            self.admin_assign_refresh_keys_grid()
            app.root.current = "admin_assign_box"
        except Exception:
            pass

    def _populate_assign_box_spinner(self) -> None:
        try:
            app = App.get_running_app()
            screen = app.root.get_screen("admin_assign_box")
            sp = screen.ids.get("assign_box_spinner") if hasattr(screen, "ids") else None
            if sp is None:
                return
            boxes = self._load_boxes_ordered()
            self._apply_box_spinner_values(sp, boxes)
        except Exception:
            pass

    def _populate_assign_key_spinner(self) -> None:
        try:
            app = App.get_running_app()
            screen = app.root.get_screen("admin_assign_box")
            sp = screen.ids.get("assign_key_spinner") if hasattr(screen, "ids") else None
            if sp is None:
                return
            with self._SessionLocal() as session:
                keys = session.execute(select(Key).order_by(Key.description.asc())).scalars().all()
            titles = [k.description or k.code for k in keys]
            self._assign_key_title_to_code = { (k.description or k.code): k.code for k in keys }
            sp.values = titles
            if titles and (not getattr(sp, 'text', None) or sp.text == 'Выберите ключ'):
                sp.text = titles[0]
        except Exception:
            pass

    def admin_assign_key_to_box(self, key_title: str, box_name: str) -> None:
        key_title = (key_title or '').strip()
        _, box_name = self._resolve_box_from_spinner(box_name)
        box_name = (box_name or '').strip()
        if not key_title or key_title == 'Выберите ключ' or not box_name or box_name == 'Выберите бокс':
            return
        try:
            with self._SessionLocal() as session:
                code = key_title
                try:
                    if hasattr(self, '_assign_key_title_to_code') and key_title in self._assign_key_title_to_code:
                        code = self._assign_key_title_to_code.get(key_title, key_title)
                except Exception:
                    pass
                # fallback: parse code from "Title (CODE)"
                if code and '(' in code and code.endswith(')'):
                    try:
                        parsed = code[code.rfind('(')+1:-1]
                        if parsed:
                            code = parsed
                    except Exception:
                        pass
                key = session.execute(select(Key).where(Key.code == code)).scalar_one_or_none()
                if key is None:
                    return
                box = session.execute(select(Box).where(Box.name == box_name)).scalar_one_or_none()
                if box is None:
                    return
                # Determine grid dimensions (cols=x, rows=y) with sane defaults
                cols = int(box.x) if getattr(box, 'x', None) else DEFAULT_GRID_COLS
                rows = int(box.y) if getattr(box, 'y', None) else DEFAULT_GRID_ROWS
                # Compute occupied positions for this box
                used = set()
                for px, py in session.execute(select(Key.pos_x, Key.pos_y).where(Key.box_id == box.id)).all():
                    try:
                        if px is not None and py is not None:
                            used.add((int(px), int(py)))
                    except Exception:
                        continue
                # Find first free slot (row-major: y then x)
                target_xy = None
                for yy in range(1, int(rows) + 1):
                    for xx in range(1, int(cols) + 1):
                        if (xx, yy) not in used:
                            target_xy = (xx, yy)
                            break
                    if target_xy:
                        break
                if target_xy is None:
                    # No free slots -> show error and bail
                    try:
                        self._show_error_popup('В боксе нет свободных мест для назначения')
                    except Exception:
                        pass
                    return
                # Assign box and coordinates
                key.box_id = box.id
                key.pos_x = int(target_xy[0])
                key.pos_y = int(target_xy[1])
                session.commit()
        except Exception:
            return
        # refresh grids/spinners
        try:
            self.initialize_keys_grid()
            self.admin_assign_refresh_keys_grid()
        except Exception:
            pass

    def admin_toggle_key_box(self, key_code: str, box_name: str) -> None:
        key_code = (key_code or '').strip()
        _, box_name = self._resolve_box_from_spinner(box_name)
        box_name = (box_name or '').strip()
        if not key_code or not box_name or box_name == 'Выберите бокс':
            return
        try:
            with self._SessionLocal() as session:
                key = session.execute(select(Key).where(Key.code == key_code)).scalar_one_or_none()
                if key is None:
                    return
                box = session.execute(select(Box).where(Box.name == box_name)).scalar_one_or_none()
                if box is None:
                    return
                if key.box_id == box.id:
                    # Unassign: clear box link and coordinates
                    key.box_id = None
                    try:
                        key.pos_x = None
                        key.pos_y = None
                    except Exception:
                        pass
                else:
                    # Assign path: prompt for coordinates first, then assign
                    try:
                        self.admin_prompt_assign_key_position(key.code, box.name)
                    except Exception:
                        pass
                    return
                session.commit()
        except Exception:
            return
        try:
            self.admin_assign_refresh_keys_grid()
        except Exception:
            pass

    def admin_assign_on_box_selected(self, box_name: str) -> None:
        try:
            self.admin_assign_refresh_keys_grid()
        except Exception:
            pass

    def admin_assign_refresh_keys_grid(self) -> None:
        try:
            app = App.get_running_app()
            screen = app.root.get_screen("admin_assign_box")
            grid = screen.ids.get("assign_keys_grid") if hasattr(screen, "ids") else None
            box_sp = screen.ids.get("assign_box_spinner") if hasattr(screen, "ids") else None
            page_lbl = screen.ids.get("assign_page_label") if hasattr(screen, "ids") else None
            cap_lbl = screen.ids.get("assign_capacity_label") if hasattr(screen, "ids") else None
            if grid is None or box_sp is None:
                return
            # Clear existing tiles
            try:
                grid.clear_widgets()
            except Exception:
                pass
            selected_box_id, selected_box_name = self._resolve_box_from_spinner(box_sp.text or '')
            with self._SessionLocal() as session:
                # Load keys; determine which are assigned to selected box and get positions
                keys = session.execute(select(Key).order_by(Key.description.asc(), Key.code.asc())).scalars().all()
                pos_map = {k.id: (k.pos_x, k.pos_y, k.box_id) for k in keys}
                assigned_ids = set()
                box = None
                if selected_box_name:
                    if selected_box_id is not None:
                        box = session.execute(select(Box).where(Box.id == selected_box_id)).scalar_one_or_none()
                    if box is None:
                        box = session.execute(select(Box).where(Box.name == selected_box_name)).scalar_one_or_none()
                    if box is not None:
                        assigned_ids = {k.id for k in session.execute(select(Key).where(Key.box_id == box.id)).scalars().all()}
                        try:
                            cols = int(box.x) if getattr(box, 'x', None) else DEFAULT_GRID_COLS
                            rows = int(box.y) if getattr(box, 'y', None) else DEFAULT_GRID_ROWS
                            capacity = int(cols) * int(rows)
                            used = set()
                            for k in keys:
                                try:
                                    if k.box_id == box.id and k.pos_x is not None and k.pos_y is not None:
                                        used.add((int(k.pos_x), int(k.pos_y)))
                                except Exception:
                                    continue
                            occupied = len(used)
                            free = max(0, capacity - occupied)
                            if cap_lbl is not None:
                                cap_lbl.text = (
                                    f"Бокс id={box.id}    Мест: {capacity}    "
                                    f"Занято: {occupied}    Свободно: {free}"
                                )
                        except Exception:
                            if cap_lbl is not None:
                                cap_lbl.text = ''
                else:
                    if cap_lbl is not None:
                        cap_lbl.text = ''

            # Build tiles using PermissionTile styling
            from views.widgets.permission_tile import PermissionTile

            def make_tile(title: str, code: str, is_assigned: bool, xy: tuple[int|None, int|None] | None):
                tile = PermissionTile()
                try:
                    tile.allow_toggle = False
                except Exception:
                    pass
                tile.room_name = title
                tile.key_code = code
                tile.allowed = is_assigned
                # Use assign/unassign wording for this screen
                try:
                    if is_assigned and xy and xy[0] and xy[1]:
                        tile.label_assigned = f'назначен — x={int(xy[0])}, y={int(xy[1])}'
                    else:
                        tile.label_assigned = 'назначен'
                    tile.label_unassigned = 'не назначен'
                except Exception:
                    pass
                try:
                    tile.ripple_behavior = True
                except Exception:
                    pass

                def on_press_tile(instance):
                    try:
                        app = App.get_running_app()
                        screen = app.root.get_screen("admin_assign_box")
                        sp = screen.ids.get("assign_box_spinner") if hasattr(screen, "ids") else None
                        if sp and getattr(sp, 'text', None):
                            if is_assigned:
                                self.admin_toggle_key_box(code, sp.text)
                            else:
                                self.admin_prompt_assign_key_position(code, sp.text)
                    except Exception:
                        pass

                tile.bind(on_release=on_press_tile)
                return tile


            total = len(keys)
            per_page = max(1, int(self._assign_page_size))
            max_page = (total - 1) // per_page if total else 0
            if self._assign_page > max_page:
                self._assign_page = max_page
            start = self._assign_page * per_page
            end = min(total, start + per_page)
            for k in keys[start:end]:
                title = (k.description or k.code)
                xy = None
                try:
                    px, py, bid = pos_map.get(k.id, (None, None, None))
                    if k.id in assigned_ids and px is not None and py is not None:
                        xy = (int(px), int(py))
                except Exception:
                    xy = None
                grid.add_widget(make_tile(title, k.code, k.id in assigned_ids, xy))

            if page_lbl is not None:
                try:
                    page_lbl.text = f"Стр. {self._assign_page + 1} / {max_page + 1 if total else 1}"
                except Exception:
                    pass
        except Exception:
            pass

    def admin_assign_prev_page(self) -> None:
        try:
            if self._assign_page > 0:
                self._assign_page -= 1
                self.admin_assign_refresh_keys_grid()
        except Exception:
            pass

    def admin_assign_next_page(self) -> None:
        try:
            # recompute total to clamp
            app = App.get_running_app()
            screen = app.root.get_screen("admin_assign_box")
            box_sp = screen.ids.get("assign_box_spinner") if hasattr(screen, "ids") else None
            selected_box = (box_sp.text or '').strip() if box_sp else ''
            with self._SessionLocal() as session:
                keys = session.execute(select(Key).order_by(Key.description.asc(), Key.code.asc())).scalars().all()
            total = len(keys)
            per_page = max(1, int(self._assign_page_size))
            max_page = (total - 1) // per_page if total else 0
            if self._assign_page < max_page:
                self._assign_page += 1
                self.admin_assign_refresh_keys_grid()
        except Exception:
            pass

    # ---- Assign with manual coordinates dialog ----
    def admin_prompt_assign_key_position(self, key_code: str, box_name: str) -> None:
        app = App.get_running_app()
        try:
            box_id, box_name = self._resolve_box_from_spinner(box_name)
            # Resolve box and compute dims and next free suggestion
            with self._SessionLocal() as session:
                box = None
                if box_id is not None:
                    box = session.execute(select(Box).where(Box.id == box_id)).scalar_one_or_none()
                if box is None and box_name:
                    box = session.execute(select(Box).where(Box.name == box_name)).scalar_one_or_none()
                if box is None:
                    return
                box_name = str(getattr(box, "name", "") or box_name)
                cols = int(box.x) if getattr(box, 'x', None) else DEFAULT_GRID_COLS
                rows = int(box.y) if getattr(box, 'y', None) else DEFAULT_GRID_ROWS
                used = set()
                for px, py in session.execute(select(Key.pos_x, Key.pos_y).where(Key.box_id == box.id)).all():
                    try:
                        if px is not None and py is not None:
                            used.add((int(px), int(py)))
                    except Exception:
                        continue
            # Suggest first free
            sug_x, sug_y = 1, 1
            found = False
            for yy in range(1, int(rows) + 1):
                for xx in range(1, int(cols) + 1):
                    if (xx, yy) not in used:
                        sug_x, sug_y = xx, yy
                        found = True
                        break
                if found:
                    break
            from kivy.uix.modalview import ModalView
            from kivy.uix.boxlayout import BoxLayout
            from kivy.uix.label import Label
            from kivy.uix.textinput import TextInput
            from kivy.uix.button import Button
            mv = ModalView(size_hint=(None, None), size=(480, 260), auto_dismiss=False)
            root = BoxLayout(orientation='vertical', spacing=12, padding=16)
            root.add_widget(Label(text=f'Назначить ключ {key_code} в бокс {box_name} (id={box.id})', size_hint_y=None, height=28))
            # Inputs row
            row = BoxLayout(orientation='horizontal', spacing=12, size_hint_y=None, height=44)
            row.add_widget(Label(text='X:', size_hint_x=None, width=40))
            inp_x = TextInput(text=str(sug_x), input_filter='int')
            row.add_widget(inp_x)
            row.add_widget(Label(text='Y:', size_hint_x=None, width=40))
            inp_y = TextInput(text=str(sug_y), input_filter='int')
            row.add_widget(inp_y)
            root.add_widget(row)
            # Hint
            root.add_widget(Label(text=f'Размер сетки: {cols}×{rows}. Свободно выберите точку.', size_hint_y=None, height=24))
            # Buttons
            btns = BoxLayout(orientation='horizontal', spacing=12, size_hint_y=None, height=44)
            def _on_ok(instance):
                try:
                    xv = int(inp_x.text or '0')
                    yv = int(inp_y.text or '0')
                except Exception:
                    self._show_error_popup('Введите корректные числа X и Y')
                    return
                if xv < 1 or yv < 1 or xv > int(cols) or yv > int(rows):
                    self._show_error_popup(f'Координаты вне диапазона 1..{cols}, 1..{rows}')
                    return
                # Check occupancy again and assign
                ok = self.admin_assign_key_to_box_at(key_code, box_name, xv, yv)
                if ok:
                    try:
                        mv.dismiss()
                    except Exception:
                        pass
            ok_btn = Button(text='ОК', size_hint_x=None, width=120)
            ok_btn.bind(on_release=_on_ok)
            cancel_btn = Button(text='Отмена', size_hint_x=None, width=120)
            cancel_btn.bind(on_release=lambda *a: mv.dismiss())
            btns.add_widget(cancel_btn)
            btns.add_widget(ok_btn)
            root.add_widget(btns)
            mv.add_widget(root)
            mv.open()
        except Exception:
            pass

    def admin_assign_key_to_box_at(self, key_code: str, box_name: str, x: int, y: int) -> bool:
        try:
            box_id, box_name = self._resolve_box_from_spinner(box_name)
            with self._SessionLocal() as session:
                key = session.execute(select(Key).where(Key.code == (key_code or '').strip())).scalar_one_or_none()
                if key is None:
                    return False
                box = None
                if box_id is not None:
                    box = session.execute(select(Box).where(Box.id == box_id)).scalar_one_or_none()
                if box is None and box_name:
                    box = session.execute(select(Box).where(Box.name == box_name)).scalar_one_or_none()
                if box is None:
                    return False
                cols = int(box.x) if getattr(box, 'x', None) else DEFAULT_GRID_COLS
                rows = int(box.y) if getattr(box, 'y', None) else DEFAULT_GRID_ROWS
                if x < 1 or y < 1 or x > int(cols) or y > int(rows):
                    self._show_error_popup(f'Координаты вне диапазона 1..{cols}, 1..{rows}')
                    return False
                # Occupied?
                occupied = session.execute(
                    select(Key.id).where(Key.box_id == box.id, Key.pos_x == int(x), Key.pos_y == int(y))
                ).scalar_one_or_none()
                if occupied is not None:
                    self._show_error_popup('Эта ячейка уже занята')
                    return False
                key.box_id = box.id
                key.pos_x = int(x)
                key.pos_y = int(y)
                session.commit()
        except Exception:
            return False
        # Refresh UI
        try:
            self.initialize_keys_grid()
        except Exception:
            pass
        try:
            self.admin_assign_refresh_keys_grid()
        except Exception:
            pass
        return True

    def admin_save_secret_code(self, key_title: str, secret_code: str) -> None:
        key_title = (key_title or '').strip()
        secret_code = (secret_code or '').strip()
        if not key_title or key_title == 'Выберите ключ':
            return
        try:
            with self._SessionLocal() as session:
                # распарсить code из заголовка или взять из карты
                code = None
                try:
                    if hasattr(self, '_secret_codes_title_to_code') and key_title in self._secret_codes_title_to_code:
                        code = self._secret_codes_title_to_code.get(key_title)
                except Exception:
                    pass
                if code is None and '(' in key_title and key_title.endswith(')'):
                    try:
                        code = key_title[key_title.rfind('(')+1:-1]
                    except Exception:
                        code = None
                if not code:
                    return
                q = select(Key).where(Key.code == code)
                k = session.execute(q).scalar_one_or_none()
                if k is None:
                    return
                if secret_code and self._secret_code_is_taken(session, secret_code, exclude_key_id=k.id):
                    self._show_error_popup('Секретный код уже используется другим ключом')
                    return
                k.secret_code = secret_code or None
                session.commit()
                if hasattr(self, '_secret_codes_code_to_secret'):
                    self._secret_codes_code_to_secret[code] = secret_code
        except Exception:
            self._show_error_popup('Не удалось сохранить секретный код')
            return
        self._show_success_popup('Секретный код сохранён')

    def _show_save_dialog(self, suggested_filename: str, on_path_selected) -> None:
        chooser = FileChooserListView(path=".")
        layout = BoxLayout(orientation='vertical', spacing=10, padding=10)
        layout.add_widget(chooser)
        name_input = TextInput(text=suggested_filename, multiline=False, size_hint_y=None, height=40)
        layout.add_widget(name_input)
        btns = BoxLayout(size_hint_y=None, height=44, spacing=10)
        btn_ok = Button(text='Сохранить')
        btn_cancel = Button(text='Отмена')
        btns.add_widget(btn_ok)
        btns.add_widget(btn_cancel)
        layout.add_widget(btns)
        popup = Popup(title='Сохранить файл', content=layout, size_hint=(0.8, 0.8))

        def on_ok(instance):
            try:
                directory = chooser.path or "."
                filename = (name_input.text or suggested_filename).strip()
                if not filename:
                    filename = suggested_filename
                if not filename.lower().endswith('.txt'):
                    filename += '.txt'
                full_path = directory + ("/" if not directory.endswith(("/","\\")) else "") + filename
                on_path_selected(full_path)
            finally:
                popup.dismiss()

        def on_cancel(instance):
            popup.dismiss()

        btn_ok.bind(on_release=on_ok)
        btn_cancel.bind(on_release=on_cancel)
        popup.open()

    def admin_export_events(self, user_label: str | None) -> None:
        # Определяем период из дат фильтрации
        from datetime import datetime, timedelta, timezone
        since = None
        until = None
        
        if self._export_filter_date_from is not None:
            since = self._export_filter_date_from
        if self._export_filter_date_to is not None:
            # Добавляем один день к конечной дате, чтобы включить весь день
            until = self._export_filter_date_to + timedelta(days=1)

        # Подготовим данные
        lines = []
        try:
            with self._SessionLocal() as session:
                q = (
                    session.query(Event, User.login, Key.description, Key.code)
                    .join(User, User.id == Event.user_id)
                    .join(Key, Key.id == Event.key_id)
                )
                if since is not None:
                    q = q.filter(Event.event_at >= since)
                if until is not None:
                    q = q.filter(Event.event_at < until)
                if user_label and user_label != 'Все':
                    q = q.filter(User.login == user_label)
                q = q.order_by(Event.event_at.asc())
                rows = q.all()

                # Чтобы получить события сдачи для полноты, включим также текущее состояние выдач, если они попали в период
                # (но у нас события уже логируются и для возврата)

                for ev, login, key_desc, key_code in rows:
                    ts = format_local_datetime(ev.event_at)
                    key_title = key_desc or key_code or ''
                    action_ru = 'выдача' if ev.action == 'issue' else 'сдача'
                    lines.append(f"{ts} | user={login} | key={key_title} | action={action_ru}")
        except Exception as exc:
            lines = [f"Ошибка выборки: {exc}"]

        if not lines:
            lines = ["Записей не найдено."]

        # Диалог сохранения
        def do_save(path: str):
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write("Отчёт по операциям с ключами\n")
                    period_str = ""
                    if since is not None:
                        period_str += f"От: {format_local_date(since)} "
                    if self._export_filter_date_to is not None:
                        period_str += f"До: {format_local_date(self._export_filter_date_to)}"
                    if not period_str:
                        period_str = "Все время"
                    f.write(f"Период: {period_str}\n")
                    f.write(f"Пользователь: {user_label or 'Все'}\n")
                    f.write("\n")
                    for line in lines:
                        f.write(line + "\n")
            except Exception:
                pass

        # Формируем имя файла на основе периода
        period_file = "period"
        if since is not None or self._export_filter_date_to is not None:
            period_file = "custom"
        suggested = f"key-events-{period_file}-{(user_label or 'all').lower()}.txt"
        self._show_save_dialog(suggested, do_save)

    # === Admin export table ===
    def _build_admin_export_table(self) -> None:
        """Создаёт/обновляет таблицу выдач с возможностью фильтрации по тексту."""
        from kivy.metrics import dp
        app = App.get_running_app()
        try:
            screen = app.root.get_screen("admin_export")
            container = screen.ids.get("export_table_container") if hasattr(screen, "ids") else None
            # Пер-колоночные фильтры
            f_ts = screen.ids.get("export_filter_ts") if hasattr(screen, "ids") else None
            f_user = screen.ids.get("export_filter_user") if hasattr(screen, "ids") else None
            f_key = screen.ids.get("export_filter_key") if hasattr(screen, "ids") else None
            f_action = screen.ids.get("export_filter_action") if hasattr(screen, "ids") else None
            f_box = screen.ids.get("export_filter_box") if hasattr(screen, "ids") else None
            f_code = screen.ids.get("export_filter_code") if hasattr(screen, "ids") else None
            f_room = screen.ids.get("export_filter_room") if hasattr(screen, "ids") else None
            f_when = screen.ids.get("export_filter_when") if hasattr(screen, "ids") else None
            user_sp = screen.ids.get("export_user_spinner") if hasattr(screen, "ids") else None
            if container is None:
                return
            try:
                container.clear_widgets()
            except Exception:
                pass

            # Загружаем данные согласно фильтрам по датам
            from datetime import datetime, timedelta, timezone
            since = None
            until = None
            
            if self._export_filter_date_from is not None:
                since = self._export_filter_date_from
            if self._export_filter_date_to is not None:
                # Добавляем один день к конечной дате, чтобы включить весь день
                until = self._export_filter_date_to + timedelta(days=1)
            query_user = None  # выпадающий список убран

            # Соберём 8 колонок: дата/время, пользователь, ключ, действие, box, key_code, room, issued_at (пример)
            rows_full = []
            try:
                with self._SessionLocal() as session:
                    q = (
                        session.query(Event, User.login, Key.description, Key.code, Key.box_id, Key.room_id)
                        .join(User, User.id == Event.user_id)
                        .join(Key, Key.id == Event.key_id)
                    )
                    if since is not None:
                        q = q.filter(Event.event_at >= since)
                    if until is not None:
                        q = q.filter(Event.event_at < until)
                    # фильтрацию по пользователю выполняем текстовым фильтром ниже
                    q = q.order_by(Event.event_at.desc())
                    for ev, login, key_desc, key_code, box_id, room_id in q.all():
                        try:
                            ts = format_local_datetime(ev.event_at)
                        except Exception:
                            ts = str(ev.event_at)
                        # lookup names
                        box_name = ''
                        room_name = ''
                        try:
                            if box_id is not None:
                                from sqlalchemy import select as _select
                                b = session.execute(_select(Box).where(Box.id == box_id)).scalar_one_or_none()
                                box_name = getattr(b, 'name', '') if b else ''
                        except Exception:
                            pass
                        try:
                            if room_id is not None:
                                from sqlalchemy import select as _select
                                r = session.execute(_select(Room).where(Room.id == room_id)).scalar_one_or_none()
                                room_name = getattr(r, 'name', '') if r else ''
                        except Exception:
                            pass
                        action_ru = 'выдача' if ev.action == 'issue' else 'сдача'
                        # Убираем столбцы 'Код' и 'Когда'
                        rows_full.append((ts, login or '', (key_desc or key_code or ''), action_ru, box_name, room_name))
            except Exception:
                rows_full = []

            # Применим фильтры по колонкам
            try:
                ts_q = (getattr(f_ts, 'text', '') or '').strip().lower()
                user_q = (getattr(f_user, 'text', '') or '').strip().lower()
                key_q = (getattr(f_key, 'text', '') or '').strip().lower()
                action_q = (getattr(f_action, 'text', '') or '').strip().lower()
                box_q = (getattr(f_box, 'text', '') or '').strip().lower()
                room_q = (getattr(f_room, 'text', '') or '').strip().lower()
                def row_match_col(r):
                    try:
                        return (
                            ts_q in str(r[0]).lower()
                            and user_q in str(r[1]).lower()
                            and key_q in str(r[2]).lower()
                            and action_q in str(r[3]).lower()
                            and box_q in str(r[4]).lower()
                            and room_q in str(r[5]).lower()
                        )
                    except Exception:
                        return True
                rows_full = [r for r in rows_full if row_match_col(r)]
            except Exception:
                pass

            # Пагинация вручную
            try:
                page = int(getattr(self, '_export_page', 0))
            except Exception:
                page = 0
            per_page = 7
            total = len(rows_full)
            max_page = (total - 1) // per_page if total else 0
            if page > max_page:
                page = max_page
                self._export_page = page
            start = page * per_page
            end = min(total, start + per_page)
            rows = rows_full[start:end]

            # Рендерим таблицу в едином тёмном стиле.
            # Колонки — единый источник ширин (совпадают с полями фильтров сверху).
            from kivy.factory import Factory
            try:
                from kivymd.uix.label import MDLabel
            except Exception:
                from kivy.uix.label import Label as MDLabel

            # Последняя колонка (flex=True) тянется до правого края карточки;
            # остальные фиксированы. Так таблица заполняет ширину без «обрыва».
            cols = [
                ('ts', 'Дата/время', dp(150), 'left', False),
                ('user', 'Пользователь', dp(150), 'left', False),
                ('key', 'Ключ', dp(220), 'left', False),
                ('action', 'Действие', dp(110), 'center', False),
                ('box', 'Бокс', dp(120), 'left', False),
                ('room', 'Помещение', dp(160), 'left', True),
            ]

            # Заголовок таблицы
            header_row = screen.ids.get('export_header_row') if hasattr(screen, 'ids') else None
            if header_row is not None:
                header_row.clear_widgets()
                for _key, title, w, align, flex in cols:
                    hc = Factory.AdminTableCell()
                    hc.text = title
                    hc.align = align
                    hc.bold_text = True
                    hc.color_rgba = (0.97, 0.98, 1, 1)
                    if flex:
                        hc.size_hint_x = 1
                    else:
                        hc.width = w
                    header_row.add_widget(hc)

            # Строки данных (с чередованием фона). Строки тянутся на всю ширину
            # карточки, колонки фиксированы — заголовок и данные выровнены.
            if not rows:
                empty = MDLabel(
                    text='Нет данных за выбранный период',
                    halign='center', valign='middle',
                    theme_text_color='Custom',
                )
                try:
                    empty.text_color = (0.7, 0.72, 0.76, 1)
                except Exception:
                    pass
                empty.size_hint_y = None
                empty.height = dp(80)
                container.add_widget(empty)
            else:
                for idx, r in enumerate(rows):
                    row = Factory.AdminTableRow()
                    row.bg = (0.205, 0.215, 0.235, 1) if (idx % 2 == 0) else (0.165, 0.175, 0.195, 1)
                    for (_key, _title, w, align, flex), val in zip(cols, r):
                        cell = Factory.AdminTableCell()
                        cell.text = str(val)
                        cell.align = align
                        if flex:
                            cell.size_hint_x = 1
                        else:
                            cell.width = w
                        row.add_widget(cell)
                    container.add_widget(row)

            # Лейбл пагинации
            try:
                page_lbl = screen.ids.get('export_page_label') if hasattr(screen, 'ids') else None
                if page_lbl is not None:
                    page_lbl.text = f"Стр. {page+1} / {max_page+1 if total else 1}  ·  всего: {total}"
            except Exception:
                pass
        except Exception:
            pass

    def admin_export_apply_filter(self, text: str) -> None:
        try:
            self._build_admin_export_table()
        except Exception:
            pass

    def admin_export_set_column_filter(self, column: str, value: str) -> None:
        try:
            if not hasattr(self, '_export_col_filters'):
                self._export_col_filters = {'ts': '', 'user': '', 'key': '', 'action': ''}
            self._export_col_filters[column] = value or ''
            self._build_admin_export_table()
        except Exception:
            pass

    def admin_export_refresh(self) -> None:
        try:
            self._build_admin_export_table()
        except Exception:
            pass

    def admin_export_prev_page(self) -> None:
        try:
            page = int(getattr(self, '_export_page', 0))
            if page > 0:
                self._export_page = page - 1
                self._build_admin_export_table()
        except Exception:
            pass

    def admin_export_next_page(self) -> None:
        try:
            # max_page вычисляется в билдере; просто инкрементируем и билдер сам откорректирует
            page = int(getattr(self, '_export_page', 0))
            self._export_page = page + 1
            self._build_admin_export_table()
        except Exception:
            pass

    def _admin_export_init_date_spinners(self) -> None:
        """Initialize date spinners with years, months, and days."""
        try:
            from datetime import datetime, timedelta, timezone
            import calendar
            app = App.get_running_app()
            if not app or not hasattr(app, 'root') or app.root is None:
                # Повторная попытка через небольшую задержку
                Clock.schedule_once(lambda dt: self._admin_export_init_date_spinners(), 0.1)
                return
            scr = app.root.get_screen("admin_export")
            if not scr or not hasattr(scr, 'ids'):
                # Повторная попытка через небольшую задержку
                Clock.schedule_once(lambda dt: self._admin_export_init_date_spinners(), 0.1)
                return
            
            # Годы: от текущего года до 5 лет назад
            current_year = datetime.now().year
            years = [str(y) for y in range(current_year, current_year - 6, -1)]
            
            # Месяцы
            months = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12']
            
            # Инициализируем спиннеры для "от"
            year_from = scr.ids.get("export_filter_date_from_year")
            month_from = scr.ids.get("export_filter_date_from_month")
            day_from = scr.ids.get("export_filter_date_from_day")
            
            if year_from:
                year_from.values = ['Год'] + years
            if month_from:
                month_from.values = ['Месяц'] + months
            if day_from:
                day_from.values = ['День']
            
            # Инициализируем спиннеры для "до"
            year_to = scr.ids.get("export_filter_date_to_year")
            month_to = scr.ids.get("export_filter_date_to_month")
            day_to = scr.ids.get("export_filter_date_to_day")
            
            if year_to:
                year_to.values = ['Год'] + years
            if month_to:
                month_to.values = ['Месяц'] + months
            if day_to:
                day_to.values = ['День']
            
            # Устанавливаем фильтр по умолчанию на последнюю неделю только если фильтры еще не установлены
            if self._export_filter_date_from is None and self._export_filter_date_to is None:
                now = now_local()
                week_ago = now - timedelta(days=7)
                
                # Устанавливаем значения по умолчанию (неделю назад)
                if year_from and month_from and day_from:
                    year_from.text = str(week_ago.year)
                    month_from.text = str(week_ago.month)
                    # Обновляем список дней для выбранного месяца
                    days_in_month = calendar.monthrange(week_ago.year, week_ago.month)[1]
                    days = [str(d) for d in range(1, days_in_month + 1)]
                    day_from.values = ['День'] + days
                    day_from.text = str(week_ago.day)
                    # Устанавливаем фильтр
                    self._export_filter_date_from = local_day_start(week_ago.year, week_ago.month, week_ago.day)
                
                # Устанавливаем значения по умолчанию (сегодня)
                if year_to and month_to and day_to:
                    year_to.text = str(now.year)
                    month_to.text = str(now.month)
                    # Обновляем список дней для выбранного месяца
                    days_in_month = calendar.monthrange(now.year, now.month)[1]
                    days = [str(d) for d in range(1, days_in_month + 1)]
                    day_to.values = ['День'] + days
                    day_to.text = str(now.day)
                    # Устанавливаем фильтр
                    self._export_filter_date_to = local_day_start(now.year, now.month, now.day)
                
                # Обновляем таблицу с установленными фильтрами
                Clock.schedule_once(lambda dt: self._build_admin_export_table(), 0.2)
        except Exception as e:
            try:
                KivyLogger.error(f"_admin_export_init_date_spinners error: {e}")
            except:
                pass

    def admin_export_update_date(self, field: str) -> None:
        """Update date filter when spinner values change."""
        try:
            from datetime import datetime, timezone, time as dt_time
            import calendar
            
            app = App.get_running_app()
            scr = app.root.get_screen("admin_export")
            
            if field == 'from':
                year_sp = scr.ids.get("export_filter_date_from_year")
                month_sp = scr.ids.get("export_filter_date_from_month")
                day_sp = scr.ids.get("export_filter_date_from_day")
            else:
                year_sp = scr.ids.get("export_filter_date_to_year")
                month_sp = scr.ids.get("export_filter_date_to_month")
                day_sp = scr.ids.get("export_filter_date_to_day")
            
            if not year_sp or not month_sp or not day_sp:
                return
            
            year_str = year_sp.text
            month_str = month_sp.text
            day_str = day_sp.text
            
            # Если выбран год, обновляем список месяцев (если месяц еще не выбран)
            if year_str != 'Год':
                months_list = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12']
                if month_sp.text == 'Месяц' or not month_sp.values or len(month_sp.values) <= 1:
                    month_sp.values = ['Месяц'] + months_list
            
            # Если выбран год и месяц, обновляем список дней
            if year_str != 'Год' and month_str != 'Месяц':
                try:
                    year = int(year_str)
                    month = int(month_str)
                    days_in_month = calendar.monthrange(year, month)[1]
                    days = [str(d) for d in range(1, days_in_month + 1)]
                    day_sp.values = ['День'] + days
                except Exception:
                    pass
            elif year_str == 'Год' or month_str == 'Месяц':
                # Если год или месяц не выбраны, сбрасываем список дней
                if day_sp.values != ['День']:
                    day_sp.values = ['День']
                    day_sp.text = 'День'
            
            # Если все три значения выбраны, устанавливаем дату
            if year_str != 'Год' and month_str != 'Месяц' and day_str != 'День':
                try:
                    year = int(year_str)
                    month = int(month_str)
                    day = int(day_str)
                    selected_date = local_day_start(year, month, day)
                    
                    if field == 'from':
                        self._export_filter_date_from = selected_date
                    else:
                        self._export_filter_date_to = selected_date
                    
                    self._build_admin_export_table()
                except Exception:
                    pass
            else:
                # Если не все значения выбраны, сбрасываем фильтр
                if field == 'from':
                    self._export_filter_date_from = None
                else:
                    self._export_filter_date_to = None
                self._build_admin_export_table()
        except Exception:
            pass

    def admin_export_clear_date_filter(self) -> None:
        """Clear date filters and refresh export table."""
        try:
            self._export_filter_date_from = None
            self._export_filter_date_to = None
            
            app = App.get_running_app()
            scr = app.root.get_screen("admin_export")
            
            # Сбрасываем спиннеры "от"
            year_from = scr.ids.get("export_filter_date_from_year")
            month_from = scr.ids.get("export_filter_date_from_month")
            day_from = scr.ids.get("export_filter_date_from_day")
            if year_from:
                year_from.text = 'Год'
            if month_from:
                month_from.text = 'Месяц'
            if day_from:
                day_from.text = 'День'
                day_from.values = ['День']
            
            # Сбрасываем спиннеры "до"
            year_to = scr.ids.get("export_filter_date_to_year")
            month_to = scr.ids.get("export_filter_date_to_month")
            day_to = scr.ids.get("export_filter_date_to_day")
            if year_to:
                year_to.text = 'Год'
            if month_to:
                month_to.text = 'Месяц'
            if day_to:
                day_to.text = 'День'
                day_to.values = ['День']
            
            # Обновляем таблицу
            self._build_admin_export_table()
        except Exception:
            pass

    def admin_export_delete_by_period(self) -> None:
        """Open popup to select period for deleting event records."""
        self._show_delete_period_popup_export()

    def _show_delete_period_popup_export(self) -> None:
        """Show popup with period selection for deleting events."""
        try:
            from kivy.uix.spinner import Spinner
            from datetime import timedelta
            
            layout = BoxLayout(orientation='vertical', spacing=10, padding=10)
            
            # Label
            title_label = Label(text='Выберите период для удаления записей', color=(1,1,1,1), size_hint_y=None, height=30)
            layout.add_widget(title_label)
            
            # Period selection - "От"
            period_from_layout = BoxLayout(orientation='horizontal', size_hint_y=None, height=44, spacing=5)
            period_from_layout.add_widget(Label(text='От:', color=(1,1,1,1), size_hint_x=None, width=40))
            
            year_from_sp = Spinner(text='Год', size_hint_x=0.3, size_hint_y=None, height=44,
                                   background_normal='', background_color=(0.30, 0.31, 0.32, 1), color=(1,1,1,1))
            month_from_sp = Spinner(text='Месяц', size_hint_x=0.3, size_hint_y=None, height=44,
                                    background_normal='', background_color=(0.30, 0.31, 0.32, 1), color=(1,1,1,1))
            day_from_sp = Spinner(text='День', size_hint_x=0.3, size_hint_y=None, height=44,
                                  background_normal='', background_color=(0.30, 0.31, 0.32, 1), color=(1,1,1,1))
            
            def update_from_days(dt):
                self._update_day_spinner_in_popup(year_from_sp, month_from_sp, day_from_sp)
            year_from_sp.bind(text=lambda inst, val: update_from_days(None))
            month_from_sp.bind(text=lambda inst, val: update_from_days(None))
            
            period_from_layout.add_widget(year_from_sp)
            period_from_layout.add_widget(month_from_sp)
            period_from_layout.add_widget(day_from_sp)
            layout.add_widget(period_from_layout)
            
            # Period selection - "До"
            period_to_layout = BoxLayout(orientation='horizontal', size_hint_y=None, height=44, spacing=5)
            period_to_layout.add_widget(Label(text='До:', color=(1,1,1,1), size_hint_x=None, width=40))
            
            year_to_sp = Spinner(text='Год', size_hint_x=0.3, size_hint_y=None, height=44,
                                background_normal='', background_color=(0.30, 0.31, 0.32, 1), color=(1,1,1,1))
            month_to_sp = Spinner(text='Месяц', size_hint_x=0.3, size_hint_y=None, height=44,
                                 background_normal='', background_color=(0.30, 0.31, 0.32, 1), color=(1,1,1,1))
            day_to_sp = Spinner(text='День', size_hint_x=0.3, size_hint_y=None, height=44,
                               background_normal='', background_color=(0.30, 0.31, 0.32, 1), color=(1,1,1,1))
            
            def update_to_days(dt):
                self._update_day_spinner_in_popup(year_to_sp, month_to_sp, day_to_sp)
            year_to_sp.bind(text=lambda inst, val: update_to_days(None))
            month_to_sp.bind(text=lambda inst, val: update_to_days(None))
            
            period_to_layout.add_widget(year_to_sp)
            period_to_layout.add_widget(month_to_sp)
            period_to_layout.add_widget(day_to_sp)
            layout.add_widget(period_to_layout)
            
            # Buttons
            btns = BoxLayout(size_hint_y=None, height=44, spacing=10)
            btn_delete = Button(text='Удалить')
            btn_cancel = Button(text='Отмена')
            btns.add_widget(btn_delete)
            btns.add_widget(btn_cancel)
            layout.add_widget(btns)
            
            popup = Popup(title='Удаление записей за период', content=layout, size_hint=(0.7, 0.5), auto_dismiss=False)
            
            # Initialize spinners
            Clock.schedule_once(lambda dt: self._init_period_spinners_in_popup(
                year_from_sp, month_from_sp, day_from_sp, year_to_sp, month_to_sp, day_to_sp), 0.1)
            
            def on_delete(instance):
                try:
                    date_from = self._get_date_from_spinners(year_from_sp, month_from_sp, day_from_sp)
                    date_to = self._get_date_from_spinners(year_to_sp, month_to_sp, day_to_sp)
                    
                    if date_from is None and date_to is None:
                        self._show_error_popup("Выберите период для удаления")
                        return
                    
                    popup.dismiss()
                    self._perform_export_delete(date_from, date_to)
                except Exception as e:
                    try:
                        self._show_error_popup(f"Ошибка: {str(e)}")
                    except Exception:
                        pass
            
            def on_cancel(instance):
                popup.dismiss()
            
            btn_delete.bind(on_release=on_delete)
            btn_cancel.bind(on_release=on_cancel)
            popup.open()
        except Exception as e:
            try:
                self._show_error_popup(f"Ошибка при открытии окна: {str(e)}")
            except Exception:
                pass

    def _perform_export_delete(self, date_from: datetime | None, date_to: datetime | None) -> None:
        """Perform deletion of event records for the selected period."""
        try:
            from datetime import timedelta
            
            # Формируем сообщение подтверждения
            period_str = ""
            if date_from is not None:
                period_str += f"От: {format_local_date(date_from)}"
            if date_to is not None:
                if period_str:
                    period_str += " "
                period_str += f"До: {format_local_date(date_to)}"
            
            def do_delete():
                try:
                    deleted_count = 0
                    with self._SessionLocal() as session:
                        # Формируем запрос с фильтрацией по датам
                        q = session.query(Event)
                        
                        if date_from is not None:
                            q = q.filter(Event.event_at >= date_from)
                        if date_to is not None:
                            date_to_end = date_to + timedelta(days=1)
                            q = q.filter(Event.event_at < date_to_end)
                        
                        # Подсчитываем количество записей для удаления
                        deleted_count = q.count()
                        
                        # Удаляем записи
                        q.delete(synchronize_session=False)
                        session.commit()
                    
                    # Обновляем таблицу
                    self._build_admin_export_table()
                    
                    # Показываем сообщение об успехе
                    try:
                        self._show_error_popup(f"Удалено записей: {deleted_count}")
                    except Exception:
                        pass
                except Exception as e:
                    try:
                        self._show_error_popup(f"Ошибка при удалении: {str(e)}")
                    except Exception:
                        pass
            
            self._confirm_action("Подтверждение удаления", f"Удалить записи за период: {period_str}?", do_delete)
        except Exception as e:
            try:
                self._show_error_popup(f"Ошибка: {str(e)}")
            except Exception:
                pass

    def admin_open_delete_user(self) -> None:
        app = App.get_running_app()
        try:
            screen = app.root.get_screen("admin_delete_user")
            with self._SessionLocal() as session:
                users = session.execute(select(User)).scalars().all()
                user_logins = [u.login for u in users if getattr(u, "login", None) != "admin"]
            spinner = screen.ids.get("del_user_spinner") if hasattr(screen, "ids") else None
            if spinner:
                spinner.values = user_logins
                spinner.text = user_logins[0] if user_logins else "Выберите пользователя"
            app.root.current = "admin_delete_user"
        except Exception:
            pass

    def admin_open_delete_room(self) -> None:
        app = App.get_running_app()
        try:
            screen = app.root.get_screen("admin_delete_room")
            # Populate both spinners
            self._populate_delete_room_spinner()
            self._populate_delete_key_spinner()
            app.root.current = "admin_delete_room"
        except Exception:
            pass

    def admin_open_delete_box(self) -> None:
        app = App.get_running_app()
        try:
            screen = app.root.get_screen("admin_delete_box")
            self._populate_delete_box_spinner()
            app.root.current = "admin_delete_box"
        except Exception:
            pass

    def _populate_delete_room_spinner(self) -> None:
        try:
            app = App.get_running_app()
            screen = app.root.get_screen("admin_delete_room")
            sp = screen.ids.get("del_room_spinner") if hasattr(screen, "ids") else None
            if sp is None:
                return
            with self._SessionLocal() as session:
                rooms = session.execute(select(Room).order_by(Room.name.asc())).scalars().all()
            values = [r.name for r in rooms]
            sp.values = values
            if values:
                sp.text = values[0]
            else:
                sp.text = 'Выберите помещение'
        except Exception:
            pass

    def _populate_delete_key_spinner(self) -> None:
        try:
            app = App.get_running_app()
            screen = app.root.get_screen("admin_delete_room")
            sp = screen.ids.get("del_key_spinner") if hasattr(screen, "ids") else None
            if sp is None:
                return
            with self._SessionLocal() as session:
                keys = session.execute(select(Key).order_by(Key.description.asc())).scalars().all()
            titles = [f"{(k.description or k.code)} ({k.code})" for k in keys]
            # keep map for deletions by title
            try:
                self._delete_key_title_to_code = { (f"{(k.description or k.code)} ({k.code})"): k.code for k in keys }
            except Exception:
                pass
            sp.values = titles
            if titles:
                sp.text = titles[0]
            else:
                sp.text = 'Выберите ключ'
        except Exception:
            pass

    def _populate_delete_box_spinner(self) -> None:
        try:
            app = App.get_running_app()
            screen = app.root.get_screen("admin_delete_box")
            sp = screen.ids.get("del_box_spinner") if hasattr(screen, "ids") else None
            if sp is None:
                return
            boxes = self._load_boxes_ordered()
            self._apply_box_spinner_values(sp, boxes)
        except Exception:
            pass

    def admin_delete_room_entity_request(self, room_name: str) -> None:
        sel = (room_name or '').strip()
        if not sel or sel == 'Выберите помещение':
            return
        # Block if any keys for this room are issued
        try:
            with self._SessionLocal() as session:
                room = session.execute(select(Room).where(Room.name == sel)).scalar_one_or_none()
                if room is None:
                    return
                keys = session.execute(select(Key).where(Key.room_id == room.id)).scalars().all()
                if keys:
                    key_ids = [k.id for k in keys]
                    issued = session.execute(select(IssuedKey).where(IssuedKey.key_id.in_(key_ids))).scalars().first()
                    if issued is not None:
                        self._show_error_popup('Нельзя удалить помещение: есть выданные ключи')
                        return
        except Exception:
            return

        def do_delete():
            try:
                with self._SessionLocal() as session:
                    room = session.execute(select(Room).where(Room.name == sel)).scalar_one_or_none()
                    if room is None:
                        return
                    # Cascade: delete keys of this room first
                    keys = session.execute(select(Key).where(Key.room_id == room.id)).scalars().all()
                    for k in keys:
                        session.delete(k)
                    session.delete(room)
                    session.commit()
            except Exception:
                return
            # refresh spinners and grids
            try:
                self._populate_delete_room_spinner()
                self._populate_delete_key_spinner()
                self.initialize_keys_grid()
            except Exception:
                pass

        self._confirm_action("Подтверждение удаления", f"Удалить помещение: {sel}?", do_delete)

    def admin_delete_key_request(self, key_title: str) -> None:
        sel = (key_title or '').strip()
        if not sel or sel == 'Выберите ключ':
            return
        # map to code
        code = sel
        try:
            if hasattr(self, '_delete_key_title_to_code') and sel in self._delete_key_title_to_code:
                code = self._delete_key_title_to_code.get(sel, sel)
        except Exception:
            pass

        # Block if issued now
        try:
            with self._SessionLocal() as session:
                key = session.execute(select(Key).where(Key.code == code)).scalar_one_or_none()
                if key is None:
                    return
                issued = session.execute(select(IssuedKey).where(IssuedKey.key_id == key.id)).scalar_one_or_none()
                if issued is not None:
                    self._show_error_popup('Нельзя удалить ключ: он сейчас выдан')
                    return
        except Exception:
            return

        def do_delete():
            try:
                with self._SessionLocal() as session:
                    key = session.execute(select(Key).where(Key.code == code)).scalar_one_or_none()
                    if key is None:
                        return
                    session.delete(key)
                    session.commit()
            except Exception:
                return
            try:
                self._populate_delete_key_spinner()
                self.initialize_keys_grid()
            except Exception:
                pass

        self._confirm_action("Подтверждение удаления", f"Удалить ключ: {sel}?", do_delete)

    def admin_delete_box_request(self, box_name: str) -> None:
        _, sel = self._resolve_box_from_spinner(box_name)
        sel = (sel or '').strip()
        if not sel or sel == 'Выберите бокс':
            return
        # Block if any keys for this box are issued
        try:
            with self._SessionLocal() as session:
                box = session.execute(select(Box).where(Box.name == sel)).scalar_one_or_none()
                if box is None:
                    return
                keys = session.execute(select(Key).where(Key.box_id == box.id)).scalars().all()
                if keys:
                    key_ids = [k.id for k in keys]
                    issued = session.execute(select(IssuedKey).where(IssuedKey.key_id.in_(key_ids))).scalars().first()
                    if issued is not None:
                        self._show_error_popup('Нельзя удалить бокс: есть выданные ключи')
                        return
        except Exception:
            return

        def do_delete():
            try:
                with self._SessionLocal() as session:
                    box = session.execute(select(Box).where(Box.name == sel)).scalar_one_or_none()
                    if box is None:
                        return
                    # Cascade-like: delete keys of this box first
                    keys = session.execute(select(Key).where(Key.box_id == box.id)).scalars().all()
                    for k in keys:
                        session.delete(k)
                    session.delete(box)
                    session.commit()
            except Exception:
                return
            # refresh spinners and grids
            try:
                self._populate_delete_box_spinner()
            except Exception:
                pass
            try:
                self._populate_admin_box_spinner()
            except Exception:
                pass
            try:
                self._populate_admin_box_spinner_for_create_key()
            except Exception:
                pass
            try:
                self.initialize_keys_grid()
            except Exception:
                pass

        self._confirm_action("Подтверждение удаления", f"Удалить бокс: {sel}?", do_delete)

    def admin_open_export(self) -> None:
        app = App.get_running_app()
        try:
            # Обновим список пользователей (с пунктом 'Все')
            screen = app.root.get_screen("admin_export")
            with self._SessionLocal() as session:
                users = session.execute(select(User)).scalars().all()
                user_logins = ["Все"] + [u.login for u in users]
            spinner = screen.ids.get("export_user_spinner") if hasattr(screen, "ids") else None
            if spinner:
                spinner.values = user_logins
                if not spinner.text:
                    spinner.text = "Все"
            app.root.current = "admin_export"
        except Exception:
            pass

    def _confirm_action(self, title: str, message: str, on_confirm) -> None:
        layout = BoxLayout(orientation='vertical', spacing=10, padding=10)
        layout.add_widget(Label(text=message, color=(1,1,1,1), size_hint_y=None, height=24))
        btns = BoxLayout(size_hint_y=None, height=44, spacing=10)
        btn_ok = Button(text='Да')
        btn_cancel = Button(text='Нет')
        btns.add_widget(btn_ok)
        btns.add_widget(btn_cancel)
        layout.add_widget(btns)
        popup = Popup(title=title, content=layout, size_hint=(0.6, 0.35), auto_dismiss=False)

        def _on_ok(instance):
            try:
                on_confirm()
            finally:
                popup.dismiss()

        def _on_cancel(instance):
            popup.dismiss()

        btn_ok.bind(on_release=_on_ok)
        btn_cancel.bind(on_release=_on_cancel)
        popup.open()

    def _show_error_popup(self, message: str) -> None:
        try:
            box = BoxLayout(orientation='vertical', spacing=10, padding=10)
            lbl = Label(text=message, color=(1,0.5,0.5,1), size_hint_y=None, height=24)
            box.add_widget(lbl)
            popup = Popup(title='Ошибка', content=box, size_hint=(0.5, 0.3), auto_dismiss=False)
            popup.open()
            Clock.schedule_once(lambda dt: popup.dismiss(), 1.6)
        except Exception:
            pass

    def admin_delete_user_request(self, login: str) -> None:
        login = (login or '').strip()
        if not login or login == 'Выберите пользователя':
            return

        def do_delete():
            try:
                with self._SessionLocal() as session:
                    user = session.execute(select(User).where(User.login == login)).scalar_one_or_none()
                    if user is None:
                        return
                    session.delete(user)
                    session.commit()
            except Exception:
                return
            # refresh spinners
            try:
                app = App.get_running_app()
                # permissions screen spinner
                try:
                    perm = app.root.get_screen("admin_permissions")
                    with self._SessionLocal() as session:
                        user_logins = [u.login for u in session.execute(select(User)).scalars().all()]
                    sp = perm.ids.get("admin_user_spinner") if hasattr(perm, "ids") else None
                    if sp:
                        sp.values = user_logins
                        sp.text = user_logins[0] if user_logins else 'Выберите пользователя'
                except Exception:
                    pass
                # delete screen spinner (без admin)
                try:
                    del_scr = app.root.get_screen("admin_delete_user")
                    with self._SessionLocal() as session:
                        users = session.execute(select(User)).scalars().all()
                        user_logins = [u.login for u in users if getattr(u, 'login', None) != 'admin']
                    sp2 = del_scr.ids.get("del_user_spinner") if hasattr(del_scr, "ids") else None
                    if sp2:
                        sp2.values = user_logins
                        sp2.text = user_logins[0] if user_logins else 'Выберите пользователя'
                except Exception:
                    pass
            except Exception:
                pass

        self._confirm_action("Подтверждение удаления", f"Удалить пользователя: {login}?", do_delete)

    def admin_delete_room_request(self, title_or_code: str) -> None:
        sel = (title_or_code or '').strip()
        if not sel or sel == 'Выберите помещение':
            return
        # Resolve code by title if mapping exists
        code = sel
        try:
            if hasattr(self, '_delete_room_title_to_code') and sel in self._delete_room_title_to_code:
                code = self._delete_room_title_to_code.get(sel, sel)
        except Exception:
            pass

        def do_delete():
            try:
                with self._SessionLocal() as session:
                    key = session.execute(select(Key).where(Key.code == code)).scalar_one_or_none()
                    if key is None:
                        return
                    session.delete(key)
                    session.commit()
            except Exception:
                return
            # refresh lists and grid
            try:
                app = App.get_running_app()
                # refresh permissions screen
                try:
                    perm = app.root.get_screen("admin_permissions")
                    sp = perm.ids.get("admin_user_spinner") if hasattr(perm, "ids") else None
                    if sp and sp.text and sp.text != 'Выберите пользователя':
                        self.on_admin_user_selected(sp.text)
                except Exception:
                    pass
                # refresh delete screen spinner
                try:
                    del_scr = app.root.get_screen("admin_delete_room")
                    with self._SessionLocal() as session:
                        titles = [k.description or k.code for k in session.execute(select(Key)).scalars().all()]
                        self._delete_room_title_to_code = { (k.description or k.code): k.code for k in session.execute(select(Key)).scalars().all() }
                    sp2 = del_scr.ids.get("del_room_spinner") if hasattr(del_scr, "ids") else None
                    if sp2:
                        sp2.values = titles
                        sp2.text = titles[0] if titles else 'Выберите помещение'
                except Exception:
                    pass
                # refresh main grid
                try:
                    self.initialize_keys_grid()
                except Exception:
                    pass
            except Exception:
                pass

        self._confirm_action("Подтверждение удаления", f"Удалить помещение: {sel}?", do_delete)

    # Админ: добавить пользователя
    def admin_add_user(self, login: str, password: str, phone: str, comment: str) -> None:
        app = App.get_running_app()
        login = (login or '').strip()
        password = (password or '').strip()
        phone = (phone or '').strip()
        comment = (comment or '').strip()
        if not login:
            self._show_error_popup('Укажите логин — поле обязательно')
            return
        created = False
        created_user_id: int | None = None
        try:
            with self._SessionLocal() as session:
                existing = session.execute(select(User).where(User.login == login)).scalar_one_or_none()
                if existing is None:
                    if not password:
                        self._show_error_popup('Укажите пароль — поле обязательно для нового пользователя')
                        return
                    user = User(login=login, password_hash=hash_password(password), phone=phone or None, comment=comment or None)
                    session.add(user)
                    session.flush()
                    created_user_id = int(user.id)
                    session.commit()
                    created = True
                else:
                    # Обновим телефон/комментарий; пароль — если указан
                    if phone:
                        existing.phone = phone
                    if comment:
                        existing.comment = comment
                    if password:
                        existing.password_hash = hash_password(password)
                    session.commit()
        except Exception:
            return

        # Обновим спиннеры пользователей в админке и на экране логина
        try:
            admin_screen = app.root.get_screen("admin_permissions")
            auth_screen = app.root.get_screen("auth")
            with self._SessionLocal() as session:
                users = session.execute(select(User)).scalars().all()
                user_logins_all = [u.login for u in users]
                user_logins_no_admin = [l for l in user_logins_all if l != 'admin']
            # админ-спиннер
            try:
                spinner = admin_screen.ids.get("admin_user_spinner") if hasattr(admin_screen, "ids") else None
                if spinner:
                    spinner.values = user_logins_all
                    if login in user_logins_all:
                        spinner.text = login
                # комментарий пользователя
                lbl = admin_screen.ids.get("admin_user_comment") if hasattr(admin_screen, "ids") else None
                if lbl and login:
                    lbl.text = f"Комментарий: {comment}"
            except Exception:
                pass
            # экран авторизации
            try:
                spinner_auth = auth_screen.ids.get("login_spinner") if hasattr(auth_screen, "ids") else None
                if spinner_auth:
                    spinner_auth.values = user_logins_no_admin
                    if not spinner_auth.text or spinner_auth.text == 'Выберите пользователя':
                        spinner_auth.text = user_logins_no_admin[0] if user_logins_no_admin else 'Выберите пользователя'
                # очистим пароль
                pw = auth_screen.ids.get("password_input") if hasattr(auth_screen, "ids") else None
                if pw:
                    pw.text = ''
            except Exception:
                pass
        except Exception:
            pass

        # Если создан — очистить форму и перейти к привязке RFID
        if created and created_user_id is not None:
            try:
                screen_add = app.root.get_screen("admin_add_user")
                if hasattr(screen_add, "ids"):
                    for field_id in ("admin_new_login", "admin_new_password", "admin_new_phone", "admin_new_comment"):
                        ti = screen_add.ids.get(field_id)
                        if ti:
                            ti.text = ''
            except Exception:
                pass
            try:
                self.admin_begin_user_rfid_bind(created_user_id, login)
            except Exception:
                self._show_success_popup("Пользователь создан")

    # Админ: добавить помещение/ключ
    def admin_add_room(self, code: str, description: str) -> None:
        app = App.get_running_app()
        code = (code or '').strip()
        description = (description or '').strip()
        if not code:
            return
        created = False
        try:
            with self._SessionLocal() as session:
                existing = session.execute(select(Key).where(Key.code == code)).scalar_one_or_none()
                if existing is None:
                    k = Key(code=code, description=description or None)
                    session.add(k)
                    session.commit()
                    created = True
                else:
                    if description and existing.description != description:
                        existing.description = description
                        session.commit()
        except Exception:
            return

        # Обновим список ключей в админке (для выбранного пользователя) и грид на главном экране
        try:
            admin_screen = app.root.get_screen("admin_permissions")
            spinner = admin_screen.ids.get("admin_user_spinner") if hasattr(admin_screen, "ids") else None
            if spinner and spinner.text:
                self.on_admin_user_selected(spinner.text)
        except Exception:
            pass

        # Если создано — очистить поля формы и показать popup
        if created:
            try:
                screen_add = app.root.get_screen("admin_add_room")
                if hasattr(screen_add, "ids"):
                    for field_id in ("admin_new_key_code", "admin_new_key_desc"):
                        ti = screen_add.ids.get(field_id)
                        if ti:
                            ti.text = ''
            except Exception:
                pass
            self._show_success_popup("Помещение создано")

    def admin_create_box(self, name: str, comment: str, x_text: str = '', y_text: str = '') -> None:
        app = App.get_running_app()
        name = (name or '').strip()
        comment = (comment or '').strip()
        x_text = (x_text or '').strip()
        y_text = (y_text or '').strip()
        if not name:
            return
        # Parse and validate x,y as positive integers if provided
        def _parse_dim(val: str) -> int | None:
            try:
                n = int(val)
                return n if n > 0 else None
            except Exception:
                return None
        x_val = _parse_dim(x_text)
        y_val = _parse_dim(y_text)
        if (x_text and x_val is None) or (y_text and y_val is None):
            self._show_error_popup('X и Y должны быть положительными числами')
            return
        created = False
        created_box_id: int | None = None
        try:
            with self._SessionLocal() as session:
                exists = session.execute(select(Box).where(Box.name == name)).scalar_one_or_none()
                if exists is None:
                    box = Box(name=name, comment=comment or None, x=x_val, y=y_val)
                    session.add(box)
                    session.commit()
                    session.refresh(box)
                    created_box_id = int(box.id)
                    created = True
                else:
                    updated = False
                    if comment and exists.comment != comment:
                        exists.comment = comment
                        updated = True
                    # Update dimensions if provided
                    if x_val is not None and exists.x != x_val:
                        exists.x = x_val
                        updated = True
                    if y_val is not None and exists.y != y_val:
                        exists.y = y_val
                        updated = True
                    if updated:
                        session.add(exists)
                        session.commit()
        except Exception:
            return
        # refresh box-related spinners
        try:
            self._populate_admin_box_spinner()
        except Exception:
            pass
        try:
            self._populate_admin_box_spinner_for_create_key()
        except Exception:
            pass
        try:
            self._populate_delete_box_spinner()
        except Exception:
            pass
        # clear inputs and show popup
        if created:
            try:
                screen = app.root.get_screen("admin_add_box")
                if hasattr(screen, "ids"):
                    for fid in ("admin_new_box_name", "admin_new_box_comment", "admin_new_box_x", "admin_new_box_y"):
                        ti = screen.ids.get(fid)
                        if ti:
                            ti.text = ''
            except Exception:
                pass
            if created_box_id is not None:
                self._show_success_popup(f"Бокс создан: {name} (id={created_box_id})")
            else:
                self._show_success_popup("Бокс создан")

    # --- New: Admin create room entity ---
    def admin_create_room(self, name: str, comment: str) -> None:
        app = App.get_running_app()
        name = (name or '').strip()
        comment = (comment or '').strip()
        if not name:
            return
        created = False
        try:
            with self._SessionLocal() as session:
                exists = session.execute(select(Room).where(Room.name == name)).scalar_one_or_none()
                if exists is None:
                    room = Room(name=name, comment=comment or None)
                    session.add(room)
                    session.commit()
                    created = True
                else:
                    if comment and exists.comment != comment:
                        exists.comment = comment
                        session.commit()
        except Exception:
            return
        # refresh rooms/boxes spinners
        try:
            self._refresh_admin_add_room_form()
        except Exception:
            pass
        # clear inputs and show popup
        if created:
            try:
                screen = app.root.get_screen("admin_add_room")
                if hasattr(screen, "ids"):
                    for fid in ("admin_new_room_name", "admin_new_room_comment"):
                        ti = screen.ids.get(fid)
                        if ti:
                            ti.text = ''
            except Exception:
                pass
            self._show_success_popup("Помещение создано")

    # --- New: Admin create key and link to room ---
    def admin_create_key(
        self,
        code: str,
        description: str,
        room_name: str,
        box_name: str | None = None,
        secret_code: str | None = None,
    ) -> None:
        app = App.get_running_app()
        code = (code or '').strip()
        description = (description or '').strip()
        room_name = (room_name or '').strip()
        secret_code = (secret_code or '').strip()
        _, box_name = self._resolve_box_from_spinner(box_name or '')
        box_name = (box_name or '').strip()
        if not code:
            return
        created = False
        try:
            with self._SessionLocal() as session:
                # Resolve room
                room_obj = None
                if room_name and room_name != 'Выберите помещение':
                    room_obj = session.execute(select(Room).where(Room.name == room_name)).scalar_one_or_none()
                # Resolve box
                box_obj = None
                if box_name and box_name != 'Выберите бокс':
                    box_obj = session.execute(select(Box).where(Box.name == box_name)).scalar_one_or_none()
                # Upsert key
                key = session.execute(select(Key).where(Key.code == code)).scalar_one_or_none()
                if key is None:
                    if secret_code and self._secret_code_is_taken(session, secret_code):
                        self._show_error_popup('Секретный код уже используется другим ключом')
                        return
                    key = Key(code=code, description=description or None, secret_code=secret_code or None)
                    if room_obj is not None:
                        key.room_id = room_obj.id
                    if box_obj is not None:
                        key.box_id = box_obj.id
                    session.add(key)
                    session.commit()
                    created = True
                else:
                    updated = False
                    if description and key.description != description:
                        key.description = description
                        updated = True
                    if secret_code:
                        if self._secret_code_is_taken(session, secret_code, exclude_key_id=key.id):
                            self._show_error_popup('Секретный код уже используется другим ключом')
                            return
                        if key.secret_code != secret_code:
                            key.secret_code = secret_code
                            updated = True
                    # update room link
                    new_room_id = room_obj.id if room_obj is not None else None
                    if key.room_id != new_room_id:
                        key.room_id = new_room_id
                        updated = True
                    # update box link
                    new_box_id = box_obj.id if box_obj is not None else None
                    if key.box_id != new_box_id:
                        key.box_id = new_box_id
                        updated = True
                    if updated:
                        session.commit()
        except Exception:
            self._show_error_popup('Не удалось создать ключ')
            return
        # refresh permissions screen grid and rooms spinner
        try:
            admin_screen = app.root.get_screen("admin_permissions")
            spinner = admin_screen.ids.get("admin_user_spinner") if hasattr(admin_screen, "ids") else None
            if spinner and spinner.text:
                self.on_admin_user_selected(spinner.text)
        except Exception:
            pass
        try:
            self._refresh_admin_add_room_form()
        except Exception:
            pass
        # clear fields and notify
        if created:
            try:
                screen_add = app.root.get_screen("admin_add_room")
                if hasattr(screen_add, "ids"):
                    for field_id in ("admin_new_key_code", "admin_new_key_desc", "admin_new_key_secret"):
                        ti = screen_add.ids.get(field_id)
                        if ti:
                            ti.text = ''
            except Exception:
                pass
            self._show_success_popup("Ключ создан")

    def _show_info_popup(self, message: str, title: str = 'Подсказка') -> None:
        try:
            box = BoxLayout(orientation='vertical', spacing=10, padding=10)
            lbl = Label(text=message, color=(1, 1, 1, 1))
            box.add_widget(lbl)
            btn = Button(text='OK', size_hint_y=None, height=44)
            box.add_widget(btn)
            popup = Popup(title=title, content=box, size_hint=(0.55, 0.35), auto_dismiss=False)
            btn.bind(on_release=popup.dismiss)
            popup.open()
        except Exception:
            pass

    def _show_success_popup(self, message: str) -> None:
        try:
            box = BoxLayout(orientation='vertical', spacing=10, padding=10)
            lbl = Label(text=message, color=(1,1,1,1), size_hint_y=None, height=24)
            box.add_widget(lbl)
            popup = Popup(title='Готово', content=box, size_hint=(0.4, 0.25), auto_dismiss=False)
            popup.open()
            # Автоматическое закрытие через 1 сек
            Clock.schedule_once(lambda dt: popup.dismiss(), 1.0)
        except Exception:
            pass
        try:
            # Привяжем новые ключи к плейсхолдерам (если есть свободные) и обновим цвета
            self.initialize_keys_grid()
        except Exception:
            pass

    # Обработчик нажатия на плейсхолдер ключа (вызывается напрямую из kv)
    def on_key_placeholder_press(self, target) -> None:
        """Выдача/возврат ключа в зависимости от доступности.
        Цвета: зелёный — доступен; красный — недоступен; синий — выдан текущему пользователю.
        """
        # Глобальная блокировка при неверном действии (ожидаем правильный сигнал)
        try:
            if getattr(self, '_blocked_due_to_mismatch', False):
                return
        except Exception:
            pass
        if not hasattr(target, "key_code"):
            return
        key_code = getattr(target, "key_code", None)
        if not key_code:
            return

        if not getattr(self, "_current_user", None):
            return

        with self._SessionLocal() as session:
            q_key = select(Key).where(Key.code == key_code)
            # Уточняем по текущему боксу, если выбран
            if getattr(self, 'current_box_id', None) is not None:
                q_key = q_key.where(Key.box_id == self.current_box_id)
            key_row = session.execute(q_key).scalars().first()
            if key_row is None:
                return

            # Проверяем, выдан ли ключ кому-то сейчас
            issued = session.execute(
                select(IssuedKey).where(IssuedKey.key_id == key_row.id)
            ).scalar_one_or_none()

            # Выдан текущему пользователю: на экране «Сдать ключ» — только RFID, не клик по плитке
            if issued and issued.user_id == self._current_user.id:
                if getattr(self, '_on_return_screen', False):
                    try:
                        target.sub_status_text = 'поднесите ключ к считывателю'
                    except Exception:
                        pass
                return

            # Если выдан другому — ничего не делаем
            if issued and issued.user_id != self._current_user.id:
                # Лог: попытка взять чужой выданный ключ
                try:
                    session.add(ErrorLog(user_id=self._current_user.id, key_id=key_row.id, message='взять: уже выдан другому'))
                    session.commit()
                except Exception:
                    pass
                return

            if getattr(self._current_user, "login", None) == "admin":
                try:
                    session.add(ErrorLog(user_id=self._current_user.id, key_id=key_row.id, message='взять: запрещено для admin'))
                    session.commit()
                except Exception:
                    pass
                return
            if not self._is_user_allowed_for_key(session, int(key_row.id)):
                try:
                    session.add(ErrorLog(user_id=self._current_user.id, key_id=key_row.id, message='взять: нет допуска'))
                    session.commit()
                except Exception:
                    pass
                return

            try:
                print(f"[CLICK] issue flow key_code={getattr(key_row, 'code', None)} key_id={key_row.id}")
            except Exception:
                pass
            self._begin_key_slot_flow(int(key_row.id), 'issue')

    def _perform_secret_return_by_key_id(self, key_id: int) -> bool:
        """Сдать ключ по секретному коду без RFID-допуска текущего пользователя."""
        try:
            with self._SessionLocal() as session:
                key_row = session.execute(select(Key).where(Key.id == key_id)).scalars().first()
                if key_row is None:
                    return False
                issued = session.execute(select(IssuedKey).where(IssuedKey.key_id == key_row.id)).scalars().first()
                if issued is None:
                    return False
                holder_id = int(issued.user_id)
                session.delete(issued)
                try:
                    session.add(Event(user_id=holder_id, key_id=key_row.id, action='return'))
                except Exception:
                    pass
                try:
                    uk = session.execute(
                        select(UserKey).where(UserKey.user_id == holder_id, UserKey.key_id == key_row.id)
                    ).scalars().first()
                    if uk is not None:
                        uk.state = 'не выдан'
                        uk.state_user_id = 0
                        uk.state_updated_at = func.now()
                        session.add(uk)
                except Exception:
                    pass
                session.commit()
                try:
                    self._send_sim_feedback(
                        f"EVENT:RETURN key_code={getattr(key_row, 'code', '')} key_rfid={getattr(key_row, 'rfid', '')} "
                        f"x={getattr(key_row, 'pos_x', '')} y={getattr(key_row, 'pos_y', '')} user=secret_code"
                    )
                except Exception:
                    pass
            try:
                app = App.get_running_app()
                from kivy.clock import Clock
                def _refresh(dt):
                    try:
                        self._rebuild_return_data_and_refresh()
                    except Exception:
                        pass
                    try:
                        self._rebuild_take_data_and_refresh()
                    except Exception:
                        pass
                    try:
                        main_screen = app.root.get_screen("main")
                        main_grid = main_screen.ids.get("keys_grid") if hasattr(main_screen, "ids") else None
                        if main_grid:
                            self._refresh_key_colors(main_grid)
                    except Exception:
                        pass
                Clock.schedule_once(_refresh, 0)
            except Exception:
                pass
            return True
        except Exception:
            return False

    def open_secret_code(self) -> None:
        """Диалог ввода секретного кода для сдачи ключа без RFID-допуска."""
        layout = BoxLayout(orientation='vertical', spacing=10, padding=10)
        ti = TextInput(password=True, multiline=False, size_hint_y=None, height=40)
        layout.add_widget(Label(text='Введите секретный код ключа:', color=(1,1,1,1), size_hint_y=None, height=24))
        layout.add_widget(ti)
        btns = BoxLayout(size_hint_y=None, height=44, spacing=10)
        b_ok = Button(text='ОК')
        b_cancel = Button(text='Отмена')
        btns.add_widget(b_ok)
        btns.add_widget(b_cancel)
        layout.add_widget(btns)
        popup = Popup(title='Секретный код', content=layout, size_hint=(0.6, 0.4))

        def _on_ok(instance):
            code = (ti.text or '').strip()
            if not code:
                popup.title = 'Введите код'
                return
            key_id = None
            try:
                with self._SessionLocal() as session:
                    key_row = session.execute(select(Key).where(Key.secret_code == code)).scalar_one_or_none()
                    if key_row is None:
                        popup.title = 'Неверный код'
                        return
                    issued = session.execute(select(IssuedKey).where(IssuedKey.key_id == key_row.id)).scalar_one_or_none()
                    if issued is None:
                        popup.title = 'Ключ не выдан'
                        return
                    key_id = int(key_row.id)
            except Exception:
                popup.title = 'Ошибка'
                return
            popup.dismiss()
            if key_id is None:
                return
            self._prompt_shared_rfid_for_return(key_id, secret=True)

        def _on_cancel(instance):
            popup.dismiss()

        b_ok.bind(on_release=_on_ok)
        b_cancel.bind(on_release=_on_cancel)
        popup.open()

    def _ensure_guest_user(self, session) -> int | None:
        """Гарантирует наличие специального пользователя 'guest' для выдачи по секретному коду."""
        try:
            user = session.execute(select(User).where(User.login == 'guest')).scalar_one_or_none()
            if user is None:
                user = User(login='guest', password_hash=hash_password('guest'))
                session.add(user)
                session.commit()
            return user.id
        except Exception:
            return None

    # Пересчёт раскладки при изменении размера контейнера
    def on_container_resize(self, container, grid) -> None:
        available_w = max(0, float(getattr(container, "width", 0)))
        available_h = max(0, float(getattr(container, "height", 0)))

        total_items = len(grid.children)
        if total_items == 0:
            return

        spacing = float(self.grid_spacing)
        min_w = float(self.cell_min_w)
        max_w = float(self.cell_max_w)
        min_h = float(self.cell_min_h)
        max_h = float(self.cell_max_h)
        aspect = min_h / min_w if min_w else 1.0

        cols = max(1, int(self.grid_cols))
        grid.cols = cols
        grid.spacing = (spacing, spacing)

        free_w = available_w - spacing * (cols - 1)
        raw_cell_w = min_w if free_w <= 0 else free_w / float(cols)
        cell_w = max(min(raw_cell_w, max_w), min_w)
        cell_h = max(min(cell_w * aspect, max_h), min_h)

        for child in grid.children:
            child.size_hint = (None, None)
            child.size = (cell_w, cell_h)

        # Обновляем цвета ячеек согласно доступности/выдаче
        self._refresh_key_colors(grid)

        rows = int(math.ceil(total_items / float(cols)))
        grid.minimum_width = cols * cell_w + (cols - 1) * spacing
        grid.minimum_height = rows * cell_h + (rows - 1) * spacing

    def on_paged_container_resize(self, container, grid, cols: int, rows: int) -> None:
        """Укладывает видимые тайлы в точную сетку cols x rows без прокрутки.
        Пропорции плитки берём как у UserKeyTile: 260x140 (h/w≈0.5385).
        """
        try:
            from kivy.metrics import dp
        except Exception:
            dp = lambda v: v  # fallback

        available_w = max(0.0, float(getattr(container, "width", 0)))
        available_h = max(0.0, float(getattr(container, "height", 0)))

        spacing = float(self.grid_spacing)
        grid.cols = int(max(1, cols))
        grid.spacing = (spacing, spacing)

        # свободные размеры под сами ячейки с учётом промежутков
        free_w = max(0.0, available_w - spacing * (cols - 1))
        free_h = max(0.0, available_h - spacing * (rows - 1))

        cell_w_max = free_w / float(cols) if cols > 0 else 0.0
        cell_h_max = free_h / float(rows) if rows > 0 else 0.0

        # Целевой аспект высоты к ширине
        aspect = 140.0 / 260.0

        # Выберем максимально возможный размер, соблюдая аспект
        # вариант 1: ограничиваемся по высоте
        w1 = cell_h_max / aspect if aspect > 0 else cell_w_max
        h1 = cell_h_max
        # вариант 2: ограничиваемся по ширине
        w2 = cell_w_max
        h2 = cell_w_max * aspect

        if w1 <= cell_w_max and h1 <= cell_h_max:
            cell_w, cell_h = w1, h1
        else:
            cell_w, cell_h = w2, h2

        # Применяем размеры к видимым плиткам
        for child in grid.children:
            try:
                child.size_hint = (None, None)
                child.size = (cell_w, cell_h)
            except Exception:
                pass

        # Обновим цвета текущей страницы
        self._refresh_key_colors(grid)

        # minimum_size: для сетки с size_hint 1,1 заполняет контейнер;
        # для None,None — явно задаём размер под фактическое число плиток.
        count = len(grid.children)
        if count > 0:
            used_cols = min(cols, count)
            used_rows = int(math.ceil(count / float(cols)))
        else:
            used_cols, used_rows = cols, rows
        grid.minimum_width = used_cols * cell_w + max(0, used_cols - 1) * spacing
        grid.minimum_height = used_rows * cell_h + max(0, used_rows - 1) * spacing
        sh_x, sh_y = grid.size_hint
        if sh_x is None and sh_y is None:
            grid.size = (grid.minimum_width, grid.minimum_height)

    # === Пагинация: служебные методы ===
    def _set_page_label(self, screen_name: str, label_id: str, page: int, total_pages: int) -> None:
        try:
            app = App.get_running_app()
            scr = app.root.get_screen(screen_name)
            if hasattr(scr, "ids"):
                lbl = scr.ids.get(label_id)
                if lbl is not None:
                    if total_pages <= 0:
                        lbl.text = ""
                    else:
                        lbl.text = f"стр. {page+1}/{total_pages}"
        except Exception:
            pass

    def _show_page(self, screen_name: str, container_id: str, grid_id: str, label_id: str, tiles_all: list, page_attr: str, cols: int = 5, rows: int = 3, issued_view: bool = False) -> None:
        app = App.get_running_app()
        try:
            screen = app.root.get_screen(screen_name)
        except Exception:
            return
        grid = screen.ids.get(grid_id) if hasattr(screen, "ids") else None
        container = screen.ids.get(container_id) if hasattr(screen, "ids") else None
        if grid is None or container is None:
            return

        try:
            page = int(getattr(self, page_attr, 0) or 0)
        except Exception:
            page = 0
        per_page = cols * rows
        total = len(tiles_all)
        total_pages = int(math.ceil(total / float(per_page))) if per_page > 0 else 0
        if total_pages == 0:
            grid.clear_widgets()
            self._set_page_label(screen_name, label_id, 0, 0)
            self.on_paged_container_resize(container, grid, cols, rows)
            return
        if page < 0:
            page = 0
        if page >= total_pages:
            page = total_pages - 1
        try:
            setattr(self, page_attr, page)
        except Exception:
            pass

        start = page * per_page
        end = min(start + per_page, total)
        grid.clear_widgets()
        # Добавляем срез плиток
        for tile in tiles_all[start:end]:
            try:
                grid.add_widget(tile)
            except Exception:
                continue
        # Спец-режим для синей окраски выданных (экран просмотра)
        try:
            setattr(grid, "_issued_view", bool(issued_view))
        except Exception:
            pass

        # Пересчёт размеров и цвета
        self.on_paged_container_resize(container, grid, cols, rows)
        Clock.schedule_once(lambda dt: self.on_paged_container_resize(container, grid, cols, rows), 0)
        self._set_page_label(screen_name, label_id, page, total_pages)

    def _show_issued_page(self) -> None:
        self._show_page("issued", "issued_keys_container", "issued_keys_grid", "issued_page_label", self._issued_tiles_all, "_issued_page", 5, 3, issued_view=True)

    def _show_take_page(self) -> None:
        self._show_page("take", "take_keys_container", "take_keys_grid", "take_page_label", self._take_tiles_all, "_take_page", 5, 3, issued_view=False)

    def _show_return_page(self) -> None:
        self._show_page("return", "return_keys_container", "return_keys_grid", "return_page_label", self._return_tiles_all, "_return_page", 5, 3, issued_view=False)

    def _rebuild_take_data_and_refresh(self) -> None:
        """Переcтроить список тайлов для экрана взятия (учитывая выдачи) и обновить страницу."""
        try:
            from kivy.factory import Factory
            with self._SessionLocal() as session:
                allowed_ids = set()
                if getattr(self, "_current_user", None) and self._current_user.login != "admin":
                    allowed_ids = set(
                        session.execute(select(UserKey.key_id).where(UserKey.user_id == self._current_user.id)).scalars().all()
                    )
                issued_ids = set(session.execute(select(IssuedKey.key_id)).scalars().all())
                q = select(Key).order_by(Key.description.asc(), Key.code.asc())
                if self.current_box_id is not None:
                    q = q.where(Key.box_id == self.current_box_id)
                keys_rows = session.execute(q).scalars().all()
                keys_rows = [k for k in keys_rows if (k.id in allowed_ids and k.id not in issued_ids)]
            try:
                print(f"[TAKE] rebuild tiles: allowed={len(allowed_ids)} issued={len(issued_ids)} visible={len(keys_rows)}")
            except Exception:
                pass
            all_tiles = []
            for key_row in keys_rows:
                try:
                    tile = Factory.UserKeyTile()
                    tile.key_code = key_row.code
                    tile.room_name = key_row.description or key_row.code
                    try:
                        tile.status_text = 'допуск есть'
                        tile.sub_status_text = ''
                    except Exception:
                        pass
                    all_tiles.append(tile)
                except Exception:
                    continue
            self._take_tiles_master = all_tiles
            self._take_tiles_all = self._apply_filter_to_tiles('take', all_tiles)
            # Обновить текущую страницу
            self._show_take_page()
        except Exception:
            pass

    def _rebuild_return_data_and_refresh(self) -> None:
        """Переcтроить список тайлов для экрана сдачи (учитывая возвраты) и обновить страницу."""
        try:
            from kivy.factory import Factory
            with self._SessionLocal() as session:
                issued_key_ids = set()
                if getattr(self, "_current_user", None):
                    issued_key_ids = set(
                        session.execute(
                            select(IssuedKey.key_id).where(IssuedKey.user_id == self._current_user.id)
                        ).scalars().all()
                    )
                q = select(Key).order_by(Key.description.asc(), Key.code.asc())
                if self.current_box_id is not None:
                    q = q.where(Key.box_id == self.current_box_id)
                keys_rows = session.execute(q).scalars().all()
                keys_rows = [k for k in keys_rows if k.id in issued_key_ids]
            try:
                print(f"[RETURN] rebuild tiles: issued_by_user={len(issued_key_ids)} visible={len(keys_rows)}")
            except Exception:
                pass
            all_tiles = []
            for key_row in keys_rows:
                try:
                    tile = Factory.UserKeyTile()
                    tile.key_code = key_row.code
                    tile.room_name = key_row.description or key_row.code
                    try:
                        tile.status_text = 'ключ выдан'
                        tile.sub_status_text = ''
                    except Exception:
                        pass
                    all_tiles.append(tile)
                except Exception:
                    continue
            self._return_tiles_master = all_tiles
            self._return_tiles_all = self._apply_filter_to_tiles('return', all_tiles)
            # Обновить текущую страницу
            self._show_return_page()
        except Exception:
            pass

    # Кнопки пагинации
    def issued_next_page(self) -> None:
        try:
            per = 15
            total_pages = int(math.ceil(len(self._issued_tiles_all) / float(per))) if per else 0
            if total_pages > 0 and self._issued_page < total_pages - 1:
                self._issued_page += 1
            self._show_issued_page()
        except Exception:
            pass

    def issued_prev_page(self) -> None:
        try:
            if self._issued_page > 0:
                self._issued_page -= 1
            self._show_issued_page()
        except Exception:
            pass

    def on_issued_box_changed(self, box_name: str) -> None:
        """Admin-only: change selected box for issued-keys view and rebuild list."""
        try:
            name_to_id = getattr(self, '_issued_name_to_id', {}) or {}
            self._issued_selected_box_id = name_to_id.get(box_name)
            # Re-open the view to refresh with new box filter
            self.view_issued_keys()
        except Exception:
            pass

    def go_back_from_issued(self) -> None:
        """Return from issued-keys view to appropriate home screen.

        - In user app (`root_user.kv`), go to "guest" if not authenticated, "main" if authenticated.
        - In admin app (`root_admin.kv`), go to "admin_menu".
        """
        try:
            app = App.get_running_app()
            sm = getattr(app, 'root', None)
            target = "main"
            try:
                # If admin screens are present, prefer admin menu
                if sm and hasattr(sm, 'has_screen') and sm.has_screen("admin_menu"):
                    target = "admin_menu"
                else:
                    # In user app: check authentication status
                    is_authenticated = getattr(self, '_is_authenticated', False)
                    has_current_user = getattr(self, '_current_user', None) is not None
                    if not is_authenticated and not has_current_user:
                        # Not authenticated: go to guest screen if available
                        if sm and hasattr(sm, 'has_screen') and sm.has_screen("guest"):
                            target = "guest"
                        else:
                            target = "main"
                    else:
                        # Authenticated: go to main screen
                        target = "main"
            except Exception:
                pass
            if sm is not None:
                sm.current = target
        except Exception:
            pass

    def take_next_page(self) -> None:
        try:
            if getattr(self, '_blocked_due_to_mismatch', False):
                return
            per = 15
            total_pages = int(math.ceil(len(self._take_tiles_all) / float(per))) if per else 0
            if total_pages > 0 and self._take_page < total_pages - 1:
                self._take_page += 1
            self._show_take_page()
        except Exception:
            pass

    def take_prev_page(self) -> None:
        try:
            if getattr(self, '_blocked_due_to_mismatch', False):
                return
            if self._take_page > 0:
                self._take_page -= 1
            self._show_take_page()
        except Exception:
            pass

    def return_next_page(self) -> None:
        try:
            if getattr(self, '_blocked_due_to_mismatch', False):
                return
            per = 15
            total_pages = int(math.ceil(len(self._return_tiles_all) / float(per))) if per else 0
            if total_pages > 0 and self._return_page < total_pages - 1:
                self._return_page += 1
            self._show_return_page()
        except Exception:
            pass

    def return_prev_page(self) -> None:
        try:
            if getattr(self, '_blocked_due_to_mismatch', False):
                return
            if self._return_page > 0:
                self._return_page -= 1
            self._show_return_page()
        except Exception:
            pass
        
    def on_admin_container_resize(self, container, grid) -> None:
        # Use the inner card width (minus padding) to keep the grid within bounds
        available_w = max(0, float(getattr(container, "width", 0)))
        try:
            from kivy.metrics import dp
            app = App.get_running_app()
            screen = app.root.get_screen("admin_permissions")
            card = screen.ids.get("admin_perm_card") if hasattr(screen, "ids") else None
            if card is not None:
                available_w = max(0.0, float(card.width) - float(dp(32)))
        except Exception:
            pass

        total_items = len(grid.children)
        if total_items == 0:
            return

        # Админ-сетка: адаптивная разметка под карточки PermissionTile
        from kivy.metrics import dp
        spacing = float(dp(16))
        # желаемые границы размеров карточки
        min_w = float(dp(240))
        max_w = float(dp(300))
        min_h = float(dp(110))
        max_h = float(dp(140))
        aspect = min_h / min_w if min_w else 1.0

        # Учтём внутренние отступы грида, чтобы плитки и паддинги целиком умещались
        pad_left = pad_right = pad_top = pad_bottom = 0.0
        try:
            padding = getattr(grid, "padding", 0)
            if isinstance(padding, (list, tuple)):
                if len(padding) == 4:
                    pad_left, pad_top, pad_right, pad_bottom = [float(p) for p in padding]
                elif len(padding) == 2:
                    pad_left = pad_right = float(padding[0])
                    pad_top = pad_bottom = float(padding[1])
                else:
                    pad_left = pad_right = pad_top = pad_bottom = float(padding[0]) if padding else 0.0
            else:
                v = float(padding or 0)
                pad_left = pad_right = pad_top = pad_bottom = v
        except Exception:
            pass
        pad_h = float(pad_left + pad_right)
        pad_v = float(pad_top + pad_bottom)

        # Подбираем число колонок так, чтобы ширина карточки попадала в [min_w, max_w]
        # и использовала доступную ширину максимально эффективно
        max_cols_possible = max(1, int(((available_w - pad_h) + spacing) // (min_w + spacing)))
        best_cols = 1
        best_waste = float('inf')
        for c in range(1, max(1, max_cols_possible) + 1):
            free_w = max(0.0, available_w - pad_h - spacing * (c - 1))
            if free_w <= 0:
                continue
            cw = free_w / float(c)
            cw_clamped = max(min(cw, max_w), min_w)
            # оценим «потерянную» ширину; чем меньше, тем лучше
            waste = abs(cw - cw_clamped) + max(0.0, (available_w - (cw_clamped * c + spacing * (c - 1))))
            if waste < best_waste:
                best_waste = waste
                best_cols = c

        cols = max(1, best_cols)
        grid.cols = cols
        grid.spacing = (spacing, spacing)

        free_w = max(0.0, available_w - pad_h - spacing * (cols - 1) - dp(2))
        raw_cell_w = min_w if free_w <= 0 else free_w / float(cols)
        cell_w = max(min(raw_cell_w, max_w), min_w)
        cell_h = max(min(cell_w * aspect, max_h), min_h)

        for child in grid.children:
            child.size_hint = (None, None)
            child.size = (cell_w, cell_h)

        rows = int(math.ceil(total_items / float(cols)))
        grid.minimum_width = cols * cell_w + (cols - 1) * spacing + pad_h
        grid.minimum_height = rows * cell_h + (rows - 1) * spacing + pad_v

        # Центрируем содержимое ScrollView по горизонтали и вертикали
        try:
            app = App.get_running_app()
            screen = app.root.get_screen("admin_permissions")
            scroll = screen.ids.get("admin_keys_scroll") if hasattr(screen, "ids") else None
            anchor = screen.ids.get("admin_keys_container") if hasattr(screen, "ids") else None
            if scroll is not None and anchor is not None:
                # Держим сетку выровненной слева и сверху, как в макете
                scroll.scroll_x = 0
                scroll.scroll_y = 1
        except Exception:
            pass

    def _refresh_key_colors(self, grid) -> None:
        """Устанавливает цвет каждой ячейки: доступ/недоступ/выдан.

        Для фильтрованных экранов ('take' / 'return') применяются специальные правила окраски:
        - take: зелёный, если доступен; синий, если уже выдан текущему пользователю
        - return: синий, если выдан текущему; иначе зелёный, если доступ есть, красный при отсутствии допуска
        """
        try:
            # Специальный режим: экран просмотра выданных ключей — всегда синий и с подписью пользователя
            is_issued_view = bool(getattr(grid, "_issued_view", False))
            with self._SessionLocal() as session:
                # Карта выданных ключей: key_id -> (user_id, user_login, issued_at)
                issued_rows = (
                    session.execute(
                        select(IssuedKey.key_id, User.id, User.login, IssuedKey.issued_at)
                        .join(User, User.id == IssuedKey.user_id)
                    ).all()
                )
                issued_map = {key_id: (user_id, user_login, issued_at) for key_id, user_id, user_login, issued_at in issued_rows}

                allowed_set = set()
                if getattr(self, "_current_user", None) and self._current_user.login != "admin":
                    uk_rows = (
                        session.execute(
                            select(UserKey.key_id).where(UserKey.user_id == self._current_user.id)
                        )
                        .scalars()
                        .all()
                    )
                    allowed_set = set(uk_rows)

                key_rows = session.execute(select(Key)).scalars().all()
                code_to_id = {k.code: k.id for k in key_rows}

                # Определим контекст фильтрации
                is_take_grid = False
                is_return_grid = False
                try:
                    is_take_grid = getattr(self, '_active_take_grid', None) is grid and getattr(self, '_active_filtered', None) == 'take'
                    is_return_grid = getattr(self, '_active_return_grid', None) is grid and getattr(self, '_active_filtered', None) == 'return'
                except Exception:
                    pass

                for child in grid.children:
                    code = getattr(child, "key_code", None)
                    if not code:
                        continue
                    key_id = code_to_id.get(code)
                    if key_id is None:
                        continue

                    issued_info = issued_map.get(key_id)
                    # Tiles may be Button-based (KeyPlaceholder) or MDCard-based (PermissionTile)
                    def set_tile_color(widget, rgba):
                        try:
                            # PermissionTile uses md_bg_color
                            if hasattr(widget, 'md_bg_color'):
                                widget.md_bg_color = rgba
                            else:
                                widget.background_color = rgba
                        except Exception:
                            pass

                    # Режим просмотра выданных: всегда синий + текст с пользователем
                    if is_issued_view:
                        set_tile_color(child, (0.1, 0.4, 1.0, 1.0))
                        try:
                            if issued_info:
                                _uid, issued_login, issued_at = issued_info
                                ts = format_local_datetime(issued_at)
                                if hasattr(child, 'status_text'):
                                    child.status_text = f"выдан: {issued_login}"
                                if hasattr(child, 'sub_status_text'):
                                    child.sub_status_text = ts
                        except Exception:
                            pass
                        # Не применяем остальную логику окраски в этом режиме
                        continue

                    if issued_info:
                        issued_to_user_id, issued_login, issued_at = issued_info
                        is_self = getattr(self, "_current_user", None) and issued_to_user_id == self._current_user.id
                        # Для фильтрованных экранов: синий, если выдан текущему; иначе красный
                        if is_take_grid or is_return_grid:
                            set_tile_color(child, (0.1, 0.4, 1.0, 1.0) if is_self else (0.9, 0.2, 0.2, 1.0))
                        else:
                            set_tile_color(child, (0.1, 0.4, 1.0, 1.0) if is_self else (0.9, 0.2, 0.2, 1.0))
                        try:
                            if hasattr(child, 'status_text'):
                                child.status_text = 'ключ выдан' if is_self else 'нет допуска'
                        except Exception:
                            pass
                        try:
                            if hasattr(child, 'issued_to'):
                                child.issued_to = issued_login or ""
                            if hasattr(child, 'issued_at'):
                                child.issued_at = format_local_datetime(issued_at)
                        except Exception:
                            pass
                        continue

                    # Для пользователя 'admin' выдача запрещена, поэтому считаем, что доступа нет
                    if is_take_grid:
                        # На экране взятия по умолчанию — зелёный (плитки уже отфильтрованы),
                        # но если вдруг допуск потерян — отразим красным
                        if key_id in allowed_set:
                            set_tile_color(child, (0.0, 0.8, 0.3, 1.0))
                            try:
                                if hasattr(child, 'status_text'):
                                    child.status_text = 'допуск есть'
                            except Exception:
                                pass
                        else:
                            set_tile_color(child, (0.9, 0.2, 0.2, 1.0))
                            try:
                                if hasattr(child, 'status_text'):
                                    child.status_text = 'нет допуска'
                            except Exception:
                                pass
                    elif is_return_grid:
                        # На экране сдачи: если почему-то больше не выдан — покажем зелёным при наличии допуска
                        if key_id in allowed_set:
                            set_tile_color(child, (0.0, 0.8, 0.3, 1.0))
                            try:
                                if hasattr(child, 'status_text'):
                                    child.status_text = 'допуск есть'
                            except Exception:
                                pass
                        else:
                            set_tile_color(child, (0.9, 0.2, 0.2, 1.0))
                            try:
                                if hasattr(child, 'status_text'):
                                    child.status_text = 'нет допуска'
                            except Exception:
                                pass
                    else:
                        if key_id in allowed_set:
                            set_tile_color(child, (0.0, 0.8, 0.3, 1.0))
                            try:
                                if hasattr(child, 'status_text'):
                                    child.status_text = 'допуск есть'
                                if hasattr(child, 'issued_to'):
                                    child.issued_to = ""
                                if hasattr(child, 'issued_at'):
                                    child.issued_at = ""
                            except Exception:
                                pass
                        else:
                            set_tile_color(child, (0.9, 0.2, 0.2, 1.0))
                            try:
                                if hasattr(child, 'status_text'):
                                    child.status_text = 'нет допуска'
                                if hasattr(child, 'issued_to'):
                                    child.issued_to = "нет доступа"
                                if hasattr(child, 'issued_at'):
                                    child.issued_at = ""
                            except Exception:
                                pass
        except Exception:
            pass

    def _apply_filter_to_tiles(self, screen_name: str, tiles: list) -> list:
        """Фильтрация плиток по строке поиска. Поддерживаются токены через пробел.

        Примеры:
        - "a12" — подстрока в названии/коде
        - "user:ivan" — только для экрана выданных: логин пользователя содержит ivan
        - несколько токенов объединяются И (AND)
        """
        try:
            query = (self._filter_text_map.get(screen_name, "") or "").strip().lower()
            if not query:
                return list(tiles)
            tokens = [t for t in query.split() if t]
            if not tokens:
                return list(tiles)

            def matches(tile) -> bool:
                try:
                    room_name = (getattr(tile, 'room_name', '') or '').lower()
                    key_code = (getattr(tile, 'key_code', '') or '').lower()
                    status = (getattr(tile, 'status_text', '') or '').lower()
                    # Выданные: статус включает "выдан: <login>"
                    for tok in tokens:
                        if tok.startswith('user:'):
                            # фильтрация по логину пользователя
                            val = tok[5:]
                            if not val:
                                return False
                            if 'выдан' in status:
                                if val not in status:
                                    return False
                            else:
                                return False
                        else:
                            # общий текстовый поиск по названию/коду/статусу
                            if (tok not in room_name) and (tok not in key_code) and (tok not in status):
                                return False
                    return True
                except Exception:
                    return False

            return [t for t in tiles if matches(t)]
        except Exception:
            return list(tiles)

    def on_filter_text(self, screen_name: str, text: str) -> None:
        """Обработчик изменения строки фильтра: пересобирает отфильтрованный список и сбрасывает страницу."""
        try:
            self._filter_text_map[screen_name] = text or ''
        except Exception:
            pass

        try:
            if screen_name == 'take':
                self._take_tiles_all = self._apply_filter_to_tiles('take', self._take_tiles_master)
                self._take_page = 0
                self._show_take_page()
            elif screen_name == 'return':
                self._return_tiles_all = self._apply_filter_to_tiles('return', self._return_tiles_master)
                self._return_page = 0
                self._show_return_page()
            elif screen_name == 'issued':
                self._issued_tiles_all = self._apply_filter_to_tiles('issued', self._issued_tiles_master)
                self._issued_page = 0
                self._show_issued_page()
        except Exception:
            pass

    def initialize_keys_grid(self, root=None) -> None:
        """Присваивает плейсхолдерам ключей коды из БД и обновляет цвета.

        Если передан root (корневой ScreenManager), используется он. Иначе берётся текущий app.root.
        Отображаемые названия помещений берём из description (если есть), иначе используем code.
        """
        try:
            # Получаем ссылку на ScreenManager
            screen_manager = None
            if root is not None:
                screen_manager = root
            else:
                app = App.get_running_app()
                screen_manager = getattr(app, "root", None)
            if screen_manager is None:
                return

            # Ищем главный экран и грид
            main_screen = screen_manager.get_screen("main")
            if not hasattr(main_screen, "ids"):
                return
            keys_grid = main_screen.ids.get("keys_grid")
            if keys_grid is None:
                return

            # Собираем список плейсхолдеров в порядке k_0, k_1, ...
            placeholders_ordered = []
            try:
                id_items = []
                for name, widget in main_screen.ids.items():
                    if isinstance(name, str) and name.startswith("k_"):
                        try:
                            idx = int(name.split("_")[1])
                        except Exception:
                            continue
                        id_items.append((idx, widget))
                id_items.sort(key=lambda pair: pair[0])
                placeholders_ordered = [w for _, w in id_items]
            except Exception:
                # Fallback: дети грида, перевёрнутые (в Kivy дети добавляются в начало)
                try:
                    placeholders_ordered = list(reversed(keys_grid.children))
                except Exception:
                    placeholders_ordered = []

            if not placeholders_ordered:
                return

            # Достаём ключи из БД в стабильном порядке
            with self._SessionLocal() as session:
                q = select(Key).order_by(Key.description.asc(), Key.code.asc())
                if self.current_box_id is not None:
                    q = q.where(Key.box_id == self.current_box_id)
                keys_rows = session.execute(q).scalars().all()

            assign_count = min(len(placeholders_ordered), len(keys_rows))
            for idx in range(assign_count):
                placeholder = placeholders_ordered[idx]
                key_row = keys_rows[idx]
                try:
                    placeholder.key_code = getattr(key_row, "code", "") or ""
                    desc = getattr(key_row, "description", None)
                    if desc:
                        placeholder.room_name = desc
                    elif not getattr(placeholder, "room_name", None):
                        placeholder.room_name = key_row.code
                except Exception:
                    # Продолжаем проставлять оставшиеся
                    pass

            # Удаляем пустые плитки (без соответствующего ключа) из грида, чтобы не отображались
            try:
                for idx in range(assign_count, len(placeholders_ordered)):
                    ph = placeholders_ordered[idx]
                    if ph in keys_grid.children:
                        keys_grid.remove_widget(ph)
            except Exception:
                pass

            # Обновим цвета сразу после привязки
            self._refresh_key_colors(keys_grid)
        except Exception:
            # Тихий фэйл, чтобы не ронять приложение при старте
            pass

    # Обработчик нажатия на кнопку "Войти"
    def on_auth_submit(self, login: str, password: str) -> None:
        print(f"Auth submit: login={login!r}, password=***")
        # Реальная проверка логина/пароля через БД
        app = App.get_running_app()
        # Очистим текст ошибки перед попыткой
        try:
            if hasattr(app, "root"):
                screen = app.root.get_screen("auth")
                if hasattr(screen, "ids") and "auth_error" in screen.ids:
                    screen.ids["auth_error"].text = ""
        except Exception:
            pass

        try:
            if self._auth_service is None:
                with self._SessionLocal() as session:
                    self._auth_service = AuthService(session)
                    user = self._auth_service.authenticate(login, password)
            else:
                # Создаём временную сессию для каждой проверки
                with self._SessionLocal() as session:
                    svc = AuthService(session)
                    user = svc.authenticate(login, password)
        except Exception as exc:
            # Ошибка соединения/запроса к БД
            try:
                if hasattr(app, "root"):
                    # сначала пытаемся показать на PIN-экране
                    try:
                        scrp = app.root.get_screen("auth_pin")
                        if hasattr(scrp, "ids") and "auth_error_pin" in scrp.ids:
                            scrp.ids["auth_error_pin"].text = f"Ошибка БД: {exc}"
                    except Exception:
                        pass
                    # затем на экране логина
                    try:
                        scrl = app.root.get_screen("auth_login")
                        if hasattr(scrl, "ids") and "auth_error_login" in scrl.ids:
                            scrl.ids["auth_error_login"].text = f"Ошибка БД: {exc}"
                    except Exception:
                        pass
            except Exception:
                pass
            return

        if user:
            # Сохраняем текущего пользователя
            self._current_user = user
            try:
                self._last_login = getattr(user, 'login', None)
            except Exception:
                pass
            try:
                app.root.current = "main"
                # Переключить панели кнопок в состояние после авторизации
                try:
                    screen = app.root.get_screen("main")
                    if hasattr(screen, "ids"):
                        try:
                            title = screen.ids.get("main_title")
                            if title is not None:
                                title.text = getattr(user, 'login', '') or 'Ключница'
                        except Exception:
                            pass
                        pre = screen.ids.get("pre_auth_bar")
                        post = screen.ids.get("post_auth_bar")
                        # Скрыть кнопку админки, если вошёл не admin
                        try:
                            admin_btn = post.ids.get('btn_admin_post') if hasattr(post, 'ids') else None
                            if admin_btn is not None:
                                admin_btn.disabled = (user.login != 'admin')
                                admin_btn.opacity = 1 if user.login == 'admin' else 0
                                admin_btn.size_hint_x = 1 if user.login == 'admin' else None
                                if user.login != 'admin':
                                    admin_btn.width = 0
                        except Exception:
                            pass
                        if pre and post:
                            pre.size_hint_x = None
                            pre.width = 0
                            pre.disabled = True
                            pre.opacity = 0
                            post.size_hint_x = 1
                            post.disabled = False
                            # Автоматически подстроить ширину
                            try:
                                post.width = post.minimum_width
                            except Exception:
                                pass
                            post.opacity = 1
                            # Обновить цвета ячеек после входа
                            try:
                                keys_grid = screen.ids.get("keys_grid")
                                if keys_grid:
                                    self._refresh_key_colors(keys_grid)
                            except Exception:
                                pass
                except Exception:
                    pass
                # Очистить ошибки, если были
                try:
                    for scr_name, err_id in (("auth_login", "auth_error_login"), ("auth_pin", "auth_error_pin")):
                        try:
                            scr = app.root.get_screen(scr_name)
                            if hasattr(scr, "ids") and err_id in scr.ids:
                                scr.ids[err_id].text = ""
                        except Exception:
                            pass
                except Exception:
                    pass
            except Exception:
                pass
        else:
            # В будущем: показать ошибку авторизации в UI
            print("Неверный логин или пароль")
            try:
                if hasattr(app, "root"):
                    # выводим ошибку на активном auth-экране
                    for scr_name, err_id in (("auth_pin", "auth_error_pin"), ("auth_login", "auth_error_login")):
                        try:
                            scr = app.root.get_screen(scr_name)
                            if hasattr(scr, "ids") and err_id in scr.ids:
                                scr.ids[err_id].text = "Неверный логин или пароль"
                                break
                        except Exception:
                            continue
            except Exception:
                pass

__all__ = ["AppController"]


