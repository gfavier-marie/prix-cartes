"""
Modeles SQLAlchemy pour la base de donnees.
Tables: sets, cards, market_snapshots, buy_prices, batch_runs, fx_rates
"""

from datetime import datetime, date
from enum import Enum as PyEnum
from typing import Optional
import json

from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Boolean,
    DateTime,
    Date,
    Text,
    ForeignKey,
    Index,
    Enum,
    create_engine,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Variant(PyEnum):
    """Variants de cartes Pokemon."""
    NORMAL = "NORMAL"
    REVERSE = "REVERSE"
    HOLO = "HOLO"
    FIRST_ED = "FIRST_ED"


class AnchorSource(PyEnum):
    """Source du prix ancre."""
    EBAY_ACTIVE = "EBAY_ACTIVE"
    CARDMARKET_FALLBACK = "CARDMARKET_FALLBACK"
    LAST_KNOWN = "LAST_KNOWN"


class BuyPriceStatus(PyEnum):
    """Statut du prix de rachat."""
    OK = "OK"
    LOW_CONF = "LOW_CONF"
    DISABLED = "DISABLED"


class BatchMode(PyEnum):
    """Mode de batch."""
    FULL_EBAY = "FULL_EBAY"
    HYBRID = "HYBRID"


class Set(Base):
    """Table des sets Pokemon (extensions)."""

    __tablename__ = "sets"

    id = Column(String(50), primary_key=True)  # ex: "sv08", "swsh12"
    name = Column(String(200), nullable=False)  # ex: "Surging Sparks"
    serie_id = Column(String(50), nullable=False, index=True)  # ex: "sv", "swsh"
    serie_name = Column(String(200), nullable=False)  # ex: "Scarlet & Violet"
    release_date = Column(Date, nullable=True)
    card_count = Column(Integer, nullable=True)  # Nombre total de cartes

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relations
    cards = relationship("Card", back_populates="set_info", foreign_keys="Card.set_id")

    # Index
    __table_args__ = (
        Index("ix_sets_serie", "serie_id"),
    )

    def __repr__(self) -> str:
        return f"<Set {self.id}: {self.name}>"


class Card(Base):
    """Table des cartes Pokemon."""

    __tablename__ = "cards"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Identifiants TCGdex
    tcgdex_id = Column(String(100), unique=True, nullable=False, index=True)
    set_id = Column(String(50), ForeignKey("sets.id"), nullable=False, index=True)
    local_id = Column(String(20), nullable=False)  # Numero dans le set

    # Infos carte
    name = Column(String(200), nullable=False)
    name_en = Column(String(200), nullable=True)
    set_name = Column(String(200), nullable=False)
    set_code = Column(String(20), nullable=True)  # tcgOnline code
    card_number_full = Column(String(20), nullable=True)  # ex: "136/189"
    variant = Column(Enum(Variant), default=Variant.NORMAL, nullable=False)
    rarity = Column(String(50), nullable=True)
    language_scope = Column(String(10), default="FR")  # FR/EN/JPN/ANY

    # Requete eBay
    ebay_query = Column(Text, nullable=True)
    ebay_query_override = Column(Text, nullable=True)

    # Overrides manuels (prioritaires sur les valeurs TCGdex)
    name_override = Column(String(200), nullable=True)
    local_id_override = Column(String(20), nullable=True)
    set_name_override = Column(String(200), nullable=True)
    card_number_full_override = Column(String(20), nullable=True)  # ex: "H01/H32"

    # Prix Cardmarket (via TCGdex)
    cm_trend = Column(Float, nullable=True)
    cm_avg1 = Column(Float, nullable=True)
    cm_avg7 = Column(Float, nullable=True)
    cm_avg30 = Column(Float, nullable=True)

    # Statut
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Derniere erreur de collecte
    last_error = Column(Text, nullable=True)
    last_error_at = Column(DateTime, nullable=True)

    # Relations
    set_info = relationship("Set", back_populates="cards", foreign_keys=[set_id])
    snapshots = relationship("MarketSnapshot", back_populates="card", cascade="all, delete-orphan")
    buy_price = relationship("BuyPrice", back_populates="card", uselist=False, cascade="all, delete-orphan")

    # Index composite
    __table_args__ = (
        Index("ix_cards_set_local_variant", "set_id", "local_id", "variant"),
        Index("ix_cards_cm_avg30", "cm_avg30"),
        Index("ix_cards_is_active", "is_active"),
    )

    @property
    def image_url(self) -> str:
        """Retourne l'URL de l'image TCGdex."""
        if self.set_info and self.set_info.serie_id:
            return f"https://assets.tcgdex.net/fr/{self.set_info.serie_id}/{self.set_id}/{self.local_id}/low.png"
        # Fallback si serie_id non disponible
        return f"https://assets.tcgdex.net/fr/{self.set_id}/{self.local_id}/low.png"

    @property
    def effective_ebay_query(self) -> Optional[str]:
        """Retourne la requete eBay effective (override ou auto)."""
        return self.ebay_query_override or self.ebay_query

    @property
    def cm_max(self) -> Optional[float]:
        """Retourne le max entre trend et avg30."""
        values = [v for v in [self.cm_trend, self.cm_avg30] if v is not None]
        return max(values) if values else None

    @property
    def effective_name(self) -> str:
        """Retourne le nom override s'il existe, sinon le nom TCGdex."""
        return self.name_override or self.name

    @property
    def effective_local_id(self) -> str:
        """Retourne le numero override s'il existe, sinon le numero TCGdex."""
        return self.local_id_override or self.local_id

    @property
    def effective_set_name(self) -> str:
        """Retourne le nom de serie override s'il existe, sinon le nom TCGdex."""
        return self.set_name_override or self.set_name

    @property
    def effective_card_number_full(self) -> Optional[str]:
        """Retourne le numero complet override s'il existe, sinon le numero TCGdex."""
        return self.card_number_full_override or self.card_number_full

    @property
    def has_overrides(self) -> bool:
        """Retourne True si au moins un override est defini."""
        return bool(self.name_override or self.local_id_override or self.set_name_override or self.card_number_full_override)

    def __repr__(self) -> str:
        return f"<Card {self.tcgdex_id}: {self.name} ({self.variant.value})>"


