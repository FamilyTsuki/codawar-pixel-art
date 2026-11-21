"""Assemblage des composants (racine de composition) et démarrage du serveur."""

import threading

from . import config
from .autoupdate import AutoUpdater
from .codawar import CanvasDirectory, CodawarClient
from .engine import Engine
from .security import LoginLimiter
from .storage import Store, open_backend
from .web.api import Api
from .web.server import serve


def _learn_nicknames(store, directory):
    """Pseudo Codawar de chaque compte (pour ne pas prendre ses pixels pour une attaque)."""
    for u in store.users_with_cookie():
        directory.list(u["cookie"], u.get("canvas", config.DEFAULT_CANVAS))


def main():
    store = Store(open_backend())
    client = CodawarClient()
    directory = CanvasDirectory(client)
    engine = Engine(store, client, directory)
    api = Api(store, engine, directory, LoginLimiter())

    threading.Thread(target=_learn_nicknames, args=(store, directory), daemon=True).start()
    engine.start()
    if config.AUTO_PULL:
        threading.Thread(target=AutoUpdater().loop, args=(engine.persist,), daemon=True).start()
        print(f"Mise à jour auto : branche {config.AUTO_PULL_BRANCH} toutes les {config.AUTO_PULL_INTERVAL}s", flush=True)

    httpd = serve(api)
    print(f"Codawar Pixel Studio : http://localhost:{config.PORT}  (stockage : {store.backend_name})", flush=True)
    engine.journal.log("Serveur démarré")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")
    finally:
        engine.persist()  # dernière sauvegarde propre
