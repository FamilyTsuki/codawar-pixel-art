"""Client HTTP du site Codawar."""

import http.client
import json
import re
import threading
import time

from . import config

COOKIE_NAME = "codawar_session"


def normalize_cookie(raw):
    """Nettoie un cookie collé par l'utilisateur et renvoie la valeur d'en-tête Cookie.

    Tolère : guillemets autour, préfixe « Cookie: » collé depuis les outils développeur,
    clé absente (valeur seule), espaces et sauts de ligne. Champ vide = efface le cookie.
    Lève ValueError avec un message simple si le cookie est inutilisable.
    """
    s = (raw or "").strip()
    if not s:
        return ""  # champ vidé volontairement
    if s[:7].lower() == "cookie:":  # en-tête collé entier
        s = s[7:].strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":  # guillemets autour
        s = s[1:-1].strip()
    s = " ".join(s.split())  # supprime sauts de ligne / tabulations
    if not s:
        raise ValueError("cookie vide")
    if "=" not in s:  # l'utilisateur a collé seulement la valeur
        s = f"{COOKIE_NAME}={s}"
    if f"{COOKIE_NAME}=" not in s:
        raise ValueError(f"cookie invalide : il doit contenir « {COOKIE_NAME}= »")
    return s


class CodawarClient:
    """Appels telefunc avec une connexion HTTPS persistante par thread."""

    def __init__(self, host=config.TELEFUNC_HOST):
        self.host = host
        self._local = threading.local()

    def call(self, name, args, cookie, canvas, timeout=15):
        """Renvoie le champ "ret" de la réponse, ou {"status": "error", "message": ...}."""
        body = json.dumps({"file": config.TELEFUNC_FILE, "name": name, "args": args}).encode("utf-8")
        headers = {
            "Content-Type": "text/plain",
            "x-telefunc-request": "txt",
            "Origin": f"https://{self.host}",
            "Referer": f"https://{self.host}/canvas/{canvas}",
            "Cookie": cookie,
            "User-Agent": config.USER_AGENT,
            "Connection": "keep-alive",
        }
        for attempt in range(2):  # 2e essai si la connexion réutilisée a été fermée
            try:
                return self._post(body, headers, timeout)
            except (http.client.HTTPException, OSError) as e:
                self._reset_connection()
                if attempt == 1:
                    return _error(str(e))
            except Exception as e:  # réponse illisible, etc.
                return _error(str(e))

    def _post(self, body, headers, timeout):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = http.client.HTTPSConnection(self.host, timeout=timeout)
        conn.timeout = timeout
        conn.request("POST", config.TELEFUNC_PATH, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        if resp.status != 200:
            return _error(f"HTTP {resp.status}: {resp.reason}")
        return json.loads(data.decode("utf-8")).get("ret", {})

    def _reset_connection(self):
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        self._local.conn = None

    def canvas_page(self, cookie, canvas):
        """HTML de la page d'un canvas (contient le pageContext)."""
        conn = http.client.HTTPSConnection(self.host, timeout=15)
        try:
            conn.request("GET", f"/canvas/{canvas}", headers={"Cookie": cookie, "User-Agent": "Mozilla/5.0"})
            return conn.getresponse().read().decode("utf-8", "replace")
        finally:
            conn.close()


def _error(message):
    return {"status": "error", "message": message}


class CanvasDirectory:
    """Liste des canvas accessibles + pseudo Codawar associé à chaque cookie.

    Les pseudos servent à ne pas prendre les pixels de nos propres comptes pour une attaque.
    """

    def __init__(self, client, ttl=config.CANVASES_CACHE_TTL):
        self.client = client
        self.ttl = ttl
        self._cache = {"t": 0, "list": None}
        self._nick = {}  # cookie -> pseudo Codawar

    def our_names(self):
        return {n for n in self._nick.values() if n}

    def nickname(self, cookie):
        return self._nick.get(cookie)

    def list(self, cookie, canvas):
        """Liste lue dans le pageContext de la page du site (cache partagé)."""
        if self._cache["list"] and time.time() - self._cache["t"] < self.ttl:
            return self._cache["list"]
        try:
            html = self.client.canvas_page(cookie, canvas)
        except Exception:
            return self._cache["list"]
        self._learn_nickname(cookie, html)
        found = _parse_canvases(html)
        if found is None:
            return self._cache["list"]
        self._cache.update(t=time.time(), list=found)
        return found

    def learn_async(self, cookie, canvas):
        """Apprend le pseudo d'un cookie en tâche de fond."""
        threading.Thread(target=self.list, args=(cookie, canvas), daemon=True).start()

    def _learn_nickname(self, cookie, html):
        m = re.search(r'"nickname":"([^"]{1,40})"', html)
        if m:
            self._nick[cookie] = m.group(1)


def _parse_canvases(html):
    marker = '"canvases":'
    i = html.find(marker + "[")
    if i < 0:
        return None
    try:
        arr, _ = json.JSONDecoder().raw_decode(html, i + len(marker))
        return [{"id": c["id"], "name": c.get("name") or c["id"], "archived": bool(c.get("isArchived")),
                 "unlimited": bool(c.get("hasUnlimitedCredits"))} for c in arr if c.get("id")]
    except Exception:
        return None
