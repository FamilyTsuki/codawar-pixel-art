"""Statistiques des joueurs et carte de chaleur, construites en comparant deux relectures du canvas."""

import base64
import collections
import json
import threading
import time
import zlib

from .. import config
from .board import ME


def _pack(obj):
    return base64.b64encode(zlib.compress(json.dumps(obj).encode())).decode()


def _unpack(blob):
    return json.loads(zlib.decompress(base64.b64decode(blob)).decode())


class StatsTracker:
    def __init__(self, stats=None, heat_blobs=None):
        self._lock = threading.Lock()
        self.stats = stats or {}  # canvas -> {owner -> {observed, first, last, maxRate, colors, hours}}
        self._revisions = {}      # canvas -> {(c, r) -> revision} (dernière relecture)
        self._last_t = {}         # canvas -> t de la dernière relecture
        self.heat = {}            # canvas -> {"c,r": nb de changements}
        for canvas, blob in (heat_blobs or {}).items():
            try:
                self.heat[canvas] = _unpack(blob)
            except Exception:
                pass

    # -- persistance
    def to_runtime(self):
        with self._lock:
            return {"stats": self.stats,
                    "heat": {cv: _pack(h) for cv, h in self.heat.items() if h}}

    # -- observation (abonné à BoardCache)
    def observe(self, canvas, board):
        now = board["t"]
        with self._lock:
            prev = self._revisions.get(canvas)
            cur = {k: v[3] for k, v in board["pixels"].items()}
            self._revisions[canvas] = cur
            if prev is None:  # première relecture : juste une base de comparaison
                self._last_t[canvas] = now
                return
            placers, colors = self._count_changes(canvas, board, prev, cur)
            dt = max(0.001, now - self._last_t.get(canvas, now))
            self._last_t[canvas] = now
            self._record(canvas, placers, colors, now, dt)

    def _count_changes(self, canvas, board, prev, cur):
        placers = collections.Counter()
        colors = collections.defaultdict(collections.Counter)  # owner -> {hex: n}
        heat = self.heat.setdefault(canvas, {})
        for k, rev in cur.items():
            old = prev.get(k)
            if old is not None and rev <= old:
                continue  # case inchangée
            hx, owner = board["pixels"][k][0], board["pixels"][k][1] or "?"
            placers[owner] += 1
            colors[owner][hx] += 1
            hk = f"{k[0]},{k[1]}"  # carte de chaleur : +1 par changement sur la case
            heat[hk] = heat.get(hk, 0) + 1
        return placers, colors

    def _record(self, canvas, placers, colors, now, dt):
        hour = time.localtime(now).tm_hour
        st = self.stats.setdefault(canvas, {})
        for owner, n in placers.items():
            s = st.setdefault(owner, {"observed": 0, "first": now, "last": now, "maxRate": 0.0})
            s.setdefault("colors", {})
            s.setdefault("hours", [0] * 24)
            s["observed"] += n
            s["last"] = now
            s["maxRate"] = max(s["maxRate"], n / dt)
            s["hours"][hour] += n
            for hx, k in colors[owner].items():
                s["colors"][hx] = s["colors"].get(hx, 0) + k

    # -- lecture pour l'interface
    def heatmap(self, canvas):
        with self._lock:
            return [[*map(int, k.split(",")), n] for k, n in self.heat.get(canvas, {}).items()]

    def report(self, canvas, board, me="moi"):
        """(lignes par joueur, résumé du canvas)."""
        owned, owned_colors = collections.Counter(), collections.Counter()
        if board:
            for v in board["pixels"].values():
                owned[me if v[1] == ME else (v[1] or "?")] += 1
                owned_colors[v[0]] += 1
        with self._lock:
            st = {k: dict(v) for k, v in self.stats.get(canvas, {}).items()}
        now = time.time()
        rows = [_player_row(name, owned.get(name, 0), st.get(name, {}), now) for name in set(owned) | set(st)]
        rows.sort(key=lambda r: (-r["owned"], -r["observed"]))
        total = len(board.get("zones") or [(0, 0)]) * config.ZONE_SIZE ** 2 if board else 0
        painted = len(board["pixels"]) if board else 0
        summary = {
            "painted": painted,
            "total": total,
            "coverage": round(painted / total * 100, 1) if total else 0,
            "players": len([r for r in rows if r["owned"] > 0 or r["observed"] > 0]),
            "byColor": owned_colors.most_common(12),
            "observedTotal": sum(r["observed"] for r in rows),
        }
        return rows, summary


def _player_row(name, owned, s, now):
    obs = s.get("observed", 0)
    span = max(0.001, s.get("last", 0) - s.get("first", 0))
    mx = s.get("maxRate", 0.0)
    cols = s.get("colors", {})
    return {
        "name": name,
        "owned": owned,
        "observed": obs,
        "fastestGap": round(1 / mx, 2) if mx > 0 else None,
        "perMin": round(obs / span * 60, 1) if obs > 1 and span > 1 else None,
        "avgGap": round(span / obs, 1) if obs > 1 else None,
        "lastSeen": round(now - s["last"]) if s.get("last") else None,  # secondes depuis sa dernière pose vue
        "topColor": max(cols, key=cols.get) if cols else None,
        "nColors": len(cols),
    }
