#!/usr/bin/env python3
"""
Codawar Pixel Studio — serveur local (Python stdlib uniquement).

- Sert l'éditeur web (index.html).
- File d'attente de pixels + pool de workers (connexions HTTPS keep-alive).
- Calques persistants (data/state.json) : un calque "actif" est posé puis
  surveillé en continu ; tout pixel écrasé est reposé automatiquement,
  même navigateur fermé.
"""

import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(filepath):
    """Charge les variables d'un fichier .env dans os.environ si non définies."""
    if not os.path.isfile(filepath):
        return
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip("'\"")
                    if key and key not in os.environ:
                        os.environ[key] = val
    except Exception:
        pass


_load_dotenv(os.path.join(BASE, ".env"))

# Stockage PostgreSQL via psycopg (dans .venv). Si lancé avec un python sans psycopg,
# on relance automatiquement avec celui du venv.
try:
    import psycopg  # noqa: F401
    HAS_PG = True
except ModuleNotFoundError:
    # relance avec le python du venv (son lien active le venv même si le binaire est partagé).
    # le drapeau évite une boucle si psycopg reste introuvable.
    _venv_py = os.path.join(BASE, ".venv", "bin", "python")
    if os.path.exists(_venv_py) and not os.environ.get("CODAWAR_REEXEC"):
        os.environ["CODAWAR_REEXEC"] = "1"
        os.execv(_venv_py, [_venv_py, *sys.argv])
    HAS_PG = False  # ni venv ni psycopg : repli sur le fichier JSON

import collections
import hashlib
import hmac
import http.client
import http.server
import json
import random
import re
import secrets
import subprocess
import threading
import time
import urllib.parse


# ---------------------------------------------------------------- Mise à jour auto (git pull main)

AUTO_PULL = os.environ.get("AUTO_PULL", "0") == "1"
AUTO_PULL_INTERVAL = int(os.environ.get("AUTO_PULL_INTERVAL", "300"))  # secondes
AUTO_PULL_BRANCH = os.environ.get("AUTO_PULL_BRANCH", "main")


def _git(*args):
    return subprocess.run(["git", "-C", BASE, "-c", f"safe.directory={BASE}", *args],
                          capture_output=True, text=True, timeout=120)


def auto_pull_loop(on_update):
    while True:
        time.sleep(AUTO_PULL_INTERVAL)
        try:
            if _git("fetch", "origin", AUTO_PULL_BRANCH).returncode != 0:
                continue
            local = _git("rev-parse", "HEAD").stdout.strip()
            remote = _git("rev-parse", f"origin/{AUTO_PULL_BRANCH}").stdout.strip()
            if not remote or local == remote:
                continue
            pull = _git("merge", "--ff-only", f"origin/{AUTO_PULL_BRANCH}")
            if pull.returncode != 0:
                print(f"auto-pull: fusion impossible (changements locaux ?) : {pull.stderr.strip()}", flush=True)
                continue
            print(f"auto-pull: mis à jour {local[:7]} -> {remote[:7]}, redémarrage", flush=True)
            on_update()
            os.execv(sys.executable, [sys.executable, *sys.argv])  # recharge le nouveau code
        except Exception as e:
            print(f"auto-pull: {e}", flush=True)


# ---------------------------------------------------------------- Sécurité

PEPPER = os.environ.get("AUTH_PEPPER", "").encode()      # secret serveur ajouté au mot de passe (optionnel mais recommandé)
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"  # 1 derrière HTTPS (Cosmos)
SESSION_TTL = int(os.environ.get("SESSION_TTL_DAYS", "30")) * 86400
PBKDF2_ROUNDS = 200_000
MAX_BODY = 20 * 1024 * 1024  # 20 Mo par requête
MIN_PW = 8


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode() + PEPPER, bytes.fromhex(salt), PBKDF2_ROUNDS).hex()
    return f"{salt}${dk}"


def check_pw(pw, stored):
    try:
        salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode() + PEPPER, bytes.fromhex(salt), PBKDF2_ROUNDS).hex()
        return hmac.compare_digest(dk, calc)
    except Exception:
        return False

PORT = int(os.environ.get("PORT", "5055"))
HOST = os.environ.get("HOST", "127.0.0.1")  # mettre 0.0.0.0 en conteneur (derrière Cosmos)
DATA_FILE = os.path.join(BASE, "data", "state.json")
PG_DSN = os.environ.get("CODAWAR_PG", "dbname=codawar")

TELEFUNC_HOST = "codawar.coda.school"
TELEFUNC_PATH = "/_telefunc?_telefunc=txt"
TELEFUNC_FILE = "/src/modules/canvas/infrastructure/browser-canvas-gateway.telefunc.ts"
DEFAULT_COOKIE = os.environ.get("CODAWAR_COOKIE", "")
DEFAULT_CANVAS = "playtest-sandbox"
ZONE_SIZE = 100
MAX_WORKERS = 16

# Table d'index des couleurs de onSynchronizeCanvas (cells[i+2]), reconstruite depuis le bundle JS du site :
# ["#ffffff", ...anciennes couleurs (15), ...palette posable (32) sans #ffffff]
LEGACY_COLORS = ["#e4e4e4", "#888888", "#222222", "#ffa7d1", "#e50000", "#e59500", "#a06a42", "#e5d900",
                 "#94e044", "#02be01", "#00d3dd", "#0083c7", "#0000ea", "#cf6ee4", "#820080"]
PLACEABLE_COLORS = ["#6d001a", "#be0039", "#ff4500", "#ffa800", "#ffd635", "#fff8b8", "#00a368", "#00cc78",
                    "#7eed56", "#00756f", "#009eaa", "#00ccc0", "#2450a4", "#3690ea", "#51e9f4", "#493ac1",
                    "#6a5cff", "#94b3ff", "#811e9f", "#b44ac0", "#e4abff", "#de107f", "#ff3881", "#ff99aa",
                    "#6d482f", "#9c6926", "#ffb470", "#000000", "#515252", "#898d90", "#d4d7d9", "#ffffff"]
