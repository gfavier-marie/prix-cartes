#!/usr/bin/env python3
"""
Corrige la numerotation des sous-series (TG, GG, H, SL, RC, SV, SH).

Ces cartes ont un local_id avec prefixe (ex: TG01) mais le card_number_full
utilise le total du set principal (172) au lieu du total de la sous-serie (TG30).

Actions:
- col1 (SL), dp7 (SH): card_number_format = LOCAL_ONLY (pas de total)
- Autres: card_number_full_override avec le format correct

Formats:
- TG, H, RC: sans padding (TG1/TG30, H1/H32, RC1/RC32)
- GG: padding 2 chiffres (GG01/GG70)
- SV: padding 3 chiffres (SV001/SV122)

Usage:
    python scripts/fix_subseries_numbering.py          # Dry-run
    python scripts/fix_subseries_numbering.py --apply  # Appliquer
"""

import sys
import re
from pathlib import Path

# Ajouter le chemin racine pour les imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database import get_session
from src.models import Card, CardNumberFormat


# Configuration des sous-series
SUBSERIES_CONFIG = {
    # set_id: {prefix: (max_num, padding_digits, use_local_only)}
    # padding_digits: 0 = pas de padding, 2 = 01, 3 = 001
    "col1": {"SL": (11, 0, True)},      # SL1 (LOCAL_ONLY)
    "dp7": {"SH": (3, 0, True)},        # SH1 (LOCAL_ONLY)
    "ecard2": {"H": (32, 0, False)},    # H1/H32
    "g1": {"RC": (32, 0, False)},       # RC1/RC32
    "swsh9": {"TG": (30, 0, False)},    # TG1/TG30
    "swsh10": {"TG": (30, 0, False)},   # TG1/TG30
    "swsh11": {"TG": (30, 0, False)},   # TG1/TG30
    "swsh12": {"TG": (30, 0, False)},   # TG1/TG30
    "swsh12.5": {"GG": (70, 2, False)}, # GG01/GG70
    "swsh4.5": {"SV": (122, 3, False)}, # SV001/SV122
}


def extract_prefix_and_number(local_id: str) -> tuple:
    """Extrait le prefixe et le numero d'un local_id.

    Ex: 'TG01' -> ('TG', 1)
        'SV122' -> ('SV', 122)
        'H32' -> ('H', 32)
    """
    match = re.match(r'^([A-Z]+)0*(\d+)$', local_id)
    if match:
        return match.group(1), int(match.group(2))
    return None, None


def format_number(prefix: str, num: int, padding: int) -> str:
    """Formate un numero avec le padding specifie.

    Ex: format_number('TG', 1, 0) -> 'TG1'
        format_number('GG', 1, 2) -> 'GG01'
        format_number('SV', 1, 3) -> 'SV001'
    """
    if padding == 0:
        return f"{prefix}{num}"
    else:
        return f"{prefix}{str(num).zfill(padding)}"


def fix_subseries_numbering(apply: bool = False):
    """Detecte et corrige la numerotation des sous-series."""

    with get_session() as session:
        total_to_update = 0
        total_local_only = 0
        changes = []

        for set_id, prefixes in SUBSERIES_CONFIG.items():
            for prefix, (max_num, padding, use_local_only) in prefixes.items():
                # Chercher les cartes avec ce prefixe dans ce set
                cards = session.query(Card).filter(
                    Card.set_id == set_id,
                    Card.local_id.like(f"{prefix}%")
                ).all()

                if not cards:
                    continue

                # Construire le total formate
                total_formatted = format_number(prefix, max_num, padding)

                set_changes = []
                for card in cards:
                    # Ignorer si override deja defini
                    if card.card_number_full_override and not use_local_only:
                        continue
                    if use_local_only and card.card_number_format == CardNumberFormat.LOCAL_ONLY:
                        continue

                    prefix_extracted, num = extract_prefix_and_number(card.local_id)
                    if prefix_extracted != prefix or num is None:
                        continue

                    # Formater le numero
                    num_formatted = format_number(prefix, num, padding)

                    if use_local_only:
                        # LOCAL_ONLY: pas de total
                        old_value = card.card_number_full or "N/A"
                        new_value = f"{num_formatted} (LOCAL_ONLY)"
                        set_changes.append((card, "LOCAL_ONLY", num_formatted, old_value))
                        total_local_only += 1
                    else:
                        # Construire card_number_full_override
                        new_full = f"{num_formatted}/{total_formatted}"
                        old_value = card.card_number_full or "N/A"
                        set_changes.append((card, "OVERRIDE", new_full, old_value))
                        total_to_update += 1

                if set_changes:
                    changes.append((set_id, prefix, total_formatted, set_changes))

        # Afficher les resultats
        print(f"\n{'='*70}")
        print("SOUS-SERIES DETECTEES")
        print(f"{'='*70}")

        for set_id, prefix, total_formatted, set_changes in changes:
            # Afficher le premier exemple
            example = set_changes[0]
            card, action, new_value, old_value = example

            if action == "LOCAL_ONLY":
                print(f"\n{set_id} ({prefix}): {len(set_changes)} cartes -> LOCAL_ONLY")
                print(f"  Exemple: {card.local_id} -> {new_value.split()[0]}")
            else:
                print(f"\n{set_id} ({prefix}): {len(set_changes)} cartes -> {total_formatted}")
                print(f"  Exemple: {old_value} -> {new_value}")

        print(f"\n{'='*70}")
        print(f"RESUME")
        print(f"{'='*70}")
        print(f"  Cartes avec card_number_full_override: {total_to_update}")
        print(f"  Cartes avec LOCAL_ONLY: {total_local_only}")
        print(f"  Total: {total_to_update + total_local_only}")

        # Appliquer si demande
        if apply and (total_to_update > 0 or total_local_only > 0):
            print(f"\n{'='*70}")
            print("APPLICATION DES MODIFICATIONS")
            print(f"{'='*70}")

            for set_id, prefix, total_formatted, set_changes in changes:
                count = 0
                for card, action, new_value, old_value in set_changes:
                    if action == "LOCAL_ONLY":
                        card.card_number_format = CardNumberFormat.LOCAL_ONLY
                    else:
                        card.card_number_full_override = new_value
                    count += 1

                print(f"  {set_id} ({prefix}): {count} cartes mises a jour")

            session.commit()
            print(f"\nTermine!")

        elif not apply and (total_to_update > 0 or total_local_only > 0):
            print(f"\n[DRY-RUN] Pour appliquer, relancer avec --apply")


if __name__ == "__main__":
    apply = "--apply" in sys.argv
    fix_subseries_numbering(apply=apply)
