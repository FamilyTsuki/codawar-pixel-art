"""Tower Defense Codawar : pilote automatique qui maximise les bits gagnés.

Règles du jeu (lues dans le bundle client de Codawar) :
- rendement passif = yieldBasePerHour + yieldPerJalonPerHour x record (2 + 0,25 x jalon bits/h),
  plafonné à idleCapHours (12 h) sans collecte ;
- chaque Bug tué rapporte des bits, d'autant plus que la vague est haute, jusqu'à
  killBitsToday.maximumBits par jour (vagues gagnées OU perdues) ;
- une amélioration coûte upgradeBaseCost x upgradeCostGrowth^(niveau-1) et donne
  powerPerLevel de puissance en plus ; un slot coûte slotBaseCost x slotCostGrowth^(n-2) ;
- la place des tourelles compte : un tir ne touche que la portion du chemin à portée.

Une vague se joue comme dans le navigateur : onStartTowerDefenseWave puis
onFinishTowerDefenseWave. Le client n'envoie jamais l'issue, le serveur la calcule et impose
la durée (TOWER_DEFENSE_WAVE_NOT_FINISHED tant qu'elle n'est pas écoulée).

Le « cerveau » (Brain) apprend, par compte, quelle force bat quelle vague :
    vague w gagnée  <=>  P >= k x g^w
avec P = somme(DPS x longueur de chemin à portée) des tourelles posées. g (croissance de la
difficulté par vague) et k sont ajustés sur l'historique des victoires et défaites. Un achat
qui fait passer la force de P à P' vaut ln(P'/P)/ln(g) jalons, soit 6 bits/jour chacun : le
pilote achète l'option au meilleur rapport valeur/coût tant qu'elle se rembourse sur l'horizon
choisi (prefs.tdHorizonDays), sans descendre sous la réserve (prefs.tdKeepBits).
"""

import collections
import math
import threading
import time
import uuid

from .. import config
from .journal import audience
from .wallet import account_key

TD_FILE = "/src/modules/tower-defense/infrastructure/browser-tower-defense-gateway.telefunc.ts"
TD_PAGE = "/tower-defense"

# Constantes du jeu (bundle client Codawar)
YIELD_PER_JALON_H = 0.25
POWER_PER_LEVEL = 0.08
MAX_LEVEL = 50
# chemin des Bugs (en tuiles) et emplacements des slots 1..10 (colonne, ligne)
PATH = [(0, 2.5), (20.5, 2.5), (20.5, 5.5), (3.5, 5.5), (3.5, 9.5), (24, 9.5)]
SLOTS = [(3, 0), (9, 0), (15, 0), (21, 3), (16, 6), (12, 6), (7, 6), (1, 6), (10, 10), (19, 10)]
MAP_W, MAP_H = 24, 11

G_DEFAULT = 1.18          # a priori : santé +12 %/vague, plus de Bugs et plus rapides
G_RANGE = (1.04, 1.45)
OBS_MAX = 300
HISTORY_MAX = 40


def _ts(iso):
    if not iso or not isinstance(iso, str):
        return None
    if iso.startswith("!Date:"):  # sérialisation telefunc des dates
        iso = iso[6:]
    try:
        from datetime import datetime
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------- géométrie
def _path_points(step=0.05):
    pts = []
    for (x1, y1), (x2, y2) in zip(PATH, PATH[1:]):
        n = max(1, int(round((abs(x2 - x1) + abs(y2 - y1)) / step)))
        pts += [(x1 + (x2 - x1) * i / n, y1 + (y2 - y1) * i / n) for i in range(n)]
    return pts, step


_PTS, _STEP = _path_points()
_COV = {}


def coverage(slot, rng):
    """Longueur de chemin (tuiles) à portée d'une tourelle de portée `rng` sur le slot."""
    key = (slot, round(rng or 0, 2))
    if key not in _COV:
        if not 1 <= slot <= len(SLOTS):
            return 0.0
        cx, cy = SLOTS[slot - 1][0] + 0.5, SLOTS[slot - 1][1] + 0.5
        r2 = (rng or 0) ** 2
        _COV[key] = sum(_STEP for x, y in _PTS if (x - cx) ** 2 + (y - cy) ** 2 <= r2)
    return _COV[key]


