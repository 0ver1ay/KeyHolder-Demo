from __future__ import annotations

from kivy.uix.button import Button
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.behaviors import ButtonBehavior
from kivy.uix.widget import Widget
from kivy.properties import ObjectProperty
from kivy.graphics.texture import Texture

from views.behaviors import HoverBehavior


class _GradientCache:
    _light_tex: Texture | None = None
    _dark_tex: Texture | None = None

    @staticmethod
    def _make_tex(base_int: int, light_int: int, alpha: int) -> Texture:
        height = 256
        tex = Texture.create(size=(1, height), colorfmt='rgba')
        tex.mag_filter = 'linear'
        tex.min_filter = 'linear'
        buf = bytearray(4 * height)
        base = base_int / 255.0
        light = light_int / 255.0
        for y in range(height):
            t = y / (height - 1)
            s = t * t * (3 - 2 * t)  # smoothstep
            s = 1.0 - s              # flip 180°
            v = base + (light - base) * s
            g = int(v * 255)
            idx = y * 4
            buf[idx + 0] = g
            buf[idx + 1] = g
            buf[idx + 2] = min(g + 2, 255)
            buf[idx + 3] = alpha
        tex.blit_buffer(bytes(buf), colorfmt='rgba', bufferfmt='ubyte')
        return tex

    @classmethod
    def get_light(cls) -> Texture:
        if cls._light_tex is None:
            # lighter gradient for buttons/tiles — +2 more steps overall and softer (reduced delta)
            cls._light_tex = cls._make_tex(0xA0, 0xE0, 246)
        return cls._light_tex

    @classmethod
    def get_dark(cls) -> Texture:
        if cls._dark_tex is None:
            # darker gradient for panels/containers (deeper black point for smoother look)
            cls._dark_tex = cls._make_tex(0x30, 0x85, 236)
        return cls._dark_tex


class LightGrayGradientMixin(Widget):
    grad_tex = ObjectProperty(None, rebind=True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        try:
            self.grad_tex = _GradientCache.get_light()
        except Exception:
            self.grad_tex = None


class DarkGrayGradientMixin(Widget):
    grad_tex = ObjectProperty(None, rebind=True)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        try:
            self.grad_tex = _GradientCache.get_dark()
        except Exception:
            self.grad_tex = None


class IOSGrayButton(LightGrayGradientMixin, HoverBehavior, Button):
    pass


class IOSFilledButton(LightGrayGradientMixin, HoverBehavior, Button):
    pass


class IOSOutlineButton(LightGrayGradientMixin, HoverBehavior, Button):
    pass


class IOSMenuButton(LightGrayGradientMixin, HoverBehavior, ButtonBehavior, BoxLayout):
    pass


class GlassPanel(DarkGrayGradientMixin, BoxLayout):
    pass


class IOSCardButton(LightGrayGradientMixin, HoverBehavior, ButtonBehavior, BoxLayout):
    pass


class IOSCardPanel(DarkGrayGradientMixin, BoxLayout):
    pass



