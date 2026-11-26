"""Solde de crédits Codawar, calculé exactement comme le fait le site.

Règle Codawar (code client du site) : chaque compte a un portefeuille par canvas,
+1 crédit toutes les 5 min depuis `refilledAt`, plafonné à 100 ; le temps passé dans
une des `pauses` du portefeuille ne compte pas. Le `balance` renvoyé n'est PAS réglé :
le solde réel est balance + crédits accumulés depuis refilledAt.
"""

import hashlib
import threading
import time
from datetime import datetime

REFILL_INTERVAL = 300   # s par crédit
MAX_CREDITS = 100
CREDIT_WAIT_DEFAULT = 30


def account_key(cookie):
    """Identifiant court et non réversible d'un compte (on ne garde pas le cookie en clé)."""
    return hashlib.sha1((cookie or "").encode()).hexdigest()[:12]


def _ts(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError, TypeError):
        return None


def _pauses(wallet):
    out = []
    for p in wallet.get("pauses") or []:
        s, e = _ts(p.get("start")), _ts(p.get("end"))
        if s is not None and e is not None and e > s:
            out.append((s, e))
    out.sort()
    merged = []
    for s, e in out:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _active(t, pauses):
    """Temps réel -> temps « actif » (pauses retirées)."""
    return t - sum(min(e, t) - s for s, e in pauses if s < t)


def _real(a, pauses):
    """Temps actif -> temps réel (inverse de _active)."""
    extra = 0.0
    for s, e in pauses:
        if a + extra <= s:
            break
        extra += e - s
    return a + extra


def settle(wallet, now=None):
    """(solde réel, timestamp du prochain crédit ou None si au plafond / illimité)."""
    if not wallet:
        return None, None
    bal = wallet.get("balance")
    if wallet.get("isUnlimited"):
        return bal, None
    ra = _ts(wallet.get("refilledAt"))
    if bal is None or ra is None:
        return bal, None
    now = time.time() if now is None else now
    pauses = _pauses(wallet)
    start = _active(ra, pauses)
    n = int(max(0.0, _active(now, pauses) - start) // REFILL_INTERVAL)
    if bal + n >= MAX_CREDITS:
        return max(bal, MAX_CREDITS), None
    return bal + n, _real(start + (n + 1) * REFILL_INTERVAL, pauses)


class WalletTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self._wallets = {}  # (canvas, compte) -> dernier wallet renvoyé par Codawar

    def placed(self, canvas, cookie, wallet, t=None):
        """Pose réussie : Codawar renvoie le portefeuille à jour."""
        if wallet:
            with self._lock:
                self._wallets[(canvas, account_key(cookie))] = wallet

    def observe(self, canvas, account, wallet, t=None):
        """Portefeuille lu avec le canvas ; `account` = account_key(cookie utilisé pour la lecture)."""
        if wallet:
            with self._lock:
                self._wallets[(canvas, account)] = wallet

    def account(self, canvas, cookie, now=None):
        """{credits, nextIn, unlimited} pour un compte sur un canvas (None si inconnu)."""
        with self._lock:
            w = self._wallets.get((canvas, account_key(cookie)))
        if not w:
            return None
        now = time.time() if now is None else now
        bal, nxt = settle(w, now)
        return {"credits": bal, "nextIn": (nxt - now) if nxt else None,
                "unlimited": bool(w.get("isUnlimited"))}

    def credit_wait(self, canvas, cookie):
        """Attente jusqu'au prochain crédit de ce compte sur ce canvas."""
        a = self.account(canvas, cookie)
        if not a or a["nextIn"] is None:
            return CREDIT_WAIT_DEFAULT
        return max(2, min(REFILL_INTERVAL + 5, round(a["nextIn"]) + 2))

    def snapshot(self, canvas):
        """Champs d'état pour l'interface, agrégés sur les comptes de ce canvas."""
        now = time.time()
        with self._lock:
            wallets = [w for (cv, _), w in self._wallets.items() if cv == canvas]
        settled = [settle(w, now) for w in wallets]
        balances = [b for b, _ in settled if b is not None]
        nexts = [n - now for _, n in settled if n]
        unlimited = bool(wallets) and all(w.get("isUnlimited") for w in wallets)
        return {
            "credits": sum(balances) if balances else None,
            "maxCredits": MAX_CREDITS * len(wallets),
            "accounts": len(wallets),
            "unlimited": unlimited,
            # comptes qui se rechargent encore (pas au plafond) : 1 crédit / 5 min chacun
            "refillPerMin": round(len(nexts) * 60 / REFILL_INTERVAL, 2) if not unlimited else None,
            "nextCredit": max(0, round(min(nexts))) if nexts else None,
        }
