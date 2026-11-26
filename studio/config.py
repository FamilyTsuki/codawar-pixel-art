"""Configuration : variables d'environnement et constantes.

Lu à l'import : le fichier .env doit être chargé avant (voir server.py).
"""

import os


def _env_int(name, default):
    return int(os.environ.get(name, str(default)))


def _env_flag(name, default="0"):
    return os.environ.get(name, default) == "1"


BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(BASE, "data", "state.json")
INDEX_FILE = "/index.html"

# ---------------------------------------------------------------- Serveur web
PORT = _env_int("PORT", 5055)
HOST = os.environ.get("HOST", "127.0.0.1")  # mettre 0.0.0.0 en conteneur (derrière Cosmos)
MAX_BODY = 20 * 1024 * 1024  # 20 Mo par requête

# ---------------------------------------------------------------- Sécurité
PEPPER = os.environ.get("AUTH_PEPPER", "").encode()  # secret serveur ajouté au mot de passe
COOKIE_SECURE = _env_flag("COOKIE_SECURE")              # 1 derrière HTTPS (Cosmos)
SESSION_TTL = _env_int("SESSION_TTL_DAYS", 30) * 86400
PBKDF2_ROUNDS = 200_000
MIN_PW = 8
MIN_USERNAME = 3
LOGIN_WINDOW = 900        # fenêtre de comptage des échecs de connexion (15 min)
LOGIN_MAX_FAILS = 10      # échecs tolérés dans la fenêtre
LOGIN_SLOWDOWN = 0.3      # pause avant chaque tentative (anti force brute)

# ---------------------------------------------------------------- Stockage
PG_DSN = os.environ.get("CODAWAR_PG", "dbname=codawar")
PG_RETRIES = _env_int("CODAWAR_PG_RETRIES", 15)  # la base peut démarrer après l'app (~30 s)
PG_RETRY_DELAY = 2

# ---------------------------------------------------------------- Site Codawar
TELEFUNC_HOST = "codawar.coda.school"
TELEFUNC_PATH = "/_telefunc?_telefunc=txt"
TELEFUNC_FILE = "/src/modules/canvas/infrastructure/browser-canvas-gateway.telefunc.ts"
SYNC_FUNCTION = "onSynchronizePackedCanvas"  # ex-onSynchronizeCanvas (supprimée par le site)
TELEFUNC_LOOTBOX = "/src/modules/lootbox/infrastructure/browser-lootbox-gateway.telefunc.ts"
INVITE_TTL = 7 * 24 * 3600   # durée de validité d'un lien d'invitation (s)
LOOT_INTERVAL = 60        # vérification des coffres disponibles (s)
TD_INTERVAL = 600         # vérification du Tower Defense pour collecter les bits (s)
TD_CACHE_TTL = 20         # cache de l'état Tower Defense (s)
LOOT_CACHE_TTL = 20       # cache de la page /coffres (s)
# Outils de destruction : rayon du losange touché (napalm : zone aléatoire décidée par le serveur)
DESTRUCTION_TOOLS = {"pickaxe": 0, "dynamite": 1, "bomb": 2, "napalm": None, "nuke": 5}

# Cadence des poses, PAR COMPTE (tous canvas confondus). Règle Codawar : 3 pixels/s au maximum,
# sinon avertissement puis exclusion 15 min. On espace les départs d'au moins 0,5 s
# (2/s, jamais plus de 2 poses sur une même seconde) : marge de sécurité sous la limite.
PLACE_MIN_INTERVAL = 0.5
# Codawar compte les poses à leur ARRIVÉE : deux requêtes parties à 0,5 s d'écart peuvent
# arriver groupées si la latence varie. Donc une seule requête en vol par compte, et un
# délai après chaque réponse : 4 arrivées s'étalent alors toujours sur plus d'une seconde.
PLACE_GAP_AFTER_RESPONSE = 0.4
TIMEOUT_PAUSE = 15 * 60 + 30  # exclusion STUDENT_TIMED_OUT : on ne renvoie rien avant la fin
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)"
CANVASES_CACHE_TTL = 60
DEFAULT_COOKIE = os.environ.get("CODAWAR_COOKIE", "")
DEFAULT_CANVAS = "playtest-sandbox"
ZONE_SIZE = 100

# ---------------------------------------------------------------- Moteur
WORKERS = 4  # requêtes de pose en parallèle (la cadence par compte est bornée par PLACE_MIN_INTERVAL)
LOG_SIZE = 300
PERSIST_INTERVAL = 10                                   # sauvegarde de l'état d'exécution
STATS_INTERVAL = _env_int("STATS_INTERVAL", 15)         # relecture pour construire les stats 24/24
SNAPSHOT_INTERVAL = _env_int("SNAPSHOT_INTERVAL", 300)  # instantané du canvas pour le timelapse
SNAPSHOT_KEEP = _env_int("SNAPSHOT_KEEP", 3000)         # nb max d'instantanés gardés par canvas
FAILED_RETRY_DELAY = 120   # pixel refusé définitivement : pas de nouvel essai avant 2 min
DEFENSE_DURATION = 60      # durée du mode défense après une attaque détectée
RATE_WINDOW = 30           # fenêtre du calcul de débit (s)
STATUS_PENDING_LIMIT = 4000

# Codes de rejet qui signifient "réessayer plus tard" (solde, limite, fermeture…)
RETRY_LATER_HINTS = ("wallet", "balance", "credit", "insufficient", "limit", "quota", "cooldown", "rate", "closed", "pause")
CREDIT_SHORT_HINTS = ("insufficient", "credit", "balance", "empty")

DEFAULT_SETTINGS = {
    "cookie": DEFAULT_COOKIE,
    "canvas": DEFAULT_CANVAS,
    "guardInterval": 8,       # relecture normale du canvas
    "defendInterval": 2,      # relecture rapide tant qu'un calque est attaqué
    "order": "rows",          # rows | random
    "scheduleOn": False,      # minuteur de pose : ne poser que dans une plage horaire
    "scheduleFrom": "00:00",
    "scheduleTo": "23:59",
    "vandalThreshold": 12,    # pixels écrasés d'un coup sur un calque actif = alerte
}

# ---------------------------------------------------------------- Mise à jour auto (git pull)
AUTO_PULL = _env_flag("AUTO_PULL")
AUTO_PULL_INTERVAL = _env_int("AUTO_PULL_INTERVAL", 300)
AUTO_PULL_BRANCH = os.environ.get("AUTO_PULL_BRANCH", "main")
