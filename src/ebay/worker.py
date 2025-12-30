"""
Worker eBay pour la collecte de prix et calcul des statistiques.
"""

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import numpy as np

from .client import EbayClient, EbaySearchResult, EbayItem
from ..models import Card, MarketSnapshot, AnchorSource, Variant
from ..config import get_config, EbayConfig


@dataclass
class PriceStats:
    """Statistiques de prix calculees."""
    sample_size: int = 0
    p20: Optional[float] = None
    p50: Optional[float] = None
    p80: Optional[float] = None
    dispersion: Optional[float] = None
    mean: Optional[float] = None
    std: Optional[float] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    raw_count: int = 0  # Avant nettoyage
    removed_count: int = 0  # Items supprimes

    # Nouveaux indicateurs enrichis
    p10: Optional[float] = None  # Borne basse robuste
    p90: Optional[float] = None  # Borne haute robuste
    iqr: Optional[float] = None  # Interquartile range (p75-p25)
    cv: Optional[float] = None   # Coefficient de variation (std/mean)

    # Indicateurs temporels
    age_median_days: Optional[float] = None   # Age median des annonces (jours)
    pct_recent_7d: Optional[float] = None     # % annonces < 7 jours
    pct_old_30d: Optional[float] = None       # % annonces > 30 jours

    # Indicateurs de qualite
    consensus_score: Optional[float] = None   # % annonces dans ±20% de p50


@dataclass
class CollectionResult:
    """Resultat de la collecte pour une carte."""
    card_id: int
    success: bool = False
    active_count: int = 0
    stats: Optional[PriceStats] = None
    anchor_price: Optional[float] = None
    anchor_source: AnchorSource = AnchorSource.EBAY_ACTIVE
    error: Optional[str] = None
    query_used: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    items: list[EbayItem] = field(default_factory=list)  # Annonces collectees
    # Stats pour les annonces reverse trouvees (si carte non-REVERSE)
    reverse_stats: Optional[PriceStats] = None
    reverse_items: list[EbayItem] = field(default_factory=list)


