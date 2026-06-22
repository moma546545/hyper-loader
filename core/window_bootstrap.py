from collections import OrderedDict, deque
import os
import threading

from .config import DEFAULT_SETTINGS, THEME_MODE_MAP
from .constants import AUDIO_FORMATS, AUDIO_QUALITIES, SUBTITLE_OPTIONS, VIDEO_FORMATS, VIDEO_QUALITIES
from .config_manager import ConfigManager
from .constants import (
    MAX_CONCURRENT_THUMBNAILS,
    THUMBNAIL_CLEANUP_INTERVAL_MS,
    THUMBNAIL_FAILED_MAX,
    THUMBNAIL_WAITER_TTL_SECONDS,
)
from .database import get_app_data_dir, load_session_settings
from .event_bus import DownloadFinishedEvent, ExtensionLinkReceivedEvent, ShowNotificationEvent, event_bus
from .extension_server import extension_server
from .hotkeys import setup_default_hotkeys
from .i18n import _, i18n
from .memory_guard import memory_guard
from .qt_compat import QNetworkAccessManager, QSystemTrayIcon, QStringListModel, QTimer
from .queue_manager import QueueManager
from .session_service import SessionService
from .thumbnail_manager import ThumbnailManager
from .progress_bus import ThrottledProgressBus
from .trial_manager import TrialManager
from .ui_controller import UIController
from .window_controllers import (
    AnalyzeController,
    DownloadController,
    DownloadsListController,
    DownloadsRenderController,
    ExtensionController,
    FileManagerController,
    HistoryDataController,
    HistoryPlaybackController,
    ImportController,
    LifecycleController,
    MediaToolsController,
    QueueOptimizationController,
    QueueTransferController,
    SettingsController,
    ThumbnailController,
    UpdateController,
)
from .window_controllers.tray_manager import TrayManager
from .bandwidth_scheduler import scheduler
from ui.clip_watcher import ClipWatcher
from ui.mini_mode import MiniModeWindow
from ui.themes import THEMES
from ui.widgets import WheelEventFilter
from .task_types import StatsState


def connect_bootstrap_signals(window) -> None:
    window.download_finished_ui.connect(window._handle_download_finished_event)
    window.bulk_import_finished_ui.connect(window._handle_bulk_import_result)
    window.session_loaded_ui.connect(window._handle_session_load_result)
    window.history_item_path_resolved_ui.connect(window._on_history_item_path_resolved)


def init_core_state(window) -> None:
    _init_navigation_and_queue_state(window)
    _init_config_backed_state(window)
    _init_downloads_browser_state(window)
    _init_trial_runtime_state(window)


def _init_navigation_and_queue_state(window) -> None:
    window.active_view = "search"
    window.preview_data = {}
    window.queue_manager = QueueManager(window)
    window.current_worker = None
    window.current_task = None
    window.analyze_worker = None
    window.progress_speed = "--"
    window.progress_eta = "--"
    window.progress_size = "--"


def _init_config_backed_state(window) -> None:
    window.config_manager = ConfigManager(filepath=os.path.join(get_app_data_dir(), "settings.json"))
    window.current_download_path = ""
    try:
        window.current_download_path = str(window.config_manager.get("save_path") or "").strip()
    except Exception:
        window.current_download_path = ""
    window.logs = []
    window.stats = StatsState(total_videos=0, total_audios=0, download_history=[])
    window.theme = "Modern Dark"
    window.ui_language = i18n.current_lang
    try:
        cfg_theme = str(window.config_manager.get("theme") or "").strip()
        if cfg_theme in THEMES:
            window.theme = cfg_theme
    except Exception:
        pass
    window.system_theme_sync_enabled = True
    window._last_system_theme_mode = None
    window.max_concurrent = int(DEFAULT_SETTINGS["max_concurrent"])
    try:
        window.max_concurrent = max(1, int(window.config_manager.get("max_concurrent") or window.max_concurrent))
    except Exception:
        pass
    window.storage_guard_enabled = bool(DEFAULT_SETTINGS["storage_guard_enabled"])
    window.storage_min_free_gb = max(1, int(DEFAULT_SETTINGS["storage_min_free_gb"]))
    window.bandwidth_scheduler_enabled = bool(scheduler.enabled)
    window._bandwidth_schedule_summary = scheduler.format_schedule_summary()
    window._editing_bandwidth_rule_index = None
    # Legacy compatibility: several controllers still read media constants from the window object.
    window.VIDEO_FORMATS = list(VIDEO_FORMATS)
    window.AUDIO_FORMATS = list(AUDIO_FORMATS)
    window.VIDEO_QUALITIES = list(VIDEO_QUALITIES)
    window.AUDIO_QUALITIES = list(AUDIO_QUALITIES)
    window.SUBTITLE_OPTIONS = list(SUBTITLE_OPTIONS)


