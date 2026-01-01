#!/usr/bin/env python3
"""
Migration: Ajouter les colonnes graded à la table market_snapshots.

Usage:
    python scripts/migrate_add_graded_columns.py
"""

import sqlite3
import sys
from pathlib import Path

# Chemin vers la base de données
DB_PATH = Path(__file__).parent.parent / "data" / "pricing.db"


def migrate():
    """Ajoute les colonnes graded à la table market_snapshots."""
    if not DB_PATH.exists():
        print(f"Base de données non trouvée: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    # Colonnes à ajouter
    columns = [
        ("graded_sample_size", "INTEGER"),
        ("graded_p10", "REAL"),
        ("graded_p20", "REAL"),
        ("graded_p50", "REAL"),
        ("graded_p80", "REAL"),
        ("graded_p90", "REAL"),
        ("graded_dispersion", "REAL"),
        ("graded_cv", "REAL"),
        ("graded_consensus_score", "REAL"),
        ("graded_age_median_days", "REAL"),
        ("graded_pct_recent_7d", "REAL"),
    ]

    # Vérifier les colonnes existantes
    cursor.execute("PRAGMA table_info(market_snapshots)")
    existing_columns = {row[1] for row in cursor.fetchall()}

    added = 0
    skipped = 0

    for col_name, col_type in columns:
        if col_name in existing_columns:
            print(f"  Colonne '{col_name}' existe déjà, ignorée")
            skipped += 1
        else:
            try:
                cursor.execute(f"ALTER TABLE market_snapshots ADD COLUMN {col_name} {col_type}")
                print(f"  Colonne '{col_name}' ajoutée")
                added += 1
            except sqlite3.OperationalError as e:
                print(f"  Erreur pour '{col_name}': {e}")

    conn.commit()
    conn.close()

    print(f"\nMigration terminée: {added} colonnes ajoutées, {skipped} ignorées")


if __name__ == "__main__":
    print(f"Migration: Ajout des colonnes graded à market_snapshots")
    print(f"Base de données: {DB_PATH}")
    print()
    migrate()