def _dps(t):
    return float(t.get("damagePerSecond") or 0)


def strength(t, slot):
    return _dps(t) * coverage(slot, t.get("rangeTiles") or 0)


def force(towers):
    """Force P de la défense posée."""
    return sum(strength(t, t["slot"]) for t in towers if t.get("slot") is not None)


def best_layout(towers, slots):
    """Affectation gloutonne tourelles -> slots qui maximise la force. {slot: tower}."""
    pairs = sorted(((strength(t, s), t["id"], s) for t in towers for s in slots), reverse=True)
    used_t, layout = set(), {}
    by_id = {t["id"]: t for t in towers}
    for sc, tid, s in pairs:
        if tid in used_t or s in layout or sc <= 0:
            continue
        layout[s] = by_id[tid]
        used_t.add(tid)
    return layout


def _level_gain(level):
    """Facteur de DPS d'un niveau de plus (puissance linéaire en niveau)."""
    return (1 + POWER_PER_LEVEL * level) / (1 + POWER_PER_LEVEL * (level - 1))


# ---------------------------------------------------------------- cerveau
class Brain:
    """Modèle appris d'un compte : vague w gagnée <=> ln P >= lk + w ln g."""

    def __init__(self, raw=None):
        raw = raw or {}
        self.obs = collections.deque(raw.get("obs", []), maxlen=OBS_MAX)  # [P, vague, gagnée]
        self.history = collections.deque(raw.get("history", []), maxlen=HISTORY_MAX)
        self.g, self.lk = G_DEFAULT, None
        self.fit()

    def to_raw(self):
        return {"obs": list(self.obs), "history": list(self.history)}

    def observe(self, p, wave, won):
        if p > 0:
            self.obs.append([round(p, 2), wave, bool(won)])
            self.fit()

    def _bounds(self, lg):
        hi = min((math.log(p) - w * lg for p, w, won in self.obs if won), default=None)
        lo = max((math.log(p) - w * lg for p, w, won in self.obs if not won), default=None)
        return lo, hi

    def fit(self):
        if not self.obs:
            self.g, self.lk = G_DEFAULT, None
            return
        best = None
        g = G_RANGE[0]
        while g <= G_RANGE[1] + 1e-9:
            lg = math.log(g)
            lo, hi = self._bounds(lg)
            lk = (lo + hi) / 2 if lo is not None and hi is not None else (hi if lo is None else lo + 0.05)
            errors = sum(1 for p, w, won in self.obs if (math.log(p) - w * lg >= lk) != won)
            score = (errors, abs(g - G_DEFAULT))
            if best is None or score < best[0]:
                best = (score, g, lk)
            g += 0.005
        _, self.g, self.lk = best

    def waves(self, p):
        """Vague maximale prédite (continue) pour la force p, ou None sans données."""
        if self.lk is None or p <= 0:
            return None
        return (math.log(p) - self.lk) / math.log(self.g)

    def jalons(self, p, p2):
        """Jalons gagnés en passant de la force p à p2."""
        if p <= 0:
            return 3.0 if p2 > 0 else 0.0
        return max(0.0, math.log(p2 / p) / math.log(self.g))

    def lost_frontier(self, wave, p):
        """Déjà perdu cette vague avec une force au moins égale ?"""
        return any(not won and w == wave and pp >= p * 0.999 for pp, w, won in self.obs)

    def best_farm(self, p, record):
        """Vague la plus haute (<= record) gagnée avec une force <= p, sinon record."""
        wins = [w for pp, w, won in self.obs if won and pp <= p * 1.001 and w <= record]
        losses = {w for pp, w, won in self.obs if not won and pp >= p * 0.999}
        best = max(wins, default=record)
        while best in losses and best > 1:
            best -= 1
        return max(1, min(record, best))


