"""ShadowScheduler — deterministic, exception-contained cycle runner (Phase 7).

Each cycle is wrapped so one failing cycle degrades the session but never crashes
the process. Designed to be driven either by an async loop or synchronously
(tests). It must not block the FastAPI event loop (uses asyncio.sleep).
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional


class ShadowScheduler:
    def __init__(self, on_error: Optional[Callable[[str, Exception], None]] = None):
        self.on_error = on_error
        self._tasks: list[tuple[str, int, Callable]] = []
        self._stop = False
        self.cycle_count = 0
        self.error_count = 0
        self.last_cycle_ts_ms: Optional[int] = None

    def register(self, name: str, interval_ms: int, fn: Callable) -> None:
        self._tasks.append((name, max(1, int(interval_ms)), fn))

    def run_cycle_safe(self, name: str, fn: Callable) -> tuple[bool, Optional[Exception]]:
        try:
            fn()
            self.cycle_count += 1
            self.last_cycle_ts_ms = int(time.time() * 1000)
            return True, None
        except Exception as e:  # noqa: BLE001 — contained; session degrades, no crash
            self.error_count += 1
            if self.on_error is not None:
                try:
                    self.on_error(name, e)
                except Exception:  # noqa: BLE001
                    pass
            return False, e

    def stop(self) -> None:
        self._stop = True

    async def run(self, *, max_runtime_s: Optional[float] = None) -> None:
        self._stop = False
        next_due = {name: 0.0 for name, _, _ in self._tasks}
        start = time.time()
        while not self._stop:
            now = time.time()
            if max_runtime_s and (now - start) >= max_runtime_s:
                break
            for name, interval_ms, fn in self._tasks:
                if now >= next_due[name]:
                    self.run_cycle_safe(name, fn)
                    next_due[name] = now + interval_ms / 1000.0
            await asyncio.sleep(0.05)
