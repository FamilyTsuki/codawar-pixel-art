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
from .pixel_queue import PixelQueue, SOURCE_MANUAL
from .placer import Placer
from .snapshots import Snapshotter
from .stats import StatsTracker
from .wallet import WalletTracker


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
        self.boards.subscribe(lambda _canvas, b: self.wallet.observe(b["wallet"], b["t"]))
        self.boards.subscribe(self.stats.observe)
        self.queue = PixelQueue()
        self.defense = DefenseMode()
        self.placer = Placer(client, self.queue, self.boards, self.wallet, self.defense, self.journal, settings)
        self.placer.restore_counters(runtime)
        self.guard = Guard(store, self.boards, self.queue, self.defense, self.journal,
                           directory, self.cookies, settings)
        self.snapshotter = Snapshotter(self.boards, store)

    # -- tâches de fond
    def start(self):
        for i in range(config.MAX_WORKERS):
            _spawn(self.placer.worker, i)
        _spawn(self.guard.loop)
        _spawn(self._persist_loop)
        _spawn(self._stats_loop)
        _spawn(self.snapshotter.loop)

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
        backoff = self.placer.backoff_remaining()
        if not self.cookies.any():
            return "pas de cookie Codawar"
        if self.placer.paused:
            return "en pause"
        if not within_schedule(self.store.settings):
            return "minuteur : hors plage horaire"
        if self.boards.is_closed(canvas):
            return "canvas fermé"
        if backoff > 0:
            lr = self.placer.last_reject or {}
            return f"attente {backoff}s — {lr.get('message') or lr.get('code') or 'crédits/limite'}"
        return None

    def status(self, canvas):
        pending = self.queue.pending_for(canvas)
        return {
            "paused": self.placer.paused,
            "queue": len(self.queue),
            "queueHere": len(pending),
            "blocked": self._blocked_reason(canvas),
            "lastReject": self.placer.last_reject,
            **self.wallet.snapshot(),
            "inflight": self.placer.inflight,
            "rate": self.placer.rate(),
            **self.placer.counters(),
            "backoff": self.placer.backoff_remaining(),
            "logs": self.journal.entries(),
            "pending": pending,
            "layerMissing": self.guard.layer_missing,
            "vandalAlert": self.guard.vandal_alert,
            "defending": self.defense.remaining(),
            "posting": within_schedule(self.store.settings),
            "nextGuard": self.guard.next_run_in(),
        }


def _spawn(target, *args):
    threading.Thread(target=target, args=args, daemon=True).start()
