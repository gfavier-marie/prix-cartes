"""
Importeur de cartes depuis TCGdex vers la base de donnees.
"""

from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.progress import Progress, TaskID

from sqlalchemy.orm import Session

from datetime import date

from .client import TCGdexClient, TCGdexCard, TCGdexSet
from ..models import Card, Variant, Set
from ..database import get_session
from ..config import get_config


console = Console()


class TCGdexImporter:
    """Importe les cartes depuis TCGdex."""

    def __init__(self, session: Optional[Session] = None):
        self.client = TCGdexClient()
        self._session = session
        self._owns_session = session is None

    def _get_session(self) -> Session:
        """Retourne la session active."""
        if self._session is None:
            raise RuntimeError("Session non initialisee")
        return self._session

    def import_all_sets(self, progress: Optional[Progress] = None) -> dict:
        """Importe toutes les cartes de tous les sets."""
        stats = {"sets": 0, "cards_created": 0, "cards_updated": 0, "errors": 0}

        sets = self.client.get_sets()
        console.print(f"[cyan]Found {len(sets)} sets to import[/cyan]")

        task_id: Optional[TaskID] = None
        if progress:
            task_id = progress.add_task("Importing sets...", total=len(sets))

        with get_session() as session:
            self._session = session

            for tcgdex_set in sets:
                try:
                    set_stats = self.import_set(tcgdex_set.id)
                    stats["cards_created"] += set_stats["created"]
                    stats["cards_updated"] += set_stats["updated"]
                    stats["sets"] += 1
                    # Commit apres chaque set pour voir la progression
                    session.commit()
                except Exception as e:
                    console.print(f"[red]Error importing set {tcgdex_set.id}: {e}[/red]")
                    stats["errors"] += 1
                    session.rollback()

                if progress and task_id is not None:
                    progress.update(task_id, advance=1)

            self._session = None

        return stats

    def import_set(self, set_id: str) -> dict:
        """Importe toutes les cartes d'un set."""
        stats = {"created": 0, "updated": 0, "set_created": False}

        # D'abord recuperer les infos du set et le creer/mettre a jour
        tcgdex_set = self.client.get_set(set_id)
        if tcgdex_set:
            self._upsert_set(tcgdex_set)
            stats["set_created"] = True

        cards = self.client.get_cards_from_set(set_id)

        for tcgdex_card in cards:
            # Recuperer les details complets (avec pricing)
            full_card = self.client.get_card(set_id, tcgdex_card.local_id)
            if full_card is None:
                continue

            # Creer les variants
            variants_to_create = self._get_variants(full_card)

            for variant in variants_to_create:
                result = self._upsert_card(full_card, variant)
                if result == "created":
                    stats["created"] += 1
                elif result == "updated":
                    stats["updated"] += 1

        return stats

    def _upsert_set(self, tcgdex_set: TCGdexSet) -> None:
        """Cree ou met a jour un set."""
        session = self._get_session()

        existing = session.query(Set).filter(Set.id == tcgdex_set.id).first()

        # Parser la date de sortie
        release_date = None
        if tcgdex_set.release_date:
            try:
                release_date = date.fromisoformat(tcgdex_set.release_date)
            except ValueError:
                pass

        if existing:
            # Mise a jour
            existing.name = tcgdex_set.name
            if tcgdex_set.serie_id:
                existing.serie_id = tcgdex_set.serie_id
            if tcgdex_set.serie_name:
                existing.serie_name = tcgdex_set.serie_name
            if release_date:
                existing.release_date = release_date
            if tcgdex_set.card_count_total:
                existing.card_count = tcgdex_set.card_count_total
        else:
            # Creation
            new_set = Set(
                id=tcgdex_set.id,
                name=tcgdex_set.name,
                serie_id=tcgdex_set.serie_id or "unknown",
                serie_name=tcgdex_set.serie_name or "Unknown",
                release_date=release_date,
                card_count=tcgdex_set.card_count_total,
            )
            session.add(new_set)

    def import_single_card(self, set_id: str, local_id: str) -> Optional[Card]:
        """Importe une seule carte."""
        full_card = self.client.get_card(set_id, local_id)
        if full_card is None:
            return None

        with get_session() as session:
            self._session = session
            variants = self._get_variants(full_card)

            # On retourne la premiere variante
            for variant in variants:
                self._upsert_card(full_card, variant)

            # Recuperer la carte creee
            card = session.query(Card).filter(
                Card.tcgdex_id == f"{full_card.id}-{variants[0].value}"
            ).first()

            self._session = None
            return card

    def _get_variants(self, card: TCGdexCard) -> list[Variant]:
        """Determine les variants a creer pour une carte."""
        variants = []

        # Toujours creer NORMAL si present
        if card.has_normal:
            variants.append(Variant.NORMAL)

        # Reverse
        if card.has_reverse:
            variants.append(Variant.REVERSE)

        # Holo (different de normal)
        if card.has_holo and not card.has_normal:
            variants.append(Variant.HOLO)

        # First edition
        if card.has_first_edition:
            variants.append(Variant.FIRST_ED)

        # Si aucun variant, mettre NORMAL par defaut
        if not variants:
            variants.append(Variant.NORMAL)

        return variants

    def _upsert_card(self, tcgdex_card: TCGdexCard, variant: Variant) -> str:
        """Cree ou met a jour une carte."""
        session = self._get_session()

        # ID unique: tcgdex_id + variant
        tcgdex_id = f"{tcgdex_card.id}-{variant.value}"

        existing = session.query(Card).filter(Card.tcgdex_id == tcgdex_id).first()

        if existing:
            # Mise a jour
            self._update_card(existing, tcgdex_card, variant)
            return "updated"
        else:
            # Creation
            card = self._create_card(tcgdex_card, variant)
            session.add(card)
            return "created"

    def _create_card(self, tcgdex_card: TCGdexCard, variant: Variant) -> Card:
        """Cree une nouvelle carte."""
        card = Card(
            tcgdex_id=f"{tcgdex_card.id}-{variant.value}",
            set_id=tcgdex_card.set_id,
            local_id=tcgdex_card.local_id,
            name=tcgdex_card.name,
            set_name=tcgdex_card.set_name,
            set_code=tcgdex_card.set_code,
            variant=variant,
            rarity=tcgdex_card.rarity,
            is_active=True,
        )

        # Pricing
        if tcgdex_card.pricing:
            card.cm_trend = tcgdex_card.pricing.trend
            card.cm_avg1 = tcgdex_card.pricing.avg1
            card.cm_avg7 = tcgdex_card.pricing.avg7
            card.cm_avg30 = tcgdex_card.pricing.avg30

        return card

    def _update_card(self, card: Card, tcgdex_card: TCGdexCard, variant: Variant) -> None:
        """Met a jour une carte existante."""
        card.name = tcgdex_card.name
        card.set_name = tcgdex_card.set_name
        card.set_code = tcgdex_card.set_code
        card.rarity = tcgdex_card.rarity
        card.updated_at = datetime.utcnow()

        # Pricing (mise a jour seulement si disponible)
        if tcgdex_card.pricing:
            if tcgdex_card.pricing.trend is not None:
                card.cm_trend = tcgdex_card.pricing.trend
            if tcgdex_card.pricing.avg1 is not None:
                card.cm_avg1 = tcgdex_card.pricing.avg1
            if tcgdex_card.pricing.avg7 is not None:
                card.cm_avg7 = tcgdex_card.pricing.avg7
            if tcgdex_card.pricing.avg30 is not None:
                card.cm_avg30 = tcgdex_card.pricing.avg30

    def update_pricing_only(self, card_ids: Optional[list[int]] = None) -> dict:
        """Met a jour uniquement les prix Cardmarket."""
        stats = {"updated": 0, "errors": 0}

        with get_session() as session:
            query = session.query(Card).filter(Card.is_active == True)
            if card_ids:
                query = query.filter(Card.id.in_(card_ids))

            cards = query.all()

            for card in cards:
                try:
                    # Extraire set_id et local_id depuis tcgdex_id
                    parts = card.tcgdex_id.rsplit("-", 1)
                    if len(parts) != 2:
                        continue

                    base_id = parts[0]
                    tcgdex_card = self.client.get_card_by_id(base_id)

                    if tcgdex_card and tcgdex_card.pricing:
                        if tcgdex_card.pricing.trend is not None:
                            card.cm_trend = tcgdex_card.pricing.trend
                        if tcgdex_card.pricing.avg1 is not None:
                            card.cm_avg1 = tcgdex_card.pricing.avg1
                        if tcgdex_card.pricing.avg7 is not None:
                            card.cm_avg7 = tcgdex_card.pricing.avg7
                        if tcgdex_card.pricing.avg30 is not None:
                            card.cm_avg30 = tcgdex_card.pricing.avg30
                        card.updated_at = datetime.utcnow()
                        stats["updated"] += 1

                except Exception as e:
                    console.print(f"[red]Error updating {card.tcgdex_id}: {e}[/red]")
                    stats["errors"] += 1

        return stats
