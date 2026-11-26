"""Workers de pose : vident la file et interprètent les réponses de Codawar."""

import collections
import threading
import time

from .. import config
from ..schedule import within_schedule
from .journal import audience
from .pixel_queue import bucket_of
from .wallet import account_key

PIXEL_UNCHANGED = "PIXEL_UNCHANGED"
ALLY_OVERWRITE = "ALLY_OVERWRITE_CONFIRMATION_REQUIRED"
# SESSION_EXPIRED : cookie périmé. La pause est indexée par cookie : un nouveau cookie repart aussitôt.
ACCOUNT_STOP = ("STUDENT_TIMED_OUT", "ACCOUNT_BANNED", "SESSION_EXPIRED")
RATE_LIMITED = "PLACEMENT_RATE_LIMITED"
SESSION_EXPIRED = "SESSION_EXPIRED"


def _has_hint(code, hints):
    low = code.lower()
    return any(h in low for h in hints)


class Placer:
    def __init__(self, client, queue, boards, wallet, journal, settings, pacer):
        self.client = client
        self.queue = queue
        self.boards = boards
        self.wallet = wallet
        self.journal = journal
        self.settings = settings  # callable -> dict des réglages
        self._lock = threading.Lock()
        self._paused = set()         # cookies dont l'utilisateur a mis les poses en pause
        self._backoff = {}           # lot (canvas, cookie) -> timestamp de reprise
        self._inflight_by = collections.Counter()  # lot -> requêtes en cours
        self.inflight = 0
        self.placed = self.skipped = self.errors = 0
        self._counts = collections.Counter()  # (account_key, "placed"|"skipped"|"errors") -> n
        self._reject = {}            # lot (canvas, cookie) -> dernier refus/erreur
        self._recent = collections.deque()  # (timestamp, canvas, cookie) des poses (débit)
        self.pacer = pacer  # cadence + exclusions, partagées avec les outils de destruction

    # -- état
    def set_paused(self, cookie, paused):
        """Pause des poses d'UN compte (les autres comptes continuent)."""
        with self._lock:
            (self._paused.add if paused else self._paused.discard)(cookie)
        self.journal.log("Pause" if paused else "Reprise", "warning" if paused else "info",
                         to=audience([cookie]))

    def is_paused(self, cookie):
        with self._lock:
            return cookie in self._paused

    def backoff_remaining(self, canvas=None, cookies=None):
        """Attente restante (s) : la plus courte parmi les lots en attente (de ce canvas / ces cookies)."""
        now = time.time()
        with self._lock:
            waits = [u - now for (cv, ck), u in self._backoff.items()
                     if u > now and (canvas is None or cv == canvas) and (cookies is None or ck in cookies)]
        return max(0, round(min(waits))) if waits else 0

    def last_reject_for(self, canvas, cookies=None):
        """Dernier refus sur ce canvas, limité à ces cookies (None : tous les comptes)."""
        with self._lock:
            found = [r for (cv, ck), r in self._reject.items()
                     if cv == canvas and (cookies is None or ck in cookies)]
        return max(found, key=lambda r: r["t"]) if found else None

    def inflight_for(self, cookies=None):
        with self._lock:
            return sum(n for (_, ck), n in self._inflight_by.items() if cookies is None or ck in cookies)

    def can_post(self):
        return within_schedule(self.settings())

    def _blocked(self, bucket):
        """Lot en attente (refus récent, exclusion, cadence max) ou sans crédit : on passe au suivant."""
        now = time.time()
        if bucket[1] in self._paused:
            return True
        if self._backoff.get(bucket, 0) > now:
            return True
        if self.pacer.blocked(bucket[1], now):
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

    def rate(self, canvas=None, cookies=None):
        """Poses par seconde sur la fenêtre récente (filtrées par canvas / cookies si donnés)."""
        with self._lock:
            now = time.time()
            while self._recent and now - self._recent[0][0] > config.RATE_WINDOW:
                self._recent.popleft()
            n = sum(1 for _, cv, ck in self._recent
                    if (canvas is None or cv == canvas) and (cookies is None or ck in cookies))
            return round(n / config.RATE_WINDOW, 2)

    def _count(self, cookie, kind):
        """Incrémente le total global et celui du compte (verrou déjà pris)."""
        setattr(self, kind, getattr(self, kind) + 1)
        self._counts[(account_key(cookie), kind)] += 1

    def counters(self, cookies=None):
        """Compteurs globaux (cookies=None) ou ceux de ces comptes."""
        if cookies is None:
            return {"placed": self.placed, "skipped": self.skipped, "errors": self.errors}
        keys = {account_key(c) for c in cookies}
        with self._lock:
            return {kind: sum(n for (acc, k), n in self._counts.items() if k == kind and acc in keys)
                    for kind in ("placed", "skipped", "errors")}

    def counts_runtime(self):
        """Compteurs par compte, sérialisables : {account_key: {placed, skipped, errors}}."""
        out = {}
        with self._lock:
            for (acc, kind), n in self._counts.items():
                out.setdefault(acc, {})[kind] = n
        return out

    def restore_counters(self, runtime):
        self.placed = runtime.get("placed", 0)
        self.skipped = runtime.get("skipped", 0)
        self.errors = runtime.get("errors", 0)
        for acc, kinds in (runtime.get("counts") or {}).items():
            for kind, n in kinds.items():
                self._counts[(acc, kind)] = n

    # -- boucle d'un worker

    def _take(self):
        if not self.can_post():
            return None
        with self._lock:  # verrou tenu pendant le choix : le décompte des crédits en cours reste juste
            item = self.queue.pop(self._blocked)
            if item is None:
                return None
            # un outil de destruction a pu prendre le créneau entre-temps : on remet le pixel
            if not self.pacer.reserve(item[1][2]):
                self.queue.add_if_absent(*item, front=True)
                return None
            self.inflight += 1
            self._inflight_by[bucket_of(*item)] += 1
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
                self.pacer.release(val[2])  # réponse reçue : libère le créneau du compte
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
        to = audience([cookie])
        # 5e argument allyOverwrite=False : on n'écrase jamais un pixel d'allié
        res = self.client.call("onPlacePixel", [canvas, col, row, color, False], cookie, canvas)
        status, code = res.get("status"), (res.get("code") or "")
        if status == "success":
            self._on_success(key, color, cookie, res)
        elif status == "rejected" and code == PIXEL_UNCHANGED:
            with self._lock:
                self._count(cookie, "skipped")
        elif status == "rejected" and code == ALLY_OVERWRITE:
            self.queue.mark_failed(key)  # case d'un allié : on la laisse
            with self._lock:
                self._count(cookie, "skipped")
            self.journal.log_throttled(("allié", canvas), f"Pixels d'alliés sur « {canvas} » laissés intacts", "info",
                                       to=to)
        elif status == "rejected" and code == SESSION_EXPIRED:
            self._on_session_expired(key, val, res)
        elif status == "rejected" and code in ACCOUNT_STOP:
            self._on_account_stop(key, val, code, res)
        elif status == "rejected" and code == RATE_LIMITED:
            # trop rapide pour le serveur : tout le compte ralentit (la limite est par compte)
            self.queue.add_if_absent(key, val, front=True)
            self.pacer.pause(cookie, 5)
            self.journal.log_throttled(("rate",),
                                       "Codawar : poses trop rapides — pause de 5 s du compte (limite 3 px/s)", "warning",
                                       every=600, to=to)
        elif status != "rejected" or _has_hint(code, config.RETRY_LATER_HINTS):
            self._on_retry_later(key, val, status, code, res)
        else:
            self._on_refused(key, val, code, res)

    def _on_success(self, key, color, cookie, res):
        canvas, col, row = key
        self.wallet.placed(canvas, cookie, (res.get("value") or {}).get("wallet"))
        self.boards.record_placement(canvas, col, row, color)
        self.pacer.clear_pause(cookie)
        with self._lock:
            self._count(cookie, "placed")
            self._recent.append((time.time(), canvas, cookie))
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
        to = audience([val[2]])
        with self._lock:
            self._backoff[bucket] = max(self._backoff.get(bucket, 0), time.time() + wait)
            self._reject[bucket] = reject
        # une ligne par canvas et par type d'erreur au plus toutes les 30 min (pas de spam)
        if credit_short:
            self.journal.log_throttled(("credits", canvas),
                                       f"Crédits épuisés sur « {canvas} » — 1 crédit / 5 min, les autres canvas continuent",
                                       "warning", to=to)
        else:
            self.journal.log_throttled(("pause", canvas, code or "réseau"),
                                       f"Attente sur « {canvas} » : {code or res.get('message') or 'réseau'}",
                                       "warning", every=600, to=to)

    def _on_session_expired(self, key, val, res):
        """Cookie expiré : on arrête ce compte au lieu d'envoyer chaque pixel pour rien."""
        cookie = val[2]
        self.pacer.mark_expired(cookie)
        # sa file part à la poubelle : la garde redonne les pixels des calques aux membres
        # encore valides au prochain tour (une case déjà en file ne change pas de compte)
        self.queue.clear({cookie})
        reject = {"code": SESSION_EXPIRED, "message": res.get("message", ""), "t": time.time(), "canvas": key[0]}
        with self._lock:
            self._reject[bucket_of(key, val)] = reject
        self.journal.log_throttled(("expired",),
                                   "Cookie Codawar expiré : aucune pose avec ce compte. "
                                   "Reconnecte-toi sur Codawar et recolle ton cookie dans Réglages",
                                   "error", every=600, to=audience([cookie]))

    def _on_account_stop(self, key, val, code, res):
        """Exclusion temporaire / compte banni : plus aucune pose de ce compte, sur aucun canvas."""
        cookie = val[2]
        self.queue.add_if_absent(key, val, front=True)
        wait = config.TIMEOUT_PAUSE if code == "STUDENT_TIMED_OUT" else 24 * 3600
        reject = {"code": code, "message": res.get("message", ""), "t": time.time(), "canvas": key[0]}
        self.pacer.pause(cookie, wait)
        with self._lock:
            self._reject[bucket_of(key, val)] = reject
        if code == "SESSION_EXPIRED":
            msg = "Session Codawar expirée : mettez à jour le cookie du compte (les pixels restent en file)"
        else:
            msg = f"Compte arrêté ({code}) : aucune pose pendant {wait // 60} min — {res.get('message', '')}"
        self.journal.log_throttled(("stop", code), msg, "error", every=600, to=audience([cookie]))

    def _on_refused(self, key, val, code, res):
        """Refus définitif : on n'insiste pas sur cette case pendant un moment."""
        canvas, col, row = key
        color, cookie = val[0], val[2]
        self.queue.mark_failed(key)
        reject = {"code": code, "message": res.get("message", ""), "t": time.time(), "canvas": canvas}
        with self._lock:
            self._count(cookie, "errors")
            self._reject[bucket_of(key, val)] = reject
        self.journal.log_throttled(("refus", canvas, code),
                                   f"Refusé sur « {canvas} » ({col}, {row}) {color} : {code} — {res.get('message', '')}",
                                   "error", every=300, to=audience([cookie]))
