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

from ..models import Card, MarketSnapshot, BatchRun, BatchMode, AnchorSource, ApiUsage
from ..database import get_session, get_db_session
from ..config import get_config
from ..ebay import EbayQueryBuilder, EbayWorker
from ..ebay.usage_tracker import EbayUsageTracker
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
    skipped: int = 0  # Cartes exclues (valeur trop faible)
    mismatch_count: int = 0
    low_confidence_count: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)  # (card_id, error)
    stopped_consecutive_failures: bool = False  # Arrete pour echecs consecutifs


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

        if track_api_usage:
            self._usage_session = get_db_session()
            self._usage_tracker = EbayUsageTracker(self._usage_session)
            self.worker = EbayWorker(on_api_call=self._on_api_call)
        else:
            self.worker = EbayWorker()

    def _on_api_call(self, count: int = 1) -> None:
        """Callback appele apres chaque appel API."""
        if self._usage_tracker and self._usage_session:
            self._usage_tracker.increment(count)
            self._usage_session.commit()

    def get_api_usage_today(self) -> dict:
        """Retourne l'usage API du jour."""
        if self._usage_tracker:
            from ..ebay.usage_tracker import get_ebay_usage_summary
            return get_ebay_usage_summary(self._usage_session)
        return {}

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
    ) -> tuple[BatchStats, AnomalyReport]:
        """
        Execute le batch de pricing.

        Args:
            mode: FULL_EBAY ou HYBRID
            card_ids: Liste optionnelle de card_ids a traiter
            set_id: Optionnel - filtre par set_id (ex: "base1", "sv03.5")
            limit: Limite du nombre de cartes a traiter
            progress_callback: Callback (processed, total) pour la progression

        Returns:
            (BatchStats, AnomalyReport)
        """
        stats = BatchStats()
        anomalies = AnomalyReport()
        batch_run: Optional[BatchRun] = None

        with get_session() as session:
            # Creer le batch run
            batch_run = BatchRun(
                mode=mode,
                started_at=datetime.utcnow(),
                cards_succeeded=0,
                cards_failed=0,
            )
            session.add(batch_run)
            session.flush()

            # Recuperer les cartes a traiter
            cards = self._get_cards_to_process(session, card_ids, set_id, limit)
            stats.total_cards = len(cards)
            batch_run.cards_targeted = stats.total_cards
            session.commit()  # Commit initial pour que le batch soit visible

            console.print(f"[cyan]Starting batch with {stats.total_cards} cards (mode: {mode.value})[/cyan]")

            # Reinitialiser le flag d'arret au demarrage
            clear_stop()

            # Compteur d'echecs consecutifs
            consecutive_failures = 0
            MAX_CONSECUTIVE_FAILURES = 10

            # Traiter chaque carte
            for i, card in enumerate(cards):
                # Verifier si l'arret a ete demande
                if is_stop_requested():
                    console.print("[yellow]Batch interrompu par l'utilisateur[/yellow]")
                    break

                # Verifier les echecs consecutifs (arrete juste cette serie, pas toute la queue)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    console.print(f"[red]Serie arretee: {MAX_CONSECUTIVE_FAILURES} echecs consecutifs[/red]")
                    stats.stopped_consecutive_failures = True
                    break

                try:
                    result = self._process_card(session, card, mode, anomalies)

                    if result == "success":
                        stats.succeeded += 1
                        consecutive_failures = 0  # Reset on success
                    elif result == "skipped":
                        stats.skipped += 1
                        consecutive_failures = 0  # Reset on skip (not a failure)
                    elif result == "failed":
                        stats.failed += 1
                        consecutive_failures += 1

                except Exception as e:
                    stats.failed += 1
                    consecutive_failures += 1
                    stats.errors.append((card.id, str(e)))
                    console.print(f"[red]Error processing card {card.id}: {e}[/red]")

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

            session.commit()

        return stats, anomalies

    def _get_cards_to_process(
        self,
        session: Session,
        card_ids: Optional[list[int]],
        set_id: Optional[str],
        limit: Optional[int]
    ) -> list[Card]:
        """Recupere les cartes a traiter."""
        query = session.query(Card).filter(Card.is_active == True)

        if card_ids:
            query = query.filter(Card.id.in_(card_ids))

        if set_id:
            query = query.filter(Card.set_id == set_id)

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

                # Pas de fallback - echec direct si pas de resultat eBay
                anomalies.query_issues.append({
                    "card_id": card.id,
                    "name": card.name,
                    "error": result.error,
                    "query": result.query_used,
                })
                return "failed"
            else:
                # Succes: effacer l'erreur precedente
                card.last_error = None
                card.last_error_at = None
                # Creer le snapshot depuis les donnees eBay
                snapshot = self.worker.create_snapshot(card, result, as_of, items=result.items)

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
                return "failed"

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
            f"Skipped (low value): {stats.skipped}",
        ]

        if stats.stopped_consecutive_failures:
            lines.append("*** ARRETE: 10 echecs consecutifs ***")

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
