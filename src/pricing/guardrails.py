"""
Garde-fous pour detecter les mismatch eBay vs Cardmarket.
Gestion du fallback vers Cardmarket.
"""

from dataclasses import dataclass
from typing import Optional

from ..models import Card, MarketSnapshot, AnchorSource
from ..config import get_config, GuardrailsConfig


@dataclass
class GuardrailResult:
    """Resultat de la verification des garde-fous."""
    is_mismatch: bool = False
    mismatch_reason: Optional[str] = None
    original_anchor: Optional[float] = None
    original_source: AnchorSource = AnchorSource.EBAY_ACTIVE
    final_anchor: Optional[float] = None
    final_source: AnchorSource = AnchorSource.EBAY_ACTIVE
    cardmarket_value: Optional[float] = None
    confidence_penalty: int = 0


class PriceGuardrails:
    """Verifie la coherence des prix et applique les fallbacks."""

    def __init__(self, config: Optional[GuardrailsConfig] = None):
        if config is None:
            config = get_config().guardrails
        self.config = config

    def check(
        self,
        card: Card,
        ebay_anchor: Optional[float],
        dispersion: Optional[float] = None
    ) -> GuardrailResult:
        """
        Verifie si l'ancre eBay est coherente avec Cardmarket.

        Mismatch si:
        - anchor > 2.5 * cardmarket
        - anchor < 0.4 * cardmarket
        - dispersion > 4.0

        Args:
            card: Carte avec donnees Cardmarket
            ebay_anchor: Prix ancre eBay (p20)
            dispersion: Ratio p80/p20

        Returns:
            GuardrailResult avec decision finale
        """
        result = GuardrailResult(
            original_anchor=ebay_anchor,
            original_source=AnchorSource.EBAY_ACTIVE,
        )

        # Valeur Cardmarket de reference
        cm_value = card.cm_max
        result.cardmarket_value = cm_value

        # Si pas d'ancre eBay, fallback direct
        if ebay_anchor is None or ebay_anchor <= 0:
            if cm_value and cm_value > 0:
                result.is_mismatch = True
                result.mismatch_reason = "No eBay anchor"
                result.final_anchor = cm_value
                result.final_source = AnchorSource.CARDMARKET_FALLBACK
                result.confidence_penalty = 20
            return result

        # Si pas de Cardmarket, on garde eBay sans verif
        if cm_value is None or cm_value <= 0:
            result.final_anchor = ebay_anchor
            result.final_source = AnchorSource.EBAY_ACTIVE
            return result

        # Check mismatch: anchor trop haute
        if ebay_anchor > self.config.mismatch_upper * cm_value:
            result.is_mismatch = True
            result.mismatch_reason = f"eBay ({ebay_anchor:.2f}) > {self.config.mismatch_upper}x Cardmarket ({cm_value:.2f})"
            result.final_anchor = cm_value
            result.final_source = AnchorSource.CARDMARKET_FALLBACK
            result.confidence_penalty = 15
            return result

        # Check mismatch: anchor trop basse
        if ebay_anchor < self.config.mismatch_lower * cm_value:
            result.is_mismatch = True
            result.mismatch_reason = f"eBay ({ebay_anchor:.2f}) < {self.config.mismatch_lower}x Cardmarket ({cm_value:.2f})"
            result.final_anchor = cm_value
            result.final_source = AnchorSource.CARDMARKET_FALLBACK
            result.confidence_penalty = 15
            return result

        # Check dispersion excessive
        if dispersion is not None and dispersion > self.config.dispersion_bad:
            result.is_mismatch = True
            result.mismatch_reason = f"Dispersion too high ({dispersion:.2f} > {self.config.dispersion_bad})"
            result.final_anchor = cm_value
            result.final_source = AnchorSource.CARDMARKET_FALLBACK
            result.confidence_penalty = 10
            return result

        # Tout OK, on garde eBay
        result.final_anchor = ebay_anchor
        result.final_source = AnchorSource.EBAY_ACTIVE
        return result

    def apply_to_snapshot(
        self,
        snapshot: MarketSnapshot,
        card: Card
    ) -> GuardrailResult:
        """
        Applique les garde-fous a un snapshot et le met a jour.

        Args:
            snapshot: Snapshot a verifier
            card: Carte avec donnees Cardmarket

        Returns:
            GuardrailResult
        """
        result = self.check(
            card=card,
            ebay_anchor=snapshot.anchor_price,
            dispersion=snapshot.dispersion,
        )

        # Mettre a jour le snapshot
        snapshot.anchor_price = result.final_anchor
        snapshot.anchor_source = result.final_source

        # Ajouter info mismatch aux metadata
        if result.is_mismatch:
            meta = snapshot.get_raw_meta()
            meta["mismatch"] = {
                "reason": result.mismatch_reason,
                "original_anchor": result.original_anchor,
                "cardmarket_value": result.cardmarket_value,
            }
            snapshot.set_raw_meta(meta)

        return result

    def should_use_cardmarket_only(self, card: Card) -> bool:
        """
        Determine si on doit utiliser uniquement Cardmarket (mode HYBRID).

        Utilise pour les cartes a faible valeur ou sans requete eBay.
        """
        # Pas de requete eBay definie
        if not card.effective_ebay_query:
            return True

        # Cardmarket indisponible
        if card.cm_max is None:
            return False

        return False

    def get_fallback_anchor(
        self,
        card: Card,
        last_known: Optional[float] = None
    ) -> tuple[Optional[float], AnchorSource]:
        """
        Retourne l'ancre de fallback pour une carte.

        Ordre de priorite:
        1. Cardmarket (trend ou avg30)
        2. Dernier prix connu
        3. None

        Args:
            card: Carte
            last_known: Dernier prix connu (du batch precedent)

        Returns:
            (anchor, source)
        """
        # Cardmarket
        cm_value = card.cm_max
        if cm_value and cm_value > 0:
            return cm_value, AnchorSource.CARDMARKET_FALLBACK

        # Dernier prix connu
        if last_known and last_known > 0:
            return last_known, AnchorSource.LAST_KNOWN

        return None, AnchorSource.CARDMARKET_FALLBACK
