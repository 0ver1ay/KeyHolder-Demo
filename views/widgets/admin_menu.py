from __future__ import annotations

from kivy.metrics import dp
from kivy.properties import ColorProperty, NumericProperty, StringProperty
from kivymd.uix.card import MDCard
from kivymd.uix.gridlayout import MDGridLayout

TILE_COUNT = 13
HYSTERESIS_DP = 48


class MenuTile(MDCard):
    title = StringProperty("")
    subtitle = StringProperty("")
    icon_name = StringProperty("account-plus")
    normal_color = ColorProperty((0.22, 0.23, 0.24, 1))
    hover_color = ColorProperty((0.26, 0.27, 0.28, 1))

    __events__ = ("on_release",)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.md_bg_color = self.normal_color
        self.elevation = 3
        self.ripple_behavior = True

    def on_touch_up(self, touch):
        if self.collide_point(*touch.pos):
            self.dispatch("on_release")
            return True
        return super().on_touch_up(touch)

    def on_release(self):
        pass


class AdminMenuGrid(MDGridLayout):
    """Menu grid with stable column count (hysteresis) to avoid layout jumps."""

    rows_count = NumericProperty(5)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._stable_cols = 3
        self.cols = 3
        self.rows_count = self._rows_for_cols(3)
        self.bind(width=self._sync_cols)

    @staticmethod
    def _rows_for_cols(cols: int) -> int:
        return (TILE_COUNT + cols - 1) // cols

    def _pick_cols(self, width: float, current: int) -> int:
        t3 = dp(1040)
        t2 = dp(680)
        h = dp(HYSTERESIS_DP)

        if current == 3:
            if width < t3 - h:
                return 2 if width >= t2 else 1
            return 3
        if current == 2:
            if width >= t3 + h:
                return 3
            if width < t2 - h:
                return 1
            return 2
        if width >= t3 + h:
            return 3
        if width >= t2 + h:
            return 2
        return 1

    def _sync_cols(self, *_args) -> None:
        w = float(self.width or 0)
        if w <= 0:
            return
        new_cols = self._pick_cols(w, self._stable_cols)
        if new_cols == self._stable_cols:
            return
        self._stable_cols = new_cols
        self.cols = new_cols
        self.rows_count = self._rows_for_cols(new_cols)

    def on_kv_post(self, base_widget):
        super().on_kv_post(base_widget)
        self._sync_cols()