def _init_downloads_browser_state(window) -> None:
    window.cookies_path = ""
    window.search_history = []
    window._search_history_limit = int(DEFAULT_SETTINGS["search_history_limit"])
    window.search_history_ttl_days = max(1, int(DEFAULT_SETTINGS["search_history_ttl_days"]))
    window.search_history_model = QStringListModel(window)
    window.active_workers = {}
    window._active_workers_lock = threading.RLock()
    window.queue_items = []
    window.downloads_filter = "completed"
    window.queue_state_filter = "all"
    window.downloads_sort = "Date (Newest)"
    window.downloads_page = 1
    window.downloads_page_size = 6


def _init_trial_runtime_state(window) -> None:
    window.trial_enabled = str(os.getenv("SNAPDOWNLOADER_ENABLE_TRIAL", "")).strip().lower() in {"1", "true", "yes", "on"}
    window.trial_manager = TrialManager(
        enabled=window.trial_enabled,
        total_days=int(DEFAULT_SETTINGS["trial_total_days"]),
        load_settings=load_session_settings,
    )
    window.trial_total_days = int(window.trial_manager.total_days)
    window.trial_started_at = ""
    window.trial_days_remaining = window.trial_total_days


def init_controllers_and_runtime(window) -> None:
    _init_controller_instances(window)
    _init_metadata_and_thumbnail_runtime(window)
    _init_downloads_render_runtime(window)
    _init_runtime_flags(window)


def _init_controller_instances(window) -> None:
    window.page_animation = None
    window.trial_timer = None
    window.toast_timer = None
    window.wheel_filter = WheelEventFilter(window)
    window.settings_controller = SettingsController(window)
    window.analyze_controller = AnalyzeController(window)
    window.download_controller = DownloadController(window)
    window.downloads_list_controller = DownloadsListController(window)
    window.downloads_render_controller = DownloadsRenderController(window)
    window.import_controller = ImportController(window)
    window.extension_controller = ExtensionController(window)
    window.history_data_controller = HistoryDataController(window)
    window.history_playback_controller = HistoryPlaybackController(window)
    window.file_manager_controller = FileManagerController(window)
    window.thumbnail_controller = ThumbnailController(window)
    window.media_tools_controller = MediaToolsController(window)
    window.queue_transfer_controller = QueueTransferController(window)
    window.queue_optimization_controller = QueueOptimizationController(window)
    window.tray_manager = TrayManager(window)
    window.tray_manager.setup()
    window.update_controller = UpdateController(window)
    window.lifecycle_controller = LifecycleController(window)
    window.ui_controller = UIController(window)
    window.session_service = SessionService(build_payload_callback=window.settings_controller.build_session_payload)
    window.session_service.signals.session_loaded_ui.connect(window.session_loaded_ui.emit)
    with window._active_workers_lock:
        window.pause_requested_workers = set()
        window.cancel_requested_workers = set()
        window.bandwidth_restart_requested_workers = set()


