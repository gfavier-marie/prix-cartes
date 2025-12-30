"""
Export des prix de rachat en CSV pour Pokeventes.
"""

from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy.orm import Session

from ..models import Card, BuyPrice, BuyPriceStatus, SoldListing
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

        Inclut les donnees Cardmarket, snapshots, ventes, etc.
        """
        from collections import defaultdict
        stats = {"exported": 0, "total": 0}

        with get_session() as session:
            from ..models import MarketSnapshot

            # Precharger les stats de ventes par card_id
            sales_by_card = defaultdict(lambda: {
                "count": 0,
                "total": 0.0,
                "prices": [],
                "last_date": None
            })

            for sold in session.query(SoldListing).all():
                s = sales_by_card[sold.card_id]
                s["count"] += 1
                price = sold.effective_price or 0
                s["total"] += price
                s["prices"].append(price)
                if s["last_date"] is None or (sold.detected_sold_at and sold.detected_sold_at > s["last_date"]):
                    s["last_date"] = sold.detected_sold_at

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

                # Stats de ventes
                s = sales_by_card.get(card.id, {"count": 0, "total": 0, "prices": [], "last_date": None})
                prices = s["prices"]
                row.update({
                    "sales_count": s["count"],
                    "sales_total": round(s["total"], 2) if s["total"] else "",
                    "sales_avg": round(sum(prices) / len(prices), 2) if prices else "",
                    "sales_min": round(min(prices), 2) if prices else "",
                    "sales_max": round(max(prices), 2) if prices else "",
                    "last_sale_date": s["last_date"].strftime("%Y-%m-%d") if s["last_date"] else "",
                })

                rows.append(row)
                stats["exported"] += 1

            df = pd.DataFrame(rows)

            # Arrondir (uniquement les colonnes numeriques)
            numeric_cols = ["cm_trend", "cm_avg7", "cm_avg30", "p20", "p50", "p80",
                           "dispersion", "buy_neuf", "buy_bon", "buy_correct", "anchor_price"]
            for col in numeric_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce').round(2)

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

    def export_sales(
        self,
        output_path: Path,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> dict:
        """
        Exporte les ventes detectees en CSV (une ligne par vente).

        Args:
            output_path: Chemin du fichier CSV
            date_from: Date de debut (optionnel)
            date_to: Date de fin (optionnel)

        Returns:
            Stats d'export {exported, total_value}
        """
        stats = {"exported": 0, "total_value": 0.0}

        with get_session() as session:
            query = session.query(SoldListing, Card).join(
                Card, SoldListing.card_id == Card.id
            ).order_by(SoldListing.detected_sold_at.desc())

            if date_from:
                query = query.filter(SoldListing.detected_sold_at >= date_from)
            if date_to:
                query = query.filter(SoldListing.detected_sold_at <= date_to)

            results = query.all()

            rows = []
            for sold, card in results:
                shipping = (sold.effective_price or 0) - (sold.price or 0) if sold.price else 0
                row = {
                    # Carte
                    "tcgdex_id": card.tcgdex_id,
                    "card_name": card.name,
                    "set_name": card.set_name,
                    "set_code": card.set_code or "",
                    "local_id": card.local_id,
                    "variant": card.variant.value if card.variant else "NORMAL",
                    "is_reverse": sold.is_reverse,

                    # Vente
                    "item_id": sold.item_id,
                    "title": sold.title or "",
                    "price": sold.price,
                    "shipping": round(shipping, 2) if shipping else 0,
                    "effective_price": sold.effective_price,
                    "currency": sold.currency or "EUR",
                    "condition": sold.condition or "",
                    "seller": sold.seller or "",

                    # Dates
                    "listing_date": sold.listing_date or "",
                    "detected_sold_at": sold.detected_sold_at.strftime("%Y-%m-%d %H:%M") if sold.detected_sold_at else "",

                    # URL
                    "url": sold.url or "",
                }
                rows.append(row)
                stats["exported"] += 1
                stats["total_value"] += sold.effective_price or 0

            df = pd.DataFrame(rows)

            # Arrondir
            for col in ["price", "shipping", "effective_price"]:
                if col in df.columns:
                    df[col] = df[col].round(2)

            output_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(output_path, index=False, encoding="utf-8")

        return stats

    def export_sales_summary(
        self,
        output_path: Path,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> dict:
        """
        Exporte un resume des ventes par carte (une ligne par carte).

        Args:
            output_path: Chemin du fichier CSV
            date_from: Date de debut (optionnel)
            date_to: Date de fin (optionnel)

        Returns:
            Stats d'export
        """
        from sqlalchemy import func
        from collections import defaultdict

        stats = {"exported": 0, "total_cards": 0, "total_sales": 0}

        with get_session() as session:
            # D'abord calculer les stats de ventes par card_id
            sales_query = session.query(SoldListing)
            if date_from:
                sales_query = sales_query.filter(SoldListing.detected_sold_at >= date_from)
            if date_to:
                sales_query = sales_query.filter(SoldListing.detected_sold_at <= date_to)

            # Agregger les ventes par card_id
            sales_by_card = defaultdict(lambda: {
                "count": 0,
                "total": 0.0,
                "prices": [],
                "last_date": None
            })

            for sold in sales_query.all():
                s = sales_by_card[sold.card_id]
                s["count"] += 1
                price = sold.effective_price or 0
                s["total"] += price
                s["prices"].append(price)
                if s["last_date"] is None or (sold.detected_sold_at and sold.detected_sold_at > s["last_date"]):
                    s["last_date"] = sold.detected_sold_at

            # Recuperer toutes les cartes actives
            cards = session.query(Card).filter(Card.is_active == True).all()

            rows = []
            for card in cards:
                s = sales_by_card.get(card.id, {"count": 0, "total": 0, "prices": [], "last_date": None})

                sales_count = s["count"]
                sales_total = s["total"]
                prices = s["prices"]
                last_sale_date = s["last_date"]

                row = {
                    "tcgdex_id": card.tcgdex_id,
                    "name": card.name,
                    "set_name": card.set_name,
                    "set_code": card.set_code or "",
                    "local_id": card.local_id,
                    "variant": card.variant.value if card.variant else "NORMAL",

                    # Stats de ventes
                    "sales_count": sales_count,
                    "sales_total": round(sales_total, 2),
                    "sales_avg": round(sum(prices) / len(prices), 2) if prices else "",
                    "sales_min": round(min(prices), 2) if prices else "",
                    "sales_max": round(max(prices), 2) if prices else "",
                    "last_sale_date": last_sale_date.strftime("%Y-%m-%d") if last_sale_date else "",
                }
                rows.append(row)
                stats["exported"] += 1
                if sales_count > 0:
                    stats["total_cards"] += 1
                    stats["total_sales"] += sales_count

            df = pd.DataFrame(rows)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(output_path, index=False, encoding="utf-8")

        return stats
