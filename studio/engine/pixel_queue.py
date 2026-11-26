"""File d'attente des pixels à poser.

Clé : (canvas, col, row) ; valeur : (couleur, source, cookie). Une case n'apparaît qu'une fois.

Les pixels sont rangés par « lot » = (canvas, cookie) : chaque compte a ses propres crédits
sur chaque canvas. Un lot en attente de crédits est sauté sans bloquer les autres, et les
lots sont servis à tour de rôle.
"""

import collections
import threading
import time

from .. import config

SOURCE_MANUAL = "manual"
SOURCE_LAYER = "layer"


def bucket_of(key, val):
    return (key[0], val[2])


class PixelQueue:
    def __init__(self):
        self._lock = threading.Lock()
        self._buckets = collections.OrderedDict()  # (canvas, cookie) -> OrderedDict(clé -> valeur)
        self._where = {}   # clé -> lot
        self._failed = {}  # clé -> timestamp jusqu'auquel on ne réessaie pas

    def __len__(self):
        with self._lock:
            return len(self._where)

    # -- opérations internes (verrou déjà pris)
    def _remove(self, key):
        b = self._where.pop(key, None)
        if b is None:
            return
        items = self._buckets.get(b)
        if items is not None:
            items.pop(key, None)
            if not items:
                del self._buckets[b]

    def _put(self, key, val, front):
        """Insère/remplace ; en tête si front, sinon garde la place d'une entrée du même lot."""
        b = bucket_of(key, val)
        if self._where.get(key) != b:
            self._remove(key)
        items = self._buckets.setdefault(b, collections.OrderedDict())
        items[key] = val
        self._where[key] = b
        if front:
            items.move_to_end(key, last=False)

    # -- API
    def enqueue(self, canvas, pixels, front=False, source=SOURCE_MANUAL, cookie=""):
        items = [((canvas, int(c), int(r)), (h.lower(), source, cookie)) for c, r, h in pixels]
        with self._lock:
            for key, val in (reversed(items) if front else items):
                self._failed.pop(key, None)
                self._put(key, val, front)
        return len(items)

    def add_if_absent(self, key, val, front):
        """Ajoute sans écraser une entrée existante."""
        with self._lock:
            if key not in self._where:
                self._put(key, val, front)

    def pop(self, blocked=None):
        """Prochain pixel d'un lot non bloqué (blocked(lot) -> bool), à tour de rôle entre lots."""
        with self._lock:
            for b in list(self._buckets):
                if blocked and blocked(b):
                    continue
                items = self._buckets[b]
                key, val = items.popitem(last=False)
                del self._where[key]
                if items:
                    self._buckets.move_to_end(b)  # tour de rôle
                else:
                    del self._buckets[b]
                return key, val
            return None

    def drop_source(self, source, keep_canvases=()):
        """Retire les entrées d'une source, sauf celles des canvas de `keep_canvases`."""
        with self._lock:
            for b, items in list(self._buckets.items()):
                if b[0] in keep_canvases:
                    continue
                for k in [k for k, v in items.items() if v[1] == source]:
                    self._remove(k)

    def sync_source(self, canvas, source, entries):
        """Remplace d'un coup les entrées d'une source sur un canvas.

        entries : [(clé, valeur, en_tête)]. Les cases déjà en file gardent leur place (la file
        ne passe jamais par 0 pendant le recalcul) ; celles qui ne sont plus à poser sont retirées.
        """
        wanted = {k for k, _v, _f in entries}
        with self._lock:
            for b, items in list(self._buckets.items()):
                if b[0] != canvas:
                    continue
                for k in [k for k, v in items.items() if v[1] == source and k not in wanted]:
                    self._remove(k)
            front = []
            for key, val, at_front in entries:
                if key not in self._where:
                    self._put(key, val, False)
                if at_front:
                    front.append(key)
            for key in reversed(front):  # pixels attaqués en tête de leur lot, dans l'ordre donné
                self._buckets[self._where[key]].move_to_end(key, last=False)

    def clear(self, cookies=None):
        """Vide la file, ou seulement les lots de ces cookies. Renvoie le nombre retiré."""
        with self._lock:
            if cookies is None:
                n = len(self._where)
                self._buckets.clear()
                self._where.clear()
                return n
            n = 0
            for b in [b for b in self._buckets if b[1] in cookies]:
                for k in list(self._buckets[b]):
                    self._where.pop(k, None)
                    n += 1
                del self._buckets[b]
            return n

    def mark_failed(self, key, delay=config.FAILED_RETRY_DELAY):
        with self._lock:
            self._failed[key] = time.time() + delay

    def is_failed(self, key, now):
        with self._lock:
            return self._failed.get(key, 0) >= now

    @staticmethod
    def _match(b, canvas, cookies):
        return (canvas is None or b[0] == canvas) and (cookies is None or b[1] in cookies)

    def count_for(self, canvas=None, cookies=None):
        """Pixels en file sur un canvas (None : tous) pour ces cookies (None : tous les comptes)."""
        with self._lock:
            return sum(len(items) for b, items in self._buckets.items() if self._match(b, canvas, cookies))

    def pending_for(self, canvas, cookies=None, limit=config.STATUS_PENDING_LIMIT):
        out = []
        with self._lock:
            for b, items in self._buckets.items():
                if not self._match(b, canvas, cookies):
                    continue
                for k, v in items.items():
                    if len(out) >= limit:
                        return out
                    out.append([k[1], k[2], v[0]])
        return out
