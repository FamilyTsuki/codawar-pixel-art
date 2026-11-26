"""Tower Defense Codawar : lecture de l'état et collecte automatique des bits.

Le mode Tower Defense produit des bits passivement (yieldPerHour), plafonnés au bout de
idleCapHours (12 h par défaut) : passé ce cap, les bits en trop sont perdus. Ce service
relève l'état de chaque compte et collecte les bits en attente avant le plafond, pour
qu'aucun ne soit gaspillé. Il ne joue PAS les vagues : leur issue est décidée par le
serveur selon la puissance des tourelles, il n'y a rien à automatiser sans risque.
"""

import threading
import time
import uuid

from .. import config

TD_FILE = "/src/modules/tower-defense/infrastructure/browser-tower-defense-gateway.telefunc.ts"
TD_PAGE = "/tower-defense"


def _ts(iso):
    if not iso:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


class TowerDefense:
    def __init__(self, client, store, journal, settings):
        self.client = client
        self.store = store
        self.journal = journal
        self.settings = settings  # callable -> réglages globaux
        self._lock = threading.Lock()
        self._cache = {}  # cookie -> (t, overview)

    def _call(self, cookie, name, args):
        return self.client.call(name, args, cookie, "", file=TD_FILE, page=TD_PAGE)

    def overview(self, cookie, max_age=config.TD_CACHE_TTL):
        """État du Tower Defense d'un compte, ou None si illisible."""
        if not cookie:
            return None
        with self._lock:
            hit = self._cache.get(cookie)
        if hit and time.time() - hit[0] <= max_age:
            return hit[1]
        res = self._call(cookie, "onReadTowerDefense", [])
        if res.get("status") != "success":
            return hit[1] if hit else None
        data = res.get("value") or {}
        with self._lock:
            self._cache[cookie] = (time.time(), data)
        return data

    def _forget(self, cookie):
        with self._lock:
            self._cache.pop(cookie, None)

    def collect(self, cookie):
        """Collecte les bits en attente. Renvoie la réponse telle quelle."""
        res = self._call(cookie, "onCollectYield", [str(uuid.uuid4())])
        self._forget(cookie)
        if res.get("status") == "success":
            v = res.get("value") or {}
            gained = v.get("collected") or v.get("bitsGained") or v.get("pendingBits")
            self.journal.log(f"Tower Defense : {gained if gained is not None else '?'} bits collectés")
        return res

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
            # collecte si le plafond approche, ou si assez de bits se sont accumulés
            if pending <= 0:
                continue
            if near_cap or pending >= max(5, yield_h):
                res = self.collect(cookie)
                if res.get("status") != "success":
                    self.journal.log_throttled(("td", cookie[-8:]),
                                               f"Collecte Tower Defense refusée : {res.get('code') or res.get('message')}",
                                               "warning", every=3600)

    def snapshot(self, cookie):
        """Champs d'état pour l'interface (None si illisible)."""
        data = self.overview(cookie)
        if not data:
            return None
        cap_at = _ts(data.get("idleCapReachedAt"))
        return {
            "bits": data.get("bitBalance"),
            "pending": data.get("pendingBits"),
            "yieldPerHour": data.get("yieldPerHour"),
            "recordJalon": data.get("recordJalon"),
            "totalPower": data.get("totalPower"),
            "idleCapHours": data.get("idleCapHours"),
            "capReached": bool(data.get("isIdleCapReached")),
            "capInSeconds": round(cap_at - time.time()) if cap_at else None,
            "readOnly": bool(data.get("isReadOnly")),
        }
