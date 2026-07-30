from __future__ import annotations

from typing import Callable, Dict, Tuple, Optional
from kivy.clock import Clock


SlotXY = Tuple[int, int]
PresenceSnapshot = Dict[SlotXY, bool]


class SlotRfidPoller:
    """Интерфейс опросчика RFID слотов.

    Провайдер должен возвращать снимок присутствия по слотам:
    {(x, y): present_bool}. Опросчик сравнивает снимки и вызывает колбэк
    только для изменившихся слотов.
    """

    def __init__(
        self,
        provider: Callable[[], PresenceSnapshot],
        interval_sec: float = 0.5,
    ) -> None:
        self._provider = provider
        self._interval = float(interval_sec)
        self._on_change: Optional[Callable[[SlotXY, bool], None]] = None
        self._ev = None
        self._last: PresenceSnapshot = {}

    def set_on_presence_change(self, cb: Callable[[SlotXY, bool], None]) -> None:
        self._on_change = cb

    def start(self) -> None:
        if self._ev is not None:
            return
        self._ev = Clock.schedule_interval(self._tick, self._interval)

    def stop(self) -> None:
        if self._ev is None:
            return
        try:
            Clock.unschedule(self._ev)
        except Exception:
            pass
        self._ev = None

    def _tick(self, dt) -> None:
        try:
            current = self._provider() or {}
        except Exception:
            current = {}
        # Compare with last snapshot
        try:
            for xy, present in current.items():
                prev = self._last.get(xy)
                if prev is None or bool(prev) != bool(present):
                    if self._on_change is not None:
                        try:
                            self._on_change(xy, bool(present))
                        except Exception:
                            pass
            # Also handle slots that disappeared from snapshot: treat as present=False
            for xy in list(self._last.keys()):
                if xy not in current:
                    if self._on_change is not None:
                        try:
                            self._on_change(xy, False)
                        except Exception:
                            pass
            self._last = dict(current)
        except Exception:
            pass


class MockSlotRfidPoller(SlotRfidPoller):
    """Мок-реализация на базе таймера; использует переданный провайдер.

    По сути это просто именованный алиас базового опросчика.
    """

    pass


