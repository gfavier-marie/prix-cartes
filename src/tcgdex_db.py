"""
Base de données TCGdex complète avec colonnes dynamiques.
Stocke TOUTES les données de TCGdex sans perte.
"""

import sqlite3
import json
import time
from pathlib import Path
from typing import Any, Optional
import httpx
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn

console = Console()

DB_PATH = Path(__file__).parent.parent / "data" / "tcgdex_full.db"


def get_connection() -> sqlite3.Connection:
    """Retourne une connexion à la base TCGdex."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialise la base avec les tables de base."""
    conn = get_connection()
    cursor = conn.cursor()

    # Table des sets
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tcgdex_sets (
            id TEXT PRIMARY KEY,
            name TEXT,
            _raw_json TEXT,
            _imported_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Table des cartes
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tcgdex_cards (
            id TEXT PRIMARY KEY,
            set_id TEXT,
            local_id TEXT,
            name TEXT,
            _raw_json TEXT,
            _imported_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Index
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cards_set_id ON tcgdex_cards(set_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cards_name ON tcgdex_cards(name)")

    conn.commit()
    conn.close()


def flatten_dict(d: dict, parent_key: str = '', sep: str = '_') -> dict:
    """
    Aplatit un dictionnaire imbriqué.
    Ex: {'variants': {'holo': True}} -> {'variants_holo': True}
    """
    items = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k

        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep).items())
        elif isinstance(v, list):
            # Pour les listes, on crée des colonnes indexées
            for i, item in enumerate(v):
                if isinstance(item, dict):
                    items.extend(flatten_dict(item, f"{new_key}_{i}", sep).items())
                else:
                    items.append((f"{new_key}_{i}", item))
            # Aussi stocker le count
            items.append((f"{new_key}_count", len(v)))
        else:
            items.append((new_key, v))

    return dict(items)


def sanitize_column_name(name: str) -> str:
    """Nettoie un nom de colonne pour SQLite."""
    # Remplacer les caractères spéciaux
    name = name.replace("-", "_").replace(".", "_").replace(" ", "_")
    # Préfixer si commence par un chiffre
    if name and name[0].isdigit():
        name = f"col_{name}"
    return name.lower()


def ensure_columns(cursor: sqlite3.Cursor, table: str, data: dict):
    """S'assure que toutes les colonnes existent, les crée sinon."""
    # Récupérer les colonnes existantes
    cursor.execute(f"PRAGMA table_info({table})")
    existing_cols = {row[1].lower() for row in cursor.fetchall()}

    # Ajouter les colonnes manquantes
    for key, value in data.items():
        col_name = sanitize_column_name(key)
        if col_name not in existing_cols:
            # Déterminer le type
            if isinstance(value, bool):
                col_type = "INTEGER"  # SQLite n'a pas de BOOLEAN
            elif isinstance(value, int):
                col_type = "INTEGER"
            elif isinstance(value, float):
                col_type = "REAL"
            else:
                col_type = "TEXT"

            try:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                console.print(f"[dim]+ Colonne {table}.{col_name}[/dim]")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise


def insert_or_update(cursor: sqlite3.Cursor, table: str, data: dict, raw_json: str):
    """Insère ou met à jour une ligne."""
    # Aplatir les données
    flat_data = flatten_dict(data)
    # Toujours garder le JSON brut complet
    flat_data['_raw_json'] = raw_json

    # S'assurer que les colonnes existent
    ensure_columns(cursor, table, flat_data)

    # Préparer l'insertion
    columns = [sanitize_column_name(k) for k in flat_data.keys()]
    placeholders = ['?' for _ in columns]
    values = []

    for v in flat_data.values():
        if isinstance(v, bool):
            values.append(1 if v else 0)
        elif isinstance(v, (list, dict)):
            values.append(json.dumps(v, ensure_ascii=False))
        else:
            values.append(v)

    # Upsert
    sql = f"""
        INSERT OR REPLACE INTO {table} ({', '.join(columns)})
        VALUES ({', '.join(placeholders)})
    """
    cursor.execute(sql, values)


class TCGdexFullImporter:
    """Importe toutes les données TCGdex."""

    BASE_URL = "https://api.tcgdex.net/v2/fr"

    def __init__(self):
        self._last_request = 0.0
        self._min_interval = 0.1  # 10 req/s

    def _rate_limit(self):
        elapsed = time.time() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request = time.time()

    def _get(self, endpoint: str) -> Optional[dict]:
        """Requête GET avec rate limiting."""
        self._rate_limit()
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(f"{self.BASE_URL}/{endpoint}")
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    def import_all(self):
        """Importe tous les sets et toutes les cartes."""
        init_db()
        conn = get_connection()
        cursor = conn.cursor()

        # Récupérer la liste des sets
        console.print("[cyan]Récupération des sets...[/cyan]")
        sets_list = self._get("sets")

        if not sets_list:
            console.print("[red]Erreur: impossible de récupérer les sets[/red]")
            return

        console.print(f"[green]{len(sets_list)} sets trouvés[/green]")

        total_cards = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:

            task = progress.add_task("Import des sets", total=len(sets_list))

            for set_info in sets_list:
                set_id = set_info.get('id')
                if not set_id:
                    continue

                progress.update(task, description=f"Set {set_id}")

                # Récupérer le set complet
                set_data = self._get(f"sets/{set_id}")
                if not set_data:
                    progress.advance(task)
                    continue

                # Sauvegarder le set (sans les cartes pour éviter la duplication)
                set_for_db = {k: v for k, v in set_data.items() if k != 'cards'}
                insert_or_update(cursor, 'tcgdex_sets', set_for_db, json.dumps(set_data, ensure_ascii=False))

                # Importer chaque carte du set
                cards = set_data.get('cards', [])
                for card_info in cards:
                    card_id = card_info.get('id')
                    local_id = card_info.get('localId')

                    if not card_id:
                        continue

                    # Récupérer la carte complète
                    card_data = self._get(f"cards/{card_id}")
                    if card_data:
                        # Ajouter set_id pour la relation
                        card_data['set_id'] = set_id
                        insert_or_update(cursor, 'tcgdex_cards', card_data, json.dumps(card_data, ensure_ascii=False))
                        total_cards += 1

                # Commit après chaque set
                conn.commit()
                progress.advance(task)

        conn.close()
        console.print(f"[green]Import terminé: {len(sets_list)} sets, {total_cards} cartes[/green]")
        console.print(f"[cyan]Base de données: {DB_PATH}[/cyan]")


def get_card(card_id: str) -> Optional[dict]:
    """Récupère une carte par son ID."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tcgdex_cards WHERE id = ?", (card_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        return dict(row)
    return None


def get_cards_by_set(set_id: str) -> list[dict]:
    """Récupère toutes les cartes d'un set."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tcgdex_cards WHERE set_id = ?", (set_id,))
    rows = cursor.fetchall()
    conn.close()

    return [dict(row) for row in rows]


def search_cards(name: str) -> list[dict]:
    """Recherche des cartes par nom."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tcgdex_cards WHERE name LIKE ?", (f"%{name}%",))
    rows = cursor.fetchall()
    conn.close()

    return [dict(row) for row in rows]


if __name__ == "__main__":
    importer = TCGdexFullImporter()
    importer.import_all()
