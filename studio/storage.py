"""Persistance : état de l'application (réglages, calques, sprites, comptes, sessions, runtime)
et instantanés du canvas.

Deux backends interchangeables : PostgreSQL (si psycopg et la base sont disponibles)
ou un fichier JSON. Store ne connaît que leur interface commune.
"""

import json
import os
import secrets
import threading
import time

from . import config
from .secretbox import box
from .security import hash_pw

try:
    import psycopg
except ModuleNotFoundError:
    psycopg = None

STATE_KEYS = ("settings", "layers", "sprites", "runtime", "users", "sessions", "projects")


def _log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- Backends

class JsonBackend:
    """État complet dans un fichier JSON (écriture atomique). Pas d'instantanés."""

    name = "json"

    def __init__(self, path=config.DATA_FILE):
        self.path = path

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def save(self, state):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, self.path)

    def save_runtime(self, state):
        self.save(state)  # un seul fichier : tout est réécrit

    def add_snapshot(self, canvas, t, w, h, data, x0=0, y0=0):
        pass

    def list_snapshots(self, canvas):
        return []

    def get_snapshot(self, sid):
        return None

    def get_snapshots(self, ids):
        return []


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (k text PRIMARY KEY, v jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS layers  (id text PRIMARY KEY, ord int NOT NULL, data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS sprites (id text PRIMARY KEY, ord int NOT NULL, data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS runtime (k text PRIMARY KEY, v jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS users (
  id text PRIMARY KEY, username text UNIQUE NOT NULL, pw text NOT NULL,
  cookie text NOT NULL DEFAULT '', canvas text NOT NULL DEFAULT 'playtest-sandbox', prefs jsonb NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS sessions (token text PRIMARY KEY, uid text NOT NULL, created double precision NOT NULL);
CREATE TABLE IF NOT EXISTS projects (id text PRIMARY KEY, ord int NOT NULL, data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS snapshots (
  id bigserial PRIMARY KEY, canvas text NOT NULL, t double precision NOT NULL,
  w int NOT NULL, h int NOT NULL, data text NOT NULL);
CREATE INDEX IF NOT EXISTS snapshots_canvas_t ON snapshots (canvas, t);
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS x0 int NOT NULL DEFAULT 0;
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS y0 int NOT NULL DEFAULT 0;
"""

# Supprime les instantanés identiques au précédent du même canvas (timelapse sans doublons).
DEDUP_SNAPSHOTS = """
DELETE FROM snapshots WHERE id IN (
  SELECT id FROM (
    SELECT id, data, w, h, x0, y0,
           LAG(data) OVER win AS p_data, LAG(w) OVER win AS p_w, LAG(h) OVER win AS p_h,
           LAG(x0) OVER win AS p_x0, LAG(y0) OVER win AS p_y0
    FROM snapshots WINDOW win AS (PARTITION BY canvas ORDER BY t, id)
  ) s
  WHERE data = p_data AND w = p_w AND h = p_h AND x0 = p_x0 AND y0 = p_y0
)
"""


class PostgresBackend:
    """État dans PostgreSQL ; reconnexion automatique si la connexion tombe."""

    name = "postgres"

    def __init__(self, dsn=config.PG_DSN):
        self.dsn = dsn
        self.conn = None
        self.fallback = JsonBackend()  # base injoignable : on écrit au moins le fichier

    @classmethod
    def connect_with_retries(cls, dsn=config.PG_DSN, tries=config.PG_RETRIES):
        """Backend connecté, ou None si la base reste injoignable."""
        if psycopg is None:
            return None
        backend = cls(dsn)
        for i in range(tries):
            if backend._connect():
                return backend
            if i < tries - 1:
                time.sleep(config.PG_RETRY_DELAY)
        return None

    def _connect(self):
        try:
            self.conn = psycopg.connect(self.dsn, autocommit=True, connect_timeout=5)
            with self.conn.cursor() as cur:
                cur.execute(SCHEMA)
                cur.execute(DEDUP_SNAPSHOTS)
                if cur.rowcount:
                    _log(f"Timelapse : {cur.rowcount} instantané(s) en double supprimé(s)")
            return True
        except Exception as e:
            _log(f"PostgreSQL pas prêt ({e})")
            self.conn = None
            return False

    def _ready(self):
        """Connexion utilisable (reconnexion si elle est tombée)."""
        if self.conn is None:
            self._connect()
        return self.conn is not None

    def _write(self, what, fn):
        """Exécute fn(cur) dans une transaction ; marque la connexion tombée en cas d'erreur."""
        try:
            with self.conn.transaction(), self.conn.cursor() as cur:
                fn(cur)
        except Exception as e:
            _log(f"Écriture {what} PostgreSQL échouée : {e}")
            self.conn = None  # reconnexion au prochain appel

    def _read(self, fn, default):
        if not self._ready():
            return default
        try:
            with self.conn.cursor() as cur:
                return fn(cur)
        except Exception:
            return default

    # -- état
    def load(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT k, v FROM settings")
            settings = dict(cur.fetchall())
            cur.execute("SELECT data FROM layers ORDER BY ord")
            layers = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT data FROM sprites ORDER BY ord")
            sprites = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT data FROM projects ORDER BY ord")
            projects = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT k, v FROM runtime")
            runtime = dict(cur.fetchall())
            cur.execute("SELECT id, username, pw, cookie, canvas, prefs FROM users")
            users = {id_: {"id": id_, "username": un, "pw": pw, "cookie": ck, "canvas": cv, "prefs": pr}
                     for id_, un, pw, ck, cv, pr in cur.fetchall()}
            cur.execute("SELECT token, uid, created FROM sessions")
            sessions = {t: {"uid": u, "created": cr} for t, u, cr in cur.fetchall()}
        return {"settings": settings, "layers": layers, "sprites": sprites, "projects": projects,
                "runtime": runtime, "users": users, "sessions": sessions}

    def save(self, state):
        if not self._ready():
            return self.fallback.save(state)

        # réécrit tout l'état dans une transaction (état petit : simple et sûr)
        def write(cur):
            _upsert_kv(cur, "settings", state["settings"])
            _replace_rows(cur, "layers", state["layers"])
            _replace_rows(cur, "sprites", state["sprites"])
            _replace_rows(cur, "projects", state["projects"])
            _upsert_kv(cur, "runtime", state["runtime"])
            _replace_users(cur, state["users"])
            _replace_sessions(cur, state["sessions"])
        self._write("de l'état", write)

    def save_runtime(self, state):
        if not self._ready():
            return self.fallback.save(state)
        self._write("runtime", lambda cur: _upsert_kv(cur, "runtime", state["runtime"]))

    # -- instantanés du canvas (timelapse)
    def add_snapshot(self, canvas, t, w, h, data, x0=0, y0=0):
        """Enregistre un instantané, sauf s'il est identique au dernier de ce canvas."""
        if not self._ready():
            return

        def write(cur):
            cur.execute("SELECT data, w, h, x0, y0 FROM snapshots WHERE canvas=%s ORDER BY t DESC, id DESC LIMIT 1",
                        (canvas,))
            last = cur.fetchone()
            if last and tuple(last) == (data, w, h, x0, y0):
                return  # rien n'a changé depuis le dernier instantané
            cur.execute("INSERT INTO snapshots(canvas, t, w, h, data, x0, y0) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (canvas, t, w, h, data, x0, y0))
            cur.execute("DELETE FROM snapshots WHERE canvas=%s AND id NOT IN "
                        "(SELECT id FROM snapshots WHERE canvas=%s ORDER BY t DESC LIMIT %s)",
                        (canvas, canvas, config.SNAPSHOT_KEEP))
        self._write("snapshot", write)

    def list_snapshots(self, canvas):
        def read(cur):
            cur.execute("SELECT id, t FROM snapshots WHERE canvas=%s ORDER BY t", (canvas,))
            return [{"id": i, "t": t} for i, t in cur.fetchall()]
        return self._read(read, [])

    def get_snapshot(self, sid):
        def read(cur):
            cur.execute("SELECT canvas, t, w, h, data, x0, y0 FROM snapshots WHERE id=%s", (sid,))
            r = cur.fetchone()
            return {"canvas": r[0], "t": r[1], "w": r[2], "h": r[3], "data": r[4], "x0": r[5], "y0": r[6]} if r else None
        return self._read(read, None)

    def get_snapshots(self, ids):
        def read(cur):
            cur.execute("SELECT id, t, w, h, data, x0, y0 FROM snapshots WHERE id = ANY(%s)", (list(ids),))
            return [{"id": r[0], "t": r[1], "w": r[2], "h": r[3], "data": r[4], "x0": r[5], "y0": r[6]}
                    for r in cur.fetchall()]
        return self._read(read, [])


def _upsert_kv(cur, table, mapping):
    for k, v in mapping.items():
        cur.execute(f"INSERT INTO {table}(k, v) VALUES (%s, %s::jsonb) "
                    "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", (k, json.dumps(v)))


def _delete_missing(cur, table, column, keep):
    if keep:
        cur.execute(f"DELETE FROM {table} WHERE {column} <> ALL(%s)", (list(keep),))
    else:
        cur.execute(f"DELETE FROM {table}")


def _replace_rows(cur, table, items):
    _delete_missing(cur, table, "id", [it["id"] for it in items])
    for i, it in enumerate(items):
        cur.execute(f"INSERT INTO {table}(id, ord, data) VALUES (%s, %s, %s::jsonb) "
                    "ON CONFLICT (id) DO UPDATE SET ord = EXCLUDED.ord, data = EXCLUDED.data",
                    (it["id"], i, json.dumps(it)))


def _replace_users(cur, users):
    _delete_missing(cur, "users", "id", users)
    for u in users.values():
        cur.execute("INSERT INTO users(id, username, pw, cookie, canvas, prefs) VALUES (%s,%s,%s,%s,%s,%s::jsonb) "
                    "ON CONFLICT (id) DO UPDATE SET username=EXCLUDED.username, pw=EXCLUDED.pw, "
                    "cookie=EXCLUDED.cookie, canvas=EXCLUDED.canvas, prefs=EXCLUDED.prefs",
                    (u["id"], u["username"], u["pw"], u.get("cookie", ""),
                     u.get("canvas", config.DEFAULT_CANVAS), json.dumps(u.get("prefs", {}))))


def _replace_sessions(cur, sessions):
    _delete_missing(cur, "sessions", "token", sessions)
    for t, s in sessions.items():
        s = _session_record(s)
        cur.execute("INSERT INTO sessions(token, uid, created) VALUES (%s,%s,%s) "
                    "ON CONFLICT (token) DO UPDATE SET uid=EXCLUDED.uid", (t, s["uid"], s["created"]))


def _session_record(s):
    """Normalise une session (ancien format : token -> uid)."""
    if isinstance(s, dict):
        return {"uid": s["uid"], "created": s.get("created", time.time())}
    return {"uid": s, "created": time.time()}


def _has_plain_cookie(state):
    holders = [state.get("settings") or {}, *(state.get("users") or {}).values()]
    return any(box.is_plain(h.get("cookie")) for h in holders)


def open_backend():
    """PostgreSQL si disponible, sinon repli sur le fichier JSON."""
    if psycopg is not None:
        pg = PostgresBackend.connect_with_retries()
        if pg:
            return pg
        _log(f"PostgreSQL injoignable — repli sur {config.DATA_FILE}")
    return JsonBackend()


# ---------------------------------------------------------------- Store

class Store:
    """État de l'application en mémoire + persistance via un backend.

    Toute modification passe par une méthode de Store, sous self.lock, puis est sauvegardée.
    """

    def __init__(self, backend):
        self.lock = threading.RLock()
        self.backend = backend
        self.settings = dict(config.DEFAULT_SETTINGS)
        self.layers = []
        self.sprites = []
        self.projects = []  # projets de groupe : {id, name, canvas, owner, members[]}
        self.runtime = {}   # état d'exécution persistant : compteurs, stats, journal
        self.users = {}     # id -> {id, username, pw, cookie, canvas, prefs}
        self.sessions = {}  # token -> {uid, created}
        # cookies chiffrés illisibles (clé ou module absents) : réécrits tels quels
        # à la sauvegarde plutôt qu'effacés ; uid (ou "settings") -> jeton
        self._sealed = {}
        self._load()

    @property
    def backend_name(self):
        return self.backend.name

    # -- chargement / sauvegarde
    def _load(self):
        state = self.backend.load() or {}
        plain = _has_plain_cookie(state)  # avant _apply, qui déchiffre en place
        self._apply(state)
        if isinstance(self.backend, PostgresBackend) and not self.layers and not self.sprites:
            self._migrate_from_json()
        elif box.enabled and plain:
            self.save()  # anciens cookies en clair : chiffrés dès le démarrage
            _log("Cookies Codawar chiffrés dans la base")

    def _apply(self, state):
        self.settings.update(state.get("settings", {}))
        self.layers = state.get("layers", [])
        self.sprites = state.get("sprites", [])
        self.projects = state.get("projects", [])
        self.runtime = state.get("runtime", {})
        self.users = state.get("users", {})
        self.sessions = state.get("sessions", {})
        self._open_cookies()

    def _open_cookies(self):
        """Déchiffre les cookies chargés (en mémoire, ils restent en clair)."""
        for key, holder in [("settings", self.settings), *self.users.items()]:
            raw = holder.get("cookie") or ""
            holder["cookie"] = box.decrypt(raw)
            if raw and not holder["cookie"]:
                self._sealed[key] = raw

    def _migrate_from_json(self):
        legacy = JsonBackend().load()
        if not legacy:
            return
        self.settings.update(legacy.get("settings", {}))
        self.layers = legacy.get("layers", [])
        self.sprites = legacy.get("sprites", [])
        self.projects = legacy.get("projects", [])
        # comptes, sessions et état d'exécution aussi : sinon un repli JSON
        # temporaire ferait perdre les comptes créés entre-temps
        self.runtime = legacy.get("runtime", {}) or self.runtime
        self.users = {**legacy.get("users", {}), **self.users}
        self.sessions = {**legacy.get("sessions", {}), **self.sessions}
        self._open_cookies()
        self.save()
        _log(f"Migration de {config.DATA_FILE} vers PostgreSQL : "
             f"{len(self.layers)} calques, {len(self.sprites)} sprites, {len(self.users)} comptes")

    def _state(self):
        """État à persister : copie avec les cookies Codawar chiffrés."""
        state = {k: getattr(self, k) for k in STATE_KEYS}
        state["settings"] = {**self.settings, "cookie": self._seal("settings", self.settings)}
        state["users"] = {uid: {**u, "cookie": self._seal(uid, u)} for uid, u in self.users.items()}
        return state

    def _seal(self, key, holder):
        plain = holder.get("cookie") or ""
        return box.encrypt(plain) if plain else self._sealed.get(key, "")

    def save(self):
        with self.lock:
            self.backend.save(self._state())

    def save_runtime(self, data):
        """Persiste l'état d'exécution (compteurs, stats, journal) sans réécrire tout le reste."""
        with self.lock:
            self.runtime = data
            self.backend.save_runtime(self._state())

    # -- réglages moteur partagés
    def update_settings(self, values):
        with self.lock:
            for k in config.DEFAULT_SETTINGS:
                if k in values and k != "cookie":  # le cookie est par compte
                    self.settings[k] = values[k]
            for k in [k for k in self.settings if k not in config.DEFAULT_SETTINGS]:
                del self.settings[k]  # anciens réglages supprimés (workers, delay, defendWorkers…)
            self.save()
            return self.settings

    # -- comptes
    def create_user(self, username, pw):
        with self.lock:
            if self.user_by_name(username):
                return None
            uid = secrets.token_hex(8)
            self.users[uid] = {"id": uid, "username": username, "pw": hash_pw(pw),
                               "cookie": "", "canvas": config.DEFAULT_CANVAS, "prefs": {}}
            if len(self.users) == 1:
                self._adopt_orphans(uid)
            self.save()
            return self.users[uid]

    def _adopt_orphans(self, uid):
        """Le premier compte hérite des calques/sprites sans propriétaire (données migrées)."""
        for item in self.layers + self.sprites:
            item.setdefault("owner", uid)

    def user_by_name(self, username):
        name = username.lower()
        return next((u for u in self.users.values() if u["username"].lower() == name), None)

    def update_account(self, user, values):
        """Cookie Codawar, canvas par défaut et préférences d'un compte."""
        with self.lock:
            if "cookie" in values:
                user["cookie"] = (values.get("cookie") or "").strip()
                self._sealed.pop(user["id"], None)  # nouveau cookie : l'ancien jeton illisible est oublié
            if "canvas" in values:
                user["canvas"] = values.get("canvas") or self.settings["canvas"]
            if isinstance(values.get("prefs"), dict):
                user["prefs"] = values["prefs"]
            self.save()

    def users_with_cookie(self):
        with self.lock:
            return [u for u in self.users.values() if u.get("cookie")]

    # -- sessions
    def new_session(self, uid):
        with self.lock:
            tok = secrets.token_urlsafe(32)
            self.sessions[tok] = {"uid": uid, "created": time.time()}
            self._prune_sessions()
            self.save()
            return tok

    def session_user(self, token):
        """Compte associé à un jeton de session valide, sinon None."""
        if not token or token not in self.sessions:
            return None
        s = self.sessions[token]
        if not isinstance(s, dict):  # ancien format token -> uid
            s = self.sessions[token] = _session_record(s)
        if time.time() - s.get("created", 0) > config.SESSION_TTL:
            self.delete_session(token)
            return None
        return self.users.get(s["uid"])

    def _prune_sessions(self):
        now = time.time()
        for t in [t for t, s in self.sessions.items()
                  if isinstance(s, dict) and now - s.get("created", 0) > config.SESSION_TTL]:
            del self.sessions[t]

    def delete_session(self, token):
        with self.lock:
            if self.sessions.pop(token, None) is not None:
                self.save()

    # -- sprites
    def sprites_of(self, uid):
        with self.lock:
            return [s for s in self.sprites if s.get("owner") == uid]

    def save_sprite(self, sprite):
        with self.lock:
            self.sprites = [s for s in self.sprites if s["id"] != sprite["id"]]
            self.sprites.append(sprite)
            self.save()

    def delete_sprite_of(self, uid, sid):
        with self.lock:
            if any(s["id"] == sid and s.get("owner") == uid for s in self.sprites):
                self.sprites = [s for s in self.sprites if s["id"] != sid]
                self.save()

    # -- projets de groupe
    #
    # Un projet réunit plusieurs comptes autour des mêmes calques : tous les membres
    # les voient et les modifient, et la défense puise dans les crédits de TOUS
    # les membres (voir CookieResolver.for_layer). L'entrée se fait sur invitation
    # du créateur uniquement.
    def find_project(self, pid):
        with self.lock:
            return next((p for p in self.projects if p["id"] == pid), None)

    def projects_of(self, uid):
        with self.lock:
            return [p for p in self.projects if uid in p.get("members", [])]

    def project_ids_of(self, uid):
        return {p["id"] for p in self.projects_of(uid)}

    def is_member(self, uid, pid):
        p = self.find_project(pid)
        return bool(p and uid in p.get("members", []))

    def create_project(self, uid, name, canvas):
        p = {"id": secrets.token_hex(8), "name": (name or "Projet").strip()[:60], "canvas": canvas,
             "owner": uid, "members": [uid], "created": time.time()}
        with self.lock:
            self.projects.append(p)
            self.save()
        return p

    def rename_project(self, pid, uid, name):
        with self.lock:
            p = next((p for p in self.projects if p["id"] == pid), None)
            if not p or p["owner"] != uid:
                return None
            p["name"] = (name or p["name"]).strip()[:60]
            self.save()
            return p

    def project_invite(self, pid, uid, username):
        """Le créateur ajoute un membre par son pseudo de studio."""
        with self.lock:
            p = next((p for p in self.projects if p["id"] == pid), None)
            if not p or p["owner"] != uid:
                return None, "seul le créateur peut inviter"
            target = next((u for u in self.users.values()
                           if u["username"].lower() == (username or "").strip().lower()), None)
            if not target:
                return None, "compte inconnu"
            if target["id"] in p["members"]:
                return None, "déjà membre"
            p["members"].append(target["id"])
            self.save()
            return p, None

    # -- liens d'invitation
    #
    # Un lien vaut invitation : qui l'a peut rejoindre le projet, donc modifier ses
    # calques et y engager ses crédits. Le jeton est donc imprévisible, limité en
    # nombre d'usages et en durée, et révocable à tout moment par le créateur.
    def project_invite_link(self, pid, uid, max_uses=10, ttl=config.INVITE_TTL):
        with self.lock:
            p = next((p for p in self.projects if p["id"] == pid), None)
            if not p or p["owner"] != uid:
                return None, "seul le créateur peut créer un lien"
            link = {"token": secrets.token_urlsafe(24), "project": pid, "created": time.time(),
                    "expires": time.time() + ttl, "maxUses": max(1, int(max_uses)), "uses": 0}
            p.setdefault("links", []).append(link)
            self.save()
            return link, None

    def project_links(self, pid, uid):
        p = self.find_project(pid)
        if not p or p["owner"] != uid:
            return []
        now = time.time()
        return [dict(l, valid=l["uses"] < l["maxUses"] and l["expires"] > now)
                for l in p.get("links", [])]

    def project_revoke_link(self, pid, uid, token):
        with self.lock:
            p = next((p for p in self.projects if p["id"] == pid), None)
            if not p or p["owner"] != uid:
                return False
            p["links"] = [l for l in p.get("links", []) if l["token"] != token]
            self.save()
            return True

    def find_invite(self, token):
        """Projet visé par un jeton encore valable, sinon (None, raison)."""
        if not token:
            return None, "lien absent"
        with self.lock:
            for p in self.projects:
                for l in p.get("links", []):
                    if secrets.compare_digest(l["token"], token):
                        if l["uses"] >= l["maxUses"]:
                            return None, "lien déjà utilisé au maximum"
                        if l["expires"] <= time.time():
                            return None, "lien expiré"
                        return (p, l), None
        return None, "lien inconnu ou révoqué"

    def redeem_invite(self, token, uid):
        """Fait entrer uid dans le projet du lien et décompte un usage."""
        found, err = self.find_invite(token)
        if err:
            return None, err
        with self.lock:
            p, link = found
            if uid in p["members"]:
                return p, None  # déjà membre : on ne consomme pas le lien
            p["members"].append(uid)
            link["uses"] += 1
            self.save()
            return p, None

    def project_remove(self, pid, uid, member_id):
        """Le créateur retire un membre ; un membre peut se retirer lui-même."""
        with self.lock:
            p = next((p for p in self.projects if p["id"] == pid), None)
            if not p:
                return None, "projet inconnu"
            if uid != p["owner"] and uid != member_id:
                return None, "non autorisé"
            if member_id == p["owner"]:
                return None, "le créateur ne peut pas quitter son projet (supprime-le)"
            if member_id not in p["members"]:
                return None, "pas membre"
            p["members"].remove(member_id)
            self.save()
            return p, None

    def delete_project(self, pid, uid):
        """Supprime le projet ; ses calques redeviennent personnels à leur auteur."""
        with self.lock:
            p = next((p for p in self.projects if p["id"] == pid), None)
            if not p or p["owner"] != uid:
                return False
            for l in self.layers:
                if l.get("project") == pid:
                    l.pop("project", None)
            self.projects = [x for x in self.projects if x["id"] != pid]
            self.save()
            return True

    # -- calques
    def can_edit_layer(self, uid, layer):
        """Son propre calque, ou un calque d'un projet dont on est membre."""
        if not layer:
            return False
        if layer.get("owner") == uid:
            return True
        pid = layer.get("project")
        return bool(pid and self.is_member(uid, pid))

    def layers_of(self, uid):
        """Calques personnels + calques des projets dont on est membre."""
        mine = self.project_ids_of(uid)
        with self.lock:
            return [l for l in self.layers
                    if l.get("owner") == uid or l.get("project") in mine]

    def active_layers(self):
        with self.lock:
            return [l for l in self.layers if l.get("active")]

    def find_layer(self, lid):
        with self.lock:
            return next((l for l in self.layers if l["id"] == lid), None)

    def upsert_layer(self, layer):
        with self.lock:
            for i, l in enumerate(self.layers):
                if l["id"] == layer["id"]:
                    self.layers[i] = layer
                    break
            else:
                self.layers.append(layer)
            self.save()

    def delete_layer_of(self, uid, lid):
        if not self.can_edit_layer(uid, self.find_layer(lid)):
            return
        with self.lock:
            self.layers = [l for l in self.layers if l["id"] != lid]
            self.save()

    def reorder_layers(self, ids):
        with self.lock:
            pos = {i: n for n, i in enumerate(ids)}
            self.layers.sort(key=lambda l: pos.get(l["id"], 1e9))
            self.save()

    def watched_canvases(self):
        """Canvas à relire en continu : canvas des comptes configurés et des calques."""
        with self.lock:
            canvases = {u.get("canvas") for u in self.users.values() if u.get("cookie") and u.get("canvas")}
            canvases |= {l.get("canvas") for l in self.layers}
        return {c for c in canvases if c}

    # -- instantanés du canvas (timelapse)
    def add_snapshot(self, canvas, t, w, h, data, x0=0, y0=0):
        with self.lock:
            self.backend.add_snapshot(canvas, t, w, h, data, x0, y0)

    def list_snapshots(self, canvas):
        with self.lock:
            return self.backend.list_snapshots(canvas)

    def get_snapshot(self, sid):
        with self.lock:
            return self.backend.get_snapshot(sid)

    def get_snapshots(self, ids):
        with self.lock:
            return self.backend.get_snapshots(ids)
