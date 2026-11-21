#!/usr/bin/env python3
"""
Codawar Pixel Studio — point d'entrée du serveur local.

- Sert l'éditeur web (index.html).
- File d'attente de pixels + pool de workers (connexions HTTPS keep-alive).
- Calques persistants : un calque "actif" est posé puis surveillé en continu ;
  tout pixel écrasé est reposé automatiquement, même navigateur fermé.

Le code est dans le package studio/ (voir studio/__init__.py).
"""

import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from studio.dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(BASE, ".env"))  # avant tout import qui lit la configuration


def _ensure_psycopg():
    """Stockage PostgreSQL via psycopg (dans .venv) : si absent, relance avec le python du venv.

    Le lien du venv l'active même si le binaire est partagé ; le drapeau évite une boucle.
    Ni venv ni psycopg : repli sur le fichier JSON.
    """
    try:
        import psycopg  # noqa: F401
    except ModuleNotFoundError:
        venv_py = os.path.join(BASE, ".venv", "bin", "python")
        if os.path.exists(venv_py) and not os.environ.get("CODAWAR_REEXEC"):
            os.environ["CODAWAR_REEXEC"] = "1"
            os.execv(venv_py, [venv_py, *sys.argv])


if __name__ == "__main__":
    _ensure_psycopg()
    from studio.app import main

    main()
