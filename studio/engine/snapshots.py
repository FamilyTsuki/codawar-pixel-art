"""Instantanés périodiques du canvas pour le timelapse (24/24)."""

import base64
import time
import zlib

from .. import config
from ..palette import COLOR_POS


def encode_grid(board):
    """Grille compressée : 1 octet = index couleur ; 0 = blanc/vide."""
    w, h = board["w"], board["h"]
    x0, y0 = board.get("x0", 0), board.get("y0", 0)  # coin haut-gauche (peut être négatif)
    grid = bytearray(w * h)
    for (c, r), v in board["pixels"].items():
        c, r = c - x0, r - y0
        if 0 <= c < w and 0 <= r < h:
            grid[r * w + c] = COLOR_POS.get(v[0], 0) & 0xFF
    return base64.b64encode(zlib.compress(bytes(grid), 6)).decode()


def decode_grid(data):
    return list(zlib.decompress(base64.b64decode(data)))


class Snapshotter:
    def __init__(self, boards, store, interval=config.SNAPSHOT_INTERVAL):
        self.boards = boards
        self.store = store
        self.interval = interval
        self._last = {}  # canvas -> t du dernier instantané
        self._last_data = {}  # canvas -> (données, w, h, x0, y0) du dernier instantané (anti-doublon)

    def loop(self):
        while True:
            time.sleep(min(self.interval, 30))
            self.tick(time.time())

    def tick(self, now):
        for canvas, board in self.boards.items():
            if now - self._last.get(canvas, 0) < self.interval:
                continue
            self._last[canvas] = now
            try:
                sig = (encode_grid(board), board["w"], board["h"], board.get("x0", 0), board.get("y0", 0))
                if self._last_data.get(canvas) == sig:
                    continue  # canvas inchangé : pas de nouvel instantané
                self._last_data[canvas] = sig
                self.store.add_snapshot(canvas, board["t"], *sig[1:3], sig[0], *sig[3:])
            except Exception as e:
                print(f"snapshot: {e}", flush=True)
