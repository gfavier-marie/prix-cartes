#!/bin/bash
# Arrêter le serveur Prix-Cartes

lsof -ti :5001 | xargs kill -9 2>/dev/null
echo "Serveur Prix-Cartes arrêté"
