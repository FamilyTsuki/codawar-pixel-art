"""Engine : assemble les composants du moteur et expose l'état pour l'interface."""

import threading
import time

from .. import config
from ..schedule import within_schedule
from .board import BoardCache
from .cookies import CookieResolver
from .defense import DefenseMode
from .guard import Guard
from .journal import Journal, audience
from .loot import LootService
from .towerdefense import TowerDefense
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
                           directory, self.cookies, settings, self.wallet)
        self.snapshotter = Snapshotter(self.boards, store)
        self.loot = LootService(client, store, self.journal, self.pacer)
        self.td = TowerDefense(client, store, self.journal, settings)

    # -- tâches de fond
    def start(self):
        for _ in range(config.WORKERS):
            _spawn(self.placer.worker)
        _spawn(self.guard.loop)
        _spawn(self._persist_loop)
        _spawn(self._stats_loop)
        _spawn(self.snapshotter.loop)
        _spawn(self.loot.loop)
        _spawn(self.td.loop)

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
        data = {**self.placer.counters(), "counts": self.placer.counts_runtime(),
                **self.stats.to_runtime(), "logs": self.journal.entries()}
        self.store.save_runtime(data)

    # -- actions
    def enqueue(self, canvas, pixels, cookie, front=False):
        """Pixels posés avec CE cookie (jamais celui d'un autre compte)."""
        if not cookie:
            return 0
        return self.queue.enqueue(canvas, pixels, front=front, source=SOURCE_MANUAL, cookie=cookie)

    def clear_queue(self, cookie):
        """Vide la file d'UN compte ; les pixels des calques reviennent au prochain tour de garde."""
        n = self.queue.clear({cookie}) if cookie else 0
        self.journal.log(f"File vidée ({n} pixels)", "warning", to=audience([cookie]))

    # -- état pour l'interface
    def _blocked_reason(self, canvas, cookies):
        """Raison pour laquelle la file de ce compte n'avance pas (None si elle avance)."""
        if not cookies:
            return "pas de cookie Codawar"
        backoff = self.placer.backoff_remaining(canvas, cookies)
        if all(self.placer.is_paused(c) for c in cookies):
            return "en pause"
        if not within_schedule(self.store.settings):
            return "minuteur : hors plage horaire"
        if self.boards.is_closed(canvas):
            return "canvas fermé"
        if backoff > 0 and self.queue.count_for(canvas, cookies):
            lr = self.placer.last_reject_for(canvas, cookies) or {}
            w = self.wallet.snapshot(canvas, cookies)
            if w["credits"] == 0 and not w["unlimited"]:
                return f"plus de crédit — prochain dans {fmt_wait(backoff)}"
            return f"attente {fmt_wait(backoff)} — {lr.get('message') or lr.get('code') or 'crédits/limite'}"
        return None

    def layer_eta(self, layer_ids=None):
        """Secondes estimées avant la fin de chaque calque actif (de layer_ids si donné).

        Les calques d'un même compte sur un même canvas partagent ses crédits :
        on additionne leurs pixels manquants. Règle Codawar : 1 crédit / 5 min, max 100.
        """
        missing = self.guard.layer_missing or {}
        groups = {}
        for l in self.store.active_layers():
            if layer_ids is not None and l["id"] not in layer_ids:
                continue
            cookie = self.cookies.for_owner(l.get("owner"))
            if not cookie:
                continue  # sans cookie, rien n'est posé : pas d'estimation
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
            speed = max(self.placer.rate(canvas, {cookie}), 1.0)  # pixels/s quand il y a des crédits
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

    def status(self, canvas, user):
        """État vu par UN compte : sa file, son débit, son solde, son journal, ses calques.

        Rien d'un autre compte n'y figure (sauf les calques des projets dont il est membre).
        """
        cookie = user.get("cookie") or ""
        cookies = {cookie} if cookie else set()
        mine = {l["id"] for l in self.store.layers_of(user["id"])}
        missing = self.guard.layer_missing or {}
        alert = self.guard.vandal_alert
        return {
            "paused": bool(cookie) and self.placer.is_paused(cookie),
            "queue": self.queue.count_for(None, cookies),
            "queueHere": self.queue.count_for(canvas, cookies),
            "blocked": self._blocked_reason(canvas, cookies),
            "lastReject": self.placer.last_reject_for(canvas, cookies),
            **self.wallet.snapshot(canvas, cookies),
            "inflight": self.placer.inflight_for(cookies),
            "rate": self.placer.rate(canvas, cookies),
            "rateAll": self.placer.rate(None, cookies),
            **self.placer.counters(cookies),
            "backoff": self.placer.backoff_remaining(canvas, cookies),
            "layerEta": self.layer_eta(mine),
            "logs": self.journal.entries(audience(cookies, [user["id"]])),
            "pending": self.queue.pending_for(canvas, cookies),
            "layerMissing": {lid: n for lid, n in missing.items() if lid in mine},
            "vandalAlert": alert if alert and alert.get("layerId") in mine else None,
            "defending": self.defense.remaining(),
            "posting": within_schedule(self.store.settings),
            "nextGuard": self.guard.next_run_in(),
        }


def fmt_wait(sec):
    return f"{sec} s" if sec < 60 else f"{sec // 60} min {sec % 60:02d} s"


def _spawn(target, *args):
    threading.Thread(target=target, args=args, daemon=True).start()