class MarketSnapshot(Base):
    """Historique des collectes de marche."""

    __tablename__ = "market_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    card_id = Column(Integer, ForeignKey("cards.id", ondelete="CASCADE"), nullable=False)
    as_of_date = Column(Date, nullable=False, index=True)

    # Metriques eBay
    active_count = Column(Integer, nullable=True)  # Total annonces
    sample_size = Column(Integer, nullable=True)   # Items utilises pour stats

    # Percentiles
    p20 = Column(Float, nullable=True)
    p50 = Column(Float, nullable=True)
    p80 = Column(Float, nullable=True)

    # Qualite
    dispersion = Column(Float, nullable=True)  # p80/p20

    # Nouveaux indicateurs enrichis
    p10 = Column(Float, nullable=True)  # Borne basse robuste
    p90 = Column(Float, nullable=True)  # Borne haute robuste
    iqr = Column(Float, nullable=True)  # Interquartile range (p75-p25)
    cv = Column(Float, nullable=True)   # Coefficient de variation

    # Indicateurs temporels
    age_median_days = Column(Float, nullable=True)   # Age median des annonces
    pct_recent_7d = Column(Float, nullable=True)     # % annonces < 7 jours
    pct_old_30d = Column(Float, nullable=True)       # % annonces > 30 jours

    # Score de consensus
    consensus_score = Column(Float, nullable=True)   # % dans ±20% de p50

    # Stats pour les annonces REVERSE (si carte non-REVERSE)
    reverse_sample_size = Column(Integer, nullable=True)
    reverse_p10 = Column(Float, nullable=True)
    reverse_p20 = Column(Float, nullable=True)
    reverse_p50 = Column(Float, nullable=True)
    reverse_p80 = Column(Float, nullable=True)
    reverse_p90 = Column(Float, nullable=True)
    reverse_dispersion = Column(Float, nullable=True)
    reverse_cv = Column(Float, nullable=True)
    reverse_consensus_score = Column(Float, nullable=True)
    reverse_age_median_days = Column(Float, nullable=True)
    reverse_pct_recent_7d = Column(Float, nullable=True)

    # Ancre finale
    anchor_price = Column(Float, nullable=True)
    anchor_source = Column(Enum(AnchorSource), nullable=True)

    # Confiance
    confidence_score = Column(Integer, nullable=True)  # 0-100

    # Metadata debug (JSON)
    raw_meta = Column(Text, nullable=True)  # JSON: query, outliers, errors...

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relations
    card = relationship("Card", back_populates="snapshots")

    # Index
    __table_args__ = (
        Index("ix_snapshots_card_date", "card_id", "as_of_date"),
    )

    def set_raw_meta(self, data: dict) -> None:
        """Stocke les metadata en JSON."""
        self.raw_meta = json.dumps(data, ensure_ascii=False, default=str)

    def get_raw_meta(self) -> dict:
        """Recupere les metadata depuis JSON."""
        if self.raw_meta:
            return json.loads(self.raw_meta)
        return {}

    def __repr__(self) -> str:
        return f"<MarketSnapshot card_id={self.card_id} date={self.as_of_date}>"


