"""Sécurité : hachage des mots de passe, cookie de session, limite de tentatives."""

import collections
import hashlib
import hmac
import secrets
import threading
import time

from . import config


def _derive(pw, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", pw.encode() + config.PEPPER,
                               bytes.fromhex(salt_hex), config.PBKDF2_ROUNDS).hex()


def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    return f"{salt}${_derive(pw, salt)}"


def check_pw(pw, stored):
    try:
        salt, dk = stored.split("$")
        return hmac.compare_digest(dk, _derive(pw, salt))
    except (ValueError, AttributeError):
        return False


def session_cookie(tok="", clear=False):
    attrs = "HttpOnly; Path=/; SameSite=Strict" + ("; Secure" if config.COOKIE_SECURE else "")
    if clear:
        return f"sid=; {attrs}; Max-Age=0"
    return f"sid={tok}; {attrs}; Max-Age={config.SESSION_TTL}"


class LoginLimiter:
    """Compte les échecs de connexion par IP sur une fenêtre glissante."""

    def __init__(self, window=config.LOGIN_WINDOW, max_fails=config.LOGIN_MAX_FAILS):
        self.window = window
        self.max_fails = max_fails
        self._fails = collections.defaultdict(list)  # ip -> [timestamps d'échec]
        self._lock = threading.Lock()

    def blocked(self, ip):
        now = time.time()
        with self._lock:
            fails = [t for t in self._fails[ip] if now - t < self.window]
            self._fails[ip] = fails
            return len(fails) >= self.max_fails

    def fail(self, ip):
        with self._lock:
            self._fails[ip].append(time.time())

    def reset(self, ip):
        with self._lock:
            self._fails.pop(ip, None)
