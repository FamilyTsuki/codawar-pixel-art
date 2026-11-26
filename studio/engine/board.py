"""Lecture du canvas Codawar et cache des dernières relectures."""

import json
import threading
import time

from .. import config
from ..palette import color_at
from .wallet import account_key

CELL_STRIDE = 6  # [col, row, colorIdx, ownerIdx, protection, revision]
ME = "(vous)"    # propriétaire noté localement pour nos propres poses


def parse_board(value, t):
    """Convertit la réponse onSynchronizePackedCanvas (cells peut être un texte JSON).

    pixels : {(col, row): [hex, owner, protection, revision]} ; revision est incrémentée à chaque pose.
    """
    cells, owners = value["pixels"]["cells"], value["pixels"]["owners"]
    if isinstance(cells, str):  # format « packed » : cellules envoyées en texte JSON
        cells = json.loads(cells)
    pixels = {}
    for i in range(0, len(cells), CELL_STRIDE):
        oi = cells[i + 3]
        pixels[(cells[i], cells[i + 1])] = [
            color_at(cells[i + 2]),
            owners[oi]["name"] if 0 <= oi < len(owners) else "",
            cells[i + 4],
            cells[i + 5],
        ]
    # Les zones peuvent avoir des indices négatifs (canvas agrandi à gauche / en haut) :
    # les coordonnées des pixels sont absolues, la zone (-1, 0) couvre les colonnes -100..-1.
    zones = value.get("zones") or [{"zoneColumn": 0, "zoneRow": 0}]
    zs = config.ZONE_SIZE
    cols = [z["zoneColumn"] for z in zones]
    rows = [z["zoneRow"] for z in zones]
    return {
        "t": t,
        "x0": min(cols) * zs,  # première colonne de la boîte englobante
        "y0": min(rows) * zs,  # première ligne
        "w": (max(cols) - min(cols) + 1) * zs,
        "h": (max(rows) - min(rows) + 1) * zs,
        "zones": sorted({(z["zoneColumn"], z["zoneRow"]) for z in zones}),
        "pixels": pixels,
        "wallet": value.get("wallet"),
        "closure": value.get("closure"),
    }


def in_board(board, c, r):
    """Vrai si la case (c, r) appartient à une zone ouverte du canvas (forme pas forcément rectangulaire)."""
    zs = config.ZONE_SIZE
    zones = board.get("_zone_set")
    if zones is None:
        zones = board["_zone_set"] = set(board.get("zones") or [(0, 0)])
    return (c // zs, r // zs) in zones


class BoardCache:
    """Dernière lecture de chaque canvas ; prévient les observateurs à chaque nouvelle lecture."""

    def __init__(self, client, default_cookie):
        self.client = client
        self.default_cookie = default_cookie  # callable -> cookie de repli
        self._lock = threading.Lock()
        self._boards = {}     # canvas -> board
        self._observers = []  # fn(canvas, board)

    def subscribe(self, fn):
        self._observers.append(fn)

    def get(self, canvas):
        with self._lock:
            return self._boards.get(canvas)

    def items(self):
        with self._lock:
            return list(self._boards.items())

    def canvases(self):
        with self._lock:
            return set(self._boards)

    def fetch(self, canvas, max_age=0, cookie=None):
        """Board de moins de max_age s, relu si besoin ; ancienne lecture (ou None) si échec."""
        cached = self.get(canvas)
        if cached and time.time() - cached["t"] <= max_age:
            return cached
        cookie = cookie or self.default_cookie()
        res = self.client.call(config.SYNC_FUNCTION, [canvas], cookie, canvas, timeout=30)
        if res.get("status") != "success":
            return cached
        board = parse_board(res["value"], time.time())
        board["account"] = account_key(cookie)  # compte dont le portefeuille a été lu
        for fn in self._observers:  # avant publication : aucune pose ne modifie le board pendant ce temps
            fn(canvas, board)
        with self._lock:
            self._boards[canvas] = board
        return board

    def record_placement(self, canvas, col, row, color):
        """Reporte localement une pose réussie (sans attendre la prochaine relecture)."""
        with self._lock:
            board = self._boards.get(canvas)
            if not board:
                return
            old = board["pixels"].get((col, row))
            board["pixels"][(col, row)] = [color, ME, old[2] if old else 0, (old[3] if old else 0) + 1]

    def is_closed(self, canvas):
        board = self.get(canvas)
        return bool(board and (board.get("closure") or {}).get("isClosedForPlayer"))
