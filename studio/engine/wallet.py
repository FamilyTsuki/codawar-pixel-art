"""Solde de crédits Codawar et estimation de la vitesse de rechargement."""

import calendar
import threading
import time

REFILL_MIN_WINDOW = 45   # durée minimale d'une mesure de rechargement (s)
REFILL_MAX_RATE = 2      # crédits/s au-delà : mesure aberrante
CREDIT_WAIT_DEFAULT = 30


def seconds_until_refill(wallet):
    """Secondes avant le prochain crédit d'après wallet["refilledAt"], ou None."""
    ra = (wallet or {}).get("refilledAt")
    if not ra:
        return None
    try:
        return calendar.timegm(time.strptime(ra[:19], "%Y-%m-%dT%H:%M:%S")) - time.time()
    except (ValueError, TypeError):
        return None


class WalletTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self.wallet = None
        self.refill_rate = 0.0  # crédits/seconde estimés (moyenne glissante)
        self._base_balance = None
        self._base_t = 0

    def set(self, wallet):
        """Solde renvoyé après une pose."""
        if wallet:
            with self._lock:
                self.wallet = wallet

    def observe(self, wallet, t):
        """Solde lu avec le canvas : met à jour l'estimation du rechargement."""
        with self._lock:
            self.wallet = wallet or self.wallet
            self._track_refill(wallet, t)

    def _track_refill(self, wallet, t):
        """Mesure sur des fenêtres ≥45 s où le solde monte."""
        if not wallet or wallet.get("isUnlimited"):
            return
        bal = wallet.get("balance")
        if bal is None:
            return
        if self._base_balance is None or bal < self._base_balance:
            # première mesure, ou on a posé des pixels : nouvelle base
            self._base_balance, self._base_t = bal, t
            return
        dt = t - self._base_t
        if dt >= REFILL_MIN_WINDOW and bal > self._base_balance:
            rate = (bal - self._base_balance) / dt
            if rate <= REFILL_MAX_RATE:
                self.refill_rate = rate if not self.refill_rate else 0.6 * self.refill_rate + 0.4 * rate
            self._base_balance, self._base_t = bal, t

    def credit_wait(self):
        """Attente jusqu'au prochain crédit, au lieu de marteler toutes les 10 s."""
        nxt = seconds_until_refill(self.wallet)
        if nxt is None:
            return CREDIT_WAIT_DEFAULT
        return max(15, min(300, round(nxt) + 2))

    def snapshot(self):
        """Champs d'état pour l'interface."""
        w = self.wallet or {}
        nxt = seconds_until_refill(w)
        return {
            "credits": w.get("balance"),
            "unlimited": bool(w.get("isUnlimited")),
            "refillPerMin": round(self.refill_rate * 60, 2) if self.refill_rate else None,
            "nextCredit": max(0, round(nxt)) if nxt is not None else None,
            "wallet": self.wallet,
        }
