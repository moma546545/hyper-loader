import logging
import threading
from core.qt_compat import QObject, QTimer
from core.constants import PROGRESS_BUS_DRAIN_MS
from core.qt_dispatch import run_on_qt_main_thread

logger = logging.getLogger("SnapDownloader.ProgressBus")


class ThrottledProgressBus(QObject):
    """
    A thread-safe accumulator that drains progress updates at a controlled
    configurable interval, reducing GUI event-loop traffic and stutters.
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self._lock = threading.Lock()
        self._updates = {}  # wid -> (progress, speed, eta)
        self._timer = QTimer(self)
        self._timer.setInterval(PROGRESS_BUS_DRAIN_MS)
        self._timer.timeout.connect(self._drain)
        self._timer_is_active = False

    def post(self, wid, progress, speed, eta):
        with self._lock:
            self._updates[wid] = (progress, speed, eta)
            if not self._timer_is_active:
                self._timer_is_active = True
                run_on_qt_main_thread(self._start_timer)

    def _start_timer(self):
        try:
            self._timer.start()
        except Exception as exc:
            logger.debug(f"Failed to start progress bus timer: {exc}")

    def _stop_timer(self):
        try:
            self._timer.stop()
        except Exception as exc:
            logger.debug(f"Failed to stop progress bus timer: {exc}")

    def _drain(self):
        # Triggered by QTimer.timeout, runs on the main GUI thread.
        with self._lock:
            if not self._updates:
                self._timer.stop()
                self._timer_is_active = False
                return
            updates = dict(self._updates)
            self._updates.clear()

        has_active_view_downloads = (
            getattr(self.window, "active_view", None) == "downloads"
            and getattr(self.window, "downloads_filter", None) in {"active", "queued", "scheduled"}
        )

        for wid, (progress, speed, eta) in updates.items():
            try:
                # 1. Update the queue task (emits progress_updated -> _on_queue_progress_updated)
                self.window.queue_manager.set_task_progress(wid, progress, speed, eta)

                # 2. Update search view and mini window
                controller = getattr(self.window, "download_controller", None)
                if controller is not None and hasattr(controller, "_process_download_progress"):
                    controller._process_download_progress(wid, progress, speed, eta)
            except Exception as exc:
                logger.debug(f"Error draining progress for worker {wid}: {exc}")

        if has_active_view_downloads:
            try:
                controller = getattr(self.window, "download_controller", None)
                if controller is not None and hasattr(controller, "schedule_downloads_refresh"):
                    controller.schedule_downloads_refresh(80)
            except Exception as exc:
                logger.debug(f"Error scheduling downloads refresh during drain: {exc}")

    def shutdown(self):
        run_on_qt_main_thread(self._stop_timer)
        with self._lock:
            self._updates.clear()
            self._timer_is_active = False
