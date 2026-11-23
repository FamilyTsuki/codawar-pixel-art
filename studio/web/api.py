"""Routes de l'API JSON.

Chaque route est une méthode (Request) -> Response, déclarée dans la table ROUTES.
Ajouter une route = ajouter une méthode et une ligne dans la table, sans toucher au handler HTTP.
"""

import time
from dataclasses import dataclass, field

from .. import config
from ..palette import COLOR_INDEX
from ..codawar import normalize_cookie
from ..security import check_pw, session_cookie
from ..engine.snapshots import decode_grid



@dataclass
class Request:
    sid: str | None
    ip: str
    query: dict = field(default_factory=dict)
    body: dict = field(default_factory=dict)
    user: dict | None = None


@dataclass
class Response:
    data: dict
    code: int = 200
    cookie: str | None = None


def ok(**kwargs):
    return Response({"success": True}, **kwargs)


def fail(code, message=None, **extra):
    data = {"success": False, **extra}
    if message:
        data["message"] = message
    return Response(data, code)


@dataclass(frozen=True)
class Route:
    handler: str            # nom de la méthode d'Api
    auth: bool = True       # session requise
    throttled: bool = False  # limite anti force brute (connexion / inscription)


ROUTES = {
    ("GET", "/api/me"): Route("me", auth=False),
    ("GET", "/api/canvases"): Route("canvases"),
    ("GET", "/api/layers"): Route("layers"),
    ("GET", "/api/sprites"): Route("sprites"),
    ("GET", "/api/status"): Route("status"),
    ("GET", "/api/stats"): Route("stats"),
    ("GET", "/api/heatmap"): Route("heatmap"),
    ("GET", "/api/timelapse"): Route("timelapse"),
    ("GET", "/api/timelapse/frame"): Route("timelapse_frame"),
    ("POST", "/api/register"): Route("register", auth=False, throttled=True),
    ("POST", "/api/login"): Route("login", auth=False, throttled=True),
    ("POST", "/api/logout"): Route("logout", auth=False),
    ("POST", "/api/account"): Route("account"),
    ("POST", "/api/sprites/save"): Route("save_sprite"),
    ("POST", "/api/sprites/delete"): Route("delete_sprite"),
    ("POST", "/api/canvas"): Route("canvas"),
    ("POST", "/api/place"): Route("place"),
    ("POST", "/api/queue/clear"): Route("clear_queue"),
    ("POST", "/api/pause"): Route("pause"),
    ("POST", "/api/guard/run"): Route("run_guard"),
    ("POST", "/api/settings"): Route("settings"),
    ("POST", "/api/layers/save"): Route("save_layer"),
    ("POST", "/api/layers/delete"): Route("delete_layer"),
    ("POST", "/api/layers/reorder"): Route("reorder_layers"),
}


