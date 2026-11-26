"""Surveillance des calques actifs : relit le canvas, remet en file les pixels faux,
détecte le vandalisme et déclenche le mode défense."""

import collections
import random
import threading
import time

from .board import ME, in_board
from .pixel_queue import SOURCE_LAYER


def build_target(layers, board):
    """Pixels voulus par les calques : ({(c, r): hex}, {(c, r): id du calque}).

    Ordre des calques : le dernier est au-dessus.
    """
    target, owner_of = {}, {}
    for l in layers:
        for dx, dy, h in l["pixels"]:
            c, r = l["x"] + dx, l["y"] + dy
            if in_board(board, c, r):
                target[(c, r)] = h.lower()
                owner_of[(c, r)] = l["id"]
    return target, owner_of


class Guard:
    def __init__(self, store, boards, queue, defense, journal, directory, cookies, settings, wallet):
        self.store = store
        self.boards = boards
        self.queue = queue
        self.defense = defense
        self.journal = journal
        self.directory = directory  # pseudos de nos comptes
        self.cookies = cookies      # CookieResolver
        self.wallet = wallet        # crédits par compte : répartition des pixels d'un projet
        self.settings = settings    # callable -> dict des réglages
        self._lock = threading.Lock()
        self._kick = threading.Event()
        self.last_run = 0
        self.layer_missing = {}     # id calque -> pixels encore faux
        self._prev_attacked = {}    # id calque -> cases écrasées au tour précédent
        self.vandal_alert = None

    # -- déclenchement
    def kick(self):
        """Relecture immédiate."""
        self._kick.set()

    def drop_targets(self):
        """Retire tout de suite les pixels venant des calques (la surveillance les recalcule)."""
        self.queue.drop_source(SOURCE_LAYER)
        self.kick()

    def _interval(self):
        s = self.settings()
        if self.defense.active():  # mode défense : relecture rapide
            return max(1, float(s.get("defendInterval", 2)))
        return max(3, float(s.get("guardInterval", 8)))

    def next_run_in(self):
        interval = float(self.settings().get("guardInterval", 20))
        return max(0, round(self.last_run + interval - time.time()))

    def loop(self):
        while True:
            kicked = self._kick.wait(timeout=1)
            self._kick.clear()
            if kicked or time.time() - self.last_run >= self._interval():
                self.last_run = time.time()
                try:
                    self.run()
                except Exception as e:
                    self.journal.log(f"Erreur surveillance : {e}", "error")

    # -- un tour de surveillance
    def run(self):
        by_canvas = collections.defaultdict(list)
        for l in self.store.active_layers():
            by_canvas[l["canvas"]].append(l)
        # canvas sans calque actif : plus rien à poser pour eux ; les autres sont resynchronisés
        # après relecture (sans vider la file avant, sinon elle tombe à 0 pendant la lecture)
        self.queue.drop_source(SOURCE_LAYER, keep_canvases=set(by_canvas))
        missing = {}
        for canvas, layers in by_canvas.items():
            missing.update(self._guard_canvas(canvas, layers))
        with self._lock:
            self.layer_missing = missing

    def _guard_canvas(self, canvas, layers):
        # un calque de projet a plusieurs cookies : ceux de tous ses membres
        cookies_of = {l["id"]: self.cookies.for_layer(l) for l in layers}
        board = self.boards.fetch(canvas, max_age=0, cookie=cookies_of[layers[0]["id"]][0])
        if board is None:
            self.journal.log(f"Lecture du canvas '{canvas}' impossible", "error")
            return {}
        now = time.time()
        pixels = board["pixels"]
        target, owner_of = build_target(layers, board)
        # case vide ≠ blanc : un pixel blanc voulu sur une case vide doit être posé
        todo = [(c, r, h) for (c, r), h in target.items()
                if pixels.get((c, r), [None])[0] != h and not self.queue.is_failed((canvas, c, r), now)]

        missing = {l["id"]: 0 for l in layers}
        for c, r, _ in todo:
            missing[owner_of[(c, r)]] += 1

        # un pixel "attaqué" = une de NOS cases maintenant peinte par un AUTRE joueur (pas vide, pas nous)
        ours = self.directory.our_names() | {"", ME}

        def attacked(c, r):
            o = pixels.get((c, r))
            return bool(o) and o[1] not in ours

        self._detect_vandalism(canvas, layers, pixels, target, owner_of, attacked, now)
        self._enqueue(canvas, todo, owner_of, cookies_of, attacked)
        return missing

    def _detect_vandalism(self, canvas, layers, pixels, target, owner_of, attacked, now):
        """Alerte basée sur de VRAIES attaques (pas sur des pixels jamais posés)."""
        atk = collections.Counter()                          # calque -> cases écrasées par d'autres
        atk_who = collections.defaultdict(collections.Counter)  # calque -> {joueur: n}
        for (c, r), lid in owner_of.items():
            if attacked(c, r) and pixels[(c, r)][0] != target[(c, r)]:
                atk[lid] += 1
                atk_who[lid][pixels[(c, r)][1]] += 1
        threshold = int(self.settings().get("vandalThreshold", 12))
        for l in layers:
            prev = self._prev_attacked.get(l["id"], 0)
            jump = atk[l["id"]] - prev
            if prev <= 2 and jump >= threshold:
                who = ", ".join(f"{n} ({k})" for n, k in atk_who[l["id"]].most_common(3)) or "inconnu"
                self.journal.log(f"VANDALISME sur « {l['name']} » : {jump} pixels écrasés. Auteur(s) : {who}", "error")
                with self._lock:
                    self.vandal_alert = {"layer": l["name"], "canvas": canvas, "count": jump, "who": who, "t": now}
            self._prev_attacked[l["id"]] = atk[l["id"]]
        if sum(atk.values()):  # défense : relecture rapide + plus de workers
            self.defense.trigger()

    def _share(self, canvas, cookies):
        """Répartit les pixels d'un calque entre les comptes, au prorata de leurs crédits.

        Un projet met en commun les crédits de ses membres : celui qui en a le plus
        prend le plus de pixels. Les comptes à sec reçoivent quand même une part
        minimale, car la file est resynchronisée à chaque tour de garde et leurs
        crédits remontent entre-temps (1 crédit / 5 min).
        """
        if len(cookies) == 1:
            return [(cookies[0], 1.0)]
        weights = []
        for ck in cookies:
            acc = self.wallet.account(canvas, ck)
            if not acc:
                weights.append(1.0)          # solde pas encore connu
            elif acc["unlimited"]:
                weights.append(100.0)
            else:
                weights.append(max(0.25, float(acc["credits"] or 0)))
        total = sum(weights) or 1.0
        return [(ck, w / total) for ck, w in zip(cookies, weights)]

    def _enqueue(self, canvas, todo, owner_of, cookies_of, attacked):
        """Pixels attaqués en tête de file, puis ordre ligne par ligne (ou aléatoire)."""
        if self.settings().get("order") == "random":
            random.shuffle(todo)
        else:
            todo.sort(key=lambda p: (p[1], p[0]))
        todo.sort(key=lambda p: 0 if attacked(p[0], p[1]) else 1)
        spread = {lid: _Spread(self._share(canvas, cks)) for lid, cks in cookies_of.items()}
        entries = []
        for c, r, h in todo:
            lid = owner_of[(c, r)]
            # calque personnel : son auteur ; calque de projet : réparti entre les membres
            cookie = spread[lid].next()
            entries.append(((canvas, c, r), (h, SOURCE_LAYER, cookie), attacked(c, r)))
        self.queue.sync_source(canvas, SOURCE_LAYER, entries)


class _Spread:
    """Round-robin pondéré « lisse » (algorithme de nginx).

    Répartit les pixels d'un calque de projet au prorata des crédits de chaque membre,
    en alternant les comptes au lieu de donner une longue série au plus riche.
    """

    def __init__(self, shares):
        self.shares = shares            # [(cookie, poids)]
        self._cur = [0.0] * len(shares)

    def next(self):
        total = sum(w for _, w in self.shares)
        best = 0
        for i, (_, w) in enumerate(self.shares):
            self._cur[i] += w
            if self._cur[i] > self._cur[best]:
                best = i
        self._cur[best] -= total
        return self.shares[best][0]
