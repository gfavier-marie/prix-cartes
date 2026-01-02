#!/usr/bin/env python3
"""
Migration: Ajouter la colonne results_json a la table batch_runs.

Cette colonne permet de stocker les resultats detailles du batch
pour export CSV (succes/echecs par carte avec raison).

Usage:
    python scripts/migrate_add_batch_results.py
"""

import sqlite3
import sys
from pathlib import Path

# Chemin vers la base de donnees
DB_PATH = Path(__file__).parent.parent / "data" / "pricing.db"


def migrate():
    """Ajoute la colonne results_json a la table batch_runs."""
    if not DB_PATH.exists():
        print(f"Base de donnees non trouvee: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    # Colonne a ajouter
    col_name = "results_json"
    col_type = "TEXT"

    # Verifier les colonnes existantes
    cursor.execute("PRAGMA table_info(batch_runs)")
    existing_columns = {row[1] for row in cursor.fetchall()}

    if col_name in existing_columns:
        print(f"  Colonne '{col_name}' existe deja, ignoree")
    else:
        try:
            cursor.execute(f"ALTER TABLE batch_runs ADD COLUMN {col_name} {col_type}")
            print(f"  Colonne '{col_name}' ajoutee")
        except sqlite3.OperationalError as e:
            print(f"  Erreur pour '{col_name}': {e}")

    conn.commit()
    conn.close()

    print(f"\nMigration terminee")


if __name__ == "__main__":
    print(f"Migration: Ajout de la colonne results_json a batch_runs")
    print(f"Base de donnees: {DB_PATH}")
    print()
    migrate()
