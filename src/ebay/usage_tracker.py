"""
Tracker d'utilisation de l'API eBay.
Permet de suivre le nombre d'appels quotidiens et de les comparer a la limite.
"""

import json
import os
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ..models import ApiUsage
from ..config import get_config

# Fichier cache pour les rate limits eBay
RATE_LIMITS_CACHE_FILE = os.path.join(os.path.dirname(__file__), "../../data/ebay_rate_limits.json")

# Fichier pour stocker le blocage 429
RATE_LIMITED_FILE = os.path.join(os.path.dirname(__file__), "../../data/ebay_rate_limited.json")

# Heure de reset de l'API eBay (9h du matin heure locale)
EBAY_RESET_HOUR = 9


def get_ebay_api_date() -> date:
    """
    Retourne la date "API eBay" actuelle.

    eBay réinitialise son compteur à 9h du matin.
    Avant 9h, on considère qu'on est toujours sur le jour précédent.
    """
    now = datetime.now()
    if now.hour < EBAY_RESET_HOUR:
        # Avant 9h, on est encore sur le "jour API" précédent
        return (now - timedelta(days=1)).date()
    return now.date()


class EbayUsageTracker:
    """Tracker pour l'utilisation de l'API eBay."""

    API_NAME = "ebay"

    def __init__(self, session: Session, daily_limit: Optional[int] = None):
        """
        Args:
            session: Session SQLAlchemy
            daily_limit: Limite quotidienne (defaut: depuis config)
        """
        self.session = session
        self.daily_limit = daily_limit or get_config().ebay.daily_limit

    def _get_or_create_today(self) -> ApiUsage:
        """Recupere ou cree l'enregistrement du jour API eBay (reset à 9h)."""
        api_date = get_ebay_api_date()

        usage = self.session.query(ApiUsage).filter(
            ApiUsage.api_name == self.API_NAME,
            ApiUsage.usage_date == api_date
        ).first()

        if not usage:
            usage = ApiUsage(
                api_name=self.API_NAME,
                usage_date=api_date,
                call_count=0,
                daily_limit=self.daily_limit
            )
            self.session.add(usage)
            self.session.flush()

        return usage

    def increment(self, count: int = 1) -> ApiUsage:
        """
        Incremente le compteur d'appels.

        Args:
            count: Nombre d'appels a ajouter

        Returns:
            L'enregistrement ApiUsage mis a jour
        """
        usage = self._get_or_create_today()
        usage.call_count += count
        usage.daily_limit = self.daily_limit  # MAJ si config changee
        self.session.flush()
        return usage

    def get_today_usage(self) -> ApiUsage:
        """Retourne l'usage du jour."""
        return self._get_or_create_today()

    def get_remaining(self) -> int:
        """Retourne le nombre d'appels restants."""
        usage = self._get_or_create_today()
        return usage.remaining or self.daily_limit

    def get_usage_percent(self) -> float:
        """Retourne le pourcentage d'utilisation."""
        usage = self._get_or_create_today()
        return usage.usage_percent or 0.0

    def is_limit_reached(self) -> bool:
        """Verifie si la limite quotidienne est atteinte."""
        return self.get_remaining() <= 0

    def get_history(self, days: int = 7) -> list[ApiUsage]:
        """
        Retourne l'historique des derniers jours API.

        Args:
            days: Nombre de jours d'historique

        Returns:
            Liste des enregistrements ApiUsage
        """
        start_date = get_ebay_api_date() - timedelta(days=days - 1)

        return self.session.query(ApiUsage).filter(
            ApiUsage.api_name == self.API_NAME,
            ApiUsage.usage_date >= start_date
        ).order_by(ApiUsage.usage_date.desc()).all()


def get_ebay_usage_summary(session: Session) -> dict:
    """
    Retourne un resume de l'utilisation eBay.

    Returns:
        Dict avec: today_count, daily_limit, remaining, percent, history, reset
    """
    tracker = EbayUsageTracker(session)
    today = tracker.get_today_usage()
    history = tracker.get_history(7)

    # Recuperer les rate limits depuis le cache
    rate_limits = get_cached_rate_limits()

    result = {
        "today_count": today.call_count,
        "daily_limit": today.daily_limit,
        "remaining": today.remaining,
        "percent": round(today.usage_percent or 0, 1),
        "is_limit_reached": tracker.is_limit_reached(),
        "history": [
            {
                "date": str(h.usage_date),
                "count": h.call_count,
                "limit": h.daily_limit,
                "percent": round(h.usage_percent or 0, 1)
            }
            for h in history
        ]
    }

    # Ajouter les infos eBay si disponibles
    if rate_limits:
        result["ebay_count"] = rate_limits.get("count")
        result["ebay_limit"] = rate_limits.get("limit")
        result["ebay_remaining"] = rate_limits.get("remaining")
        result["ebay_reset"] = rate_limits.get("reset")
        result["ebay_reset_formatted"] = rate_limits.get("reset_formatted")
        result["cached_at"] = rate_limits.get("cached_at")

    return result


