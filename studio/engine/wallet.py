"""Solde de crédits Codawar et estimation de la vitesse de rechargement.

Chaque canvas a son propre portefeuille, et chaque compte (cookie) aussi : on suit donc
un portefeuille par couple (canvas, compte) et on agrège par canvas pour l'interface.

Rechargement estimé par comptabilité : sur une fenêtre glissante,
    rechargé = (solde_fin - solde_début) + crédits dépensés entre les deux
ce qui reste juste même quand on pose en continu (le solde ne monte alors jamais).
"""

import calendar
import collections
import hashlib
import threading
import time

REFILL_WINDOW = 300      # fenêtre glissante de mesure du rechargement (s)
REFILL_MIN_SPAN = 45     # durée minimale de mesure avant d'afficher une estimation (s)
REFILL_MAX_RATE = 5      # crédits/s au-delà : mesure aberrante
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


def account_key(cookie):
    """Identifiant court et non réversible d'un compte (on ne garde pas le cookie en clé)."""
    return hashlib.sha1((cookie or "").encode()).hexdigest()[:12]


class _Account:
    """Portefeuille d'un compte sur un canvas."""

    def __init__(self):
        self.wallet = None
        self.spent = 0                     # crédits dépensés (poses réussies) depuis le début
        self.points = collections.deque()  # (t, solde, dépensés cumulés)
        self.rate = 0.0                    # crédits/s estimés

    def observe(self, wallet, t):
        self.wallet = wallet
        if wallet.get("isUnlimited"):
            return
        bal = wallet.get("balance")
        if bal is None:
            return
        self.points.append((t, bal, self.spent))
        while self.points and t - self.points[0][0] > REFILL_WINDOW:
            self.points.popleft()
        self._estimate()

    def _estimate(self):
        t0, b0, s0 = self.points[0]
        t1, b1, s1 = self.points[-1]
        span = t1 - t0
        if span < REFILL_MIN_SPAN:
            return
        refilled = (b1 - b0) + (s1 - s0)
        idle = s1 == s0 and b1 == b0  # aucune pose, solde figé : peut-être au plafond, on garde l'estimation
        if idle and self.rate:
            return
        rate = max(0.0, refilled / span)
        if rate <= REFILL_MAX_RATE:
            self.rate = rate


class WalletTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._accounts = {}  # (canvas, compte) -> _Account

    def _acc(self, canvas, cookie):
        k = (canvas, account_key(cookie))
        acc = self._accounts.get(k)
        if acc is None:
            acc = self._accounts[k] = _Account()
        return acc

    def placed(self, canvas, cookie, wallet, t=None):
        """Pose réussie : 1 crédit dépensé ; solde renvoyé par Codawar."""
        with self._lock:
            acc = self._acc(canvas, cookie)
            acc.spent += 1
            if wallet:
                acc.observe(wallet, t or time.time())

    def observe(self, canvas, account, wallet, t):
        """Solde lu avec le canvas ; `account` = account_key(cookie utilisé pour la lecture)."""
        if not wallet:
            return
        with self._lock:
            k = (canvas, account)
            acc = self._accounts.get(k) or self._accounts.setdefault(k, _Account())
            acc.observe(wallet, t)

    def credit_wait(self, canvas, cookie):
        """Attente jusqu'au prochain crédit, au lieu de marteler toutes les 10 s."""
        with self._lock:
            acc = self._accounts.get((canvas, account_key(cookie)))
            nxt = seconds_until_refill(acc.wallet) if acc else None
        if nxt is None:
            return CREDIT_WAIT_DEFAULT
        return max(15, min(300, round(nxt) + 2))

    def snapshot(self, canvas):
        """Champs d'état pour l'interface, agrégés sur les comptes de ce canvas."""
        with self._lock:
            accs = [a for (cv, _), a in self._accounts.items() if cv == canvas and a.wallet]
            wallets = [a.wallet for a in accs]
            balances = [w.get("balance") for w in wallets if w.get("balance") is not None]
            unlimited = bool(wallets) and all(w.get("isUnlimited") for w in wallets)
            rate = sum(a.rate for a in accs)
            nexts = [n for n in (seconds_until_refill(w) for w in wallets) if n is not None]
        return {
            "credits": sum(balances) if balances else None,
            "accounts": len(wallets),
            "unlimited": unlimited,
            "refillPerMin": round(rate * 60, 2) if rate else None,
            "nextCredit": max(0, round(min(nexts))) if nexts else None,
        }
