"""
Client eBay Browse API avec OAuth2.
Documentation: https://developer.ebay.com/api-docs/buy/browse/overview.html
"""

import base64
import time
from dataclasses import dataclass, field
from typing import Optional, Any, Callable

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from ..config import get_config, EbayConfig


@dataclass
class EbayItem:
    """Representation d'un item eBay."""
    item_id: str
    title: str
    price: float
    currency: str
    shipping_cost: Optional[float] = None
    shipping_currency: Optional[str] = None
    condition: Optional[str] = None
    condition_id: Optional[str] = None
    image_url: Optional[str] = None
    item_web_url: Optional[str] = None
    seller_username: Optional[str] = None
    listing_date: Optional[str] = None  # Date de mise en vente

    @property
    def effective_price(self) -> float:
        """Prix effectif = prix + port."""
        if self.shipping_cost is not None:
            return self.price + self.shipping_cost
        return self.price


@dataclass
class EbaySearchResult:
    """Resultat d'une recherche eBay."""
    total: int
    items: list[EbayItem] = field(default_factory=list)
    offset: int = 0
    limit: int = 0
    warnings: list[str] = field(default_factory=list)


class EbayAuthError(Exception):
    """Erreur d'authentification eBay."""
    pass


class EbayAPIError(Exception):
    """Erreur API eBay."""
    pass


