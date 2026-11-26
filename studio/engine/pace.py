"""Cadence partagée des actions de pose d'un compte Codawar.

La limite du site (3 pixels/s, sinon exclusion 15 min) compte toutes les actions qui
touchent le canvas : poses de pixels ET outils de destruction. Les deux chemins passent
donc par le même garde-fou, sinon un outil utilisé pendant que la file tourne ajoute une
requête dans la même seconde et peut déclencher l'exclusion.
"""

import threading
import time

from .. import config


class AccountPacer:
    def __init__(self):
        self._lock = threading.Lock()
        self._last = {}    # cookie -> départ de la dernière action
        self._pause = {}   # cookie -> fin d'une exclusion / interdiction

    def blocked(self, cookie, now=None):
        """Vrai si ce compte ne doit envoyer aucune action maintenant."""
        now = time.time() if now is None else now
        with self._lock:
            return (self._pause.get(cookie, 0) > now
                    or now - self._last.get(cookie, 0) < config.PLACE_MIN_INTERVAL)

    def reserve(self, cookie, now=None):
        """Réserve un créneau ; False si le compte est bloqué (rien n'est réservé)."""
        now = time.time() if now is None else now
        with self._lock:
            if (self._pause.get(cookie, 0) > now
                    or now - self._last.get(cookie, 0) < config.PLACE_MIN_INTERVAL):
                return False
            self._last[cookie] = now
            return True

    def wait_turn(self, cookie, timeout=30):
        """Attend un créneau libre pour une action manuelle ; False si le compte reste bloqué."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.reserve(cookie):
                return True
            time.sleep(0.1)
        return False

    def pause(self, cookie, seconds):
        with self._lock:
            self._pause[cookie] = max(self._pause.get(cookie, 0), time.time() + seconds)

    def pause_remaining(self, cookie):
        with self._lock:
            return max(0, round(self._pause.get(cookie, 0) - time.time()))

    def clear_pause(self, cookie):
        with self._lock:
            self._pause.pop(cookie, None)
