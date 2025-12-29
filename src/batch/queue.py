"""
Queue de batchs pour execution sequentielle.
"""

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Callable
from enum import Enum


class QueueItemStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class QueueItem:
    """Element de la queue."""
    set_id: str
    set_name: str
    status: QueueItemStatus = QueueItemStatus.PENDING
    added_at: datetime = field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    cards_targeted: int = 0
    cards_succeeded: int = 0
    cards_failed: int = 0
    error: Optional[str] = None


class BatchQueue:
    """
    Queue de batchs avec execution sequentielle.

    Singleton partage entre threads.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._queue: list[QueueItem] = []
        self._current: Optional[QueueItem] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_requested = threading.Event()
        self._initialized = True

    def add(self, set_id: str, set_name: str) -> QueueItem:
        """Ajoute un set a la queue."""
        # Verifier si deja dans la queue
        for item in self._queue:
            if item.set_id == set_id and item.status == QueueItemStatus.PENDING:
                return item  # Deja en attente

        item = QueueItem(set_id=set_id, set_name=set_name)
        self._queue.append(item)

        # Demarrer le worker si pas deja en cours
        self._ensure_worker_running()

        return item

    def add_multiple(self, sets: list[dict]) -> list[QueueItem]:
        """Ajoute plusieurs sets a la queue."""
        items = []
        for s in sets:
            item = self.add(s["set_id"], s["set_name"])
            items.append(item)
        return items

    def get_status(self) -> dict:
        """Retourne le statut de la queue."""
        pending = [i for i in self._queue if i.status == QueueItemStatus.PENDING]
        completed = [i for i in self._queue if i.status == QueueItemStatus.COMPLETED]
        failed = [i for i in self._queue if i.status == QueueItemStatus.FAILED]

        return {
            "running": self._current is not None,
            "current": self._format_item(self._current) if self._current else None,
            "pending": [self._format_item(i) for i in pending],
            "pending_count": len(pending),
            "completed_count": len(completed),
            "failed_count": len(failed),
            "total_in_queue": len(self._queue),
        }

    def _format_item(self, item: QueueItem) -> dict:
        """Formate un item pour l'API."""
        return {
            "set_id": item.set_id,
            "set_name": item.set_name,
            "status": item.status.value,
            "added_at": item.added_at.isoformat() if item.added_at else None,
            "started_at": item.started_at.isoformat() if item.started_at else None,
            "finished_at": item.finished_at.isoformat() if item.finished_at else None,
            "cards_targeted": item.cards_targeted,
            "cards_succeeded": item.cards_succeeded,
            "cards_failed": item.cards_failed,
            "error": item.error,
        }

    def stop(self):
        """Demande l'arret de la queue."""
        self._stop_requested.set()
        # Arreter aussi le batch en cours
        from .runner import request_stop
        request_stop()

    def clear_pending(self):
        """Supprime les items en attente."""
        self._queue = [i for i in self._queue if i.status != QueueItemStatus.PENDING]

    def clear_completed(self):
        """Supprime les items termines de la liste."""
        self._queue = [i for i in self._queue
                       if i.status not in (QueueItemStatus.COMPLETED, QueueItemStatus.FAILED, QueueItemStatus.CANCELLED)]

    def _ensure_worker_running(self):
        """Demarre le worker s'il n'est pas deja actif."""
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return

        self._stop_requested.clear()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()

    def _worker_loop(self):
        """Boucle principale du worker."""
        from .runner import BatchRunner, clear_stop
        from ..models import BatchMode

        while not self._stop_requested.is_set():
            # Trouver le prochain item en attente
            next_item = None
            for item in self._queue:
                if item.status == QueueItemStatus.PENDING:
                    next_item = item
                    break

            if next_item is None:
                # Plus rien a traiter
                break

            # Traiter cet item
            self._current = next_item
            next_item.status = QueueItemStatus.RUNNING
            next_item.started_at = datetime.utcnow()

            try:
                # Reinitialiser le flag d'arret pour ce batch
                clear_stop()

                # Callback pour mettre a jour la progression en temps reel
                def progress_callback(processed: int, total: int, succeeded: int = 0, failed: int = 0):
                    next_item.cards_targeted = total
                    next_item.cards_succeeded = succeeded
                    next_item.cards_failed = failed

                runner = BatchRunner()
                stats, _ = runner.run(
                    mode=BatchMode.FULL_EBAY,
                    set_id=next_item.set_id,
                    progress_callback=progress_callback,
                )

                next_item.cards_targeted = stats.total_cards
                next_item.cards_succeeded = stats.succeeded
                next_item.cards_failed = stats.failed
                next_item.status = QueueItemStatus.COMPLETED

            except Exception as e:
                next_item.status = QueueItemStatus.FAILED
                next_item.error = str(e)

            finally:
                next_item.finished_at = datetime.utcnow()
                self._current = None

        # Marquer les items restants comme annules si arret demande
        if self._stop_requested.is_set():
            for item in self._queue:
                if item.status == QueueItemStatus.PENDING:
                    item.status = QueueItemStatus.CANCELLED


# Instance globale
_queue = BatchQueue()


def get_queue() -> BatchQueue:
    """Retourne l'instance de la queue."""
    return _queue
