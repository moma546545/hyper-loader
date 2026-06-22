from types import SimpleNamespace

from core.window_controllers.lifecycle_controller import LifecycleController


class _DummyThread:
    def __init__(self, alive=True):
        self.alive = bool(alive)
        self.join_calls = []

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.join_calls.append(timeout)
        self.alive = False


class _DummyTimer:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class _DummyWidget:
    def __init__(self):
        self.hidden = False
        self.deleted = False

    def hide(self):
        self.hidden = True

    def deleteLater(self):
        self.deleted = True


class _DummyWorker:
    def __init__(self, running=True):
        self.running = bool(running)
        self.stop_calls = 0
        self.wait_calls = []

    def stop(self):
        self.stop_calls += 1
        self.running = False

    def wait(self, timeout):
        self.wait_calls.append(timeout)

    def isRunning(self):
        return self.running


class _DummyAnalyzeWorker(_DummyWorker):
    def __init__(self, running=True):
        super().__init__(running=running)
        self.interruption_requests = 0

    def requestInterruption(self):
        self.interruption_requests += 1

    def quit(self):
        self.running = False


class _DummyLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _DummyTrayManager:
    def __init__(self):
        self.cleaned = False

    def cleanup(self):
        self.cleaned = True


class _DummyController:
    def __init__(self):
        self.shutdown_calls = 0

    def shutdown(self):
        self.shutdown_calls += 1


def test_lifecycle_controller_close_event_uses_session_service(monkeypatch):
    extension_server_stop = []
    memory_guard_stop = []
    subscriptions_stop = []
    unsubscribed = []

    monkeypatch.setattr(
        "core.window_controllers.lifecycle_controller.memory_guard",
        SimpleNamespace(stop=lambda: memory_guard_stop.append(True)),
    )
    monkeypatch.setattr(
        "core.window_controllers.lifecycle_controller.event_bus",
        SimpleNamespace(unsubscribe=lambda event_type, callback: unsubscribed.append((event_type, callback))),
    )
    monkeypatch.setattr(
        "core.channel_subscriptions.subscription_manager",
        SimpleNamespace(stop=lambda: subscriptions_stop.append(True)),
    )

    session_load_thread = _DummyThread(alive=True)
    session_service = SimpleNamespace(
        stop_calls=0,
        _load_thread=session_load_thread,
    )

    def _session_service_stop():
        session_service.stop_calls += 1

    session_service.stop = _session_service_stop

    active_worker = _DummyWorker(running=True)
    analyze_worker = _DummyAnalyzeWorker(running=True)
    tray_manager = _DummyTrayManager()
    download_controller = _DummyController()
    update_controller = _DummyController()
    clip_watcher = _DummyWidget()
    mini_window = _DummyWidget()
    tray_icon = _DummyWidget()

    window = SimpleNamespace(
        extension_server=SimpleNamespace(stop=lambda: extension_server_stop.append(True)),
        clip_watcher=clip_watcher,
        _active_workers_lock=_DummyLock(),
        active_workers={1: active_worker},
        analyze_worker=analyze_worker,
        tray_manager=tray_manager,
        tray_icon=tray_icon,
        download_controller=download_controller,
        update_controller=update_controller,
        session_service=session_service,
        hotkey_manager=SimpleNamespace(stop=lambda: None),
        mini_window=mini_window,
        _on_show_notification_event=lambda *_args: None,
        _on_download_finished_event=lambda *_args: None,
        _save_session_calls=[],
    )

    for timer_name in (
        "trial_timer",
        "_scheduler_timer",
        "_storage_watchdog_timer",
        "toast_timer",
        "_visible_thumb_timer",
        "_thumbnail_cleanup_timer",
        "_settings_autosave_timer",
        "_search_history_save_timer",
        "_system_theme_timer",
        "_queue_ai_timer",
    ):
        setattr(window, timer_name, _DummyTimer())

    def _save_session(sync=False):
        window._save_session_calls.append(sync)

    window._save_session = _save_session

    controller = LifecycleController(window)
    controller.close_event()

    assert extension_server_stop == [True]
    assert subscriptions_stop == [True]
    assert memory_guard_stop == [True]
    assert clip_watcher.hidden is True
    assert mini_window.hidden is True
    assert mini_window.deleted is True
    assert active_worker.stop_calls == 1
    assert active_worker.wait_calls == [5000]
    assert analyze_worker.interruption_requests == 1
    assert analyze_worker.wait_calls == [2000]
    assert download_controller.shutdown_calls == 1
    assert update_controller.shutdown_calls == 1
    assert tray_manager.cleaned is True
    assert tray_icon.hidden is True
    assert window._save_session_calls == [True]
    assert session_service.stop_calls == 1
    assert session_load_thread.join_calls == [1.0]
    assert len(unsubscribed) == 2
    for timer_name in (
        "trial_timer",
        "_scheduler_timer",
        "_storage_watchdog_timer",
        "toast_timer",
        "_visible_thumb_timer",
        "_thumbnail_cleanup_timer",
        "_settings_autosave_timer",
        "_search_history_save_timer",
        "_system_theme_timer",
        "_queue_ai_timer",
    ):
        assert getattr(window, timer_name).stopped is True