def save_rate_limits(rate_limits: dict) -> None:
    """Sauvegarde les rate limits dans le cache."""
    if not rate_limits:
        return

    # Formatter la date de reset pour affichage
    reset_formatted = None
    if rate_limits.get("reset"):
        try:
            reset_dt = datetime.fromisoformat(rate_limits["reset"].replace("Z", "+00:00"))
            reset_formatted = reset_dt.strftime("%d/%m %H:%M")
        except Exception:
            pass

    data = {
        **rate_limits,
        "reset_formatted": reset_formatted,
        "cached_at": datetime.now().isoformat(),
    }

    try:
        os.makedirs(os.path.dirname(RATE_LIMITS_CACHE_FILE), exist_ok=True)
        with open(RATE_LIMITS_CACHE_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def get_cached_rate_limits() -> Optional[dict]:
    """Recupere les rate limits depuis le cache."""
    try:
        if os.path.exists(RATE_LIMITS_CACHE_FILE):
            with open(RATE_LIMITS_CACHE_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def refresh_rate_limits_from_ebay() -> Optional[dict]:
    """Appelle l'API eBay pour rafraichir les rate limits."""
    from .client import EbayClient

    try:
        client = EbayClient()
        rate_limits = client.get_rate_limits()
        if rate_limits:
            save_rate_limits(rate_limits)
            return rate_limits
    except Exception:
        pass
    return None


def set_rate_limited() -> None:
    """
    Enregistre qu'on a recu une erreur 429.
    Le blocage sera actif jusqu'au prochain reset a 9h.
    """
    data = {
        "rate_limited_at": datetime.now().isoformat(),
        "api_date": str(get_ebay_api_date()),
    }
    try:
        os.makedirs(os.path.dirname(RATE_LIMITED_FILE), exist_ok=True)
        with open(RATE_LIMITED_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def is_rate_limited() -> bool:
    """
    Verifie si on est bloque suite a une erreur 429.

    Retourne True si:
    - On a recu un 429 sur le jour API actuel (avant 9h)
    - Et on est toujours avant 9h (pas encore reset)

    Returns:
        True si bloque, False sinon
    """
    try:
        if not os.path.exists(RATE_LIMITED_FILE):
            return False

        with open(RATE_LIMITED_FILE, "r") as f:
            data = json.load(f)

        # Verifier que le blocage est pour le jour API actuel
        blocked_api_date = data.get("api_date")
        current_api_date = str(get_ebay_api_date())

        if blocked_api_date != current_api_date:
            # Le blocage est pour un jour API different, on peut supprimer le fichier
            try:
                os.remove(RATE_LIMITED_FILE)
            except Exception:
                pass
            return False

        # Le blocage est pour aujourd'hui, on est encore bloque
        return True

    except Exception:
        return False


def clear_rate_limited() -> None:
    """Supprime le blocage 429 (appele apres 9h ou manuellement)."""
    try:
        if os.path.exists(RATE_LIMITED_FILE):
            os.remove(RATE_LIMITED_FILE)
    except Exception:
        pass


def get_ebay_remaining() -> Optional[int]:
    """
    Retourne le nombre d'appels restants selon eBay (depuis le cache).

    Returns:
        Le nombre d'appels restants, ou None si le cache n'est pas disponible.
    """
    rate_limits = get_cached_rate_limits()
    if rate_limits and "remaining" in rate_limits:
        return rate_limits.get("remaining")
    return None


def get_ebay_rate_limit_info() -> Optional[dict]:
    """
    Retourne les infos de rate limit eBay depuis le cache.

    Returns:
        Dict avec count, limit, remaining, reset ou None si pas de cache.
    """
    return get_cached_rate_limits()


def get_rate_limited_info() -> Optional[dict]:
    """Retourne les infos de blocage si actif."""
    if not is_rate_limited():
        return None

    try:
        with open(RATE_LIMITED_FILE, "r") as f:
            data = json.load(f)

        # Calculer le temps restant jusqu'a 9h
        now = datetime.now()
        if now.hour < EBAY_RESET_HOUR:
            reset_time = now.replace(hour=EBAY_RESET_HOUR, minute=0, second=0, microsecond=0)
        else:
            # On est apres 9h, le reset est demain (ne devrait pas arriver si is_rate_limited)
            reset_time = (now + timedelta(days=1)).replace(hour=EBAY_RESET_HOUR, minute=0, second=0, microsecond=0)

        remaining = reset_time - now

        return {
            "rate_limited_at": data.get("rate_limited_at"),
            "reset_time": reset_time.strftime("%H:%M"),
            "remaining_minutes": int(remaining.total_seconds() / 60),
        }
    except Exception:
        return None
