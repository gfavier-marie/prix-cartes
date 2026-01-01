#!/usr/bin/env python3
"""
Analyse les sets et met card_number_padded=true si la carte 1 est au format 001.

Usage:
    python scripts/detect_padded_sets.py          # Mode dry-run (affiche seulement)
    python scripts/detect_padded_sets.py --apply  # Applique les modifications
"""

import sys
import re
from pathlib import Path

# Ajouter le chemin racine pour les imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database import get_session
from src.models import Card, Set


def detect_padded_sets(apply: bool = False):
    """Detecte les sets avec padding et met a jour card_number_padded."""

    with get_session() as session:
        # Recuperer tous les sets
        sets = session.query(Set).order_by(Set.id).all()

        padded_sets = []
        not_padded_sets = []

        for s in sets:
            # Chercher la carte 1 de ce set (peut etre "1", "01", "001", etc.)
            card_1 = session.query(Card).filter(
                Card.set_id == s.id,
                Card.local_id.in_(["1", "01", "001", "0001"])
            ).first()

            if not card_1:
                # Pas de carte 1, chercher la plus petite carte numerique
                cards = session.query(Card).filter(
                    Card.set_id == s.id
                ).all()

                numeric_cards = []
                for c in cards:
                    # Extraire le numero si c'est numerique
                    match = re.match(r'^0*(\d+)$', c.local_id)
                    if match:
                        numeric_cards.append((int(match.group(1)), c))

                if numeric_cards:
                    numeric_cards.sort(key=lambda x: x[0])
                    card_1 = numeric_cards[0][1]

            if not card_1:
                continue

            # Verifier si le local_id a du padding (commence par 0 et est numerique)
            is_padded = (
                card_1.local_id.startswith("0") and
                card_1.local_id.isdigit() and
                len(card_1.local_id) > 1
            )

            if is_padded:
                padded_sets.append((s, card_1.local_id))
            else:
                not_padded_sets.append((s, card_1.local_id))

        # Afficher les resultats
        print(f"\n{'='*60}")
        print(f"SETS AVEC PADDING ({len(padded_sets)})")
        print(f"{'='*60}")
        for s, local_id in padded_sets:
            count = session.query(Card).filter(Card.set_id == s.id).count()
            already_padded = session.query(Card).filter(
                Card.set_id == s.id,
                Card.card_number_padded == True
            ).count()
            print(f"  {s.id:<20} carte 1 = '{local_id}' ({count} cartes, {already_padded} deja padded)")

        print(f"\n{'='*60}")
        print(f"SETS SANS PADDING ({len(not_padded_sets)})")
        print(f"{'='*60}")
        for s, local_id in not_padded_sets[:20]:  # Limiter l'affichage
            print(f"  {s.id:<20} carte 1 = '{local_id}'")
        if len(not_padded_sets) > 20:
            print(f"  ... et {len(not_padded_sets) - 20} autres")

        # Appliquer si demande
        if apply and padded_sets:
            print(f"\n{'='*60}")
            print("APPLICATION DES MODIFICATIONS")
            print(f"{'='*60}")

            total_updated = 0
            for s, _ in padded_sets:
                # Mettre card_number_padded=True pour toutes les cartes du set
                # Note: on utilise "!= True" avec or_ pour gerer les NULL
                from sqlalchemy import or_
                updated = session.query(Card).filter(
                    Card.set_id == s.id,
                    or_(Card.card_number_padded == None, Card.card_number_padded == False)
                ).update({Card.card_number_padded: True})

                if updated > 0:
                    print(f"  {s.id}: {updated} cartes mises a jour")
                    total_updated += updated

            session.commit()
            print(f"\nTotal: {total_updated} cartes mises a jour")
        elif padded_sets and not apply:
            print(f"\n[DRY-RUN] Pour appliquer les modifications, relancer avec --apply")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    detect_padded_sets(apply=apply)