def _init_metadata_and_thumbnail_runtime(window) -> None:
    window.thumbnail_manager = ThumbnailManager(window)
    window._metadata_fetch_queue = []
    window._metadata_fetch_worker = None
    window.thumbnail_cache = OrderedDict()
    window.thumbnail_cache_max = max(50, int(DEFAULT_SETTINGS["thumbnail_cache_max"]))
    window.clean_metadata_enabled = bool(DEFAULT_SETTINGS.get("clean_metadata", True))
    # Cache failed thumbnail lookups with bounded size and expiry so
    # stale failures do not permanently block later valid thumbnails.
    window.thumbnail_failed = OrderedDict()
    window.thumbnail_failed_max = THUMBNAIL_FAILED_MAX
    window.thumbnail_failed_ttl_seconds = max(60, int(THUMBNAIL_WAITER_TTL_SECONDS) * 4)
    window.thumbnail_waiters = {}
    window._thumbnail_waiter_timestamps = {}
    window._thumbnail_state_lock = threading.RLock()
    window._active_thumbnail_requests = 0
    window._max_concurrent_thumbnails = MAX_CONCURRENT_THUMBNAILS
    window.downloads_thumbnail_jobs = []
    window._downloads_refresh_pending = False


def _init_downloads_render_runtime(window) -> None:
    window._active_download_card_refs = {}
    window._download_card_cache = {}
    window._download_card_state = {}
    window._rendered_download_rows = {}
    window._downloads_styles_cache = {}
    window._downloads_last_fingerprint = None
    window._downloads_last_entry_fingerprints = ()
    window._download_card_cache_limit = 120
    window._ui_animations = []
    window.play_sound_enabled = bool(window.config_manager.get("play_sound"))
    window.config_manager.config_changed.connect(window._on_config_changed)
    window._formats_worker = None
    window._conversion_worker = None


def _init_runtime_flags(window) -> None:
    window.auto_retry_delay_seconds = int(DEFAULT_SETTINGS["auto_retry_delay_seconds"])
    window.queue_auto_retry_limit = int(DEFAULT_SETTINGS["queue_auto_retry_limit"])
    window.queue_running = False
    window.queue_paused = False
    window._is_closing = False
    window._quit_to_tray_bypass = False
    window.close_to_tray_enabled = True
    window._downloads_render_generation = 0
    window._downloads_render_cursor = 0
    window._downloads_render_loading = False
    window._tray_progress_bucket = -1
    window._speed_history = {}
    window._speed_history_max_workers = 512
    window._display_progress_wid = None
    window._storage_guard_alerted = False
    window._storage_guard_last_message = ""
    window._normalize_folder_running = False
    window._single_download_locked = False
    window._single_download_worker_id = None
    window._queue_optimize_in_progress = False
    window._queue_optimize_request_id = 0
    window._history_path_request_seq = 0
    window._history_path_callbacks = {}
    window._history_path_lock = threading.Lock()
    window.progress_bus = ThrottledProgressBus(window)


def init_threads_and_timers(window) -> None:
    _init_ui_refresh_timer(window)
    window.session_service.start()
    _init_maintenance_timers(window)
    _init_system_watchdog_timers(window)


def _init_ui_refresh_timer(window) -> None:
    window._visible_thumb_timer = QTimer(window)
    window._visible_thumb_timer.setSingleShot(True)
    window._visible_thumb_timer.timeout.connect(window._process_visible_thumbnail_jobs)


def _init_maintenance_timers(window) -> None:
    window._thumbnail_cleanup_timer = QTimer(window)
    window._thumbnail_cleanup_timer.setInterval(THUMBNAIL_CLEANUP_INTERVAL_MS)
    window._thumbnail_cleanup_timer.timeout.connect(window._cleanup_stale_thumbnail_waiters)
    window._thumbnail_cleanup_timer.start()
    window._settings_autosave_timer = QTimer(window)
    window._settings_autosave_timer.setSingleShot(True)
    window._settings_autosave_timer.setInterval(450)
    window._settings_autosave_timer.timeout.connect(lambda: window._apply_settings_to_search(silent=True))
    window._search_history_save_timer = QTimer(window)
    window._search_history_save_timer.setSingleShot(True)
    window._search_history_save_timer.setInterval(2000)
    window._search_history_save_timer.timeout.connect(window._save_search_history_only)


