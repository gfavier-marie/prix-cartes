#!/usr/bin/env python3
"""
Script execute par cron toutes les heures.
Verifie si le batch est active et si c'est l'heure configuree.

Logique de priorisation:
- Traite d'abord les cartes jamais traitees (pas de snapshot)
- Puis les cartes avec le snapshot le plus ancien

Logique de limite API:
- Verifie l'usage AVANT de commencer
- S'arrete des que la limite parametree est atteinte
- Ex: limite=4000, usage=3999 -> 1 seule requete possible
"""
import os
import sys
from datetime import datetime

# Ajouter le dossier parent au path pour les imports
# Compatible Docker (/app) et local
script_dir = os.path.dirname(os.path.abspath(__file__))
app_dir = os.path.dirname(script_dir)
sys.path.insert(0, app_dir)

from src.database import get_session
from src.models import Settings


def get_setting(session, key: str, default: str) -> str:
    """Recupere une valeur depuis la table settings."""
    return Settings.get_value(session, key, default)


def log(message: str):
    """Log avec timestamp."""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}")


def main():
    """Point d'entree principal."""
    with get_session() as session:
        # Verifier si batch active
        enabled = get_setting(session, 'batch_enabled', 'true')
        if enabled.lower() != 'true':
            log("Batch desactive, skip")
            return

        # Verifier l'heure et minute
        batch_hour = int(get_setting(session, 'batch_hour', '3'))
        batch_minute = int(get_setting(session, 'batch_minute', '0'))  # Defaut: minute 0
        now = datetime.now()

        if now.hour != batch_hour or now.minute != batch_minute:
            # Pas l'heure, exit silencieux (pas de log pour eviter spam)
            return

        log("=== BATCH AUTOMATIQUE ===")

        # Recuperer la limite configuree
        daily_limit = int(get_setting(session, 'daily_api_limit', '5000'))

        # Recuperer l'usage actuel (compteur interne)
        from src.ebay.usage_tracker import get_ebay_usage_summary
        usage = get_ebay_usage_summary(session)
        today_count = usage.get('today_count', 0)
        remaining = daily_limit - today_count

        log(f"Limite configuree: {daily_limit} appels/jour")
        log(f"Usage actuel: {today_count} appels")
        log(f"Restants: {remaining} appels")

        # Verifier si on peut faire au moins 1 requete
        if remaining <= 0:
            log(f"Limite API atteinte ({today_count}/{daily_limit}), skip")
            return

        # Lancer le batch avec priorisation des cartes anciennes
        from src.batch.runner import BatchRunner
        from src.models import BatchMode

        log("Lancement avec priorisation: cartes sans donnees puis anciennes d'abord")

        runner = BatchRunner()
        try:
            # prioritize_oldest=True -> cartes jamais traitees puis les plus anciennes
            stats, anomalies = runner.run(
                mode=BatchMode.FULL_EBAY,
                prioritize_oldest=True
            )

            log(f"Batch termine: {stats.succeeded} succes, {stats.failed} echecs, {stats.skipped} exclus")

            if stats.stopped_api_limit:
                log(f"Arret: limite API atteinte apres {stats.processed} cartes")
            if stats.skipped_sets:
                log(f"Sets ignores ({len(stats.skipped_sets)}): {', '.join(stats.skipped_sets)}")

            # Rapport anomalies
            if anomalies.high_dispersions:
                log(f"Anomalies: {len(anomalies.high_dispersions)} hautes dispersions")
            if anomalies.mismatches:
                log(f"Anomalies: {len(anomalies.mismatches)} fallbacks Cardmarket")

        except Exception as e:
            log(f"ERREUR: {e}")
            import traceback
            traceback.print_exc()
        finally:
            runner.close()

        log("=== FIN BATCH ===")


if __name__ == '__main__':
    main()