class EbayWorker:
    """Collecte les prix eBay et calcule les statistiques."""

    def __init__(
        self,
        config: Optional[EbayConfig] = None,
        on_api_call: Optional[callable] = None
    ):
        """
        Args:
            config: Configuration eBay
            on_api_call: Callback appele apres chaque appel API
        """
        if config is None:
            config = get_config().ebay
        self.config = config
        self.client = EbayClient(config, on_api_call=on_api_call)
        self._fx_rates: dict[str, float] = {"EUR": 1.0, "USD": 0.92, "GBP": 1.17}

    def set_fx_rates(self, rates: dict[str, float]) -> None:
        """Definit les taux de change."""
        self._fx_rates = rates

    # Keywords pour identifier les cartes reverse (meme que dans client.py)
    REVERSE_KEYWORDS = ["reverse"]

    def _is_reverse_item(self, item: EbayItem) -> bool:
        """Verifie si un item est une carte reverse basé sur le titre."""
        title_lower = item.title.lower()
        return any(kw in title_lower for kw in self.REVERSE_KEYWORDS)

    def collect_for_card(self, card: Card) -> CollectionResult:
        """
        Collecte les prix eBay pour une carte.

        Args:
            card: Carte a traiter

        Returns:
            CollectionResult avec stats et ancre
        """
        result = CollectionResult(card_id=card.id)

        # Requete a utiliser
        query = card.effective_ebay_query
        if not query:
            result.success = False
            result.error = "No eBay query defined"
            return result

        result.query_used = query

        try:
            # Determiner le type de variante
            is_first_edition = card.variant == Variant.FIRST_ED
            is_reverse = card.variant == Variant.REVERSE

            # Sets promo: pas de card_number_full (format X/Y n'existe pas sur les cartes physiques)
            from ..ebay.query_builder import EbayQueryBuilder
            is_promo_set = card.set_id in EbayQueryBuilder.PROMO_SETS
            card_number_full = None if is_promo_set else card.card_number_full

            # Pour tous les variants SAUF REVERSE: recherche sans filtre reverse pour capturer les deux
            # Pour REVERSE: filtrer uniquement les reverse
            search_is_reverse = True if is_reverse else None

            # Recherche eBay avec filtres
            search_result = self.client.search_all(
                query=query,
                max_items=self.config.sample_limit,
                category_ids=self.config.category_ids,
                item_location_country="FR",  # Vendeurs français
                buying_options=["FIXED_PRICE"],  # Pas d'enchères
                filter_titles=True,  # Exclure lots/graded
                is_first_edition=is_first_edition,
                is_reverse=search_is_reverse,
                card_number=card.local_id,
                card_number_full=card_number_full,
            )

            result.active_count = search_result.total
            result.warnings = search_result.warnings

            # Pour tous les variants SAUF REVERSE: separer les items normal et reverse
            if not is_reverse:
                normal_items = []
                reverse_items = []
                for item in search_result.items:
                    if self._is_reverse_item(item):
                        reverse_items.append(item)
                    else:
                        normal_items.append(item)

                result.items = normal_items
                result.reverse_items = reverse_items

                # Calculer les stats pour les reverse si suffisamment d'items
                if reverse_items:
                    reverse_prices = self._normalize_prices(reverse_items)
                    if len(reverse_prices) >= 1:  # Au moins 1 item pour les stats reverse
                        result.reverse_stats = self._calculate_stats(
                            reverse_prices, len(reverse_items), reverse_items
                        )
            else:
                result.items = search_result.items

            # Si pas d'items normaux mais des reverse: on utilise les reverse comme items principaux
            if not result.items and result.reverse_items:
                result.items = result.reverse_items
                result.reverse_items = []
                result.reverse_stats = None
                result.warnings.append("Uniquement des annonces reverse trouvees")

            if not result.items:
                result.success = False
                result.error = "No items found"
                return result

            # Normaliser et nettoyer les prix
            prices = self._normalize_prices(result.items)

            if len(prices) < self.config.min_sample_size:
                result.success = False
                result.error = f"No valid prices after normalization ({len(prices)} items)"
                result.stats = PriceStats(sample_size=len(prices), raw_count=len(result.items))
                return result

            # Calcul des stats (avec items pour stats temporelles)
            result.stats = self._calculate_stats(prices, len(result.items), result.items)

            # Ancre = p20
            if result.stats.p20 is not None:
                result.anchor_price = result.stats.p20
                result.anchor_source = AnchorSource.EBAY_ACTIVE

            result.success = True

        except Exception as e:
            result.success = False
            result.error = str(e)

        return result

    def _normalize_prices(self, items: list[EbayItem]) -> list[float]:
        """
        Normalise les prix en EUR (hors frais de port).

        - Prix de base uniquement (sans port)
        - Conversion en EUR
        - Filtrage des valeurs invalides
        """
        prices = []

        for item in items:
            # Prix de base uniquement (hors port)
            price = item.price
            currency = item.currency

            # Convertir en EUR
            price_eur = self._convert_to_eur(price, currency)

            # Filtrer valeurs invalides
            if price_eur > 0:
                prices.append(price_eur)

        return prices

    def _convert_to_eur(self, amount: float, currency: str) -> float:
        """Convertit un montant en EUR."""
        if currency == "EUR":
            return amount
        rate = self._fx_rates.get(currency)
        if rate:
            return amount * rate
        # Devise inconnue, on garde tel quel (warning devrait etre log)
        return amount

    def _calculate_stats(
        self,
        prices: list[float],
        raw_count: int,
        items: Optional[list[EbayItem]] = None
    ) -> PriceStats:
        """
        Calcule les statistiques sur les prix.

        - Trimming des outliers
        - Percentiles (p10, p20, p50, p80, p90)
        - Dispersion et IQR
        - CV (coefficient de variation)
        - Stats temporelles (age median, % recent)
        - Score de consensus
        """
        stats = PriceStats(raw_count=raw_count)

        if not prices:
            return stats

        # Tri pour le trimming
        sorted_prices = sorted(prices)
        n = len(sorted_prices)

        # Trimming: retirer top/bottom X%
        trim_bottom = int(n * self.config.trim_bottom_pct)
        trim_top = int(n * self.config.trim_top_pct)

        if trim_bottom + trim_top < n:
            trimmed = sorted_prices[trim_bottom:n - trim_top if trim_top > 0 else n]
        else:
            trimmed = sorted_prices

        stats.removed_count = raw_count - len(trimmed)
        stats.sample_size = len(trimmed)

        if not trimmed:
            return stats

        # Conversion en numpy pour les calculs
        arr = np.array(trimmed)

        # Percentiles classiques
        stats.p20 = float(np.percentile(arr, 20))
        stats.p50 = float(np.percentile(arr, 50))
        stats.p80 = float(np.percentile(arr, 80))

        # Nouveaux percentiles (bornes robustes)
        stats.p10 = float(np.percentile(arr, 10))
        stats.p90 = float(np.percentile(arr, 90))

        # IQR (interquartile range)
        p25 = float(np.percentile(arr, 25))
        p75 = float(np.percentile(arr, 75))
        stats.iqr = p75 - p25

        # Dispersion
        if stats.p20 and stats.p20 > 0:
            stats.dispersion = stats.p80 / stats.p20

        # Stats supplementaires
        stats.mean = float(np.mean(arr))
        stats.std = float(np.std(arr))
        stats.min_price = float(np.min(arr))
        stats.max_price = float(np.max(arr))

        # CV (coefficient de variation) = std / mean
        if stats.mean and stats.mean > 0:
            stats.cv = stats.std / stats.mean

        # Score de consensus: % d'annonces dans ±20% de p50
        if stats.p50:
            lower_bound = stats.p50 * 0.8
            upper_bound = stats.p50 * 1.2
            in_range = sum(1 for p in trimmed if lower_bound <= p <= upper_bound)
            stats.consensus_score = (in_range / len(trimmed)) * 100

        # Stats temporelles (age des annonces)
        if items:
            self._calculate_temporal_stats(stats, items)

        return stats

    def _calculate_temporal_stats(self, stats: PriceStats, items: list[EbayItem]) -> None:
        """
        Calcule les statistiques temporelles sur les annonces.

        - Age median des annonces (jours)
        - % annonces < 7 jours
        - % annonces > 30 jours
        """
        from datetime import datetime, timezone

        ages_days = []
        now = datetime.now(timezone.utc)

        for item in items:
            if item.listing_date:
                try:
                    # Parse ISO date (ex: "2024-12-20T10:30:00.000Z")
                    if isinstance(item.listing_date, str):
                        listing_dt = datetime.fromisoformat(
                            item.listing_date.replace('Z', '+00:00')
                        )
                    else:
                        listing_dt = item.listing_date

                    age_days = (now - listing_dt).days
                    if age_days >= 0:
                        ages_days.append(age_days)
                except (ValueError, TypeError):
                    continue

        if not ages_days:
            return

        # Age median
        ages_arr = np.array(ages_days)
        stats.age_median_days = float(np.median(ages_arr))

        # % annonces recentes (< 7 jours)
        recent_count = sum(1 for age in ages_days if age < 7)
        stats.pct_recent_7d = (recent_count / len(ages_days)) * 100

        # % annonces anciennes (> 30 jours)
        old_count = sum(1 for age in ages_days if age > 30)
        stats.pct_old_30d = (old_count / len(ages_days)) * 100

    def create_snapshot(
        self,
        card: Card,
        result: CollectionResult,
        as_of: Optional[date] = None,
        items: Optional[list[EbayItem]] = None
    ) -> MarketSnapshot:
        """
        Cree un snapshot a partir du resultat de collecte.

        Args:
            card: Carte concernee
            result: Resultat de la collecte
            as_of: Date du snapshot (defaut: aujourd'hui)
            items: Liste des items eBay collectes

        Returns:
            MarketSnapshot pret a etre sauvegarde
        """
        if as_of is None:
            as_of = date.today()

        snapshot = MarketSnapshot(
            card_id=card.id,
            as_of_date=as_of,
            active_count=result.active_count,
            anchor_source=result.anchor_source,
        )

        if result.stats:
            snapshot.sample_size = result.stats.sample_size
            snapshot.p20 = result.stats.p20
            snapshot.p50 = result.stats.p50
            snapshot.p80 = result.stats.p80
            snapshot.dispersion = result.stats.dispersion
            # Nouveaux indicateurs
            snapshot.p10 = result.stats.p10
            snapshot.p90 = result.stats.p90
            snapshot.iqr = result.stats.iqr
            snapshot.cv = result.stats.cv
            snapshot.age_median_days = result.stats.age_median_days
            snapshot.pct_recent_7d = result.stats.pct_recent_7d
            snapshot.pct_old_30d = result.stats.pct_old_30d
            snapshot.consensus_score = result.stats.consensus_score

        # Stats reverse (si carte NORMAL et annonces reverse trouvees)
        if result.reverse_stats:
            snapshot.reverse_sample_size = result.reverse_stats.sample_size
            snapshot.reverse_p10 = result.reverse_stats.p10
            snapshot.reverse_p20 = result.reverse_stats.p20
            snapshot.reverse_p50 = result.reverse_stats.p50
            snapshot.reverse_p80 = result.reverse_stats.p80
            snapshot.reverse_p90 = result.reverse_stats.p90
            snapshot.reverse_dispersion = result.reverse_stats.dispersion
            snapshot.reverse_cv = result.reverse_stats.cv
            snapshot.reverse_consensus_score = result.reverse_stats.consensus_score
            snapshot.reverse_age_median_days = result.reverse_stats.age_median_days
            snapshot.reverse_pct_recent_7d = result.reverse_stats.pct_recent_7d

        if result.anchor_price:
            snapshot.anchor_price = result.anchor_price

        # Metadata
        meta = {
            "query": result.query_used,
            "success": result.success,
            "error": result.error,
            "warnings": result.warnings,
            "fx_rates": self._fx_rates,
        }
        if result.stats:
            meta["raw_count"] = result.stats.raw_count
            meta["removed_count"] = result.stats.removed_count
            meta["mean"] = result.stats.mean
            meta["std"] = result.stats.std
            # Nouveaux indicateurs enrichis
            meta["p10"] = result.stats.p10
            meta["p90"] = result.stats.p90
            meta["iqr"] = result.stats.iqr
            meta["cv"] = result.stats.cv
            meta["age_median_days"] = result.stats.age_median_days
            meta["pct_recent_7d"] = result.stats.pct_recent_7d
            meta["pct_old_30d"] = result.stats.pct_old_30d
            meta["consensus_score"] = result.stats.consensus_score

        # Stocker les annonces individuelles
        if items:
            meta["listings"] = [
                {
                    "item_id": item.item_id,
                    "title": item.title,
                    "price": item.price,
                    "currency": item.currency,
                    "shipping": item.shipping_cost,
                    "effective_price": item.effective_price,
                    "url": item.item_web_url,
                    "condition": item.condition,
                    "seller": item.seller_username,
                    "image": item.image_url,
                    "listing_date": item.listing_date,
                }
                for item in items[:100]  # Limiter a 100 pour tracking ventes
            ]

        # Stocker les annonces reverse separement
        if result.reverse_items:
            meta["reverse_listings"] = [
                {
                    "item_id": item.item_id,
                    "title": item.title,
                    "price": item.price,
                    "currency": item.currency,
                    "shipping": item.shipping_cost,
                    "effective_price": item.effective_price,
                    "url": item.item_web_url,
                    "condition": item.condition,
                    "seller": item.seller_username,
                    "image": item.image_url,
                    "listing_date": item.listing_date,
                }
                for item in result.reverse_items[:100]
            ]

        snapshot.set_raw_meta(meta)

        return snapshot

    def detect_sold_listings(
        self,
        session,
        card: Card,
        new_snapshot: MarketSnapshot,
        previous_snapshot: Optional[MarketSnapshot],
        is_reverse: bool = False,
        verify_via_api: bool = True
    ) -> list["SoldListing"]:
        """
        Detecte les annonces disparues entre deux snapshots.

        Verifie via l'API eBay si l'annonce a reellement ete vendue
        (et non simplement terminee manuellement).

        Args:
            session: Session SQLAlchemy
            card: La carte concernee
            new_snapshot: Le nouveau snapshot
            previous_snapshot: Le snapshot precedent (peut etre None)
            is_reverse: True si on compare les listings reverse
            verify_via_api: Si True, verifie le statut via l'API eBay

        Returns:
            Liste des SoldListing creees
        """
        from ..models import SoldListing

        if not previous_snapshot:
            return []

        # Extraire les listings des deux snapshots
        key = "reverse_listings" if is_reverse else "listings"
        old_meta = previous_snapshot.get_raw_meta()
        new_meta = new_snapshot.get_raw_meta()

        old_listings = old_meta.get(key, [])
        new_listings = new_meta.get(key, [])

        if not old_listings:
            return []

        # Creer un set des item_id actuels
        current_ids = {item.get("item_id") for item in new_listings if item.get("item_id")}

        # Trouver les disparus
        sold = []
        for listing in old_listings:
            item_id = listing.get("item_id")
            if not item_id or item_id in current_ids:
                continue

            # Verifier si deja enregistre
            existing = session.query(SoldListing).filter(
                SoldListing.item_id == item_id
            ).first()

            if existing:
                continue

            # Verifier via API si reellement vendue
            if verify_via_api:
                status_info = self.client.get_item_status(item_id)
                status = status_info.get("status")

                # Ne creer que si vraiment vendue (OUT_OF_STOCK + soldQuantity > 0)
                if status != "SOLD":
                    # Annonce terminee manuellement, supprimee ou erreur - on ignore
                    continue

            # Creer l'enregistrement
            sold_listing = SoldListing(
                card_id=card.id,
                item_id=item_id,
                title=listing.get("title"),
                price=listing.get("price"),
                effective_price=listing.get("effective_price"),
                currency=listing.get("currency", "EUR"),
                url=listing.get("url"),
                seller=listing.get("seller"),
                image_url=listing.get("image"),
                condition=listing.get("condition"),
                listing_date=listing.get("listing_date"),
                first_seen_at=previous_snapshot.created_at,
                last_seen_at=previous_snapshot.created_at,
                is_reverse=is_reverse,
            )
            session.add(sold_listing)
            sold.append(sold_listing)

        return sold
