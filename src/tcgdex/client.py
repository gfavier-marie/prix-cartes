"""
Client API TCGdex pour recuperer les cartes Pokemon.
Documentation: https://tcgdex.dev/
"""

import time
from dataclasses import dataclass
from typing import Optional, Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import get_config, TCGdexConfig


@dataclass
class TCGdexSet:
    """Representation d'un set TCGdex."""
    id: str
    name: str
    tcg_online: Optional[str] = None
    card_count_official: Optional[int] = None
    card_count_total: Optional[int] = None
    release_date: Optional[str] = None
    logo: Optional[str] = None


@dataclass
class TCGdexCardPricing:
    """Prix Cardmarket via TCGdex."""
    trend: Optional[float] = None
    avg1: Optional[float] = None
    avg7: Optional[float] = None
    avg30: Optional[float] = None


@dataclass
class TCGdexCard:
    """Representation d'une carte TCGdex."""
    id: str
    local_id: str
    name: str
    set_id: str
    set_name: str
    set_code: Optional[str] = None

    # Variants
    has_normal: bool = True
    has_reverse: bool = False
    has_holo: bool = False
    has_first_edition: bool = False

    # Infos
    rarity: Optional[str] = None
    image: Optional[str] = None

    # Pricing Cardmarket
    pricing: Optional[TCGdexCardPricing] = None


class TCGdexClient:
    """Client pour l'API TCGdex."""

    def __init__(self, config: Optional[TCGdexConfig] = None):
        if config is None:
            config = get_config().tcgdex
        self.config = config
        self.base_url = config.api_base_url
        self.language = config.language
        self._last_request_time = 0.0
        self._min_interval = 1.0 / config.requests_per_second

    def _rate_limit(self) -> None:
        """Applique le rate limiting."""
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.time()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def _get(self, endpoint: str) -> Any:
        """Effectue une requete GET avec retry."""
        self._rate_limit()
        url = f"{self.base_url}/{self.language}/{endpoint}"

        with httpx.Client(timeout=30.0) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.json()

    def get_sets(self) -> list[TCGdexSet]:
        """Recupere tous les sets."""
        data = self._get("sets")

        sets = []
        for item in data:
            sets.append(TCGdexSet(
                id=item.get("id", ""),
                name=item.get("name", ""),
                tcg_online=item.get("tcgOnline"),
                logo=item.get("logo"),
            ))
        return sets

    def get_set(self, set_id: str) -> Optional[TCGdexSet]:
        """Recupere un set par ID avec details."""
        try:
            data = self._get(f"sets/{set_id}")

            card_count = data.get("cardCount", {})
            return TCGdexSet(
                id=data.get("id", ""),
                name=data.get("name", ""),
                tcg_online=data.get("tcgOnline"),
                card_count_official=card_count.get("official"),
                card_count_total=card_count.get("total"),
                release_date=data.get("releaseDate"),
                logo=data.get("logo"),
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    def get_cards_from_set(self, set_id: str) -> list[TCGdexCard]:
        """Recupere toutes les cartes d'un set."""
        try:
            data = self._get(f"sets/{set_id}")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return []
            raise

        cards_data = data.get("cards", [])
        set_name = data.get("name", "")
        set_code = data.get("tcgOnline")

        cards = []
        for item in cards_data:
            cards.append(self._parse_card(item, set_id, set_name, set_code))
        return cards

    def get_card(self, set_id: str, local_id: str) -> Optional[TCGdexCard]:
        """Recupere une carte specifique avec tous les details."""
        try:
            data = self._get(f"sets/{set_id}/{local_id}")
            set_data = data.get("set", {})
            return self._parse_card(
                data,
                set_id,
                set_data.get("name", ""),
                set_data.get("tcgOnline"),
                with_pricing=True
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    def get_card_by_id(self, card_id: str) -> Optional[TCGdexCard]:
        """Recupere une carte par son ID complet (ex: 'swsh3-136')."""
        try:
            data = self._get(f"cards/{card_id}")
            set_data = data.get("set", {})
            return self._parse_card(
                data,
                set_data.get("id", ""),
                set_data.get("name", ""),
                set_data.get("tcgOnline"),
                with_pricing=True
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    def _parse_card(
        self,
        data: dict,
        set_id: str,
        set_name: str,
        set_code: Optional[str],
        with_pricing: bool = False
    ) -> TCGdexCard:
        """Parse les donnees d'une carte."""
        variants = data.get("variants", {})

        # Pricing Cardmarket
        pricing = None
        if with_pricing:
            pricing_data = data.get("cardmarket", {})
            if pricing_data:
                prices = pricing_data.get("prices", {})
                pricing = TCGdexCardPricing(
                    trend=prices.get("trendPrice"),
                    avg1=prices.get("avg1"),
                    avg7=prices.get("avg7"),
                    avg30=prices.get("avg30"),
                )

        return TCGdexCard(
            id=data.get("id", ""),
            local_id=str(data.get("localId", "")),
            name=data.get("name", ""),
            set_id=set_id,
            set_name=set_name,
            set_code=set_code,
            has_normal=variants.get("normal", True),
            has_reverse=variants.get("reverse", False),
            has_holo=variants.get("holo", False),
            has_first_edition=variants.get("firstEdition", False),
            rarity=data.get("rarity"),
            image=data.get("image"),
            pricing=pricing,
        )

    def search_cards(self, query: str) -> list[TCGdexCard]:
        """Recherche de cartes par nom."""
        try:
            data = self._get(f"cards?name={query}")
            cards = []
            for item in data:
                set_data = item.get("set", {})
                cards.append(self._parse_card(
                    item,
                    set_data.get("id", ""),
                    set_data.get("name", ""),
                    set_data.get("tcgOnline"),
                ))
            return cards
        except httpx.HTTPStatusError:
            return []
