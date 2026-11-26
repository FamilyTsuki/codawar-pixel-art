"""Journal d'événements affiché dans l'interface.

Chaque entrée peut viser un public (`to`) : des étiquettes de compte Codawar
("a:<account_key>") ou de compte du studio ("u:<uid>"). Une entrée sans public est
visible par tous (démarrage du serveur, erreurs générales) ; les autres ne sont
montrées qu'aux comptes concernés.
"""

import collections
import threading
import time

from .. import config
from .wallet import account_key


def audience(cookies=(), uids=()):
    """Étiquettes de public pour des cookies Codawar et/ou des comptes du studio."""
    tags = {f"a:{account_key(c)}" for c in cookies if c}
    tags |= {f"u:{u}" for u in uids if u}
    return tuple(sorted(tags))


class Journal:
    def __init__(self, entries=(), size=config.LOG_SIZE):
        self._lock = threading.Lock()
        self._entries = collections.deque(entries, maxlen=size)
        self._throttle = {}  # clé -> dernier timestamp écrit

    def log(self, msg, level="info", to=()):
        to = list(to)
        with self._lock:
            now = time.strftime("%H:%M:%S")
            last = self._entries[-1] if self._entries else None
            if last and last["msg"] == msg and last.get("to", []) == to:  # fusionne les répétitions
                last["time"] = now
                last["n"] = last.get("n", 1) + 1
                return
            entry = {"time": now, "msg": msg, "level": level}
            if to:
                entry["to"] = to
            self._entries.append(entry)

    def log_throttled(self, key, msg, level="info", every=1800, to=()):
        """N'écrit `msg` que si la même clé n'a rien écrit depuis `every` s (anti-spam)."""
        now = time.time()
        key = (key, tuple(to))  # un compte ne fait pas taire le même message d'un autre
        with self._lock:
            last = self._throttle.get(key, 0)
            if now - last < every:
                return False
            self._throttle[key] = now
        self.log(msg, level, to)
        return True

    def entries(self, tags=None):
        """Toutes les entrées (tags=None), ou celles visibles pour ces étiquettes."""
        with self._lock:
            if tags is None:
                return list(self._entries)
            tags = set(tags)
            return [{k: v for k, v in e.items() if k != "to"} for e in self._entries
                    if not e.get("to") or tags.intersection(e["to"])]
