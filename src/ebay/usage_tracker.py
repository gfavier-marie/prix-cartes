"""
Tracker d'utilisation de l'API eBay.
Permet de suivre le nombre d'appels quotidiens et de les comparer a la limite.
"""

from datetime import date
from typing import Optional

from sqlalchemy.orm import Session

from ..models import ApiUsage
from ..config import get_config


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
        """Recupere ou cree l'enregistrement du jour."""
        today = date.today()

        usage = self.session.query(ApiUsage).filter(
            ApiUsage.api_name == self.API_NAME,
            ApiUsage.usage_date == today
        ).first()

        if not usage:
            usage = ApiUsage(
                api_name=self.API_NAME,
                usage_date=today,
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
        Retourne l'historique des derniers jours.

        Args:
            days: Nombre de jours d'historique

        Returns:
            Liste des enregistrements ApiUsage
        """
        from datetime import timedelta

        start_date = date.today() - timedelta(days=days - 1)

        return self.session.query(ApiUsage).filter(
            ApiUsage.api_name == self.API_NAME,
            ApiUsage.usage_date >= start_date
        ).order_by(ApiUsage.usage_date.desc()).all()


def get_ebay_usage_summary(session: Session) -> dict:
    """
    Retourne un resume de l'utilisation eBay.

    Returns:
        Dict avec: today_count, daily_limit, remaining, percent, history
    """
    tracker = EbayUsageTracker(session)
    today = tracker.get_today_usage()
    history = tracker.get_history(7)

    return {
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
