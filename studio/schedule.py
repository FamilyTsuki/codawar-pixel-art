"""Minuteur de pose : plage horaire pendant laquelle poser est autorisé."""

import time


def _minutes(hhmm):
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return 0


def within_schedule(settings, now=None):
    """True si la pose est autorisée maintenant (minuteur désactivé = toujours)."""
    if not settings.get("scheduleOn"):
        return True
    t = time.localtime(now)
    cur = t.tm_hour * 60 + t.tm_min
    start = _minutes(settings.get("scheduleFrom", "00:00"))
    end = _minutes(settings.get("scheduleTo", "23:59"))
    if start <= end:
        return start <= cur <= end
    return cur >= start or cur <= end  # plage qui passe minuit
