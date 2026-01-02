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


class CardNumberFormat(PyEnum):
    """Format du numero de carte pour les requetes eBay."""
    LOCAL_ONLY = "LOCAL_ONLY"      # Juste le numero: "25"
    LOCAL_TOTAL = "LOCAL_TOTAL"    # Numero/Total: "25/102"
    PROMO = "PROMO"                # Numero + promo: "25 promo"


class Set(Base):
    """Table des sets Pokemon (extensions)."""

    __tablename__ = "sets"

    id = Column(String(50), primary_key=True)  # ex: "sv08", "swsh12"
    name = Column(String(200), nullable=False)  # ex: "Surging Sparks"
    serie_id = Column(String(50), nullable=False, index=True)  # ex: "sv", "swsh"
    serie_name = Column(String(200), nullable=False)  # ex: "Scarlet & Violet"
    release_date = Column(Date, nullable=True)
    card_count = Column(Integer, nullable=True)  # Nombre total de cartes
    card_count_official = Column(Integer, nullable=True)  # Total officiel (ex: 147 pour Aquapolis)

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
    card_count_official_override = Column(String(20), nullable=True)  # ex: "H32" pour les holos ecard
    card_number_format = Column(Enum(CardNumberFormat), nullable=True)  # Format pour eBay query
    card_number_padded = Column(Boolean, nullable=True)  # Si True, padding avec zeros: 1/92 -> 001/092

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
    error_count = Column(Integer, default=0, nullable=False)  # Compteur d'erreurs consecutives

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
        """Retourne le numero complet construit avec card_count_official_override si defini, sinon TCGdex."""
        # Si card_count_official_override est defini, construire local_id/count
        if self.card_count_official_override:
            local_id = self.effective_local_id
            total = self.card_count_official_override
            # Appliquer le padding si demande (toujours 3 chiffres)
            if self.card_number_padded:
                local_id = self._pad_number(local_id, '000')
                total = self._pad_number(total, '000')
            return f"{local_id}/{total}"
        # Sinon utiliser card_number_full_override si defini
        if self.card_number_full_override:
            return self.card_number_full_override
        # Sinon valeur TCGdex (avec padding si demande)
        if self.card_number_full and self.card_number_padded:
            parts = self.card_number_full.split("/")
            if len(parts) == 2:
                local_id, total = parts[0], parts[1]
                # Toujours 3 chiffres si padding active: 2/90 -> 002/090
                return f"{self._pad_number(local_id, '000')}/{self._pad_number(total, '000')}"
        return self.card_number_full

    def _pad_number(self, value: str, reference: str) -> str:
        """Ajoute des zeros devant un nombre pour atteindre la longueur de reference.

        Ex: _pad_number("1", "92") -> "01"
            _pad_number("5", "102") -> "005"
            _pad_number("H01", "H32") -> "H01" (garde tel quel si non numerique)
        """
        # Extraire la partie numerique
        if value.isdigit() and reference.isdigit():
            target_len = len(reference)
            return value.zfill(target_len)
        return value

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

    # Stats pour les annonces GRADED (PSA, CGC, PCA, etc.)
    graded_sample_size = Column(Integer, nullable=True)
    graded_p10 = Column(Float, nullable=True)
    graded_p20 = Column(Float, nullable=True)
    graded_p50 = Column(Float, nullable=True)
    graded_p80 = Column(Float, nullable=True)
    graded_p90 = Column(Float, nullable=True)
    graded_dispersion = Column(Float, nullable=True)
    graded_cv = Column(Float, nullable=True)
    graded_consensus_score = Column(Float, nullable=True)
    graded_age_median_days = Column(Float, nullable=True)
    graded_pct_recent_7d = Column(Float, nullable=True)

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

    # Resultats detailles (JSON) pour export CSV
    # Format: [{"card_id": int, "tcgdex_id": str, "name": str, "set_id": str, "set_name": str, "status": str, "error": str|null}]
    results_json = Column(Text, nullable=True)

    def set_results(self, results: list[dict]) -> None:
        """Stocke les resultats en JSON."""
        self.results_json = json.dumps(results, ensure_ascii=False, default=str)

    def get_results(self) -> list[dict]:
        """Recupere les resultats depuis JSON."""
        if self.results_json:
            return json.loads(self.results_json)
        return []

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


class Settings(Base):
    """Parametres applicatifs modifiables via l'interface admin."""

    __tablename__ = "settings"

    key = Column(String(100), primary_key=True)
    value = Column(String(500), nullable=False)
    description = Column(String(500), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Cles predefinies et valeurs par defaut
    DEFAULTS = {
        "batch_enabled": ("true", "Activer le batch automatique quotidien"),
        "batch_hour": ("3", "Heure d'execution du batch (0-23)"),
        "daily_api_limit": ("5000", "Limite quotidienne d'appels API eBay"),
        "low_value_threshold": ("10", "Seuil en euros pour cartes basse valeur"),
        "low_value_refresh_days": ("60", "Frequence de rafraichissement (jours) pour cartes basse valeur"),
        "max_error_retries": ("3", "Nombre d'erreurs avant de passer en basse priorite"),
    }

    @classmethod
    def get_value(cls, session, key: str, default: str = None) -> str:
        """Recupere une valeur de setting."""
        setting = session.query(cls).filter_by(key=key).first()
        if setting:
            return setting.value
        # Retourner la valeur par defaut si elle existe
        if key in cls.DEFAULTS:
            return cls.DEFAULTS[key][0]
        return default

    @classmethod
    def set_value(cls, session, key: str, value: str) -> None:
        """Definit une valeur de setting."""
        setting = session.query(cls).filter_by(key=key).first()
        if setting:
            setting.value = value
        else:
            description = cls.DEFAULTS.get(key, (None, None))[1]
            setting = cls(key=key, value=value, description=description)
            session.add(setting)
        session.commit()

    @classmethod
    def get_all(cls, session) -> dict:
        """Recupere tous les settings avec valeurs par defaut."""
        result = {}
        # D'abord les valeurs par defaut
        for key, (default_value, description) in cls.DEFAULTS.items():
            result[key] = {"value": default_value, "description": description}
        # Puis les valeurs en base (ecrasent les defauts)
        for setting in session.query(cls).all():
            result[setting.key] = {
                "value": setting.value,
                "description": setting.description or cls.DEFAULTS.get(setting.key, (None, ""))[1]
            }
        return result

    def __repr__(self) -> str:
        return f"<Settings {self.key}={self.value}>"
