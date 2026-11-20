#!/bin/sh
# Démarrage du conteneur : dépendances puis serveur.
set -e

# git requis seulement pour la mise à jour auto (AUTO_PULL=1)
if [ "$AUTO_PULL" = "1" ] && ! command -v git >/dev/null 2>&1; then
  apt-get update && apt-get install -y --no-install-recommends git ca-certificates
  rm -rf /var/lib/apt/lists/*
fi

pip install --no-cache-dir "psycopg[binary]==3.3.9"
exec python server.py