COLOR_INDEX = ["#ffffff"] + LEGACY_COLORS + [c for c in PLACEABLE_COLORS if c != "#ffffff"]
CELL_STRIDE = 6  # [col, row, colorIdx, ownerIdx, protection, revision]

# Codes de rejet qui signifient "réessayer plus tard" (solde, limite, fermeture…)
RETRY_LATER_HINTS = ("wallet", "balance", "credit", "insufficient", "limit", "quota", "cooldown", "rate", "closed", "pause")

DEFAULT_SETTINGS = {
    "cookie": DEFAULT_COOKIE,
    "canvas": DEFAULT_CANVAS,
    "workers": 4,
    "delay": 0.0,
    "guardInterval": 8,       # relecture normale du canvas
    "defendInterval": 2,      # relecture rapide tant qu'un calque est attaqué
    "defendWorkers": 8,       # requêtes parallèles en mode défense
    "order": "rows",          # rows | random
    "scheduleOn": False,      # minuteur de pose : ne poser que dans une plage horaire
    "scheduleFrom": "00:00",
    "scheduleTo": "23:59",
    "vandalThreshold": 12,    # pixels écrasés d'un coup sur un calque actif = alerte
}


def within_schedule(settings):
    """True si la pose est autorisée maintenant (minuteur désactivé = toujours)."""
    if not settings.get("scheduleOn"):
        return True
    def mins(hhmm):
        try:
            h, m = hhmm.split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return 0
    now = time.localtime()
    cur = now.tm_hour * 60 + now.tm_min
    a, b = mins(settings.get("scheduleFrom", "00:00")), mins(settings.get("scheduleTo", "23:59"))
    return a <= cur <= b if a <= b else (cur >= a or cur <= b)  # plage qui passe minuit


# ---------------------------------------------------------------- HTTP vers Codawar

_local = threading.local()


