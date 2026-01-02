"""
Orchestration du batch de pricing.
Pipeline complet: collecte eBay -> garde-fous -> calcul prix -> sauvegarde
"""

from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional, Callable
import threading

from rich.console import Console
from rich.progress import Progress, TaskID, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn

from sqlalchemy.orm import Session

from ..models import Card, Set, MarketSnapshot, BatchRun, BatchMode, AnchorSource, ApiUsage, Variant, Settings
from ..database import get_session, get_db_session
from ..config import get_config
from ..ebay import EbayQueryBuilder, EbayWorker
from ..ebay.client import EbayRateLimitError
from ..ebay.usage_tracker import (
    EbayUsageTracker, set_rate_limited, is_rate_limited,
    refresh_rate_limits_from_ebay, get_ebay_remaining, get_ebay_rate_limit_info
)
from ..pricing import PriceGuardrails


console = Console()

# Flag global pour l'arret du batch (partage entre threads)
_stop_requested = threading.Event()


def request_stop():
    """Demande l'arret du batch en cours."""
    _stop_requested.set()


def clear_stop():
    """Reinitialise le flag d'arret."""
    _stop_requested.clear()


def is_stop_requested() -> bool:
    """Verifie si l'arret a ete demande."""
    return _stop_requested.is_set()


@dataclass
class BatchStats:
    """Statistiques du batch."""
    total_cards: int = 0
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0  # Cartes exclues (valeur trop faible ou set ignore)
    mismatch_count: int = 0
    low_confidence_count: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)  # (card_id, error)
    skipped_sets: list[str] = field(default_factory=list)  # Sets ignores apres trop d'echecs
    stopped_api_limit: bool = False  # Arrete pour limite API quotidienne atteinte
    stopped_rate_limit: bool = False  # Arrete pour erreur 429 (rate limit eBay)
    stopped_consecutive_failures: bool = False  # Arrete pour echecs consecutifs sur un set


@dataclass
class AnomalyReport:
    """Rapport d'anomalies du batch."""
    high_variations: list[dict] = field(default_factory=list)  # Variation > X% vs precedent
    high_dispersions: list[dict] = field(default_factory=list)  # Dispersion > seuil
    query_issues: list[dict] = field(default_factory=list)  # Problemes de requete
    mismatches: list[dict] = field(default_factory=list)  # Fallback Cardmarket


