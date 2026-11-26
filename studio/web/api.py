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
    ("GET", "/api/timelapse/frames"): Route("timelapse_frames"),
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
    ("GET", "/api/projects"): Route("projects"),
    ("POST", "/api/projects/create"): Route("project_create"),
    ("POST", "/api/projects/rename"): Route("project_rename"),
    ("POST", "/api/projects/invite"): Route("project_invite"),
    ("POST", "/api/projects/remove"): Route("project_remove"),
    ("POST", "/api/projects/delete"): Route("project_delete"),
    ("GET", "/api/invite"): Route("invite_peek", auth=False),
    ("POST", "/api/cookie/test"): Route("cookie_test"),
    ("POST", "/api/invite/accept"): Route("invite_accept"),
    ("POST", "/api/projects/link"): Route("project_link"),
    ("POST", "/api/projects/link/revoke"): Route("project_link_revoke"),
    ("POST", "/api/layers/project"): Route("layer_project"),
    ("GET", "/api/loot"): Route("loot"),
    ("POST", "/api/loot/claim"): Route("loot_claim"),
    ("POST", "/api/loot/open"): Route("loot_open"),
    ("POST", "/api/tool/use"): Route("tool_use"),
    ("GET", "/api/td"): Route("td"),
    ("POST", "/api/td/collect"): Route("td_collect"),
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

    def cookie_test(self, req):
        """Vérifie un cookie auprès de Codawar et renvoie le pseudo reconnu.

        Sert à l'assistant de première connexion : on sait tout de suite si le cookie
        marche, au lieu de le découvrir plus tard dans le journal. Le cookie lui-même
        n'est jamais renvoyé, seulement ce qu'il permet de lire.
        """
        raw = req.body.get("cookie")
        if raw:
            try:
                cookie = normalize_cookie(raw)
            except ValueError as e:
                return Response({"success": False, "message": str(e)})
        else:
            cookie = self._cookie(req)
        if not cookie:
            return Response({"success": False, "message": "aucun cookie à tester"})
        canvas = self._canvas(req, req.body.get("canvas"))
        res = self.engine.boards.client.call(config.SYNC_FUNCTION, [canvas], cookie, canvas, timeout=20)
        if res.get("status") != "success":
            return Response({"success": False,
                             "message": res.get("message") or res.get("code")
                             or "cookie refusé par Codawar"})
        wallet = (res.get("value") or {}).get("wallet") or {}
        player = (self.engine.loot.overview(cookie, max_age=0) or {}).get("player") or {}
        return Response({"success": True, "canvas": canvas,
                         "nickname": player.get("nickname"),
                         "credits": wallet.get("balance"),
                         "unlimited": bool(wallet.get("isUnlimited")),
                         "mustAcceptRules": bool(player.get("mustAcceptRules"))})

    # ---------------------------------------------------------------- lecture
    # ---------------------------------------------------------------- projets de groupe
    def _project_view(self, p, uid):
        """Projet tel que le voit un membre : membres nommés, calques, crédits du groupe."""
        members = []
        for mid in p.get("members", []):
            u = self.store.users.get(mid) or {}
            acc = self.engine.wallet.account(p["canvas"], u.get("cookie", "")) if u.get("cookie") else None
            members.append({"id": mid, "username": u.get("username", "?"),
                            "owner": mid == p["owner"], "me": mid == uid,
                            "hasCookie": bool(u.get("cookie")),
                            "credits": (acc or {}).get("credits"),
                            "unlimited": bool((acc or {}).get("unlimited"))})
        layers = [l for l in self.store.layers if l.get("project") == p["id"]]
        pooled = sum(m["credits"] or 0 for m in members)
        links = [{"token": l["token"], "uses": l["uses"], "maxUses": l["maxUses"],
                  "expires": l["expires"], "valid": l["valid"]}
                 for l in self.store.project_links(p["id"], uid)]
        return {"id": p["id"], "name": p["name"], "canvas": p["canvas"],
                "owner": p["owner"], "isOwner": p["owner"] == uid, "links": links,
                "members": members, "layers": len(layers),
                "pooledCredits": pooled,
                "unlimited": any(m["unlimited"] for m in members)}

    def projects(self, req):
        uid = req.user["id"]
        return Response({"projects": [self._project_view(p, uid) for p in self.store.projects_of(uid)]})

    def project_create(self, req):
        name = (req.body.get("name") or "").strip()
        if len(name) < 2:
            return fail(400, "nom trop court")
        p = self.store.create_project(req.user["id"], name, self._canvas(req, req.body.get("canvas")))
        self.engine.journal.log(f"Projet « {p['name']} » créé par {req.user['username']}")
        return Response({"success": True, "project": self._project_view(p, req.user["id"])})

    def project_rename(self, req):
        p = self.store.rename_project(req.body.get("id"), req.user["id"], req.body.get("name"))
        if not p:
            return fail(403, "seul le créateur peut renommer")
        return Response({"success": True, "project": self._project_view(p, req.user["id"])})

    def project_invite(self, req):
        p, err = self.store.project_invite(req.body.get("id"), req.user["id"], req.body.get("username"))
        if err:
            return Response({"success": False, "message": err})
        self.engine.guard.drop_targets()  # les crédits du nouveau membre entrent dans le pot
        self.engine.journal.log(f"« {req.body.get('username')} » rejoint le projet « {p['name']} »")
        return Response({"success": True, "project": self._project_view(p, req.user["id"])})

    def project_remove(self, req):
        p, err = self.store.project_remove(req.body.get("id"), req.user["id"], req.body.get("member"))
        if err:
            return Response({"success": False, "message": err})
        self.engine.guard.drop_targets()
        return Response({"success": True, "project": self._project_view(p, req.user["id"])})

    def project_delete(self, req):
        if not self.store.delete_project(req.body.get("id"), req.user["id"]):
            return fail(403, "seul le créateur peut supprimer")
        self.engine.guard.drop_targets()
        return ok()

    def project_link(self, req):
        """Crée un lien d'invitation (le créateur seul)."""
        link, err = self.store.project_invite_link(
            req.body.get("id"), req.user["id"], int(req.body.get("maxUses") or 10))
        if err:
            return Response({"success": False, "message": err})
        return Response({"success": True, "link": link})

    def project_link_revoke(self, req):
        okr = self.store.project_revoke_link(req.body.get("id"), req.user["id"], req.body.get("token"))
        return Response({"success": okr}) if okr else fail(403, "seul le créateur peut révoquer")

    def invite_peek(self, req):
        """Aperçu d'une invitation AVANT connexion : juste de quoi décider d'entrer.

        Volontairement pauvre (nom du projet, canvas, nombre de membres) : le jeton
        circule par message, il ne doit rien révéler de plus sur le groupe.
        """
        found, err = self.store.find_invite(req.query.get("token"))
        if err:
            return Response({"success": False, "message": err})
        p, _link = found
        user = self.store.session_user(req.sid)
        return Response({"success": True, "name": p["name"], "canvas": p["canvas"],
                         "members": len(p.get("members", [])),
                         "authed": bool(user),
                         "already": bool(user and user["id"] in p.get("members", []))})

    def invite_accept(self, req):
        p, err = self.store.redeem_invite(req.body.get("token"), req.user["id"])
        if err:
            return Response({"success": False, "message": err})
        self.engine.guard.drop_targets()  # les crédits du nouveau membre entrent dans le pot
        self.engine.journal.log(f"« {req.user['username']} » rejoint « {p['name']} » par lien")
        return Response({"success": True, "project": self._project_view(p, req.user["id"])})

    def layer_project(self, req):
        """Rattache un calque à un projet, ou l'en détache (project = null)."""
        uid, lid, pid = req.user["id"], req.body.get("id"), req.body.get("project")
        layer = self.store.find_layer(lid)
        if not self.store.can_edit_layer(uid, layer):
            return fail(403, "calque d'un autre compte")
        if pid and not self.store.is_member(uid, pid):
            return fail(403, "pas membre de ce projet")
        layer = {**layer}
        if pid:
            layer["project"] = pid
        else:
            layer.pop("project", None)
        self.store.upsert_layer(layer)
        self.engine.guard.drop_targets()
        return Response({"success": True})

    # ---------------------------------------------------------------- coffres / inventaire / outils
    def loot(self, req):
        data = self.engine.loot.overview(self._cookie(req), max_age=0 if req.query.get("fresh") else config.LOOT_CACHE_TTL)
        if not data:
            return Response({"success": False, "message": "page /coffres illisible (cookie Codawar ?)"})
        player = data.get("player") or {}
        return Response({"success": True, "overview": data.get("overview"),
                         "player": {k: player.get(k) for k in ("nickname", "timeout", "ban", "mustAcceptRules")},
                         "tools": config.DESTRUCTION_TOOLS})

    def loot_claim(self, req):
        kind = req.body.get("kind")
        if kind not in ("daily", "hourly"):
            return fail(400, "kind = daily | hourly")
        return Response(self.engine.loot.claim(self._cookie(req), kind))

    def loot_open(self, req):
        return Response(self.engine.loot.open_lootboxes(self._cookie(req), bool(req.body.get("many"))))

    def tool_use(self, req):
        tool = req.body.get("tool")
        if tool not in config.DESTRUCTION_TOOLS:
            return fail(400, "outil inconnu")
        try:
            col, row = int(req.body["col"]), int(req.body["row"])
        except (KeyError, TypeError, ValueError):
            return fail(400, "case invalide")
        return Response(self.engine.loot.use_tool(self._cookie(req), self._canvas(req, req.body.get("canvas")),
                                                  tool, col, row))

    # ---------------------------------------------------------------- tower defense
    def td(self, req):
        snap = self.engine.td.snapshot(self._cookie(req))
        if not snap:
            return Response({"success": False, "message": "Tower Defense illisible (cookie Codawar ?)"})
        return Response({"success": True, **snap})

    def td_collect(self, req):
        return Response(self.engine.td.collect(self._cookie(req)))

    def canvases(self, req):
        lst = self.directory.list(self._cookie(req), self._canvas(req))
        return Response({"success": lst is not None, "canvases": lst or []})

    def layers(self, req):
        return Response({"layers": self.store.layers_of(req.user["id"]), "settings": self.store.settings,
                         "backend": self.store.backend_name,
                         "canvas": req.user.get("canvas", self.store.settings["canvas"]),
                         # le cookie Codawar ne sort jamais du serveur : seul un indice de fin,
                         # pour que l'utilisateur reconnaisse celui qui est en place
                         "cookieSet": bool(self._cookie(req)),
                         "cookieHint": self._cookie(req)[-6:] if self._cookie(req) else ""})

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
                         "x0": snap.get("x0", 0), "y0": snap.get("y0", 0),
                         "palette": COLOR_INDEX, "idx": decode_grid(snap["data"])})

    def timelapse_frames(self, req):
        """Plusieurs instantanés d'un coup, données zlib en base64 (décompressées par le navigateur)."""
        ids = [int(x) for x in req.query.get("ids", "").split(",") if x.isdigit()][:300]
        return Response({"frames": self.store.get_snapshots(ids) if ids else []})

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
        old = self.store.find_layer(layer["id"])
        if old:
            if not self.store.can_edit_layer(uid, old):
                return fail(403, "calque d'un autre compte")
            # l'auteur d'origine et le projet ne changent pas via une simple sauvegarde
            layer["owner"] = old.get("owner", uid)
            if old.get("project"):
                layer["project"] = old["project"]
            else:
                layer.pop("project", None)
        else:
            layer["owner"] = uid
            pid = layer.get("project")
            if pid and not self.store.is_member(uid, pid):
                return fail(403, "pas membre de ce projet")
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
