from __future__ import annotations

from kivy.core.window import Window
from kivy.properties import BooleanProperty


class HoverBehavior:
    """Reusable hover mixin for desktop.

    Adds a BooleanProperty `hovered` and dispatches `on_enter` / `on_leave` events.
    Safe to use with Kivy widgets and behaviors via multiple inheritance.
    """

    __events__ = ("on_enter", "on_leave")
    hovered = BooleanProperty(False)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        Window.bind(mouse_pos=self._on_mouse_pos)

    # Kivy will call this when widget is removed; unbind to avoid leaks
    def on_parent(self, instance, parent):  # type: ignore[override]
        if parent is None:
            try:
                Window.unbind(mouse_pos=self._on_mouse_pos)
            except Exception:
                pass

    def _on_mouse_pos(self, _window, pos):
        # Avoid processing when not in a window yet
        if not self.get_root_window():
            return
        # Convert to local coords and test collision
        is_inside = self.collide_point(*self.to_widget(*pos))
        if self.hovered == is_inside:
            return
        self.hovered = is_inside
        if is_inside:
            self.dispatch('on_enter')
        else:
            self.dispatch('on_leave')

    # Default handlers; users may override in subclasses or kv
    def on_enter(self, *args):  # pragma: no cover
        pass

    def on_leave(self, *args):  # pragma: no cover
        pass


