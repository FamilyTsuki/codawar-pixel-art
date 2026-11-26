"""Coffres, lootboxes, bits et outils de destruction (nouveautés Codawar).

Lecture : page /coffres du site (pageContext.data.overview : bits, coffres, inventaire, prix…).
Actions : module telefunc lootbox (onClaimDailyChest, onClaimHourlyChest, onOpenLootbox,
onOpenLootboxes) et onUseTool du module canvas. Chaque action porte un requestId (UUID)
pour que le serveur ne l'applique qu'une fois même si on la renvoie.
"""

import threading
import time
import uuid

from .. import config
from .journal import audience

CHEST_FUNCS = {"daily": "onClaimDailyChest", "hourly": "onClaimHourlyChest"}
CHEST_NAMES = {"daily": "coffre quotidien", "hourly": "coffre 2 h"}

ITEM_NAMES = {
    "pickaxe": "Pioche", "dynamite": "Dynamite", "bomb": "Bombe", "napalm": "Napalm", "nuke": "Nuke",
    "alliance-stone": "Pierre d'alliance", "mask-auto": "Masque auto", "fast-opening": "Ouverture rapide",
    "multi-opening": "Multi ouvertures", "pencil": "Crayon", "brush": "Pinceau", "line": "Trait", "text": "Texte",
}


def describe_rewards(value):
    """Texte court des gains renvoyés par une ouverture."""
    parts = []
    for r in (value or {}).get("rewards") or []:
        if not isinstance(r, dict):
            continue
        item = r.get("itemId") or r.get("toolId") or r.get("towerTypeId") or r.get("kind")
        qty = r.get("quantity") or r.get("bits") or r.get("amount")
        name = ITEM_NAMES.get(item, item or "?")
        parts.append(f"{name}{f' ×{qty}' if qty and qty != 1 else ''}")
    bits = (value or {}).get("bitsGained")
    if bits:
        parts.append(f"{bits} bits")
    return ", ".join(parts) or "rien"


class LootService:
    def __init__(self, client, store, journal, pacer):
        self.client = client
        self.store = store
        self.journal = journal
        self.pacer = pacer  # cadence partagée avec les poses de pixels
        self._lock = threading.Lock()
        self._cache = {}  # cookie -> (t, données de la page /coffres)

    # -- lecture
    def overview(self, cookie, max_age=config.LOOT_CACHE_TTL):
        """{player, overview} de la page /coffres, ou None si illisible."""
        if not cookie:
            return None
        with self._lock:
            hit = self._cache.get(cookie)
        if hit and time.time() - hit[0] <= max_age:
            return hit[1]
        try:
            data = self.client.page_data(cookie, "/coffres")
        except Exception:
            data = None
        if not data or "overview" not in data:
            return hit[1] if hit else None
        with self._lock:
            self._cache[cookie] = (time.time(), data)
        return data

    def _forget(self, cookie):
        with self._lock:
            self._cache.pop(cookie, None)

    # -- actions
    def _call(self, cookie, name, args, file=config.TELEFUNC_LOOTBOX, page="/coffres", canvas=""):
        res = self.client.call(name, args, cookie, canvas, file=file, page=page)
        self._forget(cookie)  # bits / inventaire ont changé
        return res

    def claim(self, cookie, kind):
        res = self._call(cookie, CHEST_FUNCS[kind], [str(uuid.uuid4())])
        if res.get("status") == "success":
            self.journal.log(f"{CHEST_NAMES[kind].capitalize()} ouvert : {describe_rewards(res.get('value'))}",
                             to=audience([cookie]))
        return res

    def open_lootboxes(self, cookie, many=False):
        name = "onOpenLootboxes" if many else "onOpenLootbox"
        res = self._call(cookie, name, [str(uuid.uuid4())])
        if res.get("status") == "success":
            v = res.get("value") or {}
            if not many and "reward" in v:
                v = {**v, "rewards": [v["reward"]]}
            self.journal.log(f"Lootbox{'es' if many else ''} ouverte{'s' if many else ''} : {describe_rewards(v)}",
                             to=audience([cookie]))
        return res

    def use_tool(self, cookie, canvas, tool, col, row):
        """Outil de destruction sur une case. allyOverwrite=False : on ne touche pas aux alliés.

        Un outil compte comme une pose pour Codawar : il passe par la cadence partagée,
        sinon il s'ajoute aux poses de la file dans la même seconde et le compte est exclu.
        """
        left = self.pacer.pause_remaining(cookie)
        if left:
            return {"status": "rejected", "code": "ACCOUNT_PAUSED",
                    "message": f"compte en pause {left} s (exclusion ou refus récent)"}
        if not self.pacer.wait_turn(cookie):
            return {"status": "rejected", "code": "PACED_OUT",
                    "message": "cadence maximale atteinte, réessaie dans un instant"}
        try:
            res = self._call(cookie, "onUseTool", [canvas, tool, int(col), int(row), str(uuid.uuid4()), False],
                             file=config.TELEFUNC_FILE, page=f"/canvas/{canvas}", canvas=canvas)
        finally:
            self.pacer.release(cookie)
        code = res.get("code") or ""
        if res.get("status") == "success":
            self.journal.log(f"{ITEM_NAMES.get(tool, tool)} utilisée en ({col}, {row}) sur « {canvas} »", "warning",
                             to=audience([cookie]))
        elif code == "STUDENT_TIMED_OUT":
            self.pacer.pause(cookie, config.TIMEOUT_PAUSE)
        elif code == "PLACEMENT_RATE_LIMITED":
            self.pacer.pause(cookie, 5)
        return res

    # -- coffres automatiques
    def loop(self):
        while True:
            time.sleep(config.LOOT_INTERVAL)
            try:
                self.claim_available()
            except Exception as e:
                print(f"loot: {e}", flush=True)

    def claim_available(self):
        """Ouvre les coffres des comptes qui ont coché le réglage, chacun avec SON cookie."""
        cookies = {u["cookie"] for u in self.store.users_with_cookie()
                   if u.get("cookie") and (u.get("prefs") or {}).get("autoChests")}
        for cookie in cookies:
            data = self.overview(cookie, max_age=0)
            ov = (data or {}).get("overview") or {}
            if ov.get("isTimedOut"):
                continue
            for kind in ("daily", "hourly"):
                if (ov.get(f"{kind}Chest") or {}).get("isAvailable"):
                    res = self.claim(cookie, kind)
                    if res.get("status") != "success":
                        self.journal.log_throttled(("coffre", kind),
                                                   f"Coffre non ouvert ({kind}) : {res.get('code') or res.get('message')}",
                                                   "warning", every=3600, to=audience([cookie]))