def test_lifecycle_controller_session_shutdown_falls_back_to_legacy_threads():
    save_thread = _DummyThread(alive=True)
    load_thread = _DummyThread(alive=True)
    window = SimpleNamespace(
        _save_session_calls=[],
        _session_save_shutdown=False,
        _session_save_event=SimpleNamespace(set=lambda: None),
        _session_save_thread=save_thread,
        _session_load_thread=load_thread,
    )

    def _save_session(sync=False):
        window._save_session_calls.append(sync)

    window._save_session = _save_session

    controller = LifecycleController(window)
    controller._shutdown_session_threads()

    assert window._save_session_calls == [True]
    assert window._session_save_shutdown is True
    assert save_thread.join_calls == [2.0]
    assert load_thread.join_calls == [1.0]


def test_lifecycle_controller_close_event_is_idempotent(monkeypatch):
    memory_guard_stop = []
    subscriptions_stop = []
    monkeypatch.setattr(
        "core.window_controllers.lifecycle_controller.memory_guard",
        SimpleNamespace(stop=lambda: memory_guard_stop.append(True)),
    )
    monkeypatch.setattr(
        "core.window_controllers.lifecycle_controller.event_bus",
        SimpleNamespace(unsubscribe=lambda *_args: None),
    )
    monkeypatch.setattr(
        "core.channel_subscriptions.subscription_manager",
        SimpleNamespace(stop=lambda: subscriptions_stop.append(True)),
    )

    session_service = SimpleNamespace(stop=lambda: None, _load_thread=None)
    active_worker = _DummyWorker(running=True)
    analyze_worker = _DummyAnalyzeWorker(running=True)
    window = SimpleNamespace(
        extension_server=SimpleNamespace(stop=lambda: None),
        clip_watcher=None,
        _active_workers_lock=_DummyLock(),
        active_workers={1: active_worker},
        analyze_worker=analyze_worker,
        tray_manager=_DummyTrayManager(),
        tray_icon=_DummyWidget(),
        download_controller=_DummyController(),
        update_controller=_DummyController(),
        session_service=session_service,
        hotkey_manager=SimpleNamespace(stop=lambda: None),
        mini_window=None,
        _on_show_notification_event=lambda *_args: None,
        _on_download_finished_event=lambda *_args: None,
        _save_session=lambda sync=False: None,
    )

    controller = LifecycleController(window)
    controller.close_event()
    controller.close_event()

    assert memory_guard_stop == [True]
    assert subscriptions_stop == [True]
    assert active_worker.stop_calls == 1
    assert analyze_worker.interruption_requests == 1


def test_lifecycle_controller_shutdowns_bulk_analysis_controller():
    bulk_controller = _DummyController()
    window = SimpleNamespace(bulk_analysis_controller=bulk_controller)
    controller = LifecycleController(window)

    controller._shutdown_bulk_analysis_workers()

    assert bulk_controller.shutdown_calls == 1


def test_lifecycle_controller_falls_back_to_bulk_analysis_workers():
    worker = _DummyAnalyzeWorker(running=True)
    window = SimpleNamespace(
        bulk_analysis_controller=None,
        _bulk_analysis_workers={"url": worker},
    )
    controller = LifecycleController(window)

    controller._shutdown_bulk_analysis_workers()

    assert window._bulk_analysis_workers == {}
    assert worker.interruption_requests == 1
    assert worker.wait_calls == []
