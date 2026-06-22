from types import SimpleNamespace

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    from PyQt6.QtWidgets import QApplication

from core.playlist_sync_scheduler import PlaylistSyncScheduler


def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class _FakePlaylistSyncService:
    def __init__(self):
        self.inflight = set()
        self.acquired = []
        self.released = []
        self.next_due = []

    def has_inflight_syncs(self):
        return bool(self.inflight)

    def acquire_due_playlist_for_sync(self, **_kwargs):
        if not self.next_due:
            return ""
        url = str(self.next_due.pop(0))
        if url:
            self.inflight.add(url)
            self.acquired.append(url)
        return url

    def release_inflight_sync(self, playlist_url: str):
        url = str(playlist_url or "").strip()
        self.released.append(url)
        self.inflight.discard(url)


def test_scheduler_polls_and_dispatches_due_playlist():
    _ensure_qt_app()
    service = _FakePlaylistSyncService()
    service.next_due = ["https://youtube.com/playlist?list=PLsched"]
    dispatched = []
    scheduler = PlaylistSyncScheduler(
        window=SimpleNamespace(),
        playlist_sync_service=service,
        has_active_process_callback=lambda: False,
        on_due_playlist_callback=lambda url: dispatched.append(url),
        poll_ms=60_000,
    )

    scheduler._poll_once()

    assert dispatched == ["https://youtube.com/playlist?list=PLsched"]
    assert service.acquired == ["https://youtube.com/playlist?list=PLsched"]


def test_scheduler_releases_inflight_when_dispatch_callback_fails():
    _ensure_qt_app()
    service = _FakePlaylistSyncService()
    target = "https://youtube.com/playlist?list=PLboom"
    service.next_due = [target]
    scheduler = PlaylistSyncScheduler(
        window=SimpleNamespace(),
        playlist_sync_service=service,
        has_active_process_callback=lambda: False,
        on_due_playlist_callback=lambda _url: (_ for _ in ()).throw(RuntimeError("boom")),
        poll_ms=60_000,
    )

    try:
        scheduler._poll_once()
    except RuntimeError:
        pass

    assert target in service.acquired
    assert target in service.released
    assert target not in service.inflight
