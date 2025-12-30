"""
Generateur de requetes eBay pour les cartes Pokemon.
Syntaxe eBay Browse API:
- OR: (mot1, mot2) avec virgules
- AND: espaces entre les mots
- Pas de negation (-) ni guillemets
- Max 100 caracteres
"""

from typing import Optional
from ..models import Card, Variant, CardNumberFormat


class EbayQueryBuilder:
    """Construit des requetes eBay optimisees pour les cartes Pokemon."""

    # Mots-cles pour les variants
    VARIANT_KEYWORDS = {
        Variant.REVERSE: "reverse",
        Variant.HOLO: "holo",
        Variant.FIRST_ED: "edition 1",
    }

    def __init__(self, language: str = "fr", french_only: bool = True):
        """
        Args:
            language: Langue cible (fr/en)
            french_only: Chercher uniquement les cartes françaises
        """
        self.language = language
        self.french_only = french_only

    def build_query(self, card: Card) -> str:
        """
        Construit la requete eBay pour une carte.

        Format depend de card_number_format:
        - LOCAL_ONLY: [nom] [numero]
        - LOCAL_TOTAL: [nom] [numero/total]
        - PROMO: [nom] [numero] promo

        Max 100 caracteres.
        Utilise les valeurs effectives (overrides si definis).
        """
        parts = []

        # 1. Nom de la carte (essentiel) - utilise l'override si defini
        name = self._clean_name(card.effective_name)
        parts.append(name)

        # 2. Numero de carte selon le format
        effective_local_id = card.effective_local_id
        effective_card_number_full = card.effective_card_number_full
        card_format = card.card_number_format or CardNumberFormat.LOCAL_TOTAL

        if card_format == CardNumberFormat.LOCAL_ONLY:
            # Juste le numero
            if effective_local_id:
                parts.append(effective_local_id)

        elif card_format == CardNumberFormat.PROMO:
            # Numero + "promo"
            if effective_local_id:
                parts.append(effective_local_id)
            parts.append("promo")

        else:  # LOCAL_TOTAL (defaut)
            # Numero complet X/Y
            if effective_card_number_full:
                parts.append(effective_card_number_full)
            elif effective_local_id:
                parts.append(effective_local_id)

        # 3. Seulement Edition 1 (pas holo, pas reverse, pas normal)
        if card.variant == Variant.FIRST_ED:
            parts.append(self.VARIANT_KEYWORDS[Variant.FIRST_ED])

        # Construire la requete
        query = " ".join(parts)

        # Tronquer a 100 caracteres si necessaire
        if len(query) > 100:
            query = self._truncate_query(query)

        return query

    # Caracteres speciaux a supprimer des noms de cartes
    SPECIAL_CHARS = [
        'δ',  # Delta species
        '☆',  # Gold star
        '★',  # Star
        '♀',  # Female
        '♂',  # Male
        '◇',  # Prism star
        '●',  # Bullet
        '♦',  # Diamond
        '♣',  # Club
        '♠',  # Spade
        '♥',  # Heart
        '©',  # Copyright
        '®',  # Registered
        '™',  # Trademark
    ]

    def _clean_name(self, name: str) -> str:
        """Nettoie le nom de la carte."""
        # Retirer les guillemets doubles (problematiques pour eBay)
        name = name.replace('"', '')
        # Garder les apostrophes (ex: "Double Suppression d'Énergie")
        # Remplacer les tirets par des espaces
        name = name.replace("-", " ")
        # Supprimer les caracteres speciaux (δ, ☆, etc.)
        for char in self.SPECIAL_CHARS:
            name = name.replace(char, '')
        # Nettoyer les espaces multiples
        while '  ' in name:
            name = name.replace('  ', ' ')
        return name.strip()

    def _truncate_query(self, query: str) -> str:
        """Tronque la requete a 100 caracteres intelligemment."""
        if len(query) <= 100:
            return query

        # Couper au dernier espace avant 100 caracteres
        truncated = query[:100]
        last_space = truncated.rfind(" ")
        if last_space > 50:  # Garder au moins 50 caracteres
            return truncated[:last_space]
        return truncated

    def build_minimal_query(self, card: Card) -> str:
        """Version minimale de la requete (fallback)."""
        name = self._clean_name(card.effective_name)
        parts = [name, "pokemon"]
        if card.effective_local_id:
            parts.insert(1, card.effective_local_id)
        return " ".join(parts)

    def generate_for_card(self, card: Card) -> str:
        """Genere et stocke la requete pour une carte."""
        query = self.build_query(card)
        card.ebay_query = query
        return query

    def regenerate_all(self, cards: list[Card]) -> int:
        """Regenere les requetes pour une liste de cartes."""
        count = 0
        for card in cards:
            if not card.ebay_query_override:  # Ne pas ecraser les overrides
                self.generate_for_card(card)
                count += 1
        return count


def generate_ebay_query(card: Card, language: str = "fr") -> str:
    """Fonction utilitaire pour generer une requete."""
    builder = EbayQueryBuilder(language=language)
    return builder.build_query(card)
