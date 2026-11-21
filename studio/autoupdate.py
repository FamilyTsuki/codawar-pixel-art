"""Mise à jour automatique : suit une branche git et redémarre le serveur sur le nouveau code."""

import os
import subprocess
import sys
import time

from . import config


class AutoUpdater:
    def __init__(self, repo=config.BASE, branch=config.AUTO_PULL_BRANCH, interval=config.AUTO_PULL_INTERVAL):
        self.repo = repo
        self.branch = branch
        self.interval = interval

    def _git(self, *args):
        return subprocess.run(["git", "-C", self.repo, "-c", f"safe.directory={self.repo}", *args],
                              capture_output=True, text=True, timeout=120)

    def loop(self, before_restart):
        """before_restart() est appelé juste avant de relancer le processus."""
        while True:
            time.sleep(self.interval)
            try:
                if self.update():
                    before_restart()
                    os.execv(sys.executable, [sys.executable, *sys.argv])  # recharge le nouveau code
            except Exception as e:
                print(f"auto-pull: {e}", flush=True)

    def update(self):
        """Avance en fast-forward sur origin/<branche>. True si le code a changé."""
        remote_ref = f"origin/{self.branch}"
        if self._git("fetch", "origin", self.branch).returncode != 0:
            return False
        local = self._git("rev-parse", "HEAD").stdout.strip()
        remote = self._git("rev-parse", remote_ref).stdout.strip()
        if not remote or local == remote:
            return False
        merge = self._git("merge", "--ff-only", remote_ref)
        if merge.returncode != 0:
            print(f"auto-pull: fusion impossible (changements locaux ?) : {merge.stderr.strip()}", flush=True)
            return False
        print(f"auto-pull: mis à jour {local[:7]} -> {remote[:7]}, redémarrage", flush=True)
        return True