def telefunc(name, args, cookie, canvas, timeout=15):
    """Appel telefunc avec une connexion HTTPS persistante par thread."""
    body = json.dumps({"file": TELEFUNC_FILE, "name": name, "args": args}).encode("utf-8")
    headers = {
        "Content-Type": "text/plain",
        "x-telefunc-request": "txt",
        "Origin": "https://codawar.coda.school",
        "Referer": f"https://codawar.coda.school/canvas/{canvas}",
        "Cookie": cookie,
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)",
        "Connection": "keep-alive",
    }
    for attempt in range(2):  # 2e essai si la connexion réutilisée a été fermée
        try:
            conn = getattr(_local, "conn", None)
            if conn is None:
                conn = _local.conn = http.client.HTTPSConnection(TELEFUNC_HOST, timeout=timeout)
            conn.timeout = timeout
            conn.request("POST", TELEFUNC_PATH, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            if resp.status != 200:
                return {"status": "error", "message": f"HTTP {resp.status}: {resp.reason}"}
            return json.loads(data.decode("utf-8")).get("ret", {})
        except (http.client.HTTPException, OSError) as e:
            try:
                _local.conn.close()
            except Exception:
                pass
            _local.conn = None
            if attempt == 1:
                return {"status": "error", "message": str(e)}
        except Exception as e:
            return {"status": "error", "message": str(e)}


_canvases_cache = {"t": 0, "list": None}
_nick = {}  # cookie -> pseudo Codawar, pour ne pas prendre les pixels de nos comptes pour une attaque


def our_names():
    return {n for n in _nick.values() if n}


def nick_of(cookie):
    return _nick.get(cookie)


def list_canvases(cookie, canvas):
    """Liste des canvas accessibles, lue dans le pageContext de la page du site (cache 60 s)."""
    if _canvases_cache["list"] and time.time() - _canvases_cache["t"] < 60:
        return _canvases_cache["list"]
    try:
        conn = http.client.HTTPSConnection(TELEFUNC_HOST, timeout=15)
        conn.request("GET", f"/canvas/{canvas}", headers={"Cookie": cookie, "User-Agent": "Mozilla/5.0"})
        html = conn.getresponse().read().decode("utf-8", "replace")
        conn.close()
        m = re.search(r'"nickname":"([^"]{1,40})"', html)
        if m:
            _nick[cookie] = m.group(1)
        i = html.find('"canvases":[')
        if i < 0:
            return _canvases_cache["list"]
        arr, _ = json.JSONDecoder().raw_decode(html, i + len('"canvases":'))
        out = [{"id": c["id"], "name": c.get("name") or c["id"], "archived": bool(c.get("isArchived")),
                "unlimited": bool(c.get("hasUnlimitedCredits"))} for c in arr if c.get("id")]
        _canvases_cache.update(t=time.time(), list=out)
        return out
    except Exception:
        return _canvases_cache["list"]


# ---------------------------------------------------------------- Persistance

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (k text PRIMARY KEY, v jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS layers  (id text PRIMARY KEY, ord int NOT NULL, data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS sprites (id text PRIMARY KEY, ord int NOT NULL, data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS runtime (k text PRIMARY KEY, v jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS users (
  id text PRIMARY KEY, username text UNIQUE NOT NULL, pw text NOT NULL,
  cookie text NOT NULL DEFAULT '', canvas text NOT NULL DEFAULT 'playtest-sandbox', prefs jsonb NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS sessions (token text PRIMARY KEY, uid text NOT NULL, created double precision NOT NULL);
"""


class Store:
    def __init__(self):
        self.lock = threading.RLock()
        self.settings = dict(DEFAULT_SETTINGS)
        self.layers = []
        self.sprites = []
        self.runtime = {}   # état d'exécution persistant : compteurs, stats, journal
        self.users = {}     # id -> {id, username, pw, cookie, canvas, prefs}
        self.sessions = {}  # token -> uid
        self.pg = None
        self.backend = "json"
        if HAS_PG:
            # attend la base (en conteneur elle peut démarrer après l'app) : ~30 s
            tries = int(os.environ.get("CODAWAR_PG_RETRIES", "15"))
            for i in range(tries):
                if self._connect():
                    self.backend = "postgres"
                    break
                if i < tries - 1:
                    time.sleep(2)
            else:
                print(f"PostgreSQL injoignable — repli sur {DATA_FILE}", flush=True)
        self._load()

    def _connect(self):
        try:
            self.pg = psycopg.connect(PG_DSN, autocommit=True, connect_timeout=5)
            with self.pg.cursor() as cur:
                cur.execute(SCHEMA)
            return True
        except Exception as e:
            print(f"PostgreSQL pas prêt ({e})", flush=True)
            self.pg = None
            return False

    def _ensure_pg(self):
        """Reconnexion si la connexion est tombée (longue durée de vie)."""
        if self.pg is None and self.backend == "postgres":
            self._connect()

    def _load(self):
        if self.pg:
            with self.pg.cursor() as cur:
                cur.execute("SELECT k, v FROM settings")
                self.settings.update({k: v for k, v in cur.fetchall()})
                cur.execute("SELECT data FROM layers ORDER BY ord")
                self.layers = [r[0] for r in cur.fetchall()]
                cur.execute("SELECT data FROM sprites ORDER BY ord")
                self.sprites = [r[0] for r in cur.fetchall()]
                cur.execute("SELECT k, v FROM runtime")
                self.runtime = {k: v for k, v in cur.fetchall()}
                cur.execute("SELECT id, username, pw, cookie, canvas, prefs FROM users")
                for id_, un, pw, ck, cv, pr in cur.fetchall():
                    self.users[id_] = {"id": id_, "username": un, "pw": pw, "cookie": ck, "canvas": cv, "prefs": pr}
                cur.execute("SELECT token, uid, created FROM sessions")
                self.sessions = {t: {"uid": u, "created": cr} for t, u, cr in cur.fetchall()}
            if not self.layers and not self.sprites:
                self._migrate_from_json()
            return
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                d = json.load(f)
            self.settings.update(d.get("settings", {}))
            self.layers = d.get("layers", [])
            self.sprites = d.get("sprites", [])
            self.runtime = d.get("runtime", {})
            self.users = d.get("users", {})
            self.sessions = d.get("sessions", {})
        except FileNotFoundError:
            pass

    def _migrate_from_json(self):
        try:
            with open(DATA_FILE, encoding="utf-8") as f:
                d = json.load(f)
        except FileNotFoundError:
            return
        self.settings.update(d.get("settings", {}))
        self.layers = d.get("layers", [])
        self.sprites = d.get("sprites", [])
        self.save()
        print(f"Migration de {DATA_FILE} vers PostgreSQL : {len(self.layers)} calques, {len(self.sprites)} sprites", flush=True)

    def save(self):
        with self.lock:
            self._ensure_pg()
            if self.pg:
                return self._pg_save()
            os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
            tmp = DATA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"settings": self.settings, "layers": self.layers,
                           "sprites": self.sprites, "runtime": self.runtime,
                           "users": self.users, "sessions": self.sessions}, f)
            os.replace(tmp, DATA_FILE)

    def save_runtime(self, data):
        """Persiste l'état d'exécution (compteurs, stats, journal) sans réécrire tout le reste."""
        with self.lock:
            self.runtime = data
            self._ensure_pg()
            if self.pg:
                try:
                    with self.pg.transaction(), self.pg.cursor() as cur:
                        for k, v in data.items():
                            cur.execute("INSERT INTO runtime(k, v) VALUES (%s, %s::jsonb) "
                                        "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", (k, json.dumps(v)))
                except Exception as e:
                    print(f"Écriture runtime PostgreSQL échouée : {e}", flush=True)
                    self.pg = None  # tombée : reconnexion au prochain appel
            else:
                self.save()

    def _pg_save(self):
        # réécrit tout l'état dans une transaction (état petit : simple et sûr)
        try:
            with self.pg.transaction(), self.pg.cursor() as cur:
                for k, v in self.settings.items():
                    cur.execute("INSERT INTO settings(k, v) VALUES (%s, %s::jsonb) "
                                "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", (k, json.dumps(v)))
                self._replace(cur, "layers", self.layers)
                self._replace(cur, "sprites", self.sprites)
                for k, v in self.runtime.items():
                    cur.execute("INSERT INTO runtime(k, v) VALUES (%s, %s::jsonb) "
                                "ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v", (k, json.dumps(v)))
                self._save_auth(cur)
        except Exception as e:
            print(f"Écriture PostgreSQL échouée : {e}", flush=True)
            self.pg = None  # tombée : reconnexion au prochain appel

    def _save_auth(self, cur):
        ids = list(self.users)
        cur.execute("DELETE FROM users WHERE id <> ALL(%s)" if ids else "DELETE FROM users", (ids,) if ids else ())
        for u in self.users.values():
            cur.execute("INSERT INTO users(id, username, pw, cookie, canvas, prefs) VALUES (%s,%s,%s,%s,%s,%s::jsonb) "
                        "ON CONFLICT (id) DO UPDATE SET username=EXCLUDED.username, pw=EXCLUDED.pw, "
                        "cookie=EXCLUDED.cookie, canvas=EXCLUDED.canvas, prefs=EXCLUDED.prefs",
                        (u["id"], u["username"], u["pw"], u.get("cookie", ""), u.get("canvas", DEFAULT_CANVAS), json.dumps(u.get("prefs", {}))))
        toks = list(self.sessions)
        cur.execute("DELETE FROM sessions WHERE token <> ALL(%s)" if toks else "DELETE FROM sessions", (toks,) if toks else ())
        for t, s in self.sessions.items():
            uid = s["uid"] if isinstance(s, dict) else s
            cr = s.get("created", time.time()) if isinstance(s, dict) else time.time()
            cur.execute("INSERT INTO sessions(token, uid, created) VALUES (%s,%s,%s) "
                        "ON CONFLICT (token) DO UPDATE SET uid=EXCLUDED.uid", (t, uid, cr))

    # -- comptes et sessions
    def create_user(self, username, pw):
        with self.lock:
            if any(u["username"].lower() == username.lower() for u in self.users.values()):
                return None
            uid = secrets.token_hex(8)
            self.users[uid] = {"id": uid, "username": username, "pw": hash_pw(pw),
                               "cookie": "", "canvas": DEFAULT_CANVAS, "prefs": {}}
            # le premier compte hérite des calques/sprites sans propriétaire (données migrées)
            if len(self.users) == 1:
                for l in self.layers:
                    l.setdefault("owner", uid)
                for s in self.sprites:
                    s.setdefault("owner", uid)
            self.save()
            return self.users[uid]

    def user_by_name(self, username):
        return next((u for u in self.users.values() if u["username"].lower() == username.lower()), None)

    def save_user(self, uid):
        with self.lock:
            self.save()

    def new_session(self, uid):
        with self.lock:
            tok = secrets.token_urlsafe(32)
            self.sessions[tok] = {"uid": uid, "created": time.time()}
            self._prune_sessions()
            self.save()
            return tok

    def session_uid(self, token):
        if not token:
            return None
        s = self.sessions.get(token)
        if not s:
            return None
        if isinstance(s, str):          # ancien format token->uid
            s = {"uid": s, "created": time.time()}
            self.sessions[token] = s
        if time.time() - s.get("created", 0) > SESSION_TTL:
            self.delete_session(token)
            return None
        return s["uid"]

    def _prune_sessions(self):
        now = time.time()
        for t in [t for t, s in self.sessions.items()
                  if isinstance(s, dict) and now - s.get("created", 0) > SESSION_TTL]:
            del self.sessions[t]

    def delete_session(self, token):
        with self.lock:
            if self.sessions.pop(token, None) is not None:
                self.save()

    @staticmethod
    def _replace(cur, table, items):
        ids = [it["id"] for it in items]
        if ids:
            cur.execute(f"DELETE FROM {table} WHERE id <> ALL(%s)", (ids,))
        else:
            cur.execute(f"DELETE FROM {table}")
        for i, it in enumerate(items):
            cur.execute(f"INSERT INTO {table}(id, ord, data) VALUES (%s, %s, %s::jsonb) "
                        f"ON CONFLICT (id) DO UPDATE SET ord = EXCLUDED.ord, data = EXCLUDED.data",
                        (it["id"], i, json.dumps(it)))


    def save_sprite(self, sp):
        with self.lock:
            self.sprites = [s for s in self.sprites if s["id"] != sp["id"]]
            self.sprites.append(sp)
            self.save()

    def delete_sprite(self, sid):
        with self.lock:
            self.sprites = [s for s in self.sprites if s["id"] != sid]
            self.save()

    def upsert_layer(self, layer):
        with self.lock:
            for i, l in enumerate(self.layers):
                if l["id"] == layer["id"]:
                    self.layers[i] = layer
                    break
            else:
                self.layers.append(layer)
            self.save()

    def delete_layer(self, lid):
        with self.lock:
            self.layers = [l for l in self.layers if l["id"] != lid]
            self.save()

    def reorder(self, ids):
        with self.lock:
            pos = {i: n for n, i in enumerate(ids)}
            self.layers.sort(key=lambda l: pos.get(l["id"], 1e9))
            self.save()


# ---------------------------------------------------------------- Moteur

class Engine:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.pending = collections.OrderedDict()  # (canvas, col, row) -> (color, source)
        self.inflight = 0
        self.failed = {}  # (canvas, col, row) -> timestamp jusqu'auquel on ne réessaie pas
        self.boards = {}  # canvas -> {"t", "w", "h", "pixels": {(c, r): [hex, owner, prot]}, "wallet"}
        self.paused = False
        self.backoff_until = 0
        self.placed = self.skipped = self.errors = 0
        self.recent = collections.deque()  # timestamps de poses (débit)
        self.logs = collections.deque(maxlen=300)
        self.wallet = None
        self.layer_missing = {}  # layer id -> pixels encore faux
        self.prev_missing = {}   # pour détecter un saut soudain (vandalisme)
        self.vandal_alert = None
        self.attack_until = 0    # tant que > now : mode défense (relecture rapide + priorité + plus de workers)
        self.guard_kick = threading.Event()
        self.last_guard = 0
        self.stats = {}        # canvas -> {owner -> {observed, first, last, maxRate}}
        self.stats_rev = {}    # canvas -> {(c,r) -> revision} (dernière relecture)
        self.stats_last = {}   # canvas -> t de la dernière relecture
        # restaure l'état persistant (compteurs, stats, journal)
        rt = store.runtime or {}
        self.placed = rt.get("placed", 0)
        self.skipped = rt.get("skipped", 0)
        self.errors = rt.get("errors", 0)
        self.stats = rt.get("stats", {})
        self.logs = collections.deque(rt.get("logs", []), maxlen=300)
        self._rt_dirty = False

    def persist(self):
        """Sauvegarde l'état d'exécution (compteurs, stats, journal)."""
        with self.lock:
            data = {"placed": self.placed, "skipped": self.skipped, "errors": self.errors,
                    "stats": self.stats, "logs": list(self.logs)}
        self.store.save_runtime(data)
        self._rt_dirty = False

    def persist_loop(self):
        while True:
            time.sleep(10)
            try:
                self.persist()
            except Exception as e:
                print(f"persist: {e}", flush=True)

    # -- journal
    def mark_dirty(self):
        self._rt_dirty = True
    def log(self, msg, level="info"):
        with self.lock:
            self.logs.append({"time": time.strftime("%H:%M:%S"), "msg": msg, "level": level})

    # -- lecture du canvas
    def fetch_board(self, canvas, max_age=0, cookie=None):
        with self.lock:
            b = self.boards.get(canvas)
            if b and time.time() - b["t"] <= max_age:
                return b
        res = telefunc("onSynchronizeCanvas", [canvas], cookie or self.any_cookie(), canvas, timeout=30)
        if res.get("status") != "success":
            return None if not b else b
        val = res["value"]
        cells, owners = val["pixels"]["cells"], val["pixels"]["owners"]
        pixels = {}
        for i in range(0, len(cells), CELL_STRIDE):
            ci, oi = cells[i + 2], cells[i + 3]
            pixels[(cells[i], cells[i + 1])] = [
                COLOR_INDEX[ci] if 0 <= ci < len(COLOR_INDEX) else "#ffffff",
                owners[oi]["name"] if 0 <= oi < len(owners) else "",
                cells[i + 4],
                cells[i + 5],  # revision : incrémentée à chaque pose sur la case
            ]
        zones = val.get("zones") or [{"zoneColumn": 0, "zoneRow": 0}]
        b = {
            "t": time.time(),
            "w": (max(z["zoneColumn"] for z in zones) + 1) * ZONE_SIZE,
            "h": (max(z["zoneRow"] for z in zones) + 1) * ZONE_SIZE,
            "pixels": pixels,
            "wallet": val.get("wallet"),
            "closure": val.get("closure"),
        }
        with self.lock:
            self.boards[canvas] = b
            self.wallet = b["wallet"] or self.wallet
        self.track_stats(canvas, b)
        return b

    # -- statistiques : observe les poses entre deux relectures du canvas
    def track_stats(self, canvas, b):
        now = b["t"]
        with self.lock:
            prev = self.stats_rev.get(canvas)
            cur = {k: v[3] for k, v in b["pixels"].items()}
            self.stats_rev[canvas] = cur
            if prev is None:  # première relecture : juste une base de comparaison
                self.stats_last[canvas] = now
                return
            placers = collections.Counter()
            for k, rev in cur.items():
                old = prev.get(k)
                if old is None or rev > old:  # case nouvellement peinte ou repeinte
                    placers[b["pixels"][k][1] or "?"] += 1
            dt = max(0.001, now - self.stats_last.get(canvas, now))
            self.stats_last[canvas] = now
            st = self.stats.setdefault(canvas, {})
            for owner, k in placers.items():
                s = st.setdefault(owner, {"observed": 0, "first": now, "last": now, "maxRate": 0.0})
                s["observed"] += k
                s["last"] = now
                s["maxRate"] = max(s["maxRate"], k / dt)  # rafale la plus rapide (pixels/s)

    def stats_for(self, canvas, me="moi"):
        b = self.boards.get(canvas)
        owned = collections.Counter((me if (v[1] == "(vous)") else (v[1] or "?")) for v in b["pixels"].values()) if b else collections.Counter()
        with self.lock:
            st = dict(self.stats.get(canvas, {}))
        rows = []
        names = set(owned) | set(st)
        for name in names:
            s = st.get(name, {})
            obs = s.get("observed", 0)
            span = max(0.001, s.get("last", 0) - s.get("first", 0))
            mx = s.get("maxRate", 0.0)
            rows.append({
                "name": name,
                "owned": owned.get(name, 0),
                "observed": obs,
                "fastestGap": round(1 / mx, 2) if mx > 0 else None,   # s entre 2 poses, au plus rapide
                "perMin": round(obs / span * 60, 1) if obs > 1 and span > 1 else None,
                "avgGap": round(span / obs, 1) if obs > 1 else None,
            })
        rows.sort(key=lambda r: (-r["owned"], -r["observed"]))
        return rows

    # -- cookie Codawar à utiliser (par compte)
    def any_cookie(self):
        for u in self.store.users.values():
            if u.get("cookie"):
                return u["cookie"]
        return self.store.settings.get("cookie", "")

    def cookie_for_owner(self, uid):
        u = self.store.users.get(uid) if uid else None
        return (u.get("cookie") if u else "") or self.any_cookie()

    # -- file d'attente
    def enqueue(self, canvas, pixels, front=False, source="manual", cookie=None):
        cookie = cookie or self.any_cookie()
        with self.lock:
            items = [((canvas, int(c), int(r)), (h.lower(), source, cookie)) for c, r, h in pixels]
            for key, val in (reversed(items) if front else items):
                self.failed.pop(key, None)
                self.pending[key] = val
                if front:
                    self.pending.move_to_end(key, last=False)
        return len(pixels)

    def drop_layer_pixels(self):
        """Retire tout de suite les pixels venant des calques (la surveillance les recalcule)."""
        with self.lock:
            for k in [k for k, v in self.pending.items() if v[1] == "layer"]:
                del self.pending[k]
        self.guard_kick.set()

    def clear_queue(self):
        with self.lock:
            n = len(self.pending)
            self.pending.clear()
        self.log(f"File vidée ({n} pixels)", "warning")

    def _next(self):
        with self.lock:
            if self.paused or time.time() < self.backoff_until or not self.pending:
                return None
            if not within_schedule(self.store.settings):  # minuteur de pose
                return None
            key, val = self.pending.popitem(last=False)
            self.inflight += 1
            return key, val

    def worker(self, idx):
        while True:
            active = int(self.store.settings.get("workers", 1))
            if time.time() < self.attack_until:  # mode défense : plus de requêtes en parallèle
                active = max(active, int(self.store.settings.get("defendWorkers", 8)))
            if idx >= active:
                time.sleep(0.3)
                continue
            item = self._next()
            if item is None:
                time.sleep(0.05)
                continue
            (canvas, col, row), (color, source, cookie) = item
            try:
                self._place(canvas, col, row, color, source, cookie)
            finally:
                with self.lock:
                    self.inflight -= 1
            delay = float(self.store.settings.get("delay", 0) or 0)
            if delay and time.time() >= self.attack_until:  # pas de pause en mode défense
                time.sleep(delay)

    def _place(self, canvas, col, row, color, source, cookie):
        res = telefunc("onPlacePixel", [canvas, col, row, color], cookie, canvas)
        status, code = res.get("status"), (res.get("code") or "")
        key = (canvas, col, row)
        if status == "success":
            wallet = (res.get("value") or {}).get("wallet")
            with self.lock:
                self.placed += 1
                self.recent.append(time.time())
                if wallet:
                    self.wallet = wallet
                b = self.boards.get(canvas)
                if b:
                    old = b["pixels"].get((col, row))
                    b["pixels"][(col, row)] = [color, "(vous)", old[2] if old else 0, (old[3] if old else 0) + 1]
            return
        if status == "rejected" and code == "PIXEL_UNCHANGED":
            with self.lock:
                self.skipped += 1
            return
        if status != "rejected" or any(h in code.lower() for h in RETRY_LATER_HINTS):
            wait = 10 if status == "rejected" else 2
            with self.lock:
                if key not in self.pending:
                    self.pending[key] = (color, source, cookie)
                    self.pending.move_to_end(key, last=False)
                first = time.time() >= self.backoff_until
                self.backoff_until = max(self.backoff_until, time.time() + wait)
            if first:
                self.log(f"Pause {wait}s : {code or res.get('message')} — {res.get('message', '')}", "warning")
            return
        with self.lock:
            self.errors += 1
            self.failed[key] = time.time() + 120
        self.log(f"Refusé ({col}, {row}) {color} : {code} — {res.get('message', '')}", "error")

    # -- surveillance des calques actifs
    def guard_loop(self):
        while True:
            kicked = self.guard_kick.wait(timeout=1)
            self.guard_kick.clear()
            if time.time() < self.attack_until:   # mode défense : relecture rapide
                interval = max(1, float(self.store.settings.get("defendInterval", 2)))
            else:
                interval = max(3, float(self.store.settings.get("guardInterval", 8)))
            if kicked or time.time() - self.last_guard >= interval:
                self.last_guard = time.time()
                try:
                    self.run_guard()
                except Exception as e:
                    self.log(f"Erreur surveillance : {e}", "error")

    def run_guard(self):
        with self.store.lock:
            layers = [l for l in self.store.layers if l.get("active")]
        by_canvas = collections.defaultdict(list)
        for l in layers:
            by_canvas[l["canvas"]].append(l)
        with self.lock:  # retire les anciennes cibles de calques (elles sont recalculées)
            for k in [k for k, v in self.pending.items() if v[1] == "layer"]:
                del self.pending[k]
        missing = {}
        for canvas, ls in by_canvas.items():
            cookie_of = {l["id"]: self.cookie_for_owner(l.get("owner")) for l in ls}
            b = self.fetch_board(canvas, max_age=0, cookie=cookie_of[ls[0]["id"]])
            if b is None:
                self.log(f"Lecture du canvas '{canvas}' impossible", "error")
                continue
            target, owner_of = {}, {}
            for l in ls:  # ordre des calques : le dernier est au-dessus
                for dx, dy, h in l["pixels"]:
                    c, r = l["x"] + dx, l["y"] + dy
                    if 0 <= c < b["w"] and 0 <= r < b["h"]:
                        target[(c, r)] = h.lower()
                        owner_of[(c, r)] = l["id"]
            now = time.time()
            todo = [(c, r, h) for (c, r), h in target.items()
                    if b["pixels"].get((c, r), ["#ffffff"])[0] != h and self.failed.get((canvas, c, r), 0) < now]
            for l in ls:
                missing[l["id"]] = 0
            for c, r, _ in todo:
                missing[owner_of[(c, r)]] += 1
            # Alerte vandalisme : un calque qui saute d'un coup de N pixels faux = quelqu'un l'écrase
            threshold = int(self.store.settings.get("vandalThreshold", 12))
            for l in ls:
                prev = self.prev_missing.get(l["id"], 0)
                jump = missing[l["id"]] - prev
                if prev <= 2 and jump >= threshold:
                    mine_names = our_names() | {"", "(vous)"}
                    attackers = collections.Counter(b["pixels"][(c, r)][1] for (c, r), lid in owner_of.items()
                                                    if lid == l["id"] and (c, r) in b["pixels"] and b["pixels"][(c, r)][1] not in mine_names)
                    who = ", ".join(f"{n} ({k})" for n, k in attackers.most_common(3)) or "inconnu"
                    self.log(f"VANDALISME sur « {l['name']} » : {jump} pixels écrasés. Auteur(s) : {who}", "error")
                    with self.lock:
                        self.vandal_alert = {"layer": l["name"], "canvas": canvas, "count": jump, "who": who, "t": now}
            # un pixel "attaqué" = actuellement peint par quelqu'un d'autre (pas vide, pas nous)
            mine = our_names() | {"", "(vous)"}
            def attacked(c, r):
                o = b["pixels"].get((c, r))
                return bool(o) and o[1] not in mine
            n_attacked = sum(1 for c, r, _ in todo if attacked(c, r))
            if n_attacked:  # défense : relecture rapide + plus de workers pendant 60 s
                with self.lock:
                    self.attack_until = max(self.attack_until, now + 60)
            if self.store.settings.get("order") == "random":
                random.shuffle(todo)
            else:
                todo.sort(key=lambda p: (p[1], p[0]))
            todo.sort(key=lambda p: 0 if attacked(p[0], p[1]) else 1)  # pixels attaqués d'abord
            with self.lock:
                for c, r, h in todo:
                    key = (canvas, c, r)
                    if key not in self.pending:
                        self.pending[key] = (h, "layer", cookie_of[owner_of[(c, r)]])  # cookie du propriétaire du calque
                        self.pending.move_to_end(key, last=(not attacked(c, r)))
        with self.lock:
            self.prev_missing = dict(missing)
            self.layer_missing = missing

    # -- état pour l'interface
    def status(self, canvas):
        with self.lock:
            now = time.time()
            while self.recent and now - self.recent[0] > 10:
                self.recent.popleft()
            pend = [[k[1], k[2], v[0]] for k, v in self.pending.items() if k[0] == canvas][:4000]
            return {
                "paused": self.paused,
                "queue": len(self.pending),
                "inflight": self.inflight,
                "rate": round(len(self.recent) / 10, 1),
                "placed": self.placed,
                "skipped": self.skipped,
                "errors": self.errors,
                "backoff": max(0, round(self.backoff_until - now)),
                "wallet": self.wallet,
                "logs": list(self.logs),
                "pending": pend,
                "layerMissing": self.layer_missing,
                "vandalAlert": self.vandal_alert,
                "defending": max(0, round(self.attack_until - now)),
                "posting": within_schedule(self.store.settings),
                "nextGuard": max(0, round(self.last_guard + float(self.store.settings.get("guardInterval", 20)) - now)),
            }


store = Store()
engine = Engine(store)


# ---------------------------------------------------------------- Outils de sécurité HTTP

def session_cookie(tok="", clear=False):
    attrs = "HttpOnly; Path=/; SameSite=Strict" + ("; Secure" if COOKIE_SECURE else "")
    return f"sid=; {attrs}; Max-Age=0" if clear else f"sid={tok}; {attrs}; Max-Age={SESSION_TTL}"


_login_fails = collections.defaultdict(list)   # ip -> [timestamps d'échec]
_login_lock = threading.Lock()


def login_blocked(ip):
    now = time.time()
    with _login_lock:
        fails = [t for t in _login_fails[ip] if now - t < 900]  # fenêtre 15 min
        _login_fails[ip] = fails
        return len(fails) >= 10


def login_fail(ip):
    with _login_lock:
        _login_fails[ip].append(time.time())


def login_ok(ip):
    with _login_lock:
        _login_fails.pop(ip, None)


# ---------------------------------------------------------------- Serveur web

class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE, **kwargs)

    def log_message(self, fmt, *args):
        pass  # silencieux (le polling est très bavard)

    def end_headers(self):
        # en-têtes de sécurité sur toutes les réponses
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
                         "base-uri 'none'; form-action 'self'")
        if COOKIE_SECURE:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        super().end_headers()

    def _same_origin(self):
        """Anti-CSRF : refuse une requête d'écriture venant d'un autre site."""
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs is not None:
            return sfs in ("same-origin", "same-site", "none")
        origin = self.headers.get("Origin")
        if origin:
            return urllib.parse.urlparse(origin).netloc == self.headers.get("Host", "")
        return True  # pas de signaux navigateur (curl / app locale) : autorisé

    def _json(self, data, code=200, set_cookie=None):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if set_cookie is not None:
            self.send_header("Set-Cookie", set_cookie)
        self.end_headers()
        self.wfile.write(body)

    # -- authentification
    def _cookies(self):
        out = {}
        for part in (self.headers.get("Cookie", "") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def _user(self):
        uid = store.session_uid(self._cookies().get("sid"))
        return store.users.get(uid) if uid else None

    def _my_cookie(self, user):
        return (user.get("cookie") if user else "") or ""

    def _my_canvas(self, user, canvas=None):
        return canvas or (user.get("canvas") if user else None) or store.settings["canvas"]

    def do_GET(self):
        if self.path.startswith("/data/") or self.path in ("/server.py", "/.env"):
            return self.send_error(403)
        if self.path == "/api/me":
            u = self._user()
            if not u:
                return self._json({"auth": False, "backend": store.backend})
            return self._json({"auth": True, "username": u["username"], "hasCookie": bool(u.get("cookie")),
                               "canvas": u.get("canvas", store.settings["canvas"]), "prefs": u.get("prefs", {}),
                               "backend": store.backend})
        # seule la page de l'app est servie (pas d'accès aux autres fichiers du dossier)
        if not self.path.startswith("/api/"):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self.path = "/index.html"
                return super().do_GET()
            return self.send_error(404)
        u = self._user()
        if not u:
            return self._json({"auth": False, "message": "connexion requise"}, 401)
        ck = self._my_cookie(u)
        if self.path == "/api/canvases":
            lst = list_canvases(ck, self._my_canvas(u))
            return self._json({"success": lst is not None, "canvases": lst or []})
        if self.path == "/api/layers":
            with store.lock:
                mine = [l for l in store.layers if l.get("owner") == u["id"]]
                return self._json({"layers": mine, "settings": store.settings, "backend": store.backend,
                                   "canvas": u.get("canvas", store.settings["canvas"]), "cookie": ck})
        if self.path == "/api/sprites":
            with store.lock:
                return self._json({"sprites": [s for s in store.sprites if s.get("owner") == u["id"]]})
        if self.path.startswith("/api/status"):
            canvas = self._my_canvas(u, self.path.partition("canvas=")[2] or None)
            return self._json(engine.status(canvas))
        if self.path.startswith("/api/stats"):
            canvas = self._my_canvas(u, self.path.partition("canvas=")[2] or None)
            engine.fetch_board(canvas, max_age=3, cookie=ck)
            return self._json({"rows": engine.stats_for(canvas, nick_of(ck) or u["username"]), "me": nick_of(ck) or u["username"]})
        return self._json({"success": False}, 404)

    def do_POST(self):
        if not self._same_origin():  # anti-CSRF
            return self._json({"success": False, "message": "origine refusée"}, 403)
        n = int(self.headers.get("Content-Length", 0))
        if n > MAX_BODY:
            return self._json({"success": False, "message": "requête trop volumineuse"}, 413)
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            body = {}
        p = self.path
        ip = self.client_address[0] if self.client_address else "?"

        # --- authentification (public) ---
        if p in ("/api/register", "/api/login"):
            if login_blocked(ip):
                return self._json({"success": False, "message": "trop de tentatives, réessaie dans 15 min"}, 429)
            time.sleep(0.3)  # ralentit la force brute
        if p == "/api/register":
            un, pw = (body.get("username") or "").strip(), body.get("password") or ""
            if len(un) < 3 or len(pw) < MIN_PW:
                return self._json({"success": False, "message": f"pseudo ≥ 3 et mot de passe ≥ {MIN_PW} caractères"}, 400)
            u = store.create_user(un, pw)
            if not u:
                return self._json({"success": False, "message": "pseudo déjà pris"}, 409)
            login_ok(ip)
            tok = store.new_session(u["id"])
            return self._json({"success": True, "username": un}, set_cookie=session_cookie(tok))
        if p == "/api/login":
            u = store.user_by_name((body.get("username") or "").strip())
            if not u or not check_pw(body.get("password") or "", u["pw"]):
                login_fail(ip)
                return self._json({"success": False, "message": "pseudo ou mot de passe incorrect"}, 401)
            login_ok(ip)
            tok = store.new_session(u["id"])
            return self._json({"success": True, "username": u["username"]}, set_cookie=session_cookie(tok))
        if p == "/api/logout":
            store.delete_session(self._cookies().get("sid"))
            return self._json({"success": True}, set_cookie=session_cookie(clear=True))

        # --- à partir d'ici : connexion requise ---
        u = self._user()
        if not u:
            return self._json({"auth": False, "message": "connexion requise"}, 401)
        uid, ck = u["id"], self._my_cookie(u)

        if p == "/api/account":  # cookie Codawar + canvas par défaut du compte
            with store.lock:
                if "cookie" in body:
                    u["cookie"] = (body.get("cookie") or "").strip()
                if "canvas" in body:
                    u["canvas"] = body.get("canvas") or store.settings["canvas"]
                if "prefs" in body and isinstance(body["prefs"], dict):
                    u["prefs"] = body["prefs"]
                store.save()
            if u.get("cookie"):  # apprend le pseudo Codawar de ce cookie (en tâche de fond)
                threading.Thread(target=lambda c=u["cookie"], cv=u["canvas"]: list_canvases(c, cv), daemon=True).start()
            return self._json({"success": True, "hasCookie": bool(u.get("cookie")), "canvas": u.get("canvas")})

        if p == "/api/sprites/save":
            sp = body.get("sprite")
            if not sp or "id" not in sp:
                return self._json({"success": False}, 400)
            sp["owner"] = uid
            store.save_sprite(sp)
            return self._json({"success": True})
        if p == "/api/sprites/delete":
            with store.lock:
                if any(s["id"] == body.get("id") and s.get("owner") == uid for s in store.sprites):
                    store.delete_sprite(body.get("id"))
            return self._json({"success": True})

        if p == "/api/canvas":
            canvas = self._my_canvas(u, body.get("canvas"))
            b = engine.fetch_board(canvas, max_age=float(body.get("maxAge", 2)), cookie=ck)
            if b is None:
                return self._json({"success": False, "message": "lecture impossible (cookie Codawar ? nom du canvas ?)"})
            owners, oidx, cells = [], {}, []
            for (c, r), (h, o, prot, *_rest) in b["pixels"].items():
                if o not in oidx:
                    oidx[o] = len(owners)
                    owners.append(o)
                cells.extend((c, r, h, oidx[o], prot))
            return self._json({"success": True, "w": b["w"], "h": b["h"], "owners": owners, "cells": cells,
                               "wallet": b["wallet"], "closure": b.get("closure")})

        if p == "/api/place":
            if not ck:
                return self._json({"success": False, "message": "ajoute ton cookie Codawar dans Réglages"}, 400)
            canvas = self._my_canvas(u, body.get("canvas"))
            qn = engine.enqueue(canvas, body.get("pixels", []), front=body.get("front", True), source="manual", cookie=ck)
            return self._json({"success": True, "queued": qn})

        if p == "/api/queue/clear":
            engine.clear_queue()
            return self._json({"success": True})

        if p == "/api/pause":
            engine.paused = bool(body.get("paused", True))
            engine.log("Pause" if engine.paused else "Reprise", "warning" if engine.paused else "info")
            return self._json({"success": True, "paused": engine.paused})

        if p == "/api/guard/run":
            engine.guard_kick.set()
            return self._json({"success": True})

        if p == "/api/settings":  # réglages moteur partagés
            with store.lock:
                for k in DEFAULT_SETTINGS:
                    if k in body and k != "cookie":  # le cookie est par compte (/api/account)
                        store.settings[k] = body[k]
                store.settings["workers"] = max(1, min(MAX_WORKERS, int(store.settings["workers"])))
                store.save()
            return self._json({"success": True, "settings": store.settings})

        if p == "/api/layers/save":
            layer = body.get("layer")
            if not layer or "id" not in layer:
                return self._json({"success": False, "message": "calque invalide"}, 400)
            layer["owner"] = uid
            with store.lock:
                old = next((l for l in store.layers if l["id"] == layer["id"]), None)
                if old and old.get("owner") not in (None, uid):
                    return self._json({"success": False, "message": "calque d'un autre compte"}, 403)
            store.upsert_layer(layer)
            if old and (old.get("active") and not layer.get("active") or old.get("x") != layer.get("x") or old.get("y") != layer.get("y")):
                engine.drop_layer_pixels()
            else:
                engine.guard_kick.set()
            return self._json({"success": True})

        if p == "/api/layers/delete":
            with store.lock:
                if any(l["id"] == body.get("id") and l.get("owner") == uid for l in store.layers):
                    store.delete_layer(body.get("id"))
            engine.drop_layer_pixels()
            return self._json({"success": True})

        if p == "/api/layers/reorder":
            store.reorder(body.get("ids", []))
            engine.guard_kick.set()
            return self._json({"success": True})

        return self._json({"success": False}, 404)


def main():
    # apprend le pseudo Codawar de chaque compte (pour ne pas prendre ses pixels pour une attaque)
    def prime():
        for u in list(store.users.values()):
            if u.get("cookie"):
                list_canvases(u["cookie"], u.get("canvas", DEFAULT_CANVAS))
    threading.Thread(target=prime, daemon=True).start()
    for i in range(MAX_WORKERS):
        threading.Thread(target=engine.worker, args=(i,), daemon=True).start()
    threading.Thread(target=engine.guard_loop, daemon=True).start()
    threading.Thread(target=engine.persist_loop, daemon=True).start()
    if AUTO_PULL:
        threading.Thread(target=auto_pull_loop, args=(engine.persist,), daemon=True).start()
        print(f"Mise à jour auto : branche {AUTO_PULL_BRANCH} toutes les {AUTO_PULL_INTERVAL}s", flush=True)
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    httpd = http.server.ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    print(f"Codawar Pixel Studio : http://localhost:{PORT}  (stockage : {store.backend})", flush=True)
    engine.log("Serveur démarré")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")
    finally:
        engine.persist()  # dernière sauvegarde propre


if __name__ == "__main__":
    main()
