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

            # Recherche eBay avec filtres
            search_result = self.client.search_all(
                query=query,
                max_items=self.config.sample_limit,
                category_ids=self.config.category_ids,
                item_location_country="FR",  # Vendeurs français
                buying_options=["FIXED_PRICE"],  # Pas d'enchères
                filter_titles=True,  # Exclure lots/graded
                is_first_edition=is_first_edition,
                is_reverse=is_reverse,
                card_number=card.local_id,
                card_number_full=card_number_full,
            )

            result.active_count = search_result.total
            result.warnings = search_result.warnings
            result.items = search_result.items  # Stocker les annonces

            if not search_result.items:
                result.success = False
                result.error = "No items found"
                return result

            # Normaliser et nettoyer les prix
            prices = self._normalize_prices(search_result.items)

            if len(prices) < self.config.min_sample_size:
                result.success = False
                result.error = f"No valid prices after normalization ({len(prices)} items)"
                result.stats = PriceStats(sample_size=len(prices), raw_count=len(search_result.items))
                return result

            # Calcul des stats (avec items pour stats temporelles)
            result.stats = self._calculate_stats(prices, len(search_result.items), search_result.items)

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
        Normalise les prix en EUR.

        - Prix effectif = prix + port
        - Conversion en EUR
        - Filtrage des valeurs invalides
        """
        prices = []

        for item in items:
            # Prix de base
            price = item.price
            currency = item.currency

            # Ajouter le port
            if item.shipping_cost is not None:
                # Si devise differente pour le port
                if item.shipping_currency and item.shipping_currency != currency:
                    shipping_eur = self._convert_to_eur(item.shipping_cost, item.shipping_currency)
                    price_eur = self._convert_to_eur(price, currency)
                    effective = price_eur + shipping_eur
                else:
                    effective = price + item.shipping_cost
                    effective = self._convert_to_eur(effective, currency)
            else:
                effective = self._convert_to_eur(price, currency)

            # Filtrer valeurs invalides
            if effective > 0:
                prices.append(effective)

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
                for item in items[:50]  # Limiter a 50 pour ne pas exploser la DB
            ]

        snapshot.set_raw_meta(meta)

        return snapshot
