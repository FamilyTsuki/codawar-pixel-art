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
        self._expired = set()  # cookies refusés par Codawar (SESSION_EXPIRED) : inutilisables
        self._busy = set()     # cookies avec une action en vol (une seule à la fois)
        self._end = {}         # cookie -> fin (réponse reçue) de la dernière action

    def blocked(self, cookie, now=None):
        """Vrai si ce compte ne doit envoyer aucune action maintenant."""
        now = time.time() if now is None else now
        with self._lock:
            return self._blocked(cookie, now)

    def _blocked(self, cookie, now):
        """Verrou déjà pris."""
        return (cookie in self._expired
                or cookie in self._busy
                or self._pause.get(cookie, 0) > now
                or now - self._last.get(cookie, 0) < config.PLACE_MIN_INTERVAL
                or now - self._end.get(cookie, 0) < config.PLACE_GAP_AFTER_RESPONSE)

    def reserve(self, cookie, now=None):
        """Réserve un créneau ; False si le compte est bloqué (rien n'est réservé).

        Un créneau réservé DOIT être rendu par release() une fois la réponse reçue.
        """
        now = time.time() if now is None else now
        with self._lock:
            if self._blocked(cookie, now):
                return False
            self._last[cookie] = now
            self._busy.add(cookie)
            return True

    def release(self, cookie):
        """Réponse reçue : le compte peut repartir après PLACE_GAP_AFTER_RESPONSE."""
        with self._lock:
            self._busy.discard(cookie)
            self._end[cookie] = time.time()

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

    def mark_expired(self, cookie):
        """Cookie mort : plus aucune action avec lui. Un cookie ressaisi est une autre
        chaîne, il repart donc de lui-même."""
        with self._lock:
            self._expired.add(cookie)

    def is_expired(self, cookie):
        with self._lock:
            return cookie in self._expired

    def clear_pause(self, cookie):
        with self._lock:
            self._pause.pop(cookie, None)
