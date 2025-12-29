#!/bin/bash
# Script toggle Prix-Cartes (démarre ou arrête)

cd "$(dirname "$0")"

# Vérifier si le serveur tourne
if lsof -ti :5001 > /dev/null 2>&1; then
    # Serveur actif → l'arrêter
    lsof -ti :5001 | xargs kill -9 2>/dev/null
    osascript -e 'display notification "Serveur arrêté" with title "Prix-Cartes"'
else
    # Serveur inactif → le démarrer
    python3 -m flask --app admin.app run --port 5001 &
    sleep 2
    open http://127.0.0.1:5001
    osascript -e 'display notification "Serveur démarré sur le port 5001" with title "Prix-Cartes"'
fi
