import logging

try:
    from PySide6.QtCore import QTimer
except ImportError:
    from PyQt6.QtCore import QTimer


logger = logging.getLogger("SnapDownloader.PlaylistSyncScheduler")


class PlaylistSyncScheduler:
    def __init__(
        self,
        *,
        window,
        playlist_sync_service,
        has_active_process_callback,
        on_due_playlist_callback,
        poll_ms: int = 180_000,
        min_age_seconds: int = 600,
        fetch_limit: int = 10,
    ):
        self.window = window
        self.playlist_sync_service = playlist_sync_service
        self.has_active_process_callback = has_active_process_callback
        self.on_due_playlist_callback = on_due_playlist_callback
        self.poll_ms = max(10_000, int(poll_ms or 180_000))
        self.min_age_seconds = max(0, int(min_age_seconds or 0))
        self.fetch_limit = max(1, int(fetch_limit or 1))
        self._timer = None

    def start(self) -> None:
        if self._timer is None:
            self._timer = QTimer(self.window)
            self._timer.setSingleShot(False)
            self._timer.timeout.connect(self._poll_once)
        if not self._timer.isActive():
            self._timer.start(self.poll_ms)

    def stop(self) -> None:
        timer = self._timer
        if timer is None:
            return
        try:
            timer.stop()
        except Exception as exc:
            logger.debug(f"تعذر إيقاف playlist sync scheduler timer: {exc}")

    def _poll_once(self) -> None:
        try:
            if bool(self.has_active_process_callback()):
                return
        except Exception as exc:
            logger.debug(f"[PlaylistSyncScheduler] has_active_process_callback failed: {exc}")
            return
        if self.playlist_sync_service.has_inflight_syncs():
            return
        try:
            playlist_url = self.playlist_sync_service.acquire_due_playlist_for_sync(
                min_age_seconds=self.min_age_seconds,
                limit=self.fetch_limit,
            )
        except Exception as exc:
            logger.debug(f"[PlaylistSyncScheduler] acquire_due_playlist_for_sync failed: {exc}")
            return
        if not playlist_url:
            return
        try:
            self.on_due_playlist_callback(playlist_url)
        except Exception:
            self.playlist_sync_service.release_inflight_sync(playlist_url)
            raise