class EbayClient:
    """Client pour l'API eBay Browse."""

    def __init__(
        self,
        config: Optional[EbayConfig] = None,
        on_api_call: Optional[Callable[[int], None]] = None
    ):
        """
        Args:
            config: Configuration eBay
            on_api_call: Callback appele apres chaque appel API (recoit le nombre d'appels)
        """
        if config is None:
            config = get_config().ebay
        self.config = config
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0
        self._on_api_call = on_api_call
        self._call_count = 0  # Compteur de session

    def _track_api_call(self, count: int = 1) -> None:
        """Enregistre un ou plusieurs appels API."""
        self._call_count += count
        if self._on_api_call:
            self._on_api_call(count)

    @property
    def session_call_count(self) -> int:
        """Nombre d'appels API effectues dans cette session."""
        return self._call_count

    def _get_auth_header(self) -> str:
        """Genere le header d'authentification Basic."""
        credentials = f"{self.config.client_id}:{self.config.client_secret}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"

    def _refresh_token(self) -> None:
        """Obtient un nouveau token OAuth2."""
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                self.config.auth_url,
                headers={
                    "Authorization": self._get_auth_header(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "client_credentials",
                    "scope": "https://api.ebay.com/oauth/api_scope",
                },
            )

            if response.status_code != 200:
                raise EbayAuthError(f"Auth failed: {response.status_code} - {response.text}")

            data = response.json()
            self._access_token = data["access_token"]
            # Expire un peu avant pour etre safe
            expires_in = data.get("expires_in", 7200)
            self._token_expires_at = time.time() + expires_in - 60

    def _ensure_token(self) -> str:
        """S'assure qu'on a un token valide."""
        if self._access_token is None or time.time() >= self._token_expires_at:
            self._refresh_token()
        return self._access_token  # type: ignore

    def _get_headers(self) -> dict[str, str]:
        """Headers pour les requetes API."""
        token = self._ensure_token()
        return {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": self.config.marketplace_id,
            "Content-Type": "application/json",
        }

    # Mots a exclure dans les titres (lots, graded, etc.)
    TITLE_EXCLUSIONS_BASE = [
        # Lots et bundles
        "lot ", "lots ", "bundle", "collection", "x10", "x20", "x50", "x100",
        # Cartes gradees (toutes les companies)
        "psa ", "psa-", "cgc ", "cgc-", "bgs ", "bgs-", "bcc ", "bcc-",
        "pca ", "pca-", "pcg ", "pcg-", "ace ", "ace-", "mga ", "mga-", "sgc ", "sgc-",
        "ccc ", "ccc-", "graded", "slab", "gradee", "gradée", " pg ", " pg-",
        # Faux / custom
        "proxy", "orica", "custom", "fake",
        # Codes online
        "code card", "online code", "code online",
        # Autres langues
        "japanese", "japan", "jpn", "anglais", "english", "german", "italian",
    ]

    # Exclusions pour cartes normales (exclure Edition 1)
    EDITION1_KEYWORDS = [
        "edition 1", "édition 1", "1st edition", "1ere edition", "1ère edition",
        "ed1", "ed 1", " ed.1", "1st ed", "first edition", "1ère éd",
    ]

    # Exclusions pour cartes Edition 1 (exclure Edition 2)
    EDITION2_KEYWORDS = [
        "edition 2", "édition 2", "2nd edition", "ed2", "ed 2", " ed.2",
        "unlimited", "illimité", "illimitée",
    ]

    # Par defaut: exclure Edition 1
    TITLE_EXCLUSIONS = TITLE_EXCLUSIONS_BASE + EDITION1_KEYWORDS

    def search(
        self,
        query: str,
        limit: int = 50,
        offset: int = 0,
        category_ids: Optional[list[str]] = None,
        condition_ids: Optional[list[str]] = None,
        item_location_country: Optional[str] = None,
        delivery_country: Optional[str] = None,
        buying_options: Optional[list[str]] = None,
        filter_titles: bool = True,
        is_first_edition: bool = False,
        is_reverse: bool = False,
        card_number: Optional[str] = None,
        card_number_full: Optional[str] = None,
    ) -> EbaySearchResult:
        """
        Recherche d'items via Browse API.

        Args:
            query: Requete de recherche
            limit: Nombre max de resultats (max 200)
            offset: Decalage pour pagination
            category_ids: IDs de categories a filtrer
            condition_ids: IDs de conditions (1000=New, 3000=Used, etc.)
            item_location_country: Code pays ISO (FR, DE, etc.)
            delivery_country: Livrable vers ce pays
            buying_options: FIXED_PRICE, AUCTION, BEST_OFFER
            filter_titles: Filtrer les titres pour exclure lots/graded

        Returns:
            EbaySearchResult avec les items trouves
        """
        url = f"{self.config.api_base_url}/buy/browse/v1/item_summary/search"

        params: dict[str, Any] = {
            "q": query,
            "limit": min(limit, 200),
            "offset": offset,
        }

        # Construire les filtres
        filters = []

        if category_ids:
            params["category_ids"] = ",".join(category_ids)

        if condition_ids:
            filters.append(f"conditionIds:{{{','.join(condition_ids)}}}")

        if item_location_country:
            filters.append(f"itemLocationCountry:{item_location_country}")

        if delivery_country:
            filters.append(f"deliveryCountry:{delivery_country}")

        if buying_options:
            filters.append(f"buyingOptions:{{{','.join(buying_options)}}}")

        if filters:
            params["filter"] = ",".join(filters)

        with httpx.Client(timeout=10.0) as client:
            response = client.get(url, headers=self._get_headers(), params=params)
            self._track_api_call(1)

            if response.status_code == 401:
                # Token expire, refresh et retry
                self._refresh_token()
                response = client.get(url, headers=self._get_headers(), params=params)
                self._track_api_call(1)

            if response.status_code != 200:
                raise EbayAPIError(f"Search failed: {response.status_code} - {response.text}")

            data = response.json()

        # Parser les resultats
        result = EbaySearchResult(
            total=data.get("total", 0),
            offset=data.get("offset", offset),
            limit=data.get("limit", limit),
        )

        for item_data in data.get("itemSummaries", []):
            item = self._parse_item(item_data)
            if item:
                # Filtrer les titres indesirables
                if filter_titles and self._should_exclude_title(
                    item.title, is_first_edition, is_reverse, card_number, card_number_full
                ):
                    continue
                result.items.append(item)

        # Warnings
        for warning in data.get("warnings", []):
            result.warnings.append(warning.get("message", str(warning)))

        return result

    # Keywords pour identifier les cartes reverse
    REVERSE_KEYWORDS = ["reverse", "rev ", " rev"]

    def _should_exclude_title(
        self,
        title: str,
        is_first_edition: bool = False,
        is_reverse: bool = False,
        card_number: Optional[str] = None,
        card_number_full: Optional[str] = None
    ) -> bool:
        """Verifie si le titre contient des mots a exclure."""
        import re
        title_lower = title.lower()

        # Filtrage REVERSE / NORMAL
        has_reverse = any(kw in title_lower for kw in self.REVERSE_KEYWORDS)
        if is_reverse:
            # Pour REVERSE: exclure si pas de marqueur reverse
            if not has_reverse:
                return True
        else:
            # Pour NORMAL: exclure si marqueur reverse present
            if has_reverse:
                return True

        # Exclusions de base (lots, graded, etc.)
        for exclusion in self.TITLE_EXCLUSIONS_BASE:
            if exclusion in title_lower:
                return True

        if is_first_edition:
            # Pour Edition 1: exclure les Edition 2 / Unlimited
            for exclusion in self.EDITION2_KEYWORDS:
                if exclusion in title_lower:
                    return True
            # Verifier que c'est bien une Edition 1
            has_ed1_marker = any(kw in title_lower for kw in self.EDITION1_KEYWORDS)
            if not has_ed1_marker:
                return True  # Exclure si pas de marqueur Edition 1
        else:
            # Pour Normal: exclure les Edition 1
            for exclusion in self.EDITION1_KEYWORDS:
                if exclusion in title_lower:
                    return True

        # Verifier le numero de carte si fourni
        if card_number:
            # Le numero doit apparaitre dans le titre, precede d'un non-chiffre
            # Ex: "1/102" doit matcher "1/102" mais pas "21/102" ou "1" dans "Edition 1"
            num = card_number

            if card_number_full and "/" in card_number_full:
                # On a le format complet X/Y
                num, total = card_number_full.split("/")
                # Enlever les zeros de padding pour la comparaison flexible
                num_stripped = num.lstrip('0') or '0'
                total_stripped = total.lstrip('0') or '0'
                # Pattern: X/Y avec X non precede d'un chiffre, zeros optionnels
                # Accepte 039/094, 39/94, 039/94, etc.
                pattern = rf'(?<![0-9])0*{re.escape(num_stripped)}\s*/\s*0*{re.escape(total_stripped)}'
                if re.search(pattern, title):
                    return False  # Numero trouve, ne pas exclure
            else:
                # Sets promo: pas de format X/Y
                # Accepter: "22 promo", "promo 22", "22/" (si existe quand meme)
                # Pattern: numero precede d'un non-chiffre et suivi d'un non-chiffre
                pattern = rf'(?<![0-9]){re.escape(num)}(?![0-9])'
                if re.search(pattern, title):
                    return False  # Numero trouve

            # Si le pattern n'a pas ete trouve, exclure l'annonce
            return True

        return False

    def _parse_item(self, data: dict) -> Optional[EbayItem]:
        """Parse un item depuis la reponse API."""
        try:
            price_data = data.get("price", {})
            price = float(price_data.get("value", 0))
            currency = price_data.get("currency", "EUR")

            # Shipping
            shipping_cost = None
            shipping_currency = None
            shipping_options = data.get("shippingOptions", [])
            if shipping_options:
                shipping_data = shipping_options[0].get("shippingCost", {})
                if shipping_data:
                    shipping_cost = float(shipping_data.get("value", 0))
                    shipping_currency = shipping_data.get("currency", currency)

            # Condition
            condition = data.get("condition")
            condition_id = data.get("conditionId")

            # Image
            image = data.get("image", {})
            image_url = image.get("imageUrl") if image else None

            # Seller
            seller = data.get("seller", {})
            seller_username = seller.get("username") if seller else None

            # Date de mise en vente
            listing_date = data.get("itemCreationDate")

            return EbayItem(
                item_id=data.get("itemId", ""),
                title=data.get("title", ""),
                price=price,
                currency=currency,
                shipping_cost=shipping_cost,
                shipping_currency=shipping_currency,
                condition=condition,
                condition_id=condition_id,
                image_url=image_url,
                item_web_url=data.get("itemWebUrl"),
                seller_username=seller_username,
                listing_date=listing_date,
            )
        except (ValueError, KeyError) as e:
            # Item mal forme, on skip
            return None

    def search_all(
        self,
        query: str,
        max_items: int = 100,
        is_first_edition: bool = False,
        is_reverse: bool = False,
        card_number: Optional[str] = None,
        card_number_full: Optional[str] = None,
        **kwargs
    ) -> EbaySearchResult:
        """
        Recherche avec pagination automatique.

        Args:
            query: Requete de recherche
            max_items: Nombre max d'items a recuperer
            **kwargs: Autres parametres pour search()

        Returns:
            EbaySearchResult avec tous les items
        """
        all_items: list[EbayItem] = []
        offset = 0
        limit = min(max_items, 200)
        total = 0

        while len(all_items) < max_items:
            result = self.search(
                query, limit=limit, offset=offset,
                is_first_edition=is_first_edition,
                is_reverse=is_reverse,
                card_number=card_number,
                card_number_full=card_number_full,
                **kwargs
            )
            total = result.total

            if not result.items:
                break

            all_items.extend(result.items)
            offset += limit  # Must increment by limit, not filtered count

            if offset >= total:
                break

        return EbaySearchResult(
            total=total,
            items=all_items[:max_items],
            offset=0,
            limit=max_items,
        )
