"""Deterministic simulated clock for replay.

By default there are NO wall-clock sleeps — replay runs as fast as the CPU
allows and is fully deterministic. ``speed_multiplier > 0`` is only for
visual/demo playback (it sleeps to roughly pace events to real time).
"""

from __future__ import annotations

import time


class ReplayClock:
    def __init__(self, start_ms: int = 0, speed_multiplier: float = 0.0):
        self._now = int(start_ms)
        self.speed_multiplier = float(speed_multiplier)
        self.ticks = 0

    def now_ms(self) -> int:
        return self._now

    def advance_to(self, ts_ms: int) -> None:
        ts_ms = int(ts_ms)
        if ts_ms <= self._now:
            return
        if self.speed_multiplier > 0:  # demo-only pacing; off by default
            delta_s = (ts_ms - self._now) / 1000.0 / self.speed_multiplier
            if delta_s > 0:
                time.sleep(min(delta_s, 1.0))
        self._now = ts_ms

    def tick(self) -> int:
        self.ticks += 1
        return self._now