def _init_system_watchdog_timers(window) -> None:
    window._storage_watchdog_timer = QTimer(window)
    window._storage_watchdog_timer.setInterval(5000)
    window._storage_watchdog_timer.timeout.connect(window._run_storage_watchdog)
    window._storage_watchdog_timer.start()
    window._scheduler_timer = QTimer(window)
    window._scheduler_timer.setInterval(30000)
    window._scheduler_timer.timeout.connect(window._refresh_bandwidth_schedule)
    window._update_scheduler_timer_state(force_refresh=False)
    window._system_theme_timer = QTimer(window)
    window._system_theme_timer.setInterval(10000)
    window._system_theme_timer.timeout.connect(window._refresh_system_theme)
    window._system_theme_timer.start()


def start_background_services(window) -> None:
    _bootstrap_system_theme_mode(window)
    _start_update_and_clip_services(window)
    _start_auxiliary_windows_and_shortcuts(window)
    _start_memory_and_network_services(window)


def _bootstrap_system_theme_mode(window) -> None:
    initial_system_theme_mode = window._detect_system_theme_mode()
    if initial_system_theme_mode in THEME_MODE_MAP:
        window._last_system_theme_mode = initial_system_theme_mode
        window.theme = str(THEME_MODE_MAP.get(initial_system_theme_mode, window.theme))


def _start_update_and_clip_services(window) -> None:
    QTimer.singleShot(0, window._init_database_async)
    window._init_trial_state()
    window.clip_watcher = ClipWatcher(window.theme)
    window.clip_watcher.downloadRequested.connect(window._quick_download)
    window.clip_watcher.analyzeRequested.connect(
        lambda url: (window.search_view.url_input.setText(url), window._start_analyze())
    )
    clipboard_enabled = False
    try:
        cfg = getattr(window, "config_manager", None)
        if cfg is not None and hasattr(cfg, "get"):
            clipboard_enabled = bool(cfg.get("clipboard_monitor_enabled", False))
    except Exception:
        clipboard_enabled = False
    if clipboard_enabled:
        window.clip_watcher.start()
    window.update_controller.schedule_background_update_check(initial_delay_ms=5000)


def _start_auxiliary_windows_and_shortcuts(window) -> None:
    window.mini_window = MiniModeWindow(THEMES.get(window.theme, THEMES["Modern Dark"]))
    window.mini_window.showMainRequested.connect(window._show_from_mini)
    window.mini_window.pauseRequested.connect(window._pause_queue_download)
    window.mini_window.cancelRequested.connect(window._cancel_active_display_item)
    window.hotkey_manager = setup_default_hotkeys(window)
    QTimer.singleShot(2000, window.hotkey_manager.start)
    extension_server.start()


def _start_memory_and_network_services(window) -> None:
    window_message = lambda mb: f"[RAM] High usage: {mb:.0f} MB. Clearing caches."
    window.memory_guard_message = getattr(window, "memory_guard_message", None)
    memory_guard.on_warning = lambda mb: (window._append_log(window_message(mb)), window._trim_thumbnail_cache())
    memory_guard.on_critical = lambda mb: window._warn(f"⚠️ RAM critical: {mb:.0f} MB — GC triggered")
    memory_guard.start()
    window.net_manager = QNetworkAccessManager(window)


