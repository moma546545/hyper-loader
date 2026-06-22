import logging
import threading

from core.i18n import _
from core.qt_compat import QTimer
from core.queue_ai import SmartQueueAI

logger = logging.getLogger("SnapDownloader")


class QueueOptimizationController:
    def __init__(self, window):
        self.window = window

    def retry_all_failed_queue_items(self):
        if self.window._queue_is_running() or self.window._active_workers_count() > 0:
            self.window._warn(_("أوقف التحميلات أولاً قبل إعادة المحاولة الجماعية"))
            return
        items = self.window.queue_manager.get_queue_items_snapshot()
        changed = False
        for index, item in enumerate(items):
            status = str(item.get("status", "pending")).lower()
            if status not in {"failed", "cancelled", "paused"}:
                continue
            ok = self.window.queue_manager.update_task_fields(
                index,
                {"status": "pending", "retry_count": 0, "next_retry_at": 0},
                emit_changed=False,
            )
            changed = changed or ok
        if not changed:
            self.window._info(_("لا توجد عناصر فاشلة أو ملغاة لإعادة المحاولة"))
            return
        self.window.queue_manager.queue_changed.emit()
        self.window._save_session()
        self.window._set_queue_runtime_state(running=True, paused=False)
        self.window._refresh_downloads_list()
        self.window._process_parallel_queue()

    def auto_optimize_queue(self, silent: bool = False):
        if self.window._queue_is_running() or self.window._active_workers_count() > 0:
            if not silent:
                self.window._warn(_("أوقف التحميلات أولاً قبل تحسين ترتيب الطابور"))
            return
        items = self.window.queue_manager.get_queue_items_snapshot()
        if not items:
            if not silent:
                self.window._info(_("لا توجد عناصر في الطابور"))
            return
        if len(items) >= 300:
            if self.window._queue_optimize_in_progress:
                if not silent:
                    self.window._info(_("عملية تحسين الطابور قيد التنفيذ بالفعل"))
                return
            self.window._queue_optimize_in_progress = True
            self.window._queue_optimize_request_id += 1
            request_id = int(self.window._queue_optimize_request_id)
            source_snapshot = list(items)
            if not silent:
                self.window._info(_("جاري تحسين ترتيب الطابور في الخلفية..."))

            def _worker():
                try:
                    optimized_items = SmartQueueAI.optimize_queue(source_snapshot)
                except Exception as exc:
                    QTimer.singleShot(
                        0,
                        lambda err=exc, rid=request_id, is_silent=silent: self.on_auto_optimize_queue_failed(
                            rid,
                            err,
                            is_silent,
                        ),
                    )
                    return
                QTimer.singleShot(
                    0,
                    lambda src=source_snapshot, opt=optimized_items, rid=request_id, is_silent=silent: self.apply_auto_optimized_queue(
                        rid,
                        src,
                        opt,
                        is_silent,
                    ),
                )

            threading.Thread(target=_worker, daemon=True, name="QueueOptimizeWorker").start()
            return

        self.apply_auto_optimized_queue(0, list(items), SmartQueueAI.optimize_queue(items), silent)

    def on_auto_optimize_queue_failed(self, request_id: int, exc: Exception, silent: bool):
        if request_id and request_id != self.window._queue_optimize_request_id:
            return
        self.window._queue_optimize_in_progress = False
        logger.warning(f"[Queue AI] Optimization failed: {exc}")
        if not silent:
            self.window._warn(_("فشل تحسين ترتيب الطابور"))

    def apply_auto_optimized_queue(self, request_id: int, source_snapshot: list[dict], optimized: list[dict], silent: bool):
        try:
            if request_id and request_id != self.window._queue_optimize_request_id:
                return
            current = self.window.queue_manager.get_queue_items_snapshot()
            if current != source_snapshot:
                if not silent:
                    self.window._info(_("تغيّر الطابور أثناء التحسين، أعد المحاولة"))
                return
            if optimized == source_snapshot:
                if not silent:
                    self.window._info(_("الطابور بالفعل في أفضل ترتيب"))
                return
            self.window.queue_manager.clear_queue()
            self.window.queue_manager.add_tasks(optimized)
            self.window._save_session()
            self.window._refresh_downloads_list()
            if not silent:
                self.window._info(_("تم تحسين ترتيب الطابور تلقائياً"))
        finally:
            self.window._queue_optimize_in_progress = False
