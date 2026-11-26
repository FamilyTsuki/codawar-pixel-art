"""Workers de pose : vident la file et interprètent les réponses de Codawar."""

import collections
import threading
import time

from .. import config
from ..schedule import within_schedule
from .pixel_queue import bucket_of

PIXEL_UNCHANGED = "PIXEL_UNCHANGED"
ALLY_OVERWRITE = "ALLY_OVERWRITE_CONFIRMATION_REQUIRED"
ACCOUNT_STOP = ("STUDENT_TIMED_OUT", "ACCOUNT_BANNED")
RATE_LIMITED = "PLACEMENT_RATE_LIMITED"


def _has_hint(code, hints):
    low = code.lower()
    return any(h in low for h in hints)


class Placer:
    def __init__(self, client, queue, boards, wallet, journal, settings):
        self.client = client
        self.queue = queue
        self.boards = boards
        self.wallet = wallet
        self.journal = journal
        self.settings = settings  # callable -> dict des réglages
        self._lock = threading.Lock()
        self.paused = False
        self._backoff = {}           # lot (canvas, cookie) -> timestamp de reprise
        self._inflight_by = collections.Counter()  # lot -> requêtes en cours
        self.inflight = 0
        self.placed = self.skipped = self.errors = 0
        self._reject_by_canvas = {}  # canvas -> dernier refus/erreur
        self._recent = collections.deque()  # (timestamp, canvas) des poses (débit)
        self._starts = {}  # cookie -> départ de la dernière pose (limite 3 px/s)
        self._account_pause = {}  # cookie -> timestamp de fin d'exclusion

    # -- état
    def set_paused(self, paused):
        self.paused = paused
        self.journal.log("Pause" if paused else "Reprise", "warning" if paused else "info")

    def backoff_remaining(self, canvas=None):
        """Attente restante (s) : la plus courte parmi les lots du canvas en attente."""
        now = time.time()
        with self._lock:
            waits = [u - now for (cv, _), u in self._backoff.items()
                     if u > now and (canvas is None or cv == canvas)]
        return max(0, round(min(waits))) if waits else 0

    def last_reject_for(self, canvas):
        with self._lock:
            return self._reject_by_canvas.get(canvas)

    def can_post(self):
        return not self.paused and within_schedule(self.settings())

    def _paced_out(self, cookie, now):
        """Vrai si la dernière pose de ce compte date de moins de PLACE_MIN_INTERVAL s."""
        return now - self._starts.get(cookie, 0) < config.PLACE_MIN_INTERVAL

    def _blocked(self, bucket):
        """Lot en attente (refus récent, exclusion, cadence max) ou sans crédit : on passe au suivant."""
        now = time.time()
        if self._backoff.get(bucket, 0) > now:
            return True
        if self._account_pause.get(bucket[1], 0) > now or self._paced_out(bucket[1], now):
            return True
        acc = self.wallet.account(bucket[0], bucket[1], now)
        if not acc or acc["unlimited"] or acc["credits"] is None:
            return False
        if acc["credits"] - self._inflight_by[bucket] > 0:
            return False
        # plus de crédit : on attend le prochain au lieu d'envoyer une requête refusée
        wait = acc["nextIn"] if acc["nextIn"] is not None else 30
        self._backoff[bucket] = now + max(1, wait + 1)
        return True

    def rate(self, canvas=None):
        """Poses par seconde sur la fenêtre récente (toutes ou celles d'un canvas)."""
        with self._lock:
            now = time.time()
            while self._recent and now - self._recent[0][0] > config.RATE_WINDOW:
                self._recent.popleft()
            n = sum(1 for _, cv in self._recent if canvas is None or cv == canvas)
            return round(n / config.RATE_WINDOW, 2)

    def counters(self):
        return {"placed": self.placed, "skipped": self.skipped, "errors": self.errors}

    def restore_counters(self, runtime):
        self.placed = runtime.get("placed", 0)
        self.skipped = runtime.get("skipped", 0)
        self.errors = runtime.get("errors", 0)

    # -- boucle d'un worker

    def _take(self):
        if not self.can_post():
            return None
        with self._lock:  # verrou tenu pendant le choix : le décompte des crédits en cours reste juste
            item = self.queue.pop(self._blocked)
            if item is not None:
                self.inflight += 1
                self._inflight_by[bucket_of(*item)] += 1
                self._starts[item[1][2]] = time.time()  # compte pour la limite de cadence
        return item

    def worker(self):
        while True:
            item = self._take()
            if item is None:
                time.sleep(0.2)
                continue
            key, val = item
            try:
                self._place(key, val)
            finally:
                with self._lock:
                    self.inflight -= 1
                    b = bucket_of(key, val)
                    self._inflight_by[b] -= 1
                    if self._inflight_by[b] <= 0:
                        del self._inflight_by[b]

    # -- une pose
    def _place(self, key, val):
        canvas, col, row = key
        color, _source, cookie = val
        # 5e argument allyOverwrite=False : on n'écrase jamais un pixel d'allié
        res = self.client.call("onPlacePixel", [canvas, col, row, color, False], cookie, canvas)
        status, code = res.get("status"), (res.get("code") or "")
        if status == "success":
            self._on_success(key, color, cookie, res)
        elif status == "rejected" and code == PIXEL_UNCHANGED:
            with self._lock:
                self.skipped += 1
        elif status == "rejected" and code == ALLY_OVERWRITE:
            self.queue.mark_failed(key)  # case d'un allié : on la laisse
            with self._lock:
                self.skipped += 1
            self.journal.log_throttled(("allié", canvas), f"Pixels d'alliés sur « {canvas} » laissés intacts", "info")
        elif status == "rejected" and code in ACCOUNT_STOP:
            self._on_account_stop(key, val, code, res)
        elif status == "rejected" and code == RATE_LIMITED:
            # trop rapide pour le serveur : tout le compte ralentit (la limite est par compte)
            self.queue.add_if_absent(key, val, front=True)
            with self._lock:
                self._account_pause[cookie] = max(self._account_pause.get(cookie, 0), time.time() + 5)
            self.journal.log_throttled(("rate", cookie[-8:]),
                                       "Codawar : poses trop rapides — pause de 5 s du compte (limite 3 px/s)", "warning", every=600)
        elif status != "rejected" or _has_hint(code, config.RETRY_LATER_HINTS):
            self._on_retry_later(key, val, status, code, res)
        else:
            self._on_refused(key, color, code, res)

    def _on_success(self, key, color, cookie, res):
        canvas, col, row = key
        self.wallet.placed(canvas, cookie, (res.get("value") or {}).get("wallet"))
        self.boards.record_placement(canvas, col, row, color)
        with self._lock:
            self.placed += 1
            self._recent.append((time.time(), canvas))
            self._backoff.pop((canvas, cookie), None)

    def _on_retry_later(self, key, val, status, code, res):
        """Erreur réseau, crédits épuisés, limite… : remet le pixel en tête de son lot et attend.

        L'attente ne concerne que ce lot (canvas + compte) : les autres canvas continuent.
        """
        canvas = key[0]
        bucket = bucket_of(key, val)
        credit_short = _has_hint(code, config.CREDIT_SHORT_HINTS)
        wait = self.wallet.credit_wait(canvas, val[2]) if credit_short else (10 if status == "rejected" else 2)
        self.queue.add_if_absent(key, val, front=True)
        reject = {"code": code or "réseau", "message": res.get("message", ""), "t": time.time(), "canvas": canvas}
        with self._lock:
            self._backoff[bucket] = max(self._backoff.get(bucket, 0), time.time() + wait)
            self._reject_by_canvas[canvas] = reject
        # une ligne par canvas et par type d'erreur au plus toutes les 30 min (pas de spam)
        if credit_short:
            self.journal.log_throttled(("credits", canvas),
                                       f"Crédits épuisés sur « {canvas} » — 1 crédit / 5 min, les autres canvas continuent",
                                       "warning")
        else:
            self.journal.log_throttled(("pause", canvas, code or "réseau"),
                                       f"Attente sur « {canvas} » : {code or res.get('message') or 'réseau'}",
                                       "warning", every=600)

    def _on_account_stop(self, key, val, code, res):
        """Exclusion temporaire / compte banni : plus aucune pose de ce compte, sur aucun canvas."""
        cookie = val[2]
        self.queue.add_if_absent(key, val, front=True)
        wait = config.TIMEOUT_PAUSE if code == "STUDENT_TIMED_OUT" else 24 * 3600
        reject = {"code": code, "message": res.get("message", ""), "t": time.time(), "canvas": key[0]}
        with self._lock:
            self._account_pause[cookie] = time.time() + wait
            self._reject_by_canvas[key[0]] = reject
        self.journal.log_throttled(("stop", cookie[-8:], code),
                                   f"Compte arrêté ({code}) : aucune pose pendant {wait // 60} min — {res.get('message', '')}",
                                   "error", every=600)

    def _on_refused(self, key, color, code, res):
        """Refus définitif : on n'insiste pas sur cette case pendant un moment."""
        canvas, col, row = key
        self.queue.mark_failed(key)
        reject = {"code": code, "message": res.get("message", ""), "t": time.time(), "canvas": canvas}
        with self._lock:
            self.errors += 1
            self._reject_by_canvas[canvas] = reject
        self.journal.log_throttled(("refus", canvas, code),
                                   f"Refusé sur « {canvas} » ({col}, {row}) {color} : {code} — {res.get('message', '')}",
                                   "error", every=300)