def finish_startup(window) -> None:
    window.setAcceptDrops(True)
    window._build_ui()
    window._refresh_bandwidth_schedule()
    window._load_stats()
    window._load_session_async()
    window._refresh_downloads_list()
    window.queue_manager.queue_changed.connect(window._refresh_queue_list)
    window._queue_ai_timer = QTimer(window)
    window._queue_ai_timer.setInterval(5 * 60 * 1000)
    window._queue_ai_timer.timeout.connect(lambda: window._auto_optimize_queue(silent=True))
    window._queue_ai_timer.start()
    window.queue_manager.progress_updated.connect(window._on_queue_progress_updated)
    window.queue_manager.start_worker_requested.connect(window._start_download_from_queue)
    window.queue_manager.scheduled_tasks_due.connect(window._start_queue_download)
    window.queue_manager.queue_stopped.connect(lambda: _handle_queue_stopped(window))
    window.queue_manager.queue_limit_exceeded.connect(window._on_queue_limit_exceeded)
    event_bus.subscribe(ShowNotificationEvent, window._on_show_notification_event)
    event_bus.subscribe(DownloadFinishedEvent, window._on_download_finished_event)
    event_bus.subscribe(ExtensionLinkReceivedEvent, window._on_extension_link_event)


def _queue_stopped_status_text(window) -> str:
    queue_manager = getattr(window, "queue_manager", None)
    if queue_manager is None or not hasattr(queue_manager, "get_queue_items_snapshot"):
        return _("Ready")
    try:
        snapshot = list(queue_manager.get_queue_items_snapshot() or [])
    except Exception:
        return _("Ready")
    has_runnable_or_waiting = False
    has_non_runnable = False
    for item in snapshot:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "pending") or "pending").strip().lower()
        if status in {"pending", "queued", "waiting", "running", "processing", "merging", "downloading", ""}:
            has_runnable_or_waiting = True
            break
        if status in {"paused", "cancelled", "failed"}:
            has_non_runnable = True
    if has_non_runnable and not has_runnable_or_waiting:
        return "لا توجد عناصر قابلة للتشغيل"
    return _("Ready")


def _handle_queue_stopped(window) -> None:
    window._set_controls_enabled(True)
    window._set_status(_queue_stopped_status_text(window))


def queue_is_running(window) -> bool:
    queue_manager = getattr(window, "queue_manager", None)
    if queue_manager is not None and hasattr(queue_manager, "is_running"):
        manager_running = bool(getattr(queue_manager, "is_running", False))
        window.queue_running = manager_running
        if not manager_running:
            window.queue_paused = False
        return manager_running
    return bool(getattr(window, "queue_running", False))


def queue_is_paused(window) -> bool:
    queue_manager = getattr(window, "queue_manager", None)
    if not queue_is_running(window):
        window.queue_paused = False
        return False
    if queue_manager is not None and hasattr(queue_manager, "is_paused"):
        manager_paused = bool(getattr(queue_manager, "is_paused", False))
        window.queue_paused = manager_paused
        return manager_paused
    return bool(getattr(window, "queue_paused", False))


def set_queue_runtime_state(window, *, running: bool | None = None, paused: bool | None = None) -> None:
    queue_manager = getattr(window, "queue_manager", None)
    running_value = None if running is None else bool(running)
    paused_value = None if paused is None else bool(paused)
    if queue_manager is not None:
        if hasattr(queue_manager, "set_runtime_state"):
            queue_manager.set_runtime_state(is_running=running_value, is_paused=paused_value)
        else:
            if running_value is not None and hasattr(queue_manager, "is_running"):
                queue_manager.is_running = running_value
            if paused_value is not None and hasattr(queue_manager, "is_paused"):
                active_running = running_value if running_value is not None else bool(getattr(queue_manager, "is_running", False))
                queue_manager.is_paused = paused_value if active_running else False
        window.queue_running = bool(getattr(queue_manager, "is_running", False))
        window.queue_paused = bool(getattr(queue_manager, "is_paused", False)) if window.queue_running else False
        return
    if running_value is not None:
        window.queue_running = running_value
    if paused_value is not None:
        active_running = running_value if running_value is not None else bool(getattr(window, "queue_running", False))
        window.queue_paused = paused_value if active_running else False


