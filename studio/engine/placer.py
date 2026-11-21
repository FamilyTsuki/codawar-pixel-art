"""Workers de pose : vident la file et interprètent les réponses de Codawar."""

import collections
import threading
import time

from .. import config
from ..schedule import within_schedule

PIXEL_UNCHANGED = "PIXEL_UNCHANGED"


def _has_hint(code, hints):
    low = code.lower()
    return any(h in low for h in hints)


class Placer:
    def __init__(self, client, queue, boards, wallet, defense, journal, settings):
        self.client = client
        self.queue = queue
        self.boards = boards
        self.wallet = wallet
        self.defense = defense
        self.journal = journal
        self.settings = settings  # callable -> dict des réglages
        self._lock = threading.Lock()
        self.paused = False
        self.backoff_until = 0
        self.inflight = 0
        self.placed = self.skipped = self.errors = 0
        self.last_reject = None  # dernier refus/erreur : {code, message, t}
        self._recent = collections.deque()  # timestamps de poses (débit)

    # -- état
    def set_paused(self, paused):
        self.paused = paused
        self.journal.log("Pause" if paused else "Reprise", "warning" if paused else "info")

    def backoff_remaining(self):
        return max(0, round(self.backoff_until - time.time()))

    def can_post(self):
        return not self.paused and time.time() >= self.backoff_until and within_schedule(self.settings())

    def rate(self):
        """Poses par seconde sur la fenêtre récente."""
        with self._lock:
            now = time.time()
            while self._recent and now - self._recent[0] > config.RATE_WINDOW:
                self._recent.popleft()
            return round(len(self._recent) / config.RATE_WINDOW, 1)

    def counters(self):
        return {"placed": self.placed, "skipped": self.skipped, "errors": self.errors}

    def restore_counters(self, runtime):
        self.placed = runtime.get("placed", 0)
        self.skipped = runtime.get("skipped", 0)
        self.errors = runtime.get("errors", 0)

    # -- boucle d'un worker
    def _active_workers(self):
        s = self.settings()
        active = int(s.get("workers", 1))
        if self.defense.active():  # mode défense : plus de requêtes en parallèle
            active = max(active, int(s.get("defendWorkers", 8)))
        return active

    def _take(self):
        if not self.can_post():
            return None
        item = self.queue.pop()
        if item is not None:
            with self._lock:
                self.inflight += 1
        return item

    def worker(self, idx):
        while True:
            if idx >= self._active_workers():
                time.sleep(0.3)
                continue
            item = self._take()
            if item is None:
                time.sleep(0.05)
                continue
            key, val = item
            try:
                self._place(key, val)
            finally:
                with self._lock:
                    self.inflight -= 1
            delay = float(self.settings().get("delay", 0) or 0)
            if delay and not self.defense.active():  # pas de pause en mode défense
                time.sleep(delay)

    # -- une pose
    def _place(self, key, val):
        canvas, col, row = key
        color, _source, cookie = val
        res = self.client.call("onPlacePixel", [canvas, col, row, color], cookie, canvas)
        status, code = res.get("status"), (res.get("code") or "")
        if status == "success":
            self._on_success(key, color, res)
        elif status == "rejected" and code == PIXEL_UNCHANGED:
            with self._lock:
                self.skipped += 1
        elif status != "rejected" or _has_hint(code, config.RETRY_LATER_HINTS):
            self._on_retry_later(key, val, status, code, res)
        else:
            self._on_refused(key, color, code, res)

    def _on_success(self, key, color, res):
        canvas, col, row = key
        self.wallet.set((res.get("value") or {}).get("wallet"))
        self.boards.record_placement(canvas, col, row, color)
        with self._lock:
            self.placed += 1
            self._recent.append(time.time())

    def _on_retry_later(self, key, val, status, code, res):
        """Erreur réseau, crédits épuisés, limite… : remet le pixel en tête et attend."""
        credit_short = _has_hint(code, config.CREDIT_SHORT_HINTS)
        wait = self.wallet.credit_wait() if credit_short else (10 if status == "rejected" else 2)
        self.queue.add_if_absent(key, val, front=True)
        with self._lock:
            first = time.time() >= self.backoff_until
            self.backoff_until = max(self.backoff_until, time.time() + wait)
            self.last_reject = {"code": code or "réseau", "message": res.get("message", ""), "t": time.time()}
        if first:
            # message fixe (sans chiffre) pour que le journal fusionne les répétitions
            self.journal.log("Crédits épuisés — en attente du rechargement" if credit_short
                             else f"Pause : {code or res.get('message')}", "warning")

    def _on_refused(self, key, color, code, res):
        """Refus définitif : on n'insiste pas sur cette case pendant un moment."""
        _canvas, col, row = key
        self.queue.mark_failed(key)
        with self._lock:
            self.errors += 1
            self.last_reject = {"code": code, "message": res.get("message", ""), "t": time.time()}
        self.journal.log(f"Refusé ({col}, {row}) {color} : {code} — {res.get('message', '')}", "error")
