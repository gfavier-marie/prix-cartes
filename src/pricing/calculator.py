"""
Calculateur de prix de rachat.
Applique la formule: buy = anchor * (1 - fees - margin - risk) - fixed_costs
"""

import math
from dataclasses import dataclass
from typing import Optional

from ..models import Card, MarketSnapshot, BuyPrice, AnchorSource, BuyPriceStatus
from ..config import get_config, PricingConfig


@dataclass
class RiskFactors:
    """Facteurs de risque calcules."""
    base: float = 0.0
    dispersion_penalty: float = 0.0
    supply_penalty: float = 0.0
    low_sample_penalty: float = 0.0
    fallback_penalty: float = 0.0
    consensus_adjustment: float = 0.0  # Ajustement basé sur le consensus
    age_adjustment: float = 0.0         # Ajustement basé sur l'âge des annonces
    total: float = 0.0


@dataclass
class PriceCalculation:
    """Detail du calcul de prix."""
    anchor_price: float
    fees_rate: float
    margin_target: float
    risk_total: float
    fixed_costs: float
    buy_base: float
    buy_neuf: float
    buy_bon: float
    buy_correct: float
    risk_factors: RiskFactors


class PriceCalculator:
    """Calcule les prix de rachat."""

    def __init__(self, config: Optional[PricingConfig] = None):
        if config is None:
            config = get_config().pricing
        self.config = config

    def calculate_risk(
        self,
        dispersion: Optional[float],
        active_count: Optional[int],
        sample_size: Optional[int],
        anchor_source: AnchorSource,
        age_median_days: Optional[float] = None,
        consensus_score: Optional[float] = None,
    ) -> RiskFactors:
        """
        Calcule le buffer de risque.

        risk = base
             + k1 * clamp(log(dispersion), 0, 2)
             + k2 * clamp(log(1 + active_count/1000), 0, 2)
             + k3 si sample_size < 10
             + k4 si fallback
             + ajustement consensus (peut etre negatif si bon consensus)
             + ajustement age (penalite si annonces vieilles)

        Args:
            dispersion: Ratio p80/p20
            active_count: Nombre total d'annonces eBay
            sample_size: Taille de l'echantillon
            anchor_source: Source de l'ancre
            age_median_days: Age median des annonces en jours
            consensus_score: % d'annonces dans ±20% de p50

        Returns:
            RiskFactors avec detail
        """
        risk = RiskFactors(base=self.config.risk_base)

        # Penalite dispersion
        if dispersion is not None and dispersion > 1:
            log_disp = math.log(dispersion)
            clamped = min(max(log_disp, 0), 2)
            risk.dispersion_penalty = self.config.risk_k1_dispersion * clamped

        # Penalite supply elevee
        if active_count is not None and active_count > 0:
            log_supply = math.log(1 + active_count / 1000)
            clamped = min(max(log_supply, 0), 2)
            risk.supply_penalty = self.config.risk_k2_supply * clamped

        # Penalite echantillon faible
        min_sample = get_config().ebay.min_sample_size
        if sample_size is not None and sample_size < min_sample:
            risk.low_sample_penalty = self.config.risk_k3_low_sample

        # Penalite fallback
        if anchor_source == AnchorSource.CARDMARKET_FALLBACK:
            risk.fallback_penalty = self.config.risk_k4_fallback
        elif anchor_source == AnchorSource.LAST_KNOWN:
            risk.fallback_penalty = self.config.risk_k4_fallback * 1.5

        # Ajustement consensus (peut etre negatif = bonus)
        # Consensus > 80% → bonus (-2%)
        # Consensus < 40% → penalite (+5%)
        if consensus_score is not None:
            if consensus_score >= 80:
                risk.consensus_adjustment = -0.02  # Bonus: marche stable
            elif consensus_score >= 60:
                risk.consensus_adjustment = 0.0    # Neutre
            elif consensus_score >= 40:
                risk.consensus_adjustment = 0.03   # Leger risque
            else:
                risk.consensus_adjustment = 0.05   # Marche volatile

        # Ajustement age des annonces
        # Age median > 30 jours → les prix affichés sont peut-etre trop hauts
        if age_median_days is not None:
            if age_median_days > 60:
                risk.age_adjustment = 0.05   # Annonces tres vieilles
            elif age_median_days > 30:
                risk.age_adjustment = 0.03   # Annonces vieilles
            elif age_median_days > 14:
                risk.age_adjustment = 0.01   # Leger ajustement
            else:
                risk.age_adjustment = 0.0    # Marche actif

        # Total
        risk.total = (
            risk.base +
            risk.dispersion_penalty +
            risk.supply_penalty +
            risk.low_sample_penalty +
            risk.fallback_penalty +
            risk.consensus_adjustment +
            risk.age_adjustment
        )

        return risk

    def calculate(
        self,
        anchor_price: float,
        dispersion: Optional[float] = None,
        active_count: Optional[int] = None,
        sample_size: Optional[int] = None,
        anchor_source: AnchorSource = AnchorSource.EBAY_ACTIVE,
        age_median_days: Optional[float] = None,
        consensus_score: Optional[float] = None,
    ) -> PriceCalculation:
        """
        Calcule les prix de rachat.

        Formule:
        buy_base = anchor * (1 - fees - margin - risk) - fixed_costs
        buy_neuf = buy_base * coef_neuf
        buy_bon = buy_base * coef_bon
        buy_correct = buy_base * coef_correct

        Args:
            anchor_price: Prix ancre (p20 eBay ou Cardmarket)
            dispersion: Ratio p80/p20
            active_count: Nombre d'annonces eBay
            sample_size: Taille echantillon
            anchor_source: Source de l'ancre
            age_median_days: Age median des annonces en jours
            consensus_score: % d'annonces dans ±20% de p50

        Returns:
            PriceCalculation avec tous les details
        """
        # Calcul du risque
        risk_factors = self.calculate_risk(
            dispersion=dispersion,
            active_count=active_count,
            sample_size=sample_size,
            anchor_source=anchor_source,
            age_median_days=age_median_days,
            consensus_score=consensus_score,
        )

        # Formule de base
        multiplier = 1 - self.config.fees_rate - self.config.margin_target - risk_factors.total
        buy_base = anchor_price * multiplier - self.config.fixed_costs_eur

        # Clamp et arrondi
        buy_base = self._clamp_and_round(buy_base)

        # Declinaisons par etat
        buy_neuf = self._clamp_and_round(buy_base * self.config.coef_neuf)
        buy_bon = self._clamp_and_round(buy_base * self.config.coef_bon)
        buy_correct = self._clamp_and_round(buy_base * self.config.coef_correct)

        return PriceCalculation(
            anchor_price=anchor_price,
            fees_rate=self.config.fees_rate,
            margin_target=self.config.margin_target,
            risk_total=risk_factors.total,
            fixed_costs=self.config.fixed_costs_eur,
            buy_base=buy_base,
            buy_neuf=buy_neuf,
            buy_bon=buy_bon,
            buy_correct=buy_correct,
            risk_factors=risk_factors,
        )

    def _clamp_and_round(self, value: float) -> float:
        """Applique clamp et arrondi."""
        # Clamp
        value = max(value, self.config.min_buy_price)
        value = min(value, self.config.max_buy_price)

        # Arrondi au step
        step = self.config.rounding_step
        if step > 0:
            value = round(value / step) * step

        return round(value, 2)

    def calculate_from_snapshot(
        self,
        snapshot: MarketSnapshot,
    ) -> Optional[PriceCalculation]:
        """
        Calcule les prix a partir d'un snapshot.

        Args:
            snapshot: Snapshot avec les donnees de marche

        Returns:
            PriceCalculation ou None si pas d'ancre
        """
        if snapshot.anchor_price is None or snapshot.anchor_price <= 0:
            return None

        return self.calculate(
            anchor_price=snapshot.anchor_price,
            dispersion=snapshot.dispersion,
            active_count=snapshot.active_count,
            sample_size=snapshot.sample_size,
            anchor_source=snapshot.anchor_source or AnchorSource.EBAY_ACTIVE,
            age_median_days=snapshot.age_median_days,
            consensus_score=snapshot.consensus_score,
        )

    def create_buy_price(
        self,
        card: Card,
        snapshot: MarketSnapshot,
        calculation: PriceCalculation,
    ) -> BuyPrice:
        """
        Cree un objet BuyPrice a partir du calcul.

        Args:
            card: Carte concernee
            snapshot: Snapshot source
            calculation: Resultat du calcul

        Returns:
            BuyPrice pret a etre sauvegarde
        """
        # Determiner le statut
        status = BuyPriceStatus.OK
        if snapshot.confidence_score is not None:
            if snapshot.confidence_score < 40:
                status = BuyPriceStatus.LOW_CONF
            elif snapshot.confidence_score < 60:
                status = BuyPriceStatus.LOW_CONF

        # Verifier si le prix est trop bas
        if calculation.buy_neuf <= self.config.min_buy_price:
            status = BuyPriceStatus.DISABLED

        return BuyPrice(
            card_id=card.id,
            buy_neuf=calculation.buy_neuf,
            buy_bon=calculation.buy_bon,
            buy_correct=calculation.buy_correct,
            anchor_price=snapshot.anchor_price,
            anchor_source=snapshot.anchor_source,
            confidence_score=snapshot.confidence_score,
            as_of_date=snapshot.as_of_date,
            status=status,
        )

    def should_exclude_card(self, card: Card) -> bool:
        """
        Determine si une carte doit etre exclue (valeur trop faible).

        Regle: exclure si max(trend, avg30) < MIN_CARD_VALUE_EUR
        """
        min_value = self.config.min_card_value_eur

        cm_max = card.cm_max
        if cm_max is not None:
            return cm_max < min_value

        # Pas de donnees Cardmarket: inclure par defaut (sera tag low conf)
        return False
