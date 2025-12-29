"""
Configuration centralisee pour l'outil de pricing.
Tous les parametres des specs sont definis ici.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import yaml


@dataclass
class PricingConfig:
    """Parametres de calcul du prix de rachat."""

    # Seuil minimum de valeur (exclure cartes < 3EUR)
    min_card_value_eur: float = 3.00

    # Marge cible (25-30%)
    margin_target: float = 0.27

    # Frais eBay + paiement
    fees_rate: float = 0.11

    # Couts fixes par carte (emballage, etc.)
    fixed_costs_eur: float = 0.50

    # Buffer de risque de base
    risk_base: float = 0.02

    # Coefficients de risque
    risk_k1_dispersion: float = 0.02  # Penalite dispersion
    risk_k2_supply: float = 0.01      # Penalite supply elevee
    risk_k3_low_sample: float = 0.05  # Penalite si sample < 10
    risk_k4_fallback: float = 0.03    # Penalite si fallback Cardmarket

    # Plancher/plafond prix de rachat
    min_buy_price: float = 0.50
    max_buy_price: float = 10000.00

    # Arrondi (0.10 ou 0.50)
    rounding_step: float = 0.10

    # Coefficients par etat
    coef_neuf: float = 1.00
    coef_bon: float = 0.60
    coef_correct: float = 0.30


@dataclass
class GuardrailsConfig:
    """Parametres des garde-fous Cardmarket."""

    # Seuils de mismatch
    mismatch_upper: float = 2.5   # anchor > 2.5 * cm = mismatch
    mismatch_lower: float = 0.4   # anchor < 0.4 * cm = mismatch

    # Dispersion maximale acceptable
    dispersion_bad: float = 4.0


@dataclass
class EbayConfig:
    """Parametres eBay API."""

    # Credentials (a charger depuis .env)
    client_id: str = ""
    client_secret: str = ""

    # Endpoint
    api_base_url: str = "https://api.ebay.com"
    auth_url: str = "https://api.ebay.com/identity/v1/oauth2/token"

    # Parametres de recherche
    sample_limit: int = 50  # Max 50 = 1 seule requête par carte (pas de pagination)
    min_sample_size: int = 1  # 1 annonce suffit pour un succes

    # Marketplace (FR)
    marketplace_id: str = "EBAY_FR"

    # Langue des cartes (français uniquement)
    french_only: bool = True

    # Categories Pokemon TCG
    category_ids: list[str] = field(default_factory=lambda: ["183454"])  # Pokemon TCG

    # Trimming des outliers (pourcentage)
    trim_bottom_pct: float = 0.05
    trim_top_pct: float = 0.05

    # Limite quotidienne d'appels API (Browse API = 5000/jour en production)
    daily_limit: int = 5000


@dataclass
class TCGdexConfig:
    """Parametres TCGdex API."""

    api_base_url: str = "https://api.tcgdex.net/v2"
    language: str = "fr"  # ou "en"

    # Rate limiting
    requests_per_second: float = 5.0


@dataclass
class DatabaseConfig:
    """Parametres base de donnees."""

    db_path: Path = field(default_factory=lambda: Path("data/pricing.db"))
    echo_sql: bool = False


@dataclass
class AppConfig:
    """Configuration globale de l'application."""

    pricing: PricingConfig = field(default_factory=PricingConfig)
    guardrails: GuardrailsConfig = field(default_factory=GuardrailsConfig)
    ebay: EbayConfig = field(default_factory=EbayConfig)
    tcgdex: TCGdexConfig = field(default_factory=TCGdexConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)

    # Admin
    admin_host: str = "127.0.0.1"
    admin_port: int = 5000

    @classmethod
    def load(cls, config_path: Optional[Path] = None) -> "AppConfig":
        """Charge la config depuis un fichier YAML."""
        config = cls()

        if config_path is None:
            config_path = Path("config.yaml")

        if config_path.exists():
            with open(config_path) as f:
                data = yaml.safe_load(f) or {}

            # Pricing
            if "pricing" in data:
                for key, value in data["pricing"].items():
                    if hasattr(config.pricing, key):
                        setattr(config.pricing, key, value)

            # Guardrails
            if "guardrails" in data:
                for key, value in data["guardrails"].items():
                    if hasattr(config.guardrails, key):
                        setattr(config.guardrails, key, value)

            # eBay
            if "ebay" in data:
                for key, value in data["ebay"].items():
                    if hasattr(config.ebay, key):
                        setattr(config.ebay, key, value)

            # TCGdex
            if "tcgdex" in data:
                for key, value in data["tcgdex"].items():
                    if hasattr(config.tcgdex, key):
                        setattr(config.tcgdex, key, value)

            # Database
            if "database" in data:
                if "db_path" in data["database"]:
                    config.database.db_path = Path(data["database"]["db_path"])
                if "echo_sql" in data["database"]:
                    config.database.echo_sql = data["database"]["echo_sql"]

            # Admin
            if "admin" in data:
                if "host" in data["admin"]:
                    config.admin_host = data["admin"]["host"]
                if "port" in data["admin"]:
                    config.admin_port = data["admin"]["port"]

        return config

    def save(self, config_path: Optional[Path] = None) -> None:
        """Sauvegarde la config dans un fichier YAML."""
        if config_path is None:
            config_path = Path("config.yaml")

        data = {
            "pricing": {
                "min_card_value_eur": self.pricing.min_card_value_eur,
                "margin_target": self.pricing.margin_target,
                "fees_rate": self.pricing.fees_rate,
                "fixed_costs_eur": self.pricing.fixed_costs_eur,
                "risk_base": self.pricing.risk_base,
                "min_buy_price": self.pricing.min_buy_price,
                "max_buy_price": self.pricing.max_buy_price,
                "rounding_step": self.pricing.rounding_step,
                "coef_neuf": self.pricing.coef_neuf,
                "coef_bon": self.pricing.coef_bon,
                "coef_correct": self.pricing.coef_correct,
            },
            "guardrails": {
                "mismatch_upper": self.guardrails.mismatch_upper,
                "mismatch_lower": self.guardrails.mismatch_lower,
                "dispersion_bad": self.guardrails.dispersion_bad,
            },
            "ebay": {
                "client_id": self.ebay.client_id,
                "client_secret": self.ebay.client_secret,
                "marketplace_id": self.ebay.marketplace_id,
                "sample_limit": self.ebay.sample_limit,
            },
            "tcgdex": {
                "language": self.tcgdex.language,
            },
            "database": {
                "db_path": str(self.database.db_path),
            },
            "admin": {
                "host": self.admin_host,
                "port": self.admin_port,
            },
        }

        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)


# Singleton global
_config: Optional[AppConfig] = None


def get_config() -> AppConfig:
    """Retourne la configuration globale (singleton)."""
    global _config
    if _config is None:
        _config = AppConfig.load()
    return _config


def reload_config(config_path: Optional[Path] = None) -> AppConfig:
    """Recharge la configuration."""
    global _config
    _config = AppConfig.load(config_path)
    return _config
