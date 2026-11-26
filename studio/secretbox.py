"""Chiffrement des cookies Codawar au repos (base PostgreSQL et data/state.json).

Les cookies restent en clair en mémoire (le moteur s'en sert pour poser) ; seul ce qui
est écrit sur disque est chiffré. Fernet (AES-128-CBC + HMAC-SHA256), clé :
- COOKIE_KEY si défini (clé Fernet : python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"),
- sinon dérivée de AUTH_PEPPER, déjà secret et hors de la base.

Protège contre une fuite de la base ou d'une sauvegarde, pas contre qui a accès au
serveur : la clé y est forcément, puisque l'app doit déchiffrer pour utiliser les cookies.

Les valeurs chiffrées portent le préfixe "enc1:". Une valeur sans préfixe est un ancien
cookie en clair : elle est lue telle quelle puis chiffrée à la sauvegarde suivante.
"""

import base64
import hashlib
import os
import threading

from . import config

try:
    from cryptography.fernet import Fernet, InvalidToken
except ModuleNotFoundError:  # dépendance pas encore installée (ex. juste après un auto-pull)
    Fernet = None
    InvalidToken = Exception

PREFIX = "enc1:"


def _log(msg):
    print(msg, flush=True)


def _key():
    explicit = os.environ.get("COOKIE_KEY", "").strip()
    if explicit:
        return explicit.encode()
    if config.PEPPER:
        digest = hashlib.sha256(b"codawar-cookie-key:" + config.PEPPER).digest()
        return base64.urlsafe_b64encode(digest)
    return None


class SecretBox:
    def __init__(self):
        self._fernet = None
        self._cache = {}  # clair -> jeton : évite de rechiffrer (IV aléatoire) à chaque sauvegarde
        self._lock = threading.Lock()
        key = _key()
        if Fernet is None:
            _log("ATTENTION : module cryptography absent — cookies Codawar stockés EN CLAIR")
        elif key is None:
            _log("ATTENTION : ni COOKIE_KEY ni AUTH_PEPPER — cookies Codawar stockés EN CLAIR")
        else:
            try:
                self._fernet = Fernet(key)
            except ValueError as e:
                _log(f"ATTENTION : COOKIE_KEY invalide ({e}) — cookies Codawar stockés EN CLAIR")

    @property
    def enabled(self):
        return self._fernet is not None

    def encrypt(self, value):
        if not value or not self.enabled or value.startswith(PREFIX):
            return value
        with self._lock:
            tok = self._cache.get(value)
            if tok is None:
                tok = self._cache[value] = PREFIX + self._fernet.encrypt(value.encode()).decode()
            return tok

    def decrypt(self, value):
        """Valeur en clair ; "" si le jeton est illisible (clé changée ou module absent)."""
        if not value or not value.startswith(PREFIX):
            return value or ""
        if not self.enabled:
            _log("Cookie chiffré illisible : module cryptography ou clé absents")
            return ""
        try:
            plain = self._fernet.decrypt(value[len(PREFIX):].encode()).decode()
        except InvalidToken:
            _log("Cookie chiffré illisible (clé changée ?) : à ressaisir dans l'interface")
            return ""
        with self._lock:
            self._cache[plain] = value
        return plain

    def is_plain(self, value):
        """Vrai pour un cookie non vide encore stocké en clair."""
        return bool(value) and not value.startswith(PREFIX)


box = SecretBox()
