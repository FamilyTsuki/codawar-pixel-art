"""Handler HTTP : en-têtes de sécurité, anti-CSRF, lecture du corps, aiguillage vers l'API.

Le handler ne contient aucune logique métier : il traduit la requête en Request,
appelle la route trouvée dans la table de l'Api et écrit la Response.
"""

import http.server
import json
import urllib.parse

from .. import config
from .api import Request, Response

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                               "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; "
                               "base-uri 'none'; form-action 'self'",
}
FORBIDDEN_PATHS = ("/server.py", "/.env")
APP_PAGES = ("/", "/index.html")


def make_handler(api):
    """Classe de handler liée à une instance d'Api (injection de dépendance)."""
    return type("Handler", (Handler,), {"api": api})


class Handler(http.server.SimpleHTTPRequestHandler):
    api = None  # fourni par make_handler

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=config.BASE, **kwargs)

    def log_message(self, fmt, *args):
        pass  # silencieux (le polling est très bavard)

    def end_headers(self):
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        if config.COOKIE_SECURE:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        super().end_headers()

    # -- GET
    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path.startswith("/data/") or path in FORBIDDEN_PATHS:
            return self.send_error(403)
        if not path.startswith("/api/"):
            return self._serve_app(path)
        self._dispatch("GET", path, self._request(query=query))

    def _serve_app(self, path):
        """Seule la page de l'app est servie (pas d'accès aux autres fichiers du dossier)."""
        if path not in APP_PAGES:
            return self.send_error(404)
        self.path = config.INDEX_FILE
        return super().do_GET()

    # -- POST
    def do_POST(self):
        if not self._same_origin():
            return self._send(Response({"success": False, "message": "origine refusée"}, 403))
        n = int(self.headers.get("Content-Length", 0))
        if n > config.MAX_BODY:
            return self._send(Response({"success": False, "message": "requête trop volumineuse"}, 413))
        self._dispatch("POST", self.path, self._request(body=self._read_json(n)))

    def _read_json(self, n):
        if not n:
            return {}
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return body if isinstance(body, dict) else {}

    def _same_origin(self):
        """Anti-CSRF : refuse une requête d'écriture venant d'un autre site."""
        sfs = self.headers.get("Sec-Fetch-Site")
        if sfs is not None:
            return sfs in ("same-origin", "same-site", "none")
        origin = self.headers.get("Origin")
        if origin:
            return urllib.parse.urlparse(origin).netloc == self.headers.get("Host", "")
        return True  # pas de signaux navigateur (curl / app locale) : autorisé

    # -- aiguillage
    def _request(self, query="", body=None):
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
        return Request(sid=self._cookies().get("sid"), ip=self.client_address[0] if self.client_address else "?",
                       query=params, body=body or {})

    def _dispatch(self, method, path, req):
        self._send(self.api.handle(method, path, req))

    def _cookies(self):
        out = {}
        for part in (self.headers.get("Cookie", "") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out

    def _send(self, resp):
        body = json.dumps(resp.data).encode("utf-8")
        self.send_response(resp.code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if resp.cookie is not None:
            self.send_header("Set-Cookie", resp.cookie)
        self.end_headers()
        self.wfile.write(body)


def serve(api, host=config.HOST, port=config.PORT):
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    httpd = http.server.ThreadingHTTPServer((host, port), make_handler(api))
    httpd.daemon_threads = True
    return httpd