class BatchRunner:
    """Execute le batch de pricing."""

    def __init__(self, track_api_usage: bool = True):
        """
        Args:
            track_api_usage: Si True, enregistre les appels API en base
        """
        self.config = get_config()
        self.query_builder = EbayQueryBuilder(
            language=self.config.tcgdex.language,
            french_only=self.config.ebay.french_only
        )
        self.guardrails = PriceGuardrails()

        # Tracking API usage
        self._track_api_usage = track_api_usage
        self._usage_session = None
        self._usage_tracker = None

        # Compteur de session (basé sur eBay)
        self._starting_ebay_count = 0  # Compteur eBay au démarrage du batch
        self._session_call_count = 0   # Compteur d'appels pendant ce batch

        if track_api_usage:
            self._usage_session = get_db_session()
            # Lire la limite depuis Settings (source unique de verite)
            daily_limit = int(Settings.get_value(self._usage_session, "daily_api_limit", "5000"))
            self._usage_tracker = EbayUsageTracker(self._usage_session, daily_limit=daily_limit)
            self.worker = EbayWorker(on_api_call=self._on_api_call)
        else:
            self.worker = EbayWorker()

    def _on_api_call(self, count: int = 1) -> None:
        """Callback appele apres chaque appel API."""
        # Compteur de session (pour verification limite)
        self._session_call_count += count

        if self._usage_tracker and self._usage_session:
            self._usage_tracker.increment(count)
            self._usage_session.commit()

    def get_api_usage_today(self) -> dict:
        """Retourne l'usage API du jour."""
        if self._usage_tracker:
            from ..ebay.usage_tracker import get_ebay_usage_summary
            return get_ebay_usage_summary(self._usage_session)
        return {}

    def _check_api_limit(self, session: Session) -> bool:
        """Verifie si la limite API quotidienne est atteinte.

        Logique: (compteur eBay initial + appels de ce batch) >= limite configuree

        Returns:
            True si la limite est atteinte, False sinon.
        """
        # Recuperer la limite configuree dans Settings
        daily_limit_str = Settings.get_value(session, "daily_api_limit", "5000")
        try:
            daily_limit = int(daily_limit_str)
        except ValueError:
            daily_limit = 5000

        # Calcul: eBay initial + session = total utilise
        total_count = self._starting_ebay_count + self._session_call_count

        if total_count >= daily_limit:
            return True  # Limite configuree atteinte

        return False

    def close(self) -> None:
        """Ferme la session de tracking."""
        if self._usage_session:
            self._usage_session.close()
            self._usage_session = None

    def run(
        self,
        mode: BatchMode = BatchMode.FULL_EBAY,
        card_ids: Optional[list[int]] = None,
        set_id: Optional[str] = None,
        limit: Optional[int] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        prioritize_oldest: bool = False,
    ) -> tuple[BatchStats, AnomalyReport]:
        """
        Execute le batch de pricing.

        Args:
            mode: FULL_EBAY ou HYBRID
            card_ids: Liste optionnelle de card_ids a traiter
            set_id: Optionnel - filtre par set_id (ex: "base1", "sv03.5")
            limit: Limite du nombre de cartes a traiter
            progress_callback: Callback (processed, total) pour la progression
            prioritize_oldest: Si True, traite d'abord les cartes jamais traitees ou les plus anciennes

        Returns:
            (BatchStats, AnomalyReport)
        """
        stats = BatchStats()
        anomalies = AnomalyReport()
        batch_run: Optional[BatchRun] = None

        # Rafraichir les rate limits eBay au demarrage
        rate_limits = refresh_rate_limits_from_ebay()

        # Initialiser le compteur de session depuis eBay
        self._session_call_count = 0
        if rate_limits:
            self._starting_ebay_count = rate_limits.get('count', 0)
            ebay_limit = rate_limits.get('limit', 5000)
            console.print(f"[dim]eBay: {self._starting_ebay_count}/{ebay_limit} utilisés[/dim]")
        else:
            self._starting_ebay_count = 0
            console.print("[yellow]Impossible de récupérer le compteur eBay, démarrage à 0[/yellow]")

        with get_session() as session:
            # Afficher la limite configuree et le nombre d'appels possibles
            daily_limit_str = Settings.get_value(session, "daily_api_limit", "5000")
            daily_limit = int(daily_limit_str)
            remaining = daily_limit - self._starting_ebay_count
            console.print(f"[dim]Limite configurée: {daily_limit} -> {remaining} appels possibles[/dim]")
            # Verifier la limite API AVANT de commencer
            if self._check_api_limit(session):
                console.print("[yellow]Limite API quotidienne deja atteinte, batch non demarre[/yellow]")
                stats.stopped_api_limit = True
                return stats, anomalies

            # Recuperer le nom du set si set_id specifie
            set_name = None
            if set_id:
                first_card = session.query(Card).filter(Card.set_id == set_id).first()
                if first_card:
                    set_name = first_card.set_name

            # Creer le batch run
            batch_run = BatchRun(
                mode=mode,
                started_at=datetime.utcnow(),
                set_id=set_id,
                set_name=set_name,
                cards_succeeded=0,
                cards_failed=0,
            )
            session.add(batch_run)
            session.flush()

            # Recuperer les cartes a traiter (avec priorisation si demandee)
            cards = self._get_cards_to_process(session, card_ids, set_id, limit, prioritize_oldest)
            stats.total_cards = len(cards)
            batch_run.cards_targeted = stats.total_cards
            session.commit()  # Commit initial pour que le batch soit visible

            if prioritize_oldest:
                console.print(f"[cyan]Starting batch with {stats.total_cards} cards (mode: {mode.value}, priorite: anciennes d'abord)[/cyan]")
            else:
                console.print(f"[cyan]Starting batch with {stats.total_cards} cards (mode: {mode.value})[/cyan]")

            # Reinitialiser le flag d'arret au demarrage
            clear_stop()

            # Compteur d'echecs par set (pour skip les sets problematiques)
            set_failures: dict[str, int] = {}  # set_id -> nombre d'echecs
            skipped_sets: set[str] = set()  # sets a ignorer
            MAX_SET_FAILURES = 10

            # Resultats detailles pour export CSV
            card_results: list[dict] = []

            # Traiter chaque carte
            for i, card in enumerate(cards):
                # Verifier si l'arret a ete demande
                if is_stop_requested():
                    console.print("[yellow]Batch interrompu par l'utilisateur[/yellow]")
                    break

                # Verifier la limite API quotidienne AVANT de traiter la carte
                if self._check_api_limit(session):
                    console.print("[yellow]Limite API quotidienne atteinte, arret du batch[/yellow]")
                    stats.stopped_api_limit = True
                    break

                # Verifier si le set de cette carte est a ignorer
                if card.set_id in skipped_sets:
                    stats.skipped += 1
                    stats.processed += 1
                    card_results.append({
                        "card_id": card.id,
                        "tcgdex_id": card.tcgdex_id,
                        "name": card.name,
                        "set_id": card.set_id,
                        "set_name": card.set_name,
                        "status": "skipped",
                        "error": f"Set {card.set_id} ignore (trop d'echecs)"
                    })
                    continue

                try:
                    result = self._process_card(session, card, mode, anomalies)

                    if result == "success":
                        stats.succeeded += 1
                        # Reset compteur du set en cas de succes
                        if card.set_id in set_failures:
                            set_failures[card.set_id] = 0
                        card_results.append({
                            "card_id": card.id,
                            "tcgdex_id": card.tcgdex_id,
                            "name": card.name,
                            "set_id": card.set_id,
                            "set_name": card.set_name,
                            "status": "success",
                            "error": None
                        })
                    elif result == "skipped":
                        stats.skipped += 1
                        card_results.append({
                            "card_id": card.id,
                            "tcgdex_id": card.tcgdex_id,
                            "name": card.name,
                            "set_id": card.set_id,
                            "set_name": card.set_name,
                            "status": "skipped",
                            "error": None
                        })
                    elif result == "failed":
                        stats.failed += 1
                        # Incrementer compteur d'echecs pour ce set
                        set_failures[card.set_id] = set_failures.get(card.set_id, 0) + 1
                        if set_failures[card.set_id] >= MAX_SET_FAILURES:
                            console.print(f"[yellow]Set {card.set_id} ignore apres {MAX_SET_FAILURES} echecs[/yellow]")
                            skipped_sets.add(card.set_id)
                            stats.skipped_sets.append(card.set_id)
                        # Recuperer l'erreur depuis la carte
                        card_results.append({
                            "card_id": card.id,
                            "tcgdex_id": card.tcgdex_id,
                            "name": card.name,
                            "set_id": card.set_id,
                            "set_name": card.set_name,
                            "status": "failed",
                            "error": card.last_error
                        })

                except EbayRateLimitError:
                    # Erreur 429: activer le blocage et arreter immediatement
                    console.print("[red]Erreur 429 - Rate limit eBay atteint, arret du batch[/red]")
                    set_rate_limited()
                    stats.stopped_rate_limit = True
                    card_results.append({
                        "card_id": card.id,
                        "tcgdex_id": card.tcgdex_id,
                        "name": card.name,
                        "set_id": card.set_id,
                        "set_name": card.set_name,
                        "status": "failed",
                        "error": "Erreur 429 - Rate limit eBay"
                    })
                    break

                except Exception as e:
                    stats.failed += 1
                    stats.errors.append((card.id, str(e)))
                    console.print(f"[red]Error processing card {card.id}: {e}[/red]")
                    # Incrementer compteur d'echecs pour ce set
                    set_failures[card.set_id] = set_failures.get(card.set_id, 0) + 1
                    if set_failures[card.set_id] >= MAX_SET_FAILURES:
                        console.print(f"[yellow]Set {card.set_id} ignore apres {MAX_SET_FAILURES} echecs[/yellow]")
                        skipped_sets.add(card.set_id)
                        stats.skipped_sets.append(card.set_id)
                    card_results.append({
                        "card_id": card.id,
                        "tcgdex_id": card.tcgdex_id,
                        "name": card.name,
                        "set_id": card.set_id,
                        "set_name": card.set_name,
                        "status": "failed",
                        "error": str(e)
                    })

                stats.processed += 1

                # Mettre a jour les compteurs toutes les 5 cartes
                if stats.processed % 5 == 0 or stats.processed == stats.total_cards:
                    batch_run.cards_succeeded = stats.succeeded
                    batch_run.cards_failed = stats.failed
                    session.commit()

                if progress_callback:
                    progress_callback(stats.processed, stats.total_cards, stats.succeeded, stats.failed)

            # Finaliser le batch
            batch_run.finished_at = datetime.utcnow()
            batch_run.cards_succeeded = stats.succeeded
            batch_run.cards_failed = stats.failed

            # Generer le rapport
            report = self._generate_report(stats, anomalies)
            batch_run.notes = report

            # Sauvegarder les resultats detailles pour export CSV
            batch_run.set_results(card_results)

            session.commit()

        # Rafraichir les rate limits eBay a la fin du batch (verification)
        final_limits = refresh_rate_limits_from_ebay()

        # Afficher le resume du batch
        expected_total = self._starting_ebay_count + self._session_call_count
        console.print(f"[dim]Batch: {self._session_call_count} appels API ({self._starting_ebay_count} -> {expected_total})[/dim]")

        if final_limits:
            ebay_final_count = final_limits.get('count', 0)
            ebay_limit = final_limits.get('limit', 5000)
            console.print(f"[dim]eBay final: {ebay_final_count}/{ebay_limit} utilisés[/dim]")

            # Verifier la coherence
            if ebay_final_count != expected_total:
                console.print(f"[yellow]Ecart detecte: attendu {expected_total}, eBay {ebay_final_count}[/yellow]")

        return stats, anomalies

    def _get_cards_to_process(
        self,
        session: Session,
        card_ids: Optional[list[int]],
        set_id: Optional[str],
        limit: Optional[int],
        prioritize_oldest: bool = False
    ) -> list[Card]:
        """
        Recupere les cartes a traiter avec regles de priorisation.

        Ordre de priorite (si prioritize_oldest=True):
        1. Cartes jamais explorees (pas de snapshot)
        2. Cartes en erreur < max_error_retries (cooldown 24h)
        3. Cartes en erreur >= max_error_retries OU valeur < seuil (1x par low_value_refresh_days)
        4. Autres cartes (plus ancien d'abord)

        Args:
            prioritize_oldest: Si True, applique les regles de priorisation
        """
        from sqlalchemy import func, case, and_, or_
        from sqlalchemy.orm import aliased
        from datetime import timedelta

        # Recuperer les series/sets exclus depuis la config
        config = get_config()
        excluded_series = config.tcgdex.excluded_series or []
        excluded_sets = config.tcgdex.excluded_sets or []

        if prioritize_oldest:
            # Recuperer les parametres de priorisation depuis Settings
            low_value_threshold = float(Settings.get_value(session, "low_value_threshold", "10"))
            low_value_refresh_days = int(Settings.get_value(session, "low_value_refresh_days", "60"))
            max_error_retries = int(Settings.get_value(session, "max_error_retries", "3"))

            # Subquery pour trouver le dernier snapshot de chaque carte avec anchor_price
            latest_snapshot = session.query(
                MarketSnapshot.card_id,
                func.max(MarketSnapshot.as_of_date).label('last_snapshot_date')
            ).group_by(MarketSnapshot.card_id).subquery()

            # Subquery pour recuperer l'anchor_price du dernier snapshot
            snapshot_with_price = session.query(
                MarketSnapshot.card_id,
                MarketSnapshot.anchor_price,
                MarketSnapshot.as_of_date
            ).join(
                latest_snapshot,
                and_(
                    MarketSnapshot.card_id == latest_snapshot.c.card_id,
                    MarketSnapshot.as_of_date == latest_snapshot.c.last_snapshot_date
                )
            ).subquery()

            # Cooldowns
            error_cooldown = datetime.utcnow() - timedelta(hours=24)
            low_value_cooldown = date.today() - timedelta(days=low_value_refresh_days)

            # Construire la query avec priorites
            # Priority 0: jamais explore (NULL snapshot)
            # Priority 1: erreur < max_retries et cooldown 24h OK
            # Priority 2: (erreur >= max_retries OU valeur < seuil) et cooldown X jours OK
            # Priority 3: valeur >= seuil, plus ancien d'abord

            query = session.query(Card).join(
                Set, Card.set_id == Set.id
            ).outerjoin(
                snapshot_with_price,
                Card.id == snapshot_with_price.c.card_id
            ).filter(
                Card.is_active == True,
            ).filter(
                or_(
                    # Jamais explore -> toujours inclus
                    snapshot_with_price.c.card_id.is_(None),
                    # Erreur recente < max_retries -> cooldown 24h
                    and_(
                        Card.error_count < max_error_retries,
                        or_(Card.last_error_at.is_(None), Card.last_error_at < error_cooldown)
                    ),
                    # Erreur >= max_retries -> cooldown X jours
                    and_(
                        Card.error_count >= max_error_retries,
                        snapshot_with_price.c.as_of_date < low_value_cooldown
                    ),
                    # Basse valeur -> cooldown X jours
                    and_(
                        Card.error_count < max_error_retries,
                        snapshot_with_price.c.anchor_price < low_value_threshold,
                        snapshot_with_price.c.as_of_date < low_value_cooldown
                    ),
                    # Haute valeur -> toujours inclus (sera trie par anciennete)
                    and_(
                        Card.error_count < max_error_retries,
                        or_(
                            snapshot_with_price.c.anchor_price >= low_value_threshold,
                            snapshot_with_price.c.anchor_price.is_(None)
                        )
                    ),
                )
            ).order_by(
                # Priorite: 0=jamais explore, 1=erreur normale, 2=basse valeur/erreur max, 3=haute valeur
                case(
                    (snapshot_with_price.c.card_id.is_(None), 0),  # Jamais explore
                    (and_(Card.error_count < max_error_retries, Card.last_error_at.isnot(None)), 1),  # Erreur recente
                    (Card.error_count >= max_error_retries, 2),  # Trop d'erreurs
                    (snapshot_with_price.c.anchor_price < low_value_threshold, 2),  # Basse valeur
                    else_=3  # Haute valeur normale
                ),
                snapshot_with_price.c.as_of_date.asc().nullsfirst()  # Plus ancien d'abord
            )
        else:
            query = session.query(Card).join(Set, Card.set_id == Set.id).filter(Card.is_active == True)

        if card_ids:
            query = query.filter(Card.id.in_(card_ids))

        if set_id:
            query = query.filter(Card.set_id == set_id)

        # Exclure les series et sets masques dans la config
        if excluded_series:
            query = query.filter(~Set.serie_id.in_(excluded_series))
        if excluded_sets:
            query = query.filter(~Set.id.in_(excluded_sets))

        if limit:
            query = query.limit(limit)

        return query.all()

    def _process_card(
        self,
        session: Session,
        card: Card,
        mode: BatchMode,
        anomalies: AnomalyReport
    ) -> str:
        """
        Traite une carte.

        Returns:
            "success", "skipped", ou "failed"
        """
        as_of = date.today()

        # Generer la requete eBay si necessaire
        if not card.ebay_query and not card.ebay_query_override:
            self.query_builder.generate_for_card(card)

        # Recuperer le snapshot precedent pour comparaison
        previous_snapshot = session.query(MarketSnapshot).filter(
            MarketSnapshot.card_id == card.id,
            MarketSnapshot.as_of_date < as_of
        ).order_by(MarketSnapshot.as_of_date.desc()).first()

        # Collecter les donnees eBay
        if mode == BatchMode.FULL_EBAY:
            result = self.worker.collect_for_card(card)

            if not result.success:
                # Stocker l'erreur sur la carte (avec active_count si disponible)
                error_msg = result.error
                if result.active_count > 0:
                    error_msg = f"{result.error} ({result.active_count} résultats eBay)"
                card.last_error = error_msg
                card.last_error_at = datetime.utcnow()
                card.error_count = (card.error_count or 0) + 1  # Incrementer le compteur d'erreurs

                # Pas de fallback - echec direct si pas de resultat eBay
                anomalies.query_issues.append({
                    "card_id": card.id,
                    "name": card.name,
                    "error": result.error,
                    "query": result.query_used,
                })
                return "failed"
            else:
                # Succes: effacer l'erreur precedente et reinitialiser le compteur
                card.last_error = None
                card.last_error_at = None
                card.error_count = 0
                # Creer le snapshot depuis les donnees eBay
                snapshot = self.worker.create_snapshot(card, result, as_of, items=result.items)

                # Detecter les ventes (annonces disparues)
                if previous_snapshot:
                    self.worker.detect_sold_listings(session, card, snapshot, previous_snapshot, is_reverse=False)
                    # Aussi pour les reverse si applicable
                    if card.variant != Variant.REVERSE:
                        self.worker.detect_sold_listings(session, card, snapshot, previous_snapshot, is_reverse=True)

                # Appliquer les garde-fous
                guardrail_result = self.guardrails.apply_to_snapshot(snapshot, card)

                if guardrail_result.is_mismatch:
                    anomalies.mismatches.append({
                        "card_id": card.id,
                        "name": card.name,
                        "reason": guardrail_result.mismatch_reason,
                        "original_anchor": guardrail_result.original_anchor,
                        "final_anchor": guardrail_result.final_anchor,
                    })

        else:  # HYBRID mode
            # Utiliser Cardmarket directement
            cm_value = card.cm_max
            if cm_value is None or cm_value <= 0:
                card.error_count = (card.error_count or 0) + 1
                return "failed"
            # Succes en mode HYBRID
            card.error_count = 0

            snapshot = MarketSnapshot(
                card_id=card.id,
                as_of_date=as_of,
                anchor_price=cm_value,
                anchor_source=AnchorSource.CARDMARKET_FALLBACK,
            )

        # Detecter les anomalies
        self._check_anomalies(snapshot, previous_snapshot, card, anomalies)

        # Sauvegarder le snapshot (plus de calcul de prix de rachat)
        session.add(snapshot)

        return "success"

    def _check_anomalies(
        self,
        snapshot: MarketSnapshot,
        previous: Optional[MarketSnapshot],
        card: Card,
        anomalies: AnomalyReport
    ) -> None:
        """Detecte les anomalies pour le rapport."""
        # High dispersion
        if snapshot.dispersion and snapshot.dispersion > self.config.guardrails.dispersion_bad:
            anomalies.high_dispersions.append({
                "card_id": card.id,
                "name": card.name,
                "dispersion": snapshot.dispersion,
            })

        # High variation vs previous
        if previous and previous.anchor_price and snapshot.anchor_price:
            variation = abs(snapshot.anchor_price - previous.anchor_price) / previous.anchor_price
            if variation > 0.6:  # > 60%
                anomalies.high_variations.append({
                    "card_id": card.id,
                    "name": card.name,
                    "previous": previous.anchor_price,
                    "current": snapshot.anchor_price,
                    "variation_pct": variation * 100,
                })

    def _generate_report(self, stats: BatchStats, anomalies: AnomalyReport) -> str:
        """Genere le rapport textuel du batch."""
        lines = [
            "=== BATCH REPORT ===",
            f"Total cards: {stats.total_cards}",
            f"Processed: {stats.processed}",
            f"Succeeded: {stats.succeeded}",
            f"Failed: {stats.failed}",
            f"Skipped: {stats.skipped}",
        ]

        if stats.skipped_sets:
            lines.append(f"*** Sets ignores ({len(stats.skipped_sets)}): {', '.join(stats.skipped_sets)} ***")

        if stats.stopped_api_limit:
            lines.append("*** ARRETE: Limite API quotidienne atteinte ***")

        if stats.stopped_rate_limit:
            lines.append("*** ARRETE: Erreur 429 - Rate limit eBay (bloque jusqu'a 9h) ***")

        lines.extend([
            "",
            f"High variations (>60%): {len(anomalies.high_variations)}",
            f"High dispersions: {len(anomalies.high_dispersions)}",
            f"Query issues: {len(anomalies.query_issues)}",
            f"Mismatches (fallback CM): {len(anomalies.mismatches)}",
        ])

        if stats.errors:
            lines.append("")
            lines.append("Errors:")
            for card_id, error in stats.errors[:10]:
                lines.append(f"  - Card {card_id}: {error}")
            if len(stats.errors) > 10:
                lines.append(f"  ... and {len(stats.errors) - 10} more")

        return "\n".join(lines)

    def run_with_progress(
        self,
        mode: BatchMode = BatchMode.FULL_EBAY,
        card_ids: Optional[list[int]] = None,
        set_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> tuple[BatchStats, AnomalyReport]:
        """Execute le batch avec affichage de progression Rich."""
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Processing cards...", total=100)

            def callback(processed: int, total: int):
                progress.update(task, completed=int(processed / total * 100) if total > 0 else 0)

            return self.run(mode, card_ids, set_id, limit, callback)

    def reprocess_card(self, card_id: int) -> bool:
        """Retraite une seule carte (pour l'admin)."""
        stats, _ = self.run(
            mode=BatchMode.FULL_EBAY,
            card_ids=[card_id],
        )
        return stats.succeeded > 0
