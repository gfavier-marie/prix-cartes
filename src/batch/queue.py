"""
Queue de batchs pour execution parallele.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
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
    Queue de batchs avec execution parallele.

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
        self._running: list[QueueItem] = []  # Items en cours de traitement
        self._executor: Optional[ThreadPoolExecutor] = None
        self._max_workers: int = 1
        self._stop_requested = threading.Event()
        self._queue_lock = threading.Lock()
        self._initialized = True

    def set_max_workers(self, max_workers: int) -> None:
        """Definit le nombre max de workers paralleles."""
        self._max_workers = max(1, min(max_workers, 10))  # Entre 1 et 10

    @property
    def max_workers(self) -> int:
        """Retourne le nombre max de workers."""
        return self._max_workers

    def add(self, set_id: str, set_name: str) -> QueueItem:
        """Ajoute un set a la queue."""
        with self._queue_lock:
            # Verifier si deja dans la queue
            for item in self._queue:
                if item.set_id == set_id and item.status == QueueItemStatus.PENDING:
                    return item  # Deja en attente

            item = QueueItem(set_id=set_id, set_name=set_name)
            self._queue.append(item)

        # Demarrer les workers si pas deja en cours
        self._ensure_workers_running()

        return item

    def add_multiple(self, sets: list[dict], max_workers: int = 1) -> list[QueueItem]:
        """Ajoute plusieurs sets a la queue avec nombre de workers."""
        self.set_max_workers(max_workers)
        items = []
        for s in sets:
            item = self.add(s["set_id"], s["set_name"])
            items.append(item)
        return items

    def get_status(self) -> dict:
        """Retourne le statut de la queue."""
        with self._queue_lock:
            pending = [i for i in self._queue if i.status == QueueItemStatus.PENDING]
            running = [i for i in self._queue if i.status == QueueItemStatus.RUNNING]
            completed = [i for i in self._queue if i.status == QueueItemStatus.COMPLETED]
            failed = [i for i in self._queue if i.status == QueueItemStatus.FAILED]

        return {
            "running": len(running) > 0,
            "running_items": [self._format_item(i) for i in running],
            "running_count": len(running),
            "max_workers": self._max_workers,
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

        # Shutdown executor
        if self._executor:
            self._executor.shutdown(wait=False)
            self._executor = None

    def clear_pending(self):
        """Supprime les items en attente."""
        with self._queue_lock:
            self._queue = [i for i in self._queue if i.status != QueueItemStatus.PENDING]

    def clear_completed(self):
        """Supprime les items termines de la liste."""
        with self._queue_lock:
            self._queue = [i for i in self._queue
                           if i.status not in (QueueItemStatus.COMPLETED, QueueItemStatus.FAILED, QueueItemStatus.CANCELLED)]

    def _ensure_workers_running(self):
        """Demarre le pool de workers s'il n'est pas deja actif."""
        if self._executor is not None:
            return

        self._stop_requested.clear()
        self._executor = ThreadPoolExecutor(max_workers=self._max_workers)

        # Lancer le dispatcher dans un thread separe
        dispatcher_thread = threading.Thread(target=self._dispatcher_loop, daemon=True)
        dispatcher_thread.start()

    def _dispatcher_loop(self):
        """Dispatcher qui soumet les items aux workers."""
        import time

        while not self._stop_requested.is_set():
            # Compter les items en cours
            with self._queue_lock:
                running_count = sum(1 for i in self._queue if i.status == QueueItemStatus.RUNNING)
                pending_items = [i for i in self._queue if i.status == QueueItemStatus.PENDING]

            # Si on peut lancer plus de workers
            slots_available = self._max_workers - running_count

            if slots_available > 0 and pending_items:
                # Lancer autant de workers que possible
                for item in pending_items[:slots_available]:
                    with self._queue_lock:
                        if item.status == QueueItemStatus.PENDING:
                            item.status = QueueItemStatus.RUNNING
                            item.started_at = datetime.utcnow()
                            self._executor.submit(self._process_item, item)

            # Verifier si tout est termine
            with self._queue_lock:
                has_work = any(i.status in (QueueItemStatus.PENDING, QueueItemStatus.RUNNING) for i in self._queue)

            if not has_work:
                break

            # Attendre un peu avant de recheck
            time.sleep(0.5)

        # Cleanup executor
        if self._executor:
            self._executor.shutdown(wait=True)
            self._executor = None

        # Marquer les items restants comme annules si arret demande
        if self._stop_requested.is_set():
            with self._queue_lock:
                for item in self._queue:
                    if item.status == QueueItemStatus.PENDING:
                        item.status = QueueItemStatus.CANCELLED

    def _process_item(self, item: QueueItem):
        """Traite un item dans un thread du pool."""
        from .runner import BatchRunner
        from ..models import BatchMode

        try:
            # Callback pour mettre a jour la progression en temps reel
            def progress_callback(processed: int, total: int, succeeded: int = 0, failed: int = 0):
                item.cards_targeted = total
                item.cards_succeeded = succeeded
                item.cards_failed = failed

            runner = BatchRunner()
            stats, _ = runner.run(
                mode=BatchMode.FULL_EBAY,
                set_id=item.set_id,
                progress_callback=progress_callback,
            )

            item.cards_targeted = stats.total_cards
            item.cards_succeeded = stats.succeeded
            item.cards_failed = stats.failed

            # Marquer comme echoue si arrete pour echecs consecutifs
            if stats.stopped_consecutive_failures:
                item.status = QueueItemStatus.FAILED
                item.error = "10 echecs consecutifs"
            else:
                item.status = QueueItemStatus.COMPLETED

        except Exception as e:
            item.status = QueueItemStatus.FAILED
            item.error = str(e)

        finally:
            item.finished_at = datetime.utcnow()
            # Rafraichir les rate limits eBay
            try:
                from ..ebay.usage_tracker import refresh_rate_limits_from_ebay
                refresh_rate_limits_from_ebay()
            except Exception:
                pass


# Instance globale
_queue = BatchQueue()


def get_queue() -> BatchQueue:
    """Retourne l'instance de la queue."""
    return _queue
