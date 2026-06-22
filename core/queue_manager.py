
"""
core/queue_manager.py - Manages the download queue state and logic.

This class is responsible for adding, removing, and processing tasks in the download queue.
It is designed to be decoupled from the UI, communicating state changes via Qt signals.

C-01 FIX: All access to self.items is now protected by threading.Lock to prevent
race conditions between the UI thread and download worker threads.
"""
import logging
import os
import threading
import time
import copy
import urllib.parse
from uuid import uuid4
from .task_types import DownloadTask, TaskStatus
from .utils import redact_url

try:
    from PySide6.QtCore import QObject, Signal, QTimer
except ImportError:
    from PyQt6.QtCore import QObject, pyqtSignal as Signal, QTimer

logger = logging.getLogger("SnapDownloader.QueueManager")
_ACTIVE_TASK_STATUSES = {
    TaskStatus.DOWNLOADING.value,
    TaskStatus.PROCESSING.value,
    TaskStatus.MERGING.value,
    TaskStatus.RUNNING.value,
}
_PENDING_TASK_STATUSES = {TaskStatus.PENDING.value, TaskStatus.QUEUED.value, ""}
_QUEUED_VIEW_STATUSES = {
    TaskStatus.PENDING.value,
    TaskStatus.PAUSED.value,
    TaskStatus.QUEUED.value,
    "waiting",
}


def _safe_task_label(task: dict) -> str:
    title = str((task or {}).get("title", "") or "").strip()
    url = str((task or {}).get("url", "") or "").strip()
    if title.startswith("http://") or title.startswith("https://"):
        return redact_url(title)
    if title:
        return title[:120]
    return redact_url(url)


def _ensure_task_identity(task: dict) -> dict:
    if not isinstance(task, dict):
        return task
    task_uuid = str(task.get("task_uuid", "") or "").strip()
    if not task_uuid:
        task["task_uuid"] = str(uuid4())
    return task


def _ensure_unique_task_identity(task: dict, seen_task_uuids: set[str] | None = None) -> dict:
    if not isinstance(task, dict):
        return task
    _ensure_task_identity(task)
    if seen_task_uuids is None:
        return task
    task_uuid = str(task.get("task_uuid", "") or "").strip()
    if not task_uuid or task_uuid in seen_task_uuids:
        task_uuid = str(uuid4())
        task["task_uuid"] = task_uuid
    seen_task_uuids.add(task_uuid)
    return task


def _normalized_domain(url: str) -> str:
    try:
        domain = urllib.parse.urlparse(str(url or "")).netloc.lower().strip()
    except Exception:
        return ""
    if domain.startswith("www."):
        domain = domain[4:]
    if domain in {"youtu.be", "youtube.com", "m.youtube.com", "music.youtube.com"}:
        return "youtube"
    return domain


def _normalize_media_mode(value: str) -> str:
    token = str(value or "").strip().lower()
    if token in {"audio", "sound", "صوت"}:
        return "audio"
    return "video"


def _domain_concurrency_limit(domain: str) -> int:
    normalized = str(domain or "").lower()
    if normalized == "youtube" or "youtube.com" in normalized or "youtu.be" in normalized:
        return 2
    return 4


