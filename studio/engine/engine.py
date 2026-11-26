"""Engine : assemble les composants du moteur et expose l'état pour l'interface."""

import threading
import time

from .. import config
from ..schedule import within_schedule
from .board import BoardCache
from .cookies import CookieResolver
from .defense import DefenseMode
from .guard import Guard
from .journal import Journal
from .loot import LootService
from .pace import AccountPacer
from .pixel_queue import PixelQueue, SOURCE_MANUAL
from .placer import Placer
from .snapshots import Snapshotter
from .stats import StatsTracker
from .wallet import REFILL_INTERVAL, WalletTracker


class Engine:
    def __init__(self, store, client, directory):
        self.store = store
        settings = lambda: store.settings  # noqa: E731 — lecture à chaud des réglages
        runtime = store.runtime or {}

        self.cookies = CookieResolver(store)
        self.journal = Journal(runtime.get("logs", []))
        self.wallet = WalletTracker()
        self.stats = StatsTracker(runtime.get("stats", {}), runtime.get("heat"))
        self.boards = BoardCache(client, self.cookies.any)
        self.boards.subscribe(lambda canvas, b: self.wallet.observe(canvas, b["account"], b["wallet"], b["t"]))
        self.boards.subscribe(self.stats.observe)
        self.queue = PixelQueue()
        self.pacer = AccountPacer()  # cadence 3 px/s : poses et outils de destruction
        self.defense = DefenseMode()
        self.placer = Placer(client, self.queue, self.boards, self.wallet, self.journal, settings, self.pacer)
        self.placer.restore_counters(runtime)
        self.guard = Guard(store, self.boards, self.queue, self.defense, self.journal,
                           directory, self.cookies, settings)
        self.snapshotter = Snapshotter(self.boards, store)
        self.loot = LootService(client, store, self.journal, self.pacer)

    # -- tâches de fond
    def start(self):
        for _ in range(config.WORKERS):
            _spawn(self.placer.worker)
        _spawn(self.guard.loop)
        _spawn(self._persist_loop)
        _spawn(self._stats_loop)
        _spawn(self.snapshotter.loop)
        _spawn(self.loot.loop)

    def _persist_loop(self):
        while True:
            time.sleep(config.PERSIST_INTERVAL)
            try:
                self.persist()
            except Exception as e:
                print(f"persist: {e}", flush=True)

    def _stats_loop(self):
        """Relit les canvas suivis pour construire les stats 24/24 (interface fermée ou non)."""
        while True:
            time.sleep(config.STATS_INTERVAL)
            try:
                cookie = self.cookies.any()
                if not cookie:
                    continue
                for canvas in self.boards.canvases() | self.store.watched_canvases():
                    self.boards.fetch(canvas, max_age=config.STATS_INTERVAL - 1, cookie=cookie)
            except Exception as e:
                print(f"stats_loop: {e}", flush=True)

    def persist(self):
        """Sauvegarde l'état d'exécution (compteurs, stats, carte de chaleur, journal)."""
        data = {**self.placer.counters(), **self.stats.to_runtime(), "logs": self.journal.entries()}
        self.store.save_runtime(data)

    # -- actions
    def enqueue(self, canvas, pixels, front=False, cookie=None):
        return self.queue.enqueue(canvas, pixels, front=front, source=SOURCE_MANUAL,
                                  cookie=cookie or self.cookies.any())

    def clear_queue(self):
        n = self.queue.clear()
        self.journal.log(f"File vidée ({n} pixels)", "warning")

    # -- état pour l'interface
    def _blocked_reason(self, canvas):
        """Raison globale pour laquelle la file n'avance pas (None si elle avance)."""
        backoff = self.placer.backoff_remaining(canvas)
        if not self.cookies.any():
            return "pas de cookie Codawar"
        if self.placer.paused:
            return "en pause"
        if not within_schedule(self.store.settings):
            return "minuteur : hors plage horaire"
        if self.boards.is_closed(canvas):
            return "canvas fermé"
        if backoff > 0 and self.queue.count_for(canvas):
            lr = self.placer.last_reject_for(canvas) or {}
            w = self.wallet.snapshot(canvas)
            if w["credits"] == 0 and not w["unlimited"]:
                return f"plus de crédit — prochain dans {fmt_wait(backoff)}"
            return f"attente {fmt_wait(backoff)} — {lr.get('message') or lr.get('code') or 'crédits/limite'}"
        return None

    def layer_eta(self):
        """Secondes estimées avant la fin de chaque calque actif.

        Les calques d'un même compte sur un même canvas partagent ses crédits :
        on additionne leurs pixels manquants. Règle Codawar : 1 crédit / 5 min, max 100.
        """
        missing = self.guard.layer_missing or {}
        groups = {}
        for l in self.store.active_layers():
            cookie = self.cookies.for_owner(l.get("owner"))
            g = groups.setdefault((l["canvas"], cookie), [])
            g.append(l["id"])
        out = {}
        for (canvas, cookie), ids in groups.items():
            rem = sum(missing.get(i, 0) for i in ids)
            if not rem:
                continue
            acc = self.wallet.account(canvas, cookie)
            if not acc:
                continue
            speed = max(self.placer.rate(canvas), 1.0)  # pixels/s quand il y a des crédits
            if acc["unlimited"]:
                eta = rem / speed
            else:
                credits = max(0, acc["credits"] or 0)
                if rem <= credits:
                    eta = rem / speed
                else:
                    first = acc["nextIn"] if acc["nextIn"] is not None else REFILL_INTERVAL
                    eta = credits / speed + max(0.0, first) + (rem - credits - 1) * REFILL_INTERVAL
            for i in ids:
                out[i] = round(eta)
        return out

    def status(self, canvas):
        pending = self.queue.pending_for(canvas)
        return {
            "paused": self.placer.paused,
            "queue": len(self.queue),
            "queueHere": self.queue.count_for(canvas),
            "blocked": self._blocked_reason(canvas),
            "lastReject": self.placer.last_reject_for(canvas),
            **self.wallet.snapshot(canvas),
            "inflight": self.placer.inflight,
            "rate": self.placer.rate(canvas),
            "rateAll": self.placer.rate(),
            **self.placer.counters(),
            "backoff": self.placer.backoff_remaining(canvas),
            "layerEta": self.layer_eta(),
            "logs": self.journal.entries(),
            "pending": pending,
            "layerMissing": self.guard.layer_missing,
            "vandalAlert": self.guard.vandal_alert,
            "defending": self.defense.remaining(),
            "posting": within_schedule(self.store.settings),
            "nextGuard": self.guard.next_run_in(),
        }


def fmt_wait(sec):
    return f"{sec} s" if sec < 60 else f"{sec // 60} min {sec % 60:02d} s"


def _spawn(target, *args):
    threading.Thread(target=target, args=args, daemon=True).start()
