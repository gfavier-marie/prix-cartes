"""
Generateur de requetes eBay pour les cartes Pokemon.
Syntaxe eBay Browse API:
- OR: (mot1, mot2) avec virgules
- AND: espaces entre les mots
- Pas de negation (-) ni guillemets
- Max 100 caracteres
"""

from typing import Optional
from ..models import Card, Variant


class EbayQueryBuilder:
    """Construit des requetes eBay optimisees pour les cartes Pokemon."""

    # Mots-cles pour les variants
    VARIANT_KEYWORDS = {
        Variant.REVERSE: "reverse",
        Variant.HOLO: "holo",
        Variant.FIRST_ED: "edition 1",
    }

    # Sets promo (pas de /XXX sur les cartes physiques)
    PROMO_SETS = {"svp", "swshp", "smp", "xyp", "bwp", "dpp", "basep", "mep", "P-A", "tk-xy-p"}

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

        Format: [nom] [numero]/[total] [edition 1 si FIRST_ED]
        Ex: Alakazam 1/102
        Ex: Alakazam 1/102 edition 1 ed1
        Max 100 caracteres.

        Utilise les valeurs effectives (overrides si definis).
        """
        parts = []

        # 1. Nom de la carte (essentiel) - utilise l'override si defini
        name = self._clean_name(card.effective_name)
        parts.append(name)

        # 2. Numero complet (1/102) ou juste le numero
        # Utilise effective_local_id pour les overrides
        effective_local_id = card.effective_local_id
        # Pour les sets promo, utiliser juste le numero + "promo" (pas de /XXX sur les cartes physiques)
        if card.set_id in self.PROMO_SETS:
            if effective_local_id:
                parts.append(effective_local_id)
            parts.append("promo")
        elif card.card_number_full:
            # Corriger le padding si local_id commence par 0 (sets modernes)
            # Si override du local_id, reconstruire le card_number_full
            if card.local_id_override:
                # Reconstruire le numero complet avec l'override
                total_part = card.card_number_full.split("/")[-1] if "/" in card.card_number_full else ""
                if total_part:
                    card_number = f"{effective_local_id}/{total_part}"
                else:
                    card_number = effective_local_id
            else:
                card_number = self._fix_card_number_padding(card.card_number_full, card.local_id)
            parts.append(card_number)
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

    def _fix_card_number_padding(self, card_number_full: str, local_id: str) -> str:
        """
        Corrige le padding du numero de carte si necessaire.

        Detecte la largeur du padding depuis le local_id:
        - Si local_id commence par 0 (ex: "092"), utilise cette largeur
        - Sinon, deduit la largeur depuis la longueur du local_id
          (ex: pour un set 94 cartes avec local_id="102", le padding est 3)

        Ex: "092/94" -> "092/094"
        Ex: "102/94" -> "102/094" (si le set utilise des numeros a 3 chiffres)
        """
        if not local_id:
            return card_number_full

        # Parser le format "XXX/YYY"
        if "/" not in card_number_full:
            return card_number_full

        parts = card_number_full.split("/")
        if len(parts) != 2:
            return card_number_full

        num, total = parts

        # Determiner la largeur du padding
        # Si local_id commence par 0, utilise sa longueur
        # Sinon, compare: si len(local_id) > len(total), c'est qu'il y a du padding
        if local_id.startswith("0"):
            padding_width = len(local_id)
        elif len(num) > len(total):
            # Le numero est plus long que le total -> padding manquant sur le total
            padding_width = len(num)
        else:
            # Pas de padding necessaire
            return card_number_full

        # Padder le total avec des zeros
        try:
            total_int = int(total)
            total_padded = str(total_int).zfill(padding_width)
            return f"{num}/{total_padded}"
        except ValueError:
            return card_number_full

    def _clean_name(self, name: str) -> str:
        """Nettoie le nom de la carte."""
        # Retirer les guillemets doubles (problematiques pour eBay)
        name = name.replace('"', '')
        # Garder les apostrophes (ex: "Double Suppression d'Énergie")
        # Remplacer les tirets par des espaces
        name = name.replace("-", " ")
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
