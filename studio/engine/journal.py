"""Journal d'événements affiché dans l'interface."""

import collections
import threading
import time

from .. import config


class Journal:
    def __init__(self, entries=(), size=config.LOG_SIZE):
        self._lock = threading.Lock()
        self._entries = collections.deque(entries, maxlen=size)

    def log(self, msg, level="info"):
        with self._lock:
            now = time.strftime("%H:%M:%S")
            last = self._entries[-1] if self._entries else None
            if last and last["msg"] == msg:  # fusionne les répétitions
                last["time"] = now
                last["n"] = last.get("n", 1) + 1
                return
            self._entries.append({"time": now, "msg": msg, "level": level})

    def entries(self):
        with self._lock:
            return list(self._entries)
