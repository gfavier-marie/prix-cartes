"""
Calcul du score de confiance pour les prix.
"""

import math
from dataclasses import dataclass
from typing import Optional

from ..models import AnchorSource, MarketSnapshot
from ..config import get_config, EbayConfig


@dataclass
class ConfidenceFactors:
    """Facteurs contribuant au score de confiance."""
    sample_size_score: int = 0
    dispersion_score: int = 0
    cardmarket_available_score: int = 0
    source_score: int = 0
    stability_score: int = 0  # vs batch precedent
    total: int = 0
    details: dict = None

    def __post_init__(self):
        if self.details is None:
            self.details = {}


class ConfidenceScorer:
    """Calcule un score de confiance (0-100) pour les prix."""

    # Poids des differents facteurs (total = 100)
    WEIGHT_SAMPLE_SIZE = 30
    WEIGHT_DISPERSION = 25
    WEIGHT_CARDMARKET = 15
    WEIGHT_SOURCE = 20
    WEIGHT_STABILITY = 10

    def __init__(self, min_sample: Optional[int] = None):
        if min_sample is None:
            min_sample = get_config().ebay.min_sample_size
        self.min_sample = min_sample

    def calculate(
        self,
        sample_size: Optional[int],
        dispersion: Optional[float],
        has_cardmarket: bool,
        anchor_source: AnchorSource,
        previous_anchor: Optional[float] = None,
        current_anchor: Optional[float] = None,
    ) -> ConfidenceFactors:
        """
        Calcule le score de confiance.

        Args:
            sample_size: Nombre d'items dans l'echantillon
            dispersion: Ratio p80/p20
            has_cardmarket: Donnees Cardmarket disponibles
            anchor_source: Source de l'ancre finale
            previous_anchor: Ancre du batch precedent (optionnel)
            current_anchor: Ancre actuelle (optionnel)

        Returns:
            ConfidenceFactors avec score et details
        """
        factors = ConfidenceFactors()

        # 1. Sample size score (0-30)
        factors.sample_size_score = self._score_sample_size(sample_size)
        factors.details["sample_size"] = sample_size

        # 2. Dispersion score (0-25)
        factors.dispersion_score = self._score_dispersion(dispersion)
        factors.details["dispersion"] = dispersion

        # 3. Cardmarket available (0-15)
        factors.cardmarket_available_score = self.WEIGHT_CARDMARKET if has_cardmarket else 0
        factors.details["has_cardmarket"] = has_cardmarket

        # 4. Source score (0-20)
        factors.source_score = self._score_source(anchor_source)
        factors.details["anchor_source"] = anchor_source.value

        # 5. Stability score (0-10)
        factors.stability_score = self._score_stability(previous_anchor, current_anchor)
        factors.details["variation_pct"] = self._calculate_variation(previous_anchor, current_anchor)

        # Total
        factors.total = (
            factors.sample_size_score +
            factors.dispersion_score +
            factors.cardmarket_available_score +
            factors.source_score +
            factors.stability_score
        )

        return factors

    def _score_sample_size(self, sample_size: Optional[int]) -> int:
        """Score base sur la taille de l'echantillon."""
        if sample_size is None or sample_size == 0:
            return 0

        # 1 annonce = 50% du score (accepte mais moins fiable)
        if sample_size == 1:
            return int(self.WEIGHT_SAMPLE_SIZE * 0.5)

        # 2-4 annonces = 60%
        if sample_size < 5:
            return int(self.WEIGHT_SAMPLE_SIZE * 0.6)

        # 5-9 annonces = 70%
        if sample_size < 10:
            return int(self.WEIGHT_SAMPLE_SIZE * 0.7)

        # 10-19 annonces = 80%
        if sample_size < 20:
            return int(self.WEIGHT_SAMPLE_SIZE * 0.8)

        # 20-29 annonces = 90%
        if sample_size < 30:
            return int(self.WEIGHT_SAMPLE_SIZE * 0.9)

        # 30+ annonces = 100%
        return self.WEIGHT_SAMPLE_SIZE

    def _score_dispersion(self, dispersion: Optional[float]) -> int:
        """Score base sur la dispersion."""
        if dispersion is None:
            return self.WEIGHT_DISPERSION // 2  # Score moyen si pas de data

        if dispersion <= 1.5:
            return self.WEIGHT_DISPERSION  # Excellent

        if dispersion <= 2.0:
            return int(self.WEIGHT_DISPERSION * 0.9)

        if dispersion <= 3.0:
            return int(self.WEIGHT_DISPERSION * 0.7)

        if dispersion <= 4.0:
            return int(self.WEIGHT_DISPERSION * 0.5)

        # Dispersion > 4.0: mauvais
        return int(self.WEIGHT_DISPERSION * 0.2)

    def _score_source(self, source: AnchorSource) -> int:
        """Score base sur la source de l'ancre."""
        if source == AnchorSource.EBAY_ACTIVE:
            return self.WEIGHT_SOURCE

        if source == AnchorSource.CARDMARKET_FALLBACK:
            return int(self.WEIGHT_SOURCE * 0.6)

        if source == AnchorSource.LAST_KNOWN:
            return int(self.WEIGHT_SOURCE * 0.3)

        return 0

    def _score_stability(
        self,
        previous: Optional[float],
        current: Optional[float]
    ) -> int:
        """Score base sur la stabilite vs batch precedent."""
        if previous is None or current is None:
            return self.WEIGHT_STABILITY // 2  # Score moyen si pas de comparaison

        variation = self._calculate_variation(previous, current)
        if variation is None:
            return self.WEIGHT_STABILITY // 2

        if variation <= 10:
            return self.WEIGHT_STABILITY  # Tres stable

        if variation <= 20:
            return int(self.WEIGHT_STABILITY * 0.8)

        if variation <= 30:
            return int(self.WEIGHT_STABILITY * 0.6)

        if variation <= 50:
            return int(self.WEIGHT_STABILITY * 0.4)

        # Variation > 50%: instable
        return int(self.WEIGHT_STABILITY * 0.1)

    def _calculate_variation(
        self,
        previous: Optional[float],
        current: Optional[float]
    ) -> Optional[float]:
        """Calcule la variation en pourcentage."""
        if previous is None or current is None or previous <= 0:
            return None
        return abs((current - previous) / previous) * 100

    def score_snapshot(
        self,
        snapshot: MarketSnapshot,
        has_cardmarket: bool,
        previous_snapshot: Optional[MarketSnapshot] = None
    ) -> int:
        """
        Calcule et applique le score de confiance a un snapshot.

        Args:
            snapshot: Snapshot a scorer
            has_cardmarket: Donnees Cardmarket disponibles
            previous_snapshot: Snapshot precedent pour stabilite

        Returns:
            Score de confiance (0-100)
        """
        previous_anchor = previous_snapshot.anchor_price if previous_snapshot else None

        factors = self.calculate(
            sample_size=snapshot.sample_size,
            dispersion=snapshot.dispersion,
            has_cardmarket=has_cardmarket,
            anchor_source=snapshot.anchor_source or AnchorSource.EBAY_ACTIVE,
            previous_anchor=previous_anchor,
            current_anchor=snapshot.anchor_price,
        )

        snapshot.confidence_score = factors.total

        # Ajouter aux metadata
        meta = snapshot.get_raw_meta()
        meta["confidence_factors"] = factors.details
        snapshot.set_raw_meta(meta)

        return factors.total