def _smart_priority_key(item: dict, idx: int, task_size_getter, now_ts: float) -> tuple:
    if not isinstance(item, dict):
        return (1, 1, 1, 1, idx)
    status = str(item.get("status", TaskStatus.PENDING.value) or TaskStatus.PENDING.value).lower()
    retry_count = max(0, int(item.get("retry_count", 0) or 0))
    try:
        scheduled_at = float(item.get("scheduled_at", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        scheduled_at = 0.0
    priority_boost = max(0, min(10, int(item.get("priority_boost", 0) or 0)))
    size_bytes = 0
    if callable(task_size_getter):
        try:
            size_bytes = max(0, int(task_size_getter(dict(item)) or 0))
        except Exception:
            size_bytes = 0
    due_now = 0 if scheduled_at <= 0 or scheduled_at <= now_ts else 1
    retry_penalty = 0 if retry_count == 0 else 1
    waiting_penalty = 0 if status in _PENDING_TASK_STATUSES else 1
    return (
        waiting_penalty,
        due_now,
        -priority_boost,
        retry_penalty,
        retry_count,
        size_bytes,
        idx,
    )


class QueueManager(QObject):
    # Signals to notify the UI about changes
    queue_changed = Signal()  # Emitted when items are added, removed, or reordered
    queue_started = Signal()
    queue_stopped = Signal()
    queue_paused = Signal()
    queue_resumed = Signal()
    queue_limit_exceeded = Signal(int)  # H-07: emitted with number of rejected items
    progress_updated = Signal(int, float, str, str)
    scheduled_tasks_due = Signal()
    
    # Signal to request a new download worker to be started by the main window
    start_worker_requested = Signal(dict, int) # task, queue_index

    try:
        _env_queue_limit = int(os.getenv("SNAPDOWNLOADER_MAX_QUEUE_SIZE", "50000") or 50000)
    except Exception:
        _env_queue_limit = 50000
    MAX_QUEUE_SIZE = max(1000, min(1000000, _env_queue_limit))  # H-07: Enterprise-grade queue size
    try:
        _env_db_page_threshold = int(os.getenv("SNAPDOWNLOADER_DB_PAGE_THRESHOLD", "8000") or 8000)
    except Exception:
        _env_db_page_threshold = 8000
    DB_PAGE_THRESHOLD = max(0, int(_env_db_page_threshold))
    try:
        _env_db_page_cache_ttl = float(os.getenv("SNAPDOWNLOADER_DB_PAGE_CACHE_TTL_SECONDS", "0.8") or 0.8)
    except Exception:
        _env_db_page_cache_ttl = 0.8
    DB_PAGE_CACHE_TTL_SECONDS = max(0.0, min(5.0, float(_env_db_page_cache_ttl)))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[DownloadTask] = []
        self._lock = threading.RLock()  # Re-entrant for internal helpers during queue planning
        self._change_token = 0
        self._db_page_cache: dict[tuple, tuple[float, dict]] = {}
        self._next_scheduled_at_hint: float | None = None
        self.is_running = False
        self.is_paused = False
        self.current_index = -1

        # Scheduler for time-based downloads
        self._schedule_timer = QTimer(self)
        self._schedule_timer.setInterval(30 * 1000)  # Check every 30 seconds
        self._schedule_timer.timeout.connect(self._check_scheduled_tasks)
        self._schedule_timer.start()

    def _bump_change_token(self):
        self._change_token += 1
        self._invalidate_db_page_cache_locked()
        # Any structural/task-state change can affect scheduled readiness.
        self._next_scheduled_at_hint = None

    def _invalidate_db_page_cache_locked(self):
        self._db_page_cache.clear()

    @staticmethod
    def _clone_page_payload(payload: dict) -> dict:
        if not isinstance(payload, dict):
            return {}
        cloned = dict(payload)
        cloned["entries"] = [
            dict(item) if isinstance(item, dict) else item
            for item in list(payload.get("entries", []) or [])
        ]
        return cloned

    def _build_db_page_cache_key(
        self,
        *,
        view_key: str,
        state_filter: str,
        media_filter: str,
        query: str,
        page_num: int,
        per_page: int,
        now_ts: float,
    ) -> tuple:
        now_bucket = int(float(now_ts or time.time())) if view_key in {"queued", "scheduled"} else 0
        return (
            str(view_key or "").strip().lower(),
            str(state_filter or "all").strip().lower(),
            str(media_filter or "all").strip().lower(),
            str(query or "").strip().lower(),
            int(page_num),
            int(per_page),
            int(now_bucket),
        )

    def get_change_token(self) -> int:
        with self._lock:
            return int(self._change_token)

    def add_task(self, task: DownloadTask):
        """Adds a new task to the end of the queue. Rejects if MAX_QUEUE_SIZE exceeded."""
        rejected = False
        # Ensure we have a clean copy of the task
        task_copy = DownloadTask.from_mapping(task if isinstance(task, dict) else {})
        with self._lock:
            seen_task_uuids = {
                str(item.get("task_uuid", "") or "").strip()
                for item in self.items
                if isinstance(item, dict) and str(item.get("task_uuid", "") or "").strip()
            }
            _ensure_unique_task_identity(task_copy, seen_task_uuids)
            if len(self.items) >= self.MAX_QUEUE_SIZE:
                logger.warning(f"[H-07] Queue full ({self.MAX_QUEUE_SIZE} items). Task rejected: {_safe_task_label(task_copy)}")
                rejected = True
            else:
                logger.info(f"Adding task to queue: {_safe_task_label(task_copy)}")
                self.items.append(task_copy)
                idx = len(self.items) - 1
                self._bump_change_token()
        if rejected:
            self.queue_limit_exceeded.emit(1)
            return -1
        self.queue_changed.emit()
        return idx

    def add_tasks(self, tasks: list[DownloadTask]):
        """Adds a list of tasks to the queue. Enforces MAX_QUEUE_SIZE."""
        tasks_copies = [DownloadTask.from_mapping(t if isinstance(t, dict) else {}) for t in tasks]
        with self._lock:
            seen_task_uuids = {
                str(item.get("task_uuid", "") or "").strip()
                for item in self.items
                if isinstance(item, dict) and str(item.get("task_uuid", "") or "").strip()
            }
            for task_copy in tasks_copies:
                _ensure_unique_task_identity(task_copy, seen_task_uuids)
            available = self.MAX_QUEUE_SIZE - len(self.items)
            accepted = tasks_copies[:available]
            rejected = len(tasks_copies) - len(accepted)
            if rejected > 0:
                logger.warning(f"[H-07] Queue capacity: accepted {len(accepted)}, rejected {rejected} tasks.")
            self.items.extend(accepted)
            if accepted:
                self._bump_change_token()
        if rejected > 0:
            self.queue_limit_exceeded.emit(rejected)
        logger.info(f"Adding {len(accepted)} tasks to queue.")
        self.queue_changed.emit()

    def start_queue(self):
        """Starts processing the queue."""
        should_start = False
        with self._lock:
            if self.is_running or not self.items:
                return
            logger.info("Starting queue processing.")
            self.is_running = True
            self.is_paused = False
            self.current_index = -1
            should_start = True
        if should_start:
            try:
                from core.system_keepalive import SystemKeepAlive
                SystemKeepAlive.prevent_sleep()
            except ImportError:
                pass
            self.queue_started.emit()
            self._dispatch_initial_ready_task()

    def restore_stale_running_tasks(self, active_worker_ids: set[int] | None = None) -> int:
        """
        Reset queue items left in a running state when no live worker owns them.
        Returns the number of restored tasks.
        """
        normalized_active_ids = {
            int(idx)
            for idx in (active_worker_ids or set())
            if isinstance(idx, int) and int(idx) >= 0
        }
        restored = 0
        with self._lock:
            for idx, item in enumerate(self.items):
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status", TaskStatus.PENDING.value) or TaskStatus.PENDING.value).lower()
                if status != TaskStatus.RUNNING.value or idx in normalized_active_ids:
                    continue
                item["status"] = TaskStatus.PENDING.value
                item["next_retry_at"] = 0
                restored += 1
            if restored > 0:
                self._bump_change_token()
        if restored > 0:
            self.queue_changed.emit()
        return restored

    def stop_queue(self):
        """Stops the queue and resets its state."""
        should_emit = False
        with self._lock:
            if not self.is_running:
                return
            logger.info("Stopping queue processing.")
            self.is_running = False
            self.is_paused = False
            self.current_index = -1
            should_emit = True
        if should_emit:
            try:
                from core.system_keepalive import SystemKeepAlive
                SystemKeepAlive.allow_sleep(force=True)
            except ImportError:
                pass
            self.queue_stopped.emit()

    def pause_queue(self):
        """Pauses the queue."""
        should_emit = False
        with self._lock:
            if not self.is_running or self.is_paused:
                return
            logger.info("Pausing queue.")
            self.is_paused = True
            should_emit = True
        if should_emit:
            self.queue_paused.emit()

    def resume_queue(self):
        """Resumes a paused queue."""
        should_resume = False
        with self._lock:
            if not self.is_running or not self.is_paused:
                return
            logger.info("Resuming queue.")
            self.is_paused = False
            should_resume = True
        if should_resume:
            self.queue_resumed.emit()
            self._dispatch_initial_ready_task()

    def _dispatch_initial_ready_task(self) -> None:
        """
        Kick off one ready task immediately when the queue enters the running
        state so QueueManager remains usable outside the window controller.
        The main download controller still expands dispatching to the configured
        parallelism right after start/resume in the full app.
        """
        try:
            self.dispatch_parallel_ready_tasks(1, include_queue_items=False, priority="smart")
        except Exception as exc:
            logger.debug(f"تعذر dispatch أولي للطابور: {exc}")

    def set_runtime_state(self, *, is_running: bool | None = None, is_paused: bool | None = None):
        """Synchronize orchestration state without triggering worker scheduling."""
        with self._lock:
            if is_running is not None:
                self.is_running = bool(is_running)
                if not self.is_running:
                    self.current_index = -1
            if is_paused is not None:
                requested_paused = bool(is_paused)
                self.is_paused = requested_paused if self.is_running else False

    def plan_parallel_start(
        self,
        max_tasks: int,
        *,
        active_worker_ids: set[int] | None = None,
        priority: str = "fifo",
        task_size_getter=None,
        now_ts: float | None = None,
        include_queue_items: bool = True,
    ) -> dict:
        """
        Build a single scheduling plan used by both the UI controller and the
        internal queue scheduler so domain throttling and readiness checks live
        in one place.
        """
        try:
            limit = max(0, int(max_tasks or 0))
        except (TypeError, ValueError, OverflowError):
            limit = 0
        normalized_active_ids = {
            int(idx)
            for idx in (active_worker_ids or set())
            if isinstance(idx, int) and int(idx) >= 0
        }
        needs_full_snapshot = bool(include_queue_items) or (
            priority == "smallest_first" and callable(task_size_getter)
        )
        with self._lock:
            if needs_full_snapshot:
                items_snapshot = [dict(item) if isinstance(item, dict) else item for item in self.items]
                item_records = []
            else:
                items_snapshot = []
                item_records = []
                for item in self.items:
                    if not isinstance(item, dict):
                        item_records.append(None)
                        continue
                    try:
                        scheduled_at = float(item.get("scheduled_at", 0) or 0)
                    except (TypeError, ValueError, OverflowError):
                        scheduled_at = 0.0
                    try:
                        retry_at = float(item.get("next_retry_at", 0) or 0)
                    except (TypeError, ValueError, OverflowError):
                        retry_at = 0.0
                    item_records.append(
                        (
                            str(item.get("status", TaskStatus.PENDING.value) or TaskStatus.PENDING.value).lower(),
                            str(item.get("url", "") or ""),
                            scheduled_at,
                            retry_at,
                        )
                    )
        now = float(now_ts if now_ts is not None else time.time())
        active_domains: dict[str, int] = {}
        next_retry_ts = None
        ready_indices: list[int] = []
        status_counts = {
            TaskStatus.PENDING.value: 0,
            TaskStatus.PAUSED.value: 0,
            TaskStatus.CANCELLED.value: 0,
            TaskStatus.FAILED.value: 0,
            TaskStatus.RUNNING.value: 0,
            "other": 0,
        }
        external_active_tracking = active_worker_ids is not None
        active_count = len(normalized_active_ids) if external_active_tracking else 0

        source_items = items_snapshot if needs_full_snapshot else item_records
        for idx, item in enumerate(source_items):
            if needs_full_snapshot:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status", TaskStatus.PENDING.value) or TaskStatus.PENDING.value).lower()
                url = str(item.get("url", "") or "")
            else:
                if item is None:
                    continue
                status, url, _scheduled_at, _retry_at = item
            if status in status_counts:
                status_counts[status] += 1
            else:
                status_counts["other"] += 1
            is_active = idx in normalized_active_ids or status in _ACTIVE_TASK_STATUSES
            if not is_active:
                continue
            if not external_active_tracking:
                active_count += 1
            domain = _normalized_domain(url)
            if domain:
                active_domains[domain] = active_domains.get(domain, 0) + 1

        for idx, item in enumerate(source_items):
            if needs_full_snapshot:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status", TaskStatus.PENDING.value) or TaskStatus.PENDING.value).lower()
                scheduled_at = float(item.get("scheduled_at", 0) or 0)
                retry_at = float(item.get("next_retry_at", 0) or 0)
            else:
                if item is None:
                    continue
                status, _url, scheduled_at, retry_at = item
            if idx in normalized_active_ids:
                continue
            if status not in _PENDING_TASK_STATUSES:
                continue
            if scheduled_at > now:
                if next_retry_ts is None or scheduled_at < next_retry_ts:
                    next_retry_ts = scheduled_at
                continue
            if retry_at > now:
                if next_retry_ts is None or retry_at < next_retry_ts:
                    next_retry_ts = retry_at
                continue
            ready_indices.append(idx)

        if needs_full_snapshot and len(ready_indices) > 1:
            if priority == "smallest_first" and callable(task_size_getter):
                def _estimate(idx: int):
                    try:
                        return int(task_size_getter(dict(items_snapshot[idx])) or 0)
                    except Exception:
                        return 0
                ready_indices.sort(key=lambda idx: (_estimate(idx), idx))
            elif priority == "smart":
                ready_indices.sort(
                    key=lambda idx: _smart_priority_key(
                        dict(items_snapshot[idx]),
                        idx,
                        task_size_getter,
                        now,
                    )
                )

        pending_indices: list[int] = []
        domain_usage = dict(active_domains)
        for idx in ready_indices:
            if len(pending_indices) >= limit:
                break
            if needs_full_snapshot:
                item = items_snapshot[idx]
                domain = _normalized_domain(item.get("url", "")) if isinstance(item, dict) else ""
            else:
                item = item_records[idx]
                domain = _normalized_domain(item[1]) if item is not None else ""
            if domain and domain_usage.get(domain, 0) >= _domain_concurrency_limit(domain):
                continue
            pending_indices.append(idx)
            if domain:
                domain_usage[domain] = domain_usage.get(domain, 0) + 1

        result = {
            "pending_indices": pending_indices,
            "next_retry_ts": next_retry_ts,
            "active_count": active_count,
            "status_counts": status_counts,
        }
        if include_queue_items:
            result["queue_items"] = items_snapshot
        return result

    def dispatch_parallel_ready_tasks(
        self,
        max_tasks: int,
        *,
        active_worker_ids: set[int] | None = None,
        priority: str = "fifo",
        task_size_getter=None,
        now_ts: float | None = None,
        include_queue_items: bool = True,
    ) -> dict:
        """
        Reserve ready tasks for execution and emit `start_worker_requested` for
        each selected queue item. This keeps worker handoff in the queue layer.
        """
        plan = self.plan_parallel_start(
            max_tasks,
            active_worker_ids=active_worker_ids,
            priority=priority,
            task_size_getter=task_size_getter,
            now_ts=now_ts,
            include_queue_items=include_queue_items,
        )
        pending_indices = list(plan.get("pending_indices", []) or [])
        tasks_to_emit: list[tuple[dict, int]] = []
        state_changed = False
        with self._lock:
            for idx in pending_indices:
                if not (0 <= idx < len(self.items)):
                    continue
                task = self.items[idx]
                if not isinstance(task, dict):
                    continue
                status = str(task.get("status", TaskStatus.PENDING.value) or TaskStatus.PENDING.value).lower()
                if status not in _PENDING_TASK_STATUSES:
                    continue
                if float(task.get("scheduled_at", 0) or 0) > 0:
                    task["scheduled_at"] = 0
                task["status"] = TaskStatus.RUNNING.value
                tasks_to_emit.append((copy.deepcopy(task), idx))
                state_changed = True
            if state_changed:
                self._bump_change_token()
        if state_changed:
            self.queue_changed.emit()
        for task, idx in tasks_to_emit:
            self.start_worker_requested.emit(task, idx)
        dispatched_plan = dict(plan)
        dispatched_plan["dispatched_indices"] = [idx for _task, idx in tasks_to_emit]
        return dispatched_plan

    def get_queue_items(self) -> list[dict]:
        # Preserve the legacy contract: callers of get_queue_items() receive an
        # isolated copy that they can mutate safely.
        with self._lock:
            return copy.deepcopy(self.items)

    def get_queue_items_snapshot(self) -> list[dict]:
        """Fast read-only snapshot: shallow-copy each task to reduce deepcopy overhead."""
        with self._lock:
            return [dict(item) if isinstance(item, dict) else item for item in self.items]

    def get_item_count(self) -> int:
        with self._lock:
            return len(self.items)

    @staticmethod
    def _matches_downloads_query(task: dict, query: str) -> bool:
        q = str(query or "").strip().lower()
        if not q:
            return True
        title = str((task or {}).get("title", "")).lower()
        url = str((task or {}).get("url", "")).lower()
        return q in title or q in url

    def get_download_entries_page(
        self,
        *,
        view: str,
        now_ts: float,
        queue_state_filter: str = "all",
        media_filter: str = "all",
        query: str = "",
        page: int = 1,
        page_size: int = 200,
    ) -> dict:
        view_key = str(view or "").strip().lower()
        state_filter = str(queue_state_filter or "all").strip().lower()
        media_filter_key = str(media_filter or "all").strip().lower()
        try:
            page_num = max(1, int(page or 1))
        except (TypeError, ValueError, OverflowError):
            page_num = 1
        try:
            per_page = max(1, int(page_size or 200))
        except (TypeError, ValueError, OverflowError):
            per_page = 200

        # For very large queues, prefer SQLite pagination when possible to avoid
        # repeated full Python scans on every Downloads view refresh.
        use_db_page = self.DB_PAGE_THRESHOLD > 0
        if use_db_page:
            cache_key = self._build_db_page_cache_key(
                view_key=view_key,
                state_filter=state_filter,
                media_filter=media_filter_key,
                query=query,
                page_num=page_num,
                per_page=per_page,
                now_ts=now_ts,
            )
            with self._lock:
                cached_entry = self._db_page_cache.get(cache_key)
                if cached_entry is not None:
                    expires_at, payload = cached_entry
                    if float(expires_at or 0.0) > float(time.monotonic()):
                        return self._clone_page_payload(payload)
                    self._db_page_cache.pop(cache_key, None)
            with self._lock:
                item_count = len(self.items)
            if item_count >= self.DB_PAGE_THRESHOLD:
                try:
                    from core.database import fetch_queue_entries_page_from_db
                    payload = fetch_queue_entries_page_from_db(
                        view=view_key,
                        now_ts=now_ts,
                        queue_state_filter=state_filter,
                        media_filter=media_filter_key,
                        query=query,
                        page=page_num,
                        page_size=per_page,
                    )
                    ttl = float(getattr(self, "DB_PAGE_CACHE_TTL_SECONDS", self.DB_PAGE_CACHE_TTL_SECONDS) or 0.0)
                    if ttl > 0:
                        with self._lock:
                            self._db_page_cache[cache_key] = (
                                float(time.monotonic()) + ttl,
                                self._clone_page_payload(payload),
                            )
                    return payload
                except Exception as exc:
                    logger.debug(f"[QueueManager] SQLite page fallback to memory mode: {exc}")

        matched_count = 0
        summary_total = 0
        summary_video = 0
        summary_audio = 0
        entries: list[dict] = []
        start_idx = (page_num - 1) * per_page
        end_idx = start_idx + per_page
        now_value = float(now_ts or time.time())

        with self._lock:
            for idx, item in enumerate(self.items):
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status", TaskStatus.PENDING.value) or TaskStatus.PENDING.value).lower()
                scheduled_at = float(item.get("scheduled_at", 0) or 0)
                include = False
                status_for_view = status

                if view_key == "active":
                    include = status == TaskStatus.RUNNING.value
                elif view_key == "queued":
                    include = status in _QUEUED_VIEW_STATUSES
                    if include and scheduled_at > now_value and status == TaskStatus.PENDING.value:
                        include = False
                elif view_key == "scheduled":
                    include = scheduled_at > now_value and status == TaskStatus.PENDING.value
                    if include:
                        status_for_view = "scheduled"
                else:
                    include = True

                if not include:
                    continue

                if state_filter != "all":
                    if state_filter == "pending":
                        include = status_for_view in {"pending", "scheduled"}
                    else:
                        include = status_for_view == state_filter
                    if not include:
                        continue

                if not self._matches_downloads_query(item, query):
                    continue

                item_media_mode = _normalize_media_mode(item.get("mode", "video"))
                summary_total += 1
                if item_media_mode == "audio":
                    summary_audio += 1
                else:
                    summary_video += 1

                if media_filter_key != "all" and item_media_mode != media_filter_key:
                    continue

                if matched_count >= start_idx and matched_count < end_idx:
                    entry = dict(item, queue_index=idx)
                    if status_for_view != status:
                        entry["status"] = status_for_view
                    entries.append(entry)
                matched_count += 1

        total_pages = max(1, (matched_count + per_page - 1) // per_page)
        return {
            "entries": entries,
            "total_matches": matched_count,
            "total_pages": total_pages,
            "page": min(page_num, total_pages),
            "page_size": per_page,
            "media_counts": {
                "all": summary_total,
                "video": summary_video,
                "audio": summary_audio,
            },
        }

    def get_dashboard_queue_counts(self) -> dict:
        active = 0
        queued = 0
        with self._lock:
            for item in self.items:
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status", "") or "").lower()
                if status == TaskStatus.RUNNING.value:
                    active += 1
                if status in _QUEUED_VIEW_STATUSES:
                    queued += 1
        return {"active": active, "queued": queued}

    def get_task(self, index: int) -> DownloadTask | None:
        if index is None:
            return None
        with self._lock:
            if 0 <= index < len(self.items):
                return copy.deepcopy(self.items[index])
        return None

    def set_task_status(self, index: int, status: str):
        if index is None:
            return
        updated = False
        with self._lock:
            if 0 <= index < len(self.items):
                self.items[index]["status"] = status
                self._bump_change_token()
                updated = True
        if updated:
            self.queue_changed.emit()

    def update_task_fields(self, index: int, fields: dict, emit_changed: bool = True) -> bool:
        if not isinstance(fields, dict) or not fields:
            return False
        # Deepcopy fields to prevent external references from entering internal state
        fields_copy = copy.deepcopy(fields)
        updated = False
        with self._lock:
            if 0 <= index < len(self.items):
                self.items[index].update(fields_copy)
                updated = True
                self._bump_change_token()
        if updated and emit_changed:
            self.queue_changed.emit()
        return updated

    def set_task_progress(self, index: int, progress: float, speed: str, eta: str):
        updated = False
        with self._lock:
            if 0 <= index < len(self.items):
                self.items[index]["progress"] = progress
                self.items[index]["speed"] = speed
                self.items[index]["eta"] = eta
                self._invalidate_db_page_cache_locked()
                updated = True
        if updated:
            self.progress_updated.emit(int(index), float(progress or 0.0), str(speed or ""), str(eta or ""))

    def move_task(self, from_index: int, to_index: int) -> bool:
        with self._lock:
            if self.is_running and not self.is_paused:
                return False
            if not (0 <= from_index < len(self.items)):
                return False
            if not (0 <= to_index < len(self.items)):
                return False
            if from_index == to_index:
                return False
            item = self.items.pop(from_index)
            self.items.insert(to_index, item)
            if self.is_running and self.is_paused:
                self.current_index = max(-1, min(self.current_index, len(self.items) - 1))
            self._bump_change_token()
        self.queue_changed.emit()
        return True

    def clear_queue(self):
        """Removes all items from the queue."""
        logger.info("Clearing queue.")
        with self._lock:
            self.items.clear()
            self._bump_change_token()
        self.stop_queue()
        self.queue_changed.emit()

    def _check_scheduled_tasks(self):
        """
        Periodically checks if any scheduled tasks are due. If so, and the queue
        is idle, it requests the main queue orchestrator to start processing them.
        """
        should_notify = False
        with self._lock:
            if self.is_running or self.is_paused:
                return

            now = time.time()
            if self._next_scheduled_at_hint is not None and float(self._next_scheduled_at_hint) > now:
                return
            has_due_tasks = False
            next_scheduled_at = None
            for item in self.items:
                scheduled_at = item.get("scheduled_at", 0)
                try:
                    scheduled_at_value = float(scheduled_at or 0)
                except (TypeError, ValueError, OverflowError):
                    scheduled_at_value = 0.0
                if scheduled_at_value > 0 and (next_scheduled_at is None or scheduled_at_value < next_scheduled_at):
                    next_scheduled_at = scheduled_at_value
                if scheduled_at_value > 0 and scheduled_at_value <= now:
                    has_due_tasks = True
                    break
            self._next_scheduled_at_hint = next_scheduled_at
            
            if has_due_tasks:
                should_notify = True
        if should_notify:
            logger.info("[Scheduler] Due tasks found. Requesting queue start via main orchestrator.")
            self.scheduled_tasks_due.emit()



