"""Journal d'événements affiché dans l'interface."""

import collections
import threading
import time

from .. import config


class Journal:
    def __init__(self, entries=(), size=config.LOG_SIZE):
        self._lock = threading.Lock()
        self._entries = collections.deque(entries, maxlen=size)
        self._throttle = {}  # clé -> dernier timestamp écrit

    def log(self, msg, level="info"):
        with self._lock:
            now = time.strftime("%H:%M:%S")
            last = self._entries[-1] if self._entries else None
            if last and last["msg"] == msg:  # fusionne les répétitions
                last["time"] = now
                last["n"] = last.get("n", 1) + 1
                return
            self._entries.append({"time": now, "msg": msg, "level": level})

    def log_throttled(self, key, msg, level="info", every=1800):
        """N'écrit `msg` que si la même clé n'a rien écrit depuis `every` s (anti-spam)."""
        now = time.time()
        with self._lock:
            last = self._throttle.get(key, 0)
            if now - last < every:
                return False
            self._throttle[key] = now
        self.log(msg, level)
        return True

    def entries(self):
        with self._lock:
            return list(self._entries)