# ---------------------------------------------------------------- planification
def plan(data, brain, horizon_days, keep_bits):
    """Options d'achat classées par rentabilité. Rien n'est envoyé au serveur."""
    towers = data.get("towers") or []
    placed = [t for t in towers if t.get("slot") is not None]
    reserve = [t for t in towers if t.get("slot") is None]
    p = force(placed)
    bits_per_jalon = YIELD_PER_JALON_H * 24 * horizon_days
    budget = (data.get("bitBalance") or 0) - keep_bits
    options = []
    for t in placed:
        cost = t.get("upgradeCost")
        if cost is None or (t.get("level") or 1) >= MAX_LEVEL:
            continue
        p2 = p + strength(t, t["slot"]) * (_level_gain(t.get("level") or 1) - 1)
        options.append({"kind": "upgrade", "towerId": t["id"], "cost": cost,
                        "label": f"{t['type']['name']} niv. {(t.get('level') or 1) + 1} (slot {t['slot']})",
                        "jalons": brain.jalons(p, p2)})
    owned = set(data.get("ownedSlots") or [])
    cost = data.get("nextSlotCost")
    if reserve and cost is not None and len(owned) < (data.get("maximumSlots") or 0):
        free = [s for s in range(1, len(SLOTS) + 1) if s not in owned]
        best = max(((strength(t, s), s, t) for t in reserve for s in free), default=None,
                   key=lambda x: x[0])
        if best and best[0] > 0:
            options.append({"kind": "slot", "slot": best[1], "cost": cost,
                            "label": f"slot {best[1]} pour {best[2]['type']['name']}",
                            "jalons": brain.jalons(p, p + best[0])})
    for o in options:
        o["value"] = o["jalons"] * bits_per_jalon
        o["roi"] = o["value"] / o["cost"] if o["cost"] else 0
        o["affordable"] = o["cost"] <= budget
        o["payback"] = (o["cost"] / (o["jalons"] * YIELD_PER_JALON_H * 24)) if o["jalons"] > 0 else None
    options.sort(key=lambda o: o["roi"], reverse=True)
    return options