class BuyPrice(Base):
    """Prix de rachat calcules (derniere version)."""

    __tablename__ = "buy_prices"

    card_id = Column(Integer, ForeignKey("cards.id", ondelete="CASCADE"), primary_key=True)

    # Prix par etat
    buy_neuf = Column(Float, nullable=True)
    buy_bon = Column(Float, nullable=True)
    buy_correct = Column(Float, nullable=True)

    # Infos ancre
    anchor_price = Column(Float, nullable=True)
    anchor_source = Column(Enum(AnchorSource), nullable=True)
    confidence_score = Column(Integer, nullable=True)
    as_of_date = Column(Date, nullable=True)

    # Statut
    status = Column(Enum(BuyPriceStatus), default=BuyPriceStatus.OK, nullable=False)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relations
    card = relationship("Card", back_populates="buy_price")

    # Index
    __table_args__ = (
        Index("ix_buy_prices_updated", "updated_at"),
    )

    def __repr__(self) -> str:
        return f"<BuyPrice card_id={self.card_id} neuf={self.buy_neuf}>"


class BatchRun(Base):
    """Historique des executions de batch."""

    __tablename__ = "batch_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)

    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)

    mode = Column(Enum(BatchMode), nullable=False)

    # Set cible (optionnel, pour batchs par set)
    set_id = Column(String(50), nullable=True, index=True)
    set_name = Column(String(200), nullable=True)

    # Stats
    cards_targeted = Column(Integer, default=0)
    cards_succeeded = Column(Integer, default=0)
    cards_failed = Column(Integer, default=0)

    # Notes/rapport
    notes = Column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<BatchRun {self.id} mode={self.mode.value}>"


class FxRate(Base):
    """Taux de change (pour conversion devises)."""

    __tablename__ = "fx_rates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rate_date = Column(Date, nullable=False, index=True)
    base_currency = Column(String(3), default="EUR", nullable=False)

    # Taux stockes en JSON
    rates_json = Column(Text, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def set_rates(self, rates: dict[str, float]) -> None:
        """Stocke les taux en JSON."""
        self.rates_json = json.dumps(rates)

    def get_rates(self) -> dict[str, float]:
        """Recupere les taux depuis JSON."""
        return json.loads(self.rates_json) if self.rates_json else {}

    def convert_to_eur(self, amount: float, from_currency: str) -> float:
        """Convertit un montant en EUR."""
        if from_currency == "EUR":
            return amount
        rates = self.get_rates()
        if from_currency in rates:
            return amount / rates[from_currency]
        raise ValueError(f"Taux inconnu pour {from_currency}")

    def __repr__(self) -> str:
        return f"<FxRate {self.rate_date} base={self.base_currency}>"


class ApiUsage(Base):
    """Suivi de l'utilisation quotidienne des APIs."""

    __tablename__ = "api_usage"

    id = Column(Integer, primary_key=True, autoincrement=True)
    api_name = Column(String(50), nullable=False)  # "ebay", "tcgdex"
    usage_date = Column(Date, nullable=False, index=True)
    call_count = Column(Integer, default=0, nullable=False)
    daily_limit = Column(Integer, nullable=True)  # Limite configuree

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Index composite pour lookup rapide
    __table_args__ = (
        Index("ix_api_usage_api_date", "api_name", "usage_date", unique=True),
    )

    @property
    def usage_percent(self) -> Optional[float]:
        """Pourcentage d'utilisation par rapport a la limite."""
        if self.daily_limit and self.daily_limit > 0:
            return (self.call_count / self.daily_limit) * 100
        return None

    @property
    def remaining(self) -> Optional[int]:
        """Appels restants avant d'atteindre la limite."""
        if self.daily_limit:
            return max(0, self.daily_limit - self.call_count)
        return None

    def __repr__(self) -> str:
        return f"<ApiUsage {self.api_name} {self.usage_date}: {self.call_count}/{self.daily_limit}>"


class SoldListing(Base):
    """Annonces eBay disparues (probablement vendues)."""

    __tablename__ = "sold_listings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    card_id = Column(Integer, ForeignKey("cards.id", ondelete="CASCADE"), nullable=False)

    # Infos eBay
    item_id = Column(String(100), nullable=False, index=True)  # ID unique eBay
    title = Column(String(500), nullable=True)
    price = Column(Float, nullable=True)
    effective_price = Column(Float, nullable=True)  # Prix + port
    currency = Column(String(3), default="EUR")
    url = Column(Text, nullable=True)
    seller = Column(String(100), nullable=True)
    image_url = Column(Text, nullable=True)
    condition = Column(String(50), nullable=True)
    listing_date = Column(String(50), nullable=True)  # Date de mise en ligne

    # Tracking
    first_seen_at = Column(DateTime, nullable=True)  # Premier snapshot ou vu
    last_seen_at = Column(DateTime, nullable=True)   # Dernier snapshot ou vu
    detected_sold_at = Column(DateTime, default=datetime.utcnow, nullable=False)  # Quand disparu

    # Type (normal ou reverse)
    is_reverse = Column(Boolean, default=False, nullable=False)

    # Relations
    card = relationship("Card")

    # Index
    __table_args__ = (
        Index("ix_sold_listings_card", "card_id"),
        Index("ix_sold_listings_detected", "detected_sold_at"),
        Index("ix_sold_listings_item", "item_id", unique=True),
    )

    def __repr__(self) -> str:
        return f"<SoldListing {self.item_id}: {self.effective_price}€>"
