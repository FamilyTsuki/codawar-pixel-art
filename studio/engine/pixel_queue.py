"""File d'attente des pixels à poser.

Clé : (canvas, col, row) ; valeur : (couleur, source, cookie). Une case n'apparaît qu'une fois.
"""

import collections
import threading
import time

from .. import config

SOURCE_MANUAL = "manual"
SOURCE_LAYER = "layer"


class PixelQueue:
    def __init__(self):
        self._lock = threading.Lock()
        self._pending = collections.OrderedDict()
        self._failed = {}  # clé -> timestamp jusqu'auquel on ne réessaie pas

    def __len__(self):
        with self._lock:
            return len(self._pending)

    def enqueue(self, canvas, pixels, front=False, source=SOURCE_MANUAL, cookie=""):
        items = [((canvas, int(c), int(r)), (h.lower(), source, cookie)) for c, r, h in pixels]
        with self._lock:
            for key, val in (reversed(items) if front else items):
                self._failed.pop(key, None)
                self._pending[key] = val
                if front:
                    self._pending.move_to_end(key, last=False)
        return len(items)

    def add_if_absent(self, key, val, front):
        """Ajoute sans écraser une entrée existante."""
        with self._lock:
            if key not in self._pending:
                self._pending[key] = val
                self._pending.move_to_end(key, last=not front)

    def pop(self):
        with self._lock:
            return self._pending.popitem(last=False) if self._pending else None

    def drop_source(self, source, keep_canvases=()):
        """Retire les entrées d'une source, sauf celles des canvas de `keep_canvases`."""
        with self._lock:
            for k in [k for k, v in self._pending.items() if v[1] == source and k[0] not in keep_canvases]:
                del self._pending[k]

    def sync_source(self, canvas, source, entries):
        """Remplace d'un coup les entrées d'une source sur un canvas.

        entries : [(clé, valeur, en_tête)]. Les cases déjà en file gardent leur place (la file
        ne passe jamais par 0 pendant le recalcul) ; celles qui ne sont plus à poser sont retirées.
        """
        wanted = {k for k, _v, _f in entries}
        with self._lock:
            for k in [k for k, v in self._pending.items()
                      if k[0] == canvas and v[1] == source and k not in wanted]:
                del self._pending[k]
            front = []
            for key, val, at_front in entries:
                if key in self._pending:
                    if at_front:
                        front.append(key)
                    continue
                self._pending[key] = val
                if at_front:
                    front.append(key)
            for key in reversed(front):  # pixels attaqués en tête, dans l'ordre donné
                self._pending.move_to_end(key, last=False)

    def clear(self):
        with self._lock:
            n = len(self._pending)
            self._pending.clear()
            return n

    def mark_failed(self, key, delay=config.FAILED_RETRY_DELAY):
        with self._lock:
            self._failed[key] = time.time() + delay

    def is_failed(self, key, now):
        with self._lock:
            return self._failed.get(key, 0) >= now

    def count_for(self, canvas):
        with self._lock:
            return sum(1 for k in self._pending if k[0] == canvas)

    def pending_for(self, canvas, limit=config.STATUS_PENDING_LIMIT):
        with self._lock:
            return [[k[1], k[2], v[0]] for k, v in self._pending.items() if k[0] == canvas][:limit]
