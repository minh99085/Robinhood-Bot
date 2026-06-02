"""Append-only Grok lesson memory (the feedback loop).

After each paper trade closes, Grok extracts a short lesson; lessons are appended
to data/grok_memory.jsonl and the most recent ones are injected back into every
Grok system prompt as "learned patterns", so paper-trading reasoning improves
over time. Pure stdlib, thread-safe, bounded reads.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path


class GrokMemory:
    def __init__(self, data_dir: Path, filename: str = "grok_memory.jsonl"):
        self.path = Path(data_dir) / filename
        self._lock = threading.Lock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=True)
        except OSError:
            pass

    def append(self, lesson: str, meta: dict | None = None) -> None:
        lesson = (lesson or "").strip()
        if not lesson:
            return
        rec = {"ts": round(time.time(), 1), "lesson": lesson[:240], "meta": meta or {}}
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            try:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def recent(self, n: int = 10) -> list[dict]:
        try:
            with self._lock:
                lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for ln in lines[-n:]:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out

    def recent_text(self, n: int = 10) -> str:
        rows = self.recent(n)
        if not rows:
            return ""
        return "\n".join(f"- {r.get('lesson','')}" for r in rows if r.get("lesson"))

    def count(self) -> int:
        try:
            with self._lock:
                return sum(1 for _ in self.path.open("r", encoding="utf-8"))
        except OSError:
            return 0
