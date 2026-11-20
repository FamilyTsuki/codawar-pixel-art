#!/bin/sh
# Démarrage du conteneur : installe psycopg puis lance le serveur.
set -e
pip install --no-cache-dir "psycopg[binary]==3.3.9"
exec python server.py