# ---------------------------------------------------------------- service
class TowerDefense:
    def __init__(self, client, store, journal, settings, runtime=None):
        self.client = client
        self.store = store
        self.journal = journal
        self.settings = settings  # callable -> réglages globaux
        self._lock = threading.Lock()
        self._cache = {}  # cookie -> (t, overview)
        self._brains = {k: Brain(v) for k, v in ((runtime or {}).get("td") or {}).items()}
        self._activity = {}  # account_key -> texte de l'action en cours

    # -- persistance
    def to_runtime(self):
        with self._lock:
            return {"td": {k: b.to_raw() for k, b in self._brains.items()}}

    def brain(self, cookie):
        key = account_key(cookie)
        with self._lock:
            if key not in self._brains:
                self._brains[key] = Brain()
            return self._brains[key]

    def _set_activity(self, cookie, text):
        with self._lock:
            self._activity[account_key(cookie)] = (time.time(), text)

    # -- appels serveur
    def _call(self, cookie, name, args):
        return self.client.call(name, args, cookie, "", file=TD_FILE, page=TD_PAGE)

    @staticmethod
    def _ok(res):
        return res.get("status") == "success"

    def overview(self, cookie, max_age=config.TD_CACHE_TTL):
        """État du Tower Defense d'un compte, ou None si illisible."""
        if not cookie:
            return None
        with self._lock:
            hit = self._cache.get(cookie)
        if hit and time.time() - hit[0] <= max_age:
            return hit[1]
        res = self._call(cookie, "onReadTowerDefense", [])
        if not self._ok(res):
            return hit[1] if hit else None
        data = res.get("value") or {}
        self._remember(cookie, data)
        return data

    def _remember(self, cookie, data):
        if data:
            with self._lock:
                self._cache[cookie] = (time.time(), data)

    def _forget(self, cookie):
        with self._lock:
            self._cache.pop(cookie, None)

    def _retry(self, cookie, name, args, waits, tries):
        """Appel répété tant que le serveur répond un code « réessayer » (waits: code -> s)."""
        res = {}
        for _ in range(tries):
            res = self._call(cookie, name, args)
            wait = waits.get(res.get("code")) if res.get("status") == "rejected" else None
            if wait is None:
                return res
            time.sleep(wait)
        return res

    def collect(self, cookie):
        """Collecte les bits en attente. Renvoie la réponse telle quelle."""
        res = self._call(cookie, "onCollectYield", [str(uuid.uuid4())])
        self._forget(cookie)
        if self._ok(res):
            v = res.get("value") or {}
            self._remember(cookie, v.get("overview"))
            gained = v.get("collectedBits", v.get("collected"))
            self.journal.log(f"Tower Defense : {gained if gained is not None else '?'} bits collectés",
                             to=audience([cookie]))
        return res

    # -- actions sur les tourelles
    def _arrange(self, cookie, data):
        """Pose les meilleures tourelles sur les slots qui couvrent le plus de chemin."""
        towers = data.get("towers") or []
        slots = list(data.get("ownedSlots") or [])
        target = best_layout(towers, slots)
        current = {t["slot"]: t for t in towers if t.get("slot") is not None}
        p_now = force(towers)
        p_best = sum(strength(t, s) for s, t in target.items())
        # ne bouge que si le gain vaut la peine (ou s'il reste des slots vides à remplir)
        empty = [s for s in slots if s not in current]
        if p_best <= p_now * 1.01 and not (empty and any(s in target for s in empty)):
            return data
        moves = {s: t for s, t in target.items() if current.get(s, {}).get("id") != t["id"]}
        if p_best <= p_now * 1.01:  # remplir les slots vides seulement
            moves = {s: t for s, t in moves.items() if s in empty and t.get("slot") is None}
        moving = {t["id"] for t in moves.values()}
        for s, t in current.items():  # libère les slots visés et les tourelles déplacées
            if s in moves or t["id"] in moving:
                res = self._call(cookie, "onRemoveTower", [str(uuid.uuid4()), t["id"]])
                if not self._ok(res):
                    return self.overview(cookie, max_age=0) or data
                data = res.get("value") or data
        for s, t in moves.items():
            res = self._call(cookie, "onPlaceTower", [str(uuid.uuid4()), t["id"], s])
            if self._ok(res):
                data = res.get("value") or data
        self.journal.log(f"Tower Defense : tourelles réorganisées (force {p_now:,.0f} -> "
                         f"{force(data.get('towers') or []):,.0f})".replace(",", " "),
                         to=audience([cookie]))
        return data

    def _spend(self, cookie, data, prefs):
        """Achète l'option la plus rentable tant qu'elle se rembourse sur l'horizon."""
        brain = self.brain(cookie)
        horizon = _num(prefs.get("tdHorizonDays"), config.TD_HORIZON_DAYS, 1, 365)
        keep = _num(prefs.get("tdKeepBits"), 0, 0, 10 ** 9)
        for _ in range(40):
            opts = [o for o in plan(data, brain, horizon, keep) if o["roi"] >= 1]
            if not opts:
                break
            best = opts[0]
            if not best["affordable"]:
                break  # on économise pour l'option la plus rentable
            if best["kind"] == "upgrade":
                res = self._call(cookie, "onUpgradeTower", [str(uuid.uuid4()), best["towerId"]])
            else:
                res = self._call(cookie, "onBuySlot", [str(uuid.uuid4()), best["slot"]])
            if not self._ok(res):
                self.journal.log_throttled(("td-buy",), f"Achat Tower Defense refusé : "
                                           f"{res.get('code') or res.get('message')}",
                                           "warning", every=3600, to=audience([cookie]))
                break
            data = res.get("value") or data
            self.journal.log(f"Tower Defense : {best['label']} acheté {best['cost']} bits "
                             f"(+{best['jalons']:.2f} jalon, rentable x{best['roi']:.1f})",
                             to=audience([cookie]))
            if best["kind"] == "slot":
                data = self._arrange(cookie, data)
        return data

    # -- vagues
    def run_wave(self, cookie, wave, speed=1):
        """Lance la vague `wave` et attend son issue. Renvoie la valeur de fin, ou None."""
        start = self._retry(cookie, "onStartTowerDefenseWave", [str(uuid.uuid4()), wave, speed],
                            {"TOWER_DEFENSE_WAVE_START_TOO_SOON": 2, "SERVER_UNAVAILABLE": 2}, 8)
        if not self._ok(start):
            self.journal.log_throttled(("td-wave",), f"Vague {wave} refusée : "
                                       f"{start.get('code') or start.get('message')}",
                                       "warning", every=3600, to=audience([cookie]))
            return None
        run_id = ((start.get("value") or {}).get("run") or {}).get("id")
        if not run_id:
            return None
        time.sleep(config.TD_WAVE_FIRST_POLL)
        # même requestId à chaque essai, comme le navigateur : la fin n'est comptée qu'une fois
        req = str(uuid.uuid4())
        tries = max(1, int(config.TD_WAVE_MAX_WAIT / config.TD_WAVE_POLL))
        end = self._retry(cookie, "onFinishTowerDefenseWave", [req, run_id],
                          {"TOWER_DEFENSE_WAVE_NOT_FINISHED": config.TD_WAVE_POLL,
                           "SERVER_UNAVAILABLE": 2}, tries)
        self._forget(cookie)
        if not self._ok(end):
            self.journal.log_throttled(("td-wave-end",), f"Fin de vague {wave} refusée : "
                                       f"{end.get('code') or end.get('message')}",
                                       "warning", every=3600, to=audience([cookie]))
            return None
        out = end.get("value") or {}
        self._remember(cookie, out.get("overview"))
        return out

    def choose_wave(self, data, brain):
        """(vague, est_un_jalon) à jouer maintenant, ou None."""
        record = data.get("recordJalon") or 0
        p = force(data.get("towers") or [])
        if p <= 0:
            return None
        if not brain.lost_frontier(record + 1, p):
            return record + 1, True
        kills = data.get("killBitsToday") or {}
        if record > 0 and (kills.get("bits") or 0) < (kills.get("maximumBits") or 0):
            return brain.best_farm(p, record), False
        return None

    def play(self, cookie, prefs):
        """Une étape de jeu pour un compte. Renvoie True si une vague a été jouée."""
        data = self.overview(cookie, max_age=0)
        if not data or data.get("isReadOnly"):
            return False
        self._set_activity(cookie, "organise les tourelles")
        data = self._arrange(cookie, data)
        if prefs.get("autoTowerDefenseUpgrade"):
            self._set_activity(cookie, "cherche le meilleur achat")
            data = self._spend(cookie, data, prefs)
        self._remember(cookie, data)

        brain = self.brain(cookie)
        pick = self.choose_wave(data, brain)
        if not pick:
            self._set_activity(cookie, "en attente : jalon bloqué et plafond de kills atteint")
            return False
        wave, frontier = pick
        p = force(data.get("towers") or [])
        speed = max(data.get("speeds") or [1])
        self._set_activity(cookie, f"joue la vague {wave}" + (" (jalon)" if frontier else " (farm)"))
        out = self.run_wave(cookie, wave, speed)
        if out is None:
            brain.observe(p, wave, False) if frontier else None  # évite de reboucler sur un refus
            self._set_activity(cookie, f"vague {wave} refusée")
            return False
        won = out.get("outcome") == "won"
        gained = out.get("bitsGained") or 0
        brain.observe(p, out.get("wave") or wave, won)
        with self._lock:
            brain.history.append({"t": int(time.time()), "wave": out.get("wave") or wave, "won": won,
                                  "bits": gained, "frontier": frontier,
                                  "record": bool(out.get("isNewRecord"))})
        self._set_activity(cookie, f"vague {wave} {'gagnée' if won else 'perdue'}")
        if frontier or out.get("isNewRecord"):
            self.journal.log(f"Tower Defense : jalon {wave} {'franchi !' if won else 'raté'}"
                             f"{f', +{gained} bits' if gained else ''}", to=audience([cookie]))
        return True

    def wave_loop(self):
        while True:
            played = False
            try:
                for u in self.store.users_with_cookie():
                    prefs = u.get("prefs") or {}
                    if prefs.get("autoTowerDefenseWaves"):
                        played |= self.play(u.get("cookie"), prefs)
                    elif prefs.get("autoTowerDefenseUpgrade"):
                        data = self.overview(u.get("cookie"), max_age=0)
                        if data and not data.get("isReadOnly"):
                            self._remember(u.get("cookie"), self._spend(u.get("cookie"), data, prefs))
            except Exception as e:
                print(f"towerdefense waves: {e}", flush=True)
            time.sleep(config.TD_WAVE_GAP if played else config.TD_WAVE_IDLE)

    # -- collecte automatique
    def loop(self):
        while True:
            time.sleep(config.TD_INTERVAL)
            try:
                self.collect_due()
            except Exception as e:
                print(f"towerdefense: {e}", flush=True)

    def collect_due(self):
        """Collecte pour chaque compte qui a activé l'option et dont les bits approchent du plafond."""
        for u in self.store.users_with_cookie():
            if not (u.get("prefs") or {}).get("autoTowerDefense"):
                continue
            cookie = u.get("cookie")
            data = self.overview(cookie, max_age=0)
            if not data or data.get("isReadOnly"):
                continue
            pending = data.get("pendingBits") or 0
            yield_h = data.get("yieldPerHour") or 0
            cap_at = _ts(data.get("idleCapReachedAt"))
            near_cap = data.get("isIdleCapReached") or (
                cap_at is not None and cap_at - time.time() < config.TD_INTERVAL * 2)
            if pending <= 0:
                continue
            if near_cap or pending >= max(5, yield_h):
                res = self.collect(cookie)
                if not self._ok(res):
                    self.journal.log_throttled(("td",),
                                               f"Collecte Tower Defense refusée : {res.get('code') or res.get('message')}",
                                               "warning", every=3600, to=audience([cookie]))

    # -- interface
    def snapshot(self, cookie, prefs=None):
        """État complet pour l'interface (None si illisible)."""
        data = self.overview(cookie)
        if not data:
            return None
        prefs = prefs or {}
        brain = self.brain(cookie)
        towers = data.get("towers") or []
        p = force(towers)
        horizon = _num(prefs.get("tdHorizonDays"), config.TD_HORIZON_DAYS, 1, 365)
        keep = _num(prefs.get("tdKeepBits"), 0, 0, 10 ** 9)
        cap_at = _ts(data.get("idleCapReachedAt"))
        kills = data.get("killBitsToday") or {}
        with self._lock:
            act = self._activity.get(account_key(cookie))
            history = list(brain.history)[-24:]
        pred = brain.waves(p)
        return {
            "bits": data.get("bitBalance"),
            "pending": data.get("pendingBits"),
            "yieldPerHour": data.get("yieldPerHour"),
            "recordJalon": data.get("recordJalon"),
            "totalPower": data.get("totalPower"),
            "totalDps": data.get("totalDamagePerSecond"),
            "idleCapHours": data.get("idleCapHours"),
            "capReached": bool(data.get("isIdleCapReached")),
            "capInSeconds": round(cap_at - time.time()) if cap_at else None,
            "readOnly": bool(data.get("isReadOnly")),
            "killBits": kills.get("bits"),
            "killBitsFull": kills.get("fullRateBits"),
            "killBitsMax": kills.get("maximumBits"),
            "speeds": data.get("speeds") or [1],
            "ownedSlots": data.get("ownedSlots") or [],
            "maximumSlots": data.get("maximumSlots"),
            "nextSlotCost": data.get("nextSlotCost"),
            "towers": [{
                "id": t["id"], "name": t["type"]["name"], "typeId": t["type"]["id"],
                "rarity": t.get("rarity"), "level": t.get("level"), "slot": t.get("slot"),
                "dps": round(_dps(t)), "range": t.get("rangeTiles"), "upgradeCost": t.get("upgradeCost"),
                "recycleValue": t.get("recycleValue"), "effect": t["type"].get("effect"),
                "sprite": t["type"].get("sprite"),
                "strength": round(strength(t, t["slot"])) if t.get("slot") is not None else None,
            } for t in towers],
            "map": {"w": MAP_W, "h": MAP_H, "path": PATH, "slots": SLOTS},
            "brain": {
                "force": round(p),
                "growth": round(brain.g, 3),
                "predictedWave": round(pred, 1) if pred is not None else None,
                "observations": len(brain.obs),
                "frontierBlocked": brain.lost_frontier((data.get("recordJalon") or 0) + 1, p),
                "horizonDays": horizon,
                "keepBits": keep,
                "bitsPerDay": round((data.get("yieldPerHour") or 0) * 24 + (kills.get("maximumBits") or 0)),
                "options": [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in o.items()}
                            for o in plan(data, brain, horizon, keep)[:6]],
                "activity": act[1] if act else None,
                "activityAge": round(time.time() - act[0]) if act else None,
                "history": history,
            },
        }


def _num(v, default, lo, hi):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default