def init_database_async(window, logger) -> None:
    """Initialize database and migrate legacy data in a background thread."""
    def task():
        try:
            from core.database import close_thread_connection, init_db, migrate_from_json

            init_db()
            app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            old_json = os.path.join(app_dir, "xd_stats.json")
            if os.path.exists(old_json):
                migrated = migrate_from_json(old_json)
                if migrated > 0:
                    logger.info(f"[DB] Migrated {migrated} history entries from JSON to SQLite")
            window._migrate_legacy_session_json()
        except (ImportError, OSError, RuntimeError, ValueError, TypeError) as exc:
            logger.warning(f"[DB] Async init failed: {exc}")
        finally:
            close_thread_connection()

    threading.Thread(target=task, daemon=True, name="DBInitWorker").start()


def prune_inactive_workers(window) -> int:
    stale_keys = []
    with window._active_workers_lock:
        for wid, worker in list(window.active_workers.items()):
            if worker is None:
                stale_keys.append(wid)
                continue
            try:
                if worker.isFinished():
                    stale_keys.append(wid)
            except RuntimeError:
                stale_keys.append(wid)
        if not stale_keys:
            return 0
        for wid in stale_keys:
            worker = window.active_workers.pop(wid, None)
            if window.current_worker is worker:
                window.current_worker = None
            window._speed_history.pop(wid, None)
            window.pause_requested_workers.discard(wid)
            window.cancel_requested_workers.discard(wid)
            window.bandwidth_restart_requested_workers.discard(wid)
        if window._display_progress_wid not in window.active_workers:
            window._display_progress_wid = next(iter(window.active_workers.keys()), None)
        return len(stale_keys)


def active_workers_count(window) -> int:
    with window._active_workers_lock:
        return sum(1 for worker in window.active_workers.values() if worker is not None and worker.isRunning())


def mark_pause_requested(window, wid: int) -> None:
    with window._active_workers_lock:
        window.pause_requested_workers.add(wid)


def mark_cancel_requested(window, wid: int) -> None:
    with window._active_workers_lock:
        window.cancel_requested_workers.add(wid)


def mark_bandwidth_restart_requested(window, wid: int) -> None:
    with window._active_workers_lock:
        window.bandwidth_restart_requested_workers.add(wid)


def take_worker_request_state(window, wid: int) -> tuple[bool, bool, bool]:
    with window._active_workers_lock:
        cancelled_by_user = wid in window.cancel_requested_workers
        bandwidth_restart_requested = wid in window.bandwidth_restart_requested_workers
        paused_by_user = wid in window.pause_requested_workers
        if cancelled_by_user:
            window.cancel_requested_workers.discard(wid)
        if bandwidth_restart_requested:
            window.bandwidth_restart_requested_workers.discard(wid)
        if paused_by_user:
            window.pause_requested_workers.discard(wid)
    return cancelled_by_user, bandwidth_restart_requested, paused_by_user


def close_window(window) -> None:
    close_to_tray = bool(getattr(window, "close_to_tray_enabled", True))
    tray_manager = getattr(window, "tray_manager", None)
    tray_icon = getattr(tray_manager, "tray_icon", None)
    if tray_icon is None:
        tray_icon = getattr(window, "tray_icon", None)
    if close_to_tray:
        if tray_icon is None and tray_manager is not None and hasattr(tray_manager, "setup"):
            try:
                if QSystemTrayIcon.isSystemTrayAvailable():
                    tray_manager.setup()
                    tray_icon = getattr(tray_manager, "tray_icon", None)
            except Exception:
                tray_icon = getattr(tray_manager, "tray_icon", None)
        if tray_icon is not None:
            try:
                if hasattr(tray_icon, "isVisible") and not tray_icon.isVisible() and hasattr(tray_icon, "show"):
                    tray_icon.show()
            except Exception:
                pass
            window.hide()
            active_count = window._active_workers_count()
            try:
                tray_icon.showMessage(
                    "SnapDownloader",
                    _("لا يزال يعمل في الخلفية. التحميلات النشطة: {count}").format(count=active_count),
                    QSystemTrayIcon.MessageIcon.Information,
                    3000,
                )
            except Exception:
                pass
            return
    window.close()
