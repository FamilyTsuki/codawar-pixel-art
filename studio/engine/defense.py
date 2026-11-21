"""Mode défense : déclenché quand un calque est attaqué (relecture rapide, plus de workers, pas de délai)."""

import time

from .. import config


class DefenseMode:
    def __init__(self):
        self.until = 0

    def trigger(self, duration=config.DEFENSE_DURATION):
        self.until = max(self.until, time.time() + duration)

    def active(self):
        return time.time() < self.until

    def remaining(self):
        return max(0, round(self.until - time.time()))
