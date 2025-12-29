"""
Export des prix de rachat en CSV pour Pokeventes.
"""

from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy.orm import Session

from ..models import Card, BuyPrice, BuyPriceStatus
from ..database import get_session
from ..config import get_config


class CSVExporter:
    """Exporte les prix de rachat en CSV."""

    def __init__(self):
        self.config = get_config()

    def export(
        self,
        output_path: Path,
        only_ok: bool = True,
        min_confidence: Optional[int] = None,
        include_disabled: bool = False,
    ) -> dict:
        """
        Exporte les prix en CSV.

        Args:
            output_path: Chemin du fichier CSV
            only_ok: Exclure les cartes LOW_CONF
            min_confidence: Score minimum de confiance
            include_disabled: Inclure les cartes DISABLED

        Returns:
            Stats d'export {exported, skipped, total}
        """
        stats = {"exported": 0, "skipped": 0, "total": 0}

        with get_session() as session:
            # Requete de base
            query = session.query(Card, BuyPrice).join(
                BuyPrice, Card.id == BuyPrice.card_id
            ).filter(Card.is_active == True)

            # Filtres
            if only_ok:
                query = query.filter(BuyPrice.status == BuyPriceStatus.OK)
            elif not include_disabled:
                query = query.filter(BuyPrice.status != BuyPriceStatus.DISABLED)

            if min_confidence is not None:
                query = query.filter(BuyPrice.confidence_score >= min_confidence)

            results = query.all()
            stats["total"] = len(results)

            # Construire les donnees
            rows = []
            for card, buy_price in results:
                row = self._build_row(card, buy_price)
                if row:
                    rows.append(row)
                    stats["exported"] += 1
                else:
                    stats["skipped"] += 1

            # Creer le DataFrame
            df = pd.DataFrame(rows)

            if not df.empty:
                # Ordre des colonnes
                columns = [
                    "tcgdex_id",
                    "name",
                    "set_name",
                    "set_code",
                    "local_id",
                    "variant",
                    "buy_neuf",
                    "buy_bon",
                    "buy_correct",
                    "anchor_price",
                    "anchor_source",
                    "confidence",
                    "status",
                    "updated_at",
                ]
                df = df.reindex(columns=[c for c in columns if c in df.columns])

                # Arrondir les prix
                for col in ["buy_neuf", "buy_bon", "buy_correct", "anchor_price"]:
                    if col in df.columns:
                        df[col] = df[col].round(2)

            # Sauvegarder
            output_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(output_path, index=False, encoding="utf-8")

        return stats

    def _build_row(self, card: Card, buy_price: BuyPrice) -> Optional[dict]:
        """Construit une ligne pour le CSV."""
        # Verifier que les prix sont valides
        if buy_price.buy_neuf is None or buy_price.buy_neuf <= 0:
            return None

        return {
            "tcgdex_id": card.tcgdex_id,
            "name": card.name,
            "set_name": card.set_name,
            "set_code": card.set_code or "",
            "local_id": card.local_id,
            "variant": card.variant.value if card.variant else "NORMAL",
            "buy_neuf": buy_price.buy_neuf,
            "buy_bon": buy_price.buy_bon,
            "buy_correct": buy_price.buy_correct,
            "anchor_price": buy_price.anchor_price,
            "anchor_source": buy_price.anchor_source.value if buy_price.anchor_source else "",
            "confidence": buy_price.confidence_score,
            "status": buy_price.status.value if buy_price.status else "OK",
            "updated_at": buy_price.updated_at.isoformat() if buy_price.updated_at else "",
        }

    def export_full(
        self,
        output_path: Path,
    ) -> dict:
        """
        Export complet avec toutes les infos (pour debug/analyse).

        Inclut les donnees Cardmarket, snapshots, etc.
        """
        stats = {"exported": 0, "total": 0}

        with get_session() as session:
            from ..models import MarketSnapshot

            # Toutes les cartes avec prix
            query = session.query(Card, BuyPrice, MarketSnapshot).outerjoin(
                BuyPrice, Card.id == BuyPrice.card_id
            ).outerjoin(
                MarketSnapshot,
                (Card.id == MarketSnapshot.card_id) &
                (MarketSnapshot.as_of_date == BuyPrice.as_of_date)
            ).filter(Card.is_active == True)

            results = query.all()
            stats["total"] = len(results)

            rows = []
            for card, buy_price, snapshot in results:
                row = {
                    "tcgdex_id": card.tcgdex_id,
                    "name": card.name,
                    "set_name": card.set_name,
                    "set_code": card.set_code or "",
                    "local_id": card.local_id,
                    "variant": card.variant.value if card.variant else "NORMAL",
                    "rarity": card.rarity or "",

                    # Cardmarket
                    "cm_trend": card.cm_trend,
                    "cm_avg7": card.cm_avg7,
                    "cm_avg30": card.cm_avg30,

                    # eBay query
                    "ebay_query": card.effective_ebay_query or "",
                    "has_override": bool(card.ebay_query_override),
                }

                if snapshot:
                    row.update({
                        "active_count": snapshot.active_count,
                        "sample_size": snapshot.sample_size,
                        "p20": snapshot.p20,
                        "p50": snapshot.p50,
                        "p80": snapshot.p80,
                        "dispersion": snapshot.dispersion,
                    })

                if buy_price:
                    row.update({
                        "buy_neuf": buy_price.buy_neuf,
                        "buy_bon": buy_price.buy_bon,
                        "buy_correct": buy_price.buy_correct,
                        "anchor_price": buy_price.anchor_price,
                        "anchor_source": buy_price.anchor_source.value if buy_price.anchor_source else "",
                        "confidence": buy_price.confidence_score,
                        "status": buy_price.status.value if buy_price.status else "",
                        "updated_at": buy_price.updated_at.isoformat() if buy_price.updated_at else "",
                    })

                rows.append(row)
                stats["exported"] += 1

            df = pd.DataFrame(rows)

            # Arrondir
            numeric_cols = ["cm_trend", "cm_avg7", "cm_avg30", "p20", "p50", "p80",
                           "dispersion", "buy_neuf", "buy_bon", "buy_correct", "anchor_price"]
            for col in numeric_cols:
                if col in df.columns:
                    df[col] = df[col].round(2)

            output_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(output_path, index=False, encoding="utf-8")

        return stats

    def export_anomalies(
        self,
        output_path: Path,
        dispersion_threshold: float = 4.0,
        variation_threshold: float = 0.6,
    ) -> dict:
        """
        Exporte les cartes avec anomalies pour review.

        Args:
            output_path: Chemin du fichier CSV
            dispersion_threshold: Seuil de dispersion
            variation_threshold: Seuil de variation (ex: 0.6 = 60%)

        Returns:
            Stats d'export
        """
        stats = {"exported": 0}

        with get_session() as session:
            from ..models import MarketSnapshot

            # Cartes avec high dispersion ou low confidence
            query = session.query(Card, BuyPrice, MarketSnapshot).join(
                BuyPrice, Card.id == BuyPrice.card_id
            ).join(
                MarketSnapshot,
                (Card.id == MarketSnapshot.card_id) &
                (MarketSnapshot.as_of_date == BuyPrice.as_of_date)
            ).filter(
                Card.is_active == True,
                (MarketSnapshot.dispersion > dispersion_threshold) |
                (BuyPrice.status == BuyPriceStatus.LOW_CONF) |
                (BuyPrice.confidence_score < 50)
            )

            results = query.all()

            rows = []
            for card, buy_price, snapshot in results:
                anomaly_reasons = []
                if snapshot.dispersion and snapshot.dispersion > dispersion_threshold:
                    anomaly_reasons.append(f"high_dispersion:{snapshot.dispersion:.2f}")
                if buy_price.status == BuyPriceStatus.LOW_CONF:
                    anomaly_reasons.append("low_conf_status")
                if buy_price.confidence_score and buy_price.confidence_score < 50:
                    anomaly_reasons.append(f"low_score:{buy_price.confidence_score}")

                rows.append({
                    "tcgdex_id": card.tcgdex_id,
                    "name": card.name,
                    "set_name": card.set_name,
                    "local_id": card.local_id,
                    "variant": card.variant.value if card.variant else "NORMAL",
                    "anomaly_reasons": "|".join(anomaly_reasons),
                    "ebay_query": card.effective_ebay_query or "",
                    "dispersion": snapshot.dispersion,
                    "confidence": buy_price.confidence_score,
                    "anchor_price": buy_price.anchor_price,
                    "anchor_source": buy_price.anchor_source.value if buy_price.anchor_source else "",
                    "cm_trend": card.cm_trend,
                    "cm_avg30": card.cm_avg30,
                })
                stats["exported"] += 1

            df = pd.DataFrame(rows)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(output_path, index=False, encoding="utf-8")

        return stats
