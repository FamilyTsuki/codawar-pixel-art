"""Chargement minimal d'un fichier .env (sans dépendance)."""

import os


def load_dotenv(filepath):
    """Charge les variables d'un fichier .env dans os.environ si non définies."""
    if not os.path.isfile(filepath):
        return
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                _apply_line(line.strip())
    except OSError:
        pass


def _apply_line(line):
    if not line or line.startswith("#") or "=" not in line:
        return
    key, val = line.split("=", 1)
    key = key.strip()
    if key and key not in os.environ:
        os.environ[key] = val.strip().strip("'\"")
