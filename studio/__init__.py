"""Codawar Pixel Studio — serveur (stdlib + psycopg optionnel).

Organisation :
- config      : constantes et variables d'environnement
- palette     : table des couleurs Codawar
- security    : mots de passe, cookie de session, limite de connexions
- codawar     : client HTTP du site Codawar (telefunc, liste des canvas)
- storage     : persistance (PostgreSQL ou fichier JSON)
- engine/     : moteur de pose (file, workers, surveillance, stats, timelapse)
- web/        : serveur HTTP et routes de l'API
- autoupdate  : mise à jour automatique par git
- app         : assemblage des composants et démarrage
"""