class Api:
    def __init__(self, store, engine, directory, limiter):
        self.store = store
        self.engine = engine
        self.directory = directory
        self.limiter = limiter

    # -- aiguillage
    def handle(self, method, path, req):
        route = ROUTES.get((method, path))
        if route and route.throttled:
            if self.limiter.blocked(req.ip):
                return fail(429, "trop de tentatives, réessaie dans 15 min")
            time.sleep(config.LOGIN_SLOWDOWN)  # ralentit la force brute
        if route is None or route.auth:
            req.user = self.store.session_user(req.sid)
            if not req.user:
                return Response({"auth": False, "message": "connexion requise"}, 401)
        if route is None:
            return fail(404)
        return getattr(self, route.handler)(req)

    # -- aides
    def _cookie(self, req):
        return req.user.get("cookie") or ""

    def _canvas(self, req, explicit=None):
        """Canvas demandé, sinon celui du compte, sinon celui des réglages."""
        return explicit or req.user.get("canvas") or self.store.settings["canvas"]

    def _query_canvas(self, req):
        return self._canvas(req, req.query.get("canvas"))

    def _login_success(self, req, user):
        self.limiter.reset(req.ip)
        tok = self.store.new_session(user["id"])
        return Response({"success": True, "username": user["username"]}, cookie=session_cookie(tok))

    # ---------------------------------------------------------------- authentification
    def me(self, req):
        user = self.store.session_user(req.sid)
        if not user:
            return Response({"auth": False, "backend": self.store.backend_name})
        return Response({"auth": True, "username": user["username"], "hasCookie": bool(user.get("cookie")),
                         "canvas": user.get("canvas", self.store.settings["canvas"]),
                         "prefs": user.get("prefs", {}), "backend": self.store.backend_name})

    def register(self, req):
        un, pw = (req.body.get("username") or "").strip(), req.body.get("password") or ""
        if len(un) < config.MIN_USERNAME or len(pw) < config.MIN_PW:
            return fail(400, f"pseudo ≥ {config.MIN_USERNAME} et mot de passe ≥ {config.MIN_PW} caractères")
        user = self.store.create_user(un, pw)
        if not user:
            return fail(409, "pseudo déjà pris")
        return self._login_success(req, user)

    def login(self, req):
        user = self.store.user_by_name((req.body.get("username") or "").strip())
        if not user or not check_pw(req.body.get("password") or "", user["pw"]):
            self.limiter.fail(req.ip)
            return fail(401, "pseudo ou mot de passe incorrect")
        return self._login_success(req, user)

    def logout(self, req):
        self.store.delete_session(req.sid)
        return ok(cookie=session_cookie(clear=True))

    def account(self, req):
        """Cookie Codawar + canvas par défaut + préférences du compte."""
        u, values = req.user, req.body
        if "cookie" in values:  # tolère guillemets, préfixe, clé absente
            try:
                values = {**values, "cookie": normalize_cookie(values.get("cookie"))}
            except ValueError as e:
                return fail(400, str(e))
        self.store.update_account(u, values)
        if u.get("cookie"):  # apprend le pseudo Codawar de ce cookie
            self.directory.learn_async(u["cookie"], u["canvas"])
        return Response({"success": True, "hasCookie": bool(u.get("cookie")), "canvas": u.get("canvas")})

    # ---------------------------------------------------------------- lecture
    def canvases(self, req):
        lst = self.directory.list(self._cookie(req), self._canvas(req))
        return Response({"success": lst is not None, "canvases": lst or []})

    def layers(self, req):
        return Response({"layers": self.store.layers_of(req.user["id"]), "settings": self.store.settings,
                         "backend": self.store.backend_name,
                         "canvas": req.user.get("canvas", self.store.settings["canvas"]),
                         "cookie": self._cookie(req)})

    def sprites(self, req):
        return Response({"sprites": self.store.sprites_of(req.user["id"])})

    def status(self, req):
        return Response(self.engine.status(self._query_canvas(req)))

    def stats(self, req):
        canvas, ck = self._query_canvas(req), self._cookie(req)
        board = self.engine.boards.fetch(canvas, max_age=3, cookie=ck)
        me = self.directory.nickname(ck) or req.user["username"]
        rows, summary = self.engine.stats.report(canvas, board, me)
        return Response({"rows": rows, "summary": summary, "me": me})

    def heatmap(self, req):
        canvas = self._query_canvas(req)
        board = self.engine.boards.fetch(canvas, max_age=10, cookie=self._cookie(req))
        cells = self.engine.stats.heatmap(canvas)
        return Response({"w": board["w"] if board else 100, "h": board["h"] if board else 100,
                         "x0": board.get("x0", 0) if board else 0, "y0": board.get("y0", 0) if board else 0,
                         "cells": cells, "max": max((n for *_, n in cells), default=0)})

    def timelapse(self, req):
        return Response({"frames": self.store.list_snapshots(self._query_canvas(req)), "palette": COLOR_INDEX})

    def timelapse_frame(self, req):
        sid = req.query.get("id", "")
        snap = self.store.get_snapshot(int(sid)) if sid.isdigit() else None
        if not snap:
            return fail(404)
        return Response({"w": snap["w"], "h": snap["h"], "t": snap["t"],
                         "palette": COLOR_INDEX, "idx": decode_grid(snap["data"])})

    def canvas(self, req):
        """Canvas compacté : propriétaires dédoublonnés, cellules à plat [c, r, hex, owner, prot]."""
        canvas = self._canvas(req, req.body.get("canvas"))
        board = self.engine.boards.fetch(canvas, max_age=float(req.body.get("maxAge", 2)), cookie=self._cookie(req))
        if board is None:
            return Response({"success": False, "message": "lecture impossible (cookie Codawar ? nom du canvas ?)"})
        owners, oidx, cells = [], {}, []
        for (c, r), (h, o, prot, *_rest) in board["pixels"].items():
            if o not in oidx:
                oidx[o] = len(owners)
                owners.append(o)
            cells.extend((c, r, h, oidx[o], prot))
        return Response({"success": True, "w": board["w"], "h": board["h"],
                         "x0": board.get("x0", 0), "y0": board.get("y0", 0), "zones": board.get("zones"),
                         "owners": owners, "cells": cells,
                         "wallet": board["wallet"], "closure": board.get("closure")})

    # ---------------------------------------------------------------- moteur
    def place(self, req):
        ck = self._cookie(req)
        if not ck:
            return fail(400, "ajoute ton cookie Codawar dans Réglages")
        canvas = self._canvas(req, req.body.get("canvas"))
        n = self.engine.enqueue(canvas, req.body.get("pixels", []), front=req.body.get("front", True), cookie=ck)
        return Response({"success": True, "queued": n})

    def clear_queue(self, req):
        self.engine.clear_queue()
        return ok()

    def pause(self, req):
        paused = bool(req.body.get("paused", True))
        self.engine.placer.set_paused(paused)
        return Response({"success": True, "paused": paused})

    def run_guard(self, req):
        self.engine.guard.kick()
        return ok()

    def settings(self, req):
        """Réglages moteur partagés (le cookie est par compte : /api/account)."""
        return Response({"success": True, "settings": self.store.update_settings(req.body)})

    # ---------------------------------------------------------------- sprites et calques
    def save_sprite(self, req):
        sp = req.body.get("sprite")
        if not sp or "id" not in sp:
            return fail(400)
        sp["owner"] = req.user["id"]
        self.store.save_sprite(sp)
        return ok()

    def delete_sprite(self, req):
        self.store.delete_sprite_of(req.user["id"], req.body.get("id"))
        return ok()

    def save_layer(self, req):
        layer, uid = req.body.get("layer"), req.user["id"]
        if not layer or "id" not in layer:
            return fail(400, "calque invalide")
        layer["owner"] = uid
        old = self.store.find_layer(layer["id"])
        if old and old.get("owner") not in (None, uid):
            return fail(403, "calque d'un autre compte")
        self.store.upsert_layer(layer)
        if old and _layer_moved_or_disabled(old, layer):
            self.engine.guard.drop_targets()
        else:
            self.engine.guard.kick()
        return ok()

    def delete_layer(self, req):
        self.store.delete_layer_of(req.user["id"], req.body.get("id"))
        self.engine.guard.drop_targets()
        return ok()

    def reorder_layers(self, req):
        self.store.reorder_layers(req.body.get("ids", []))
        self.engine.guard.kick()
        return ok()


def _layer_moved_or_disabled(old, new):
    """Les anciennes cibles du calque ne sont plus valables."""
    disabled = old.get("active") and not new.get("active")
    moved = old.get("x") != new.get("x") or old.get("y") != new.get("y")
    return disabled or moved
