import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QHBoxLayout, QStackedWidget, QSystemTrayIcon, QVBoxLayout, QWidget

from core.i18n import _
from ui.error_dashboard import ErrorDashboard
from ui.overlay import NotificationOverlay
from ui.playlist_view import PlaylistView
from ui.sidebar import PremiumSidebar
from ui.stats_view import StatsView
from ui.subscriptions_view import SubscriptionsView
from ui.themes import get_theme
from ui.views.downloads_view import DownloadsView
from ui.views.search_view import SearchView
from ui.views.browser_view import SmartBrowserView
from ui.views.settings_view import SettingsView
from ui.views.tools_view import ToolsView

logger = logging.getLogger("SnapDownloader")


def _center_window(window):
    screen = QApplication.primaryScreen()
    if screen is None:
        return
    geometry = screen.geometry()
    size = window.geometry()
    window.move(
        (geometry.width() - size.width()) // 2,
        (geometry.height() - size.height()) // 2,
    )


def _create_views(window):
    window.search_view = SearchView(window)
    window.browser_view = SmartBrowserView(window)
    window.downloads_view = DownloadsView(window)
    window.settings_view = SettingsView(window)
    window.tools_view = ToolsView(window)
    window.stats_view = StatsView(window.theme)
    window.error_dashboard = ErrorDashboard()
    window.playlist_view = PlaylistView(window, net_manager=window.net_manager)
    window.subscriptions_view = SubscriptionsView(window)


def _populate_main_stack(window):
    window.main_stack = QStackedWidget()
    window.main_stack.setObjectName("main_stack")
    for view in (
        window.search_view,
        window.browser_view,
        window.downloads_view,
        window.settings_view,
        window.tools_view,
        window.error_dashboard,
        window.playlist_view,
        window.subscriptions_view,
        window.stats_view,
    ):
        window.main_stack.addWidget(view)


def _bind_post_build_hooks(window):
    wire_views(window)
    window.settings_controller.reload_bandwidth_schedule_editor()
    window.settings_controller.sync_bandwidth_schedule_rule_list()
    window.settings_controller.prepare_new_bandwidth_rule()
    window.settings_controller.bind_settings_autosave()
    window.settings_controller.refresh_cookie_profiles_ui()
    window.search_view.formats_requested.connect(window._on_formats_requested)
    window.downloads_view.history_filters_changed.connect(window._refresh_downloads_list)
    window.playlist_view.analyzeRequested.connect(window._on_playlist_view_analyze_requested)
    window.playlist_view.forceAnalyzeRequested.connect(window._on_playlist_view_force_analyze_requested)
    window.playlist_view.downloadRequested.connect(window._on_playlist_view_download_requested)
    window.sidebar.view_changed.connect(lambda key: _on_sidebar_view_changed(window, key))


def _on_sidebar_view_changed(window, key: str):
    window._switch_view(key)
    if str(key or "") == "stats" and hasattr(window, "stats_view"):
        window.stats_view.refresh()


def build_main_window_ui(window):
    window.setWindowFlags(Qt.WindowType.FramelessWindowHint)
    window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
    window.setWindowTitle(_("SnapDownloader"))
    window.setMinimumSize(900, 620)
    window.resize(1180, 760)
    _center_window(window)
    window.setStyleSheet(window._qss())

    if QSystemTrayIcon.isSystemTrayAvailable():
        pass

    root = QWidget()
    layout = QVBoxLayout(root)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    window.title_bar = window._build_title_bar()
    layout.addWidget(window.title_bar)

    content_layout = QHBoxLayout()
    content_layout.setContentsMargins(0, 0, 0, 0)
    content_layout.setSpacing(0)

    window.sidebar = PremiumSidebar(get_theme(window.theme), window)
    content_layout.addWidget(window.sidebar)

    _create_views(window)
    _populate_main_stack(window)
    _bind_post_build_hooks(window)

    content_layout.addWidget(window.main_stack, 1)
    layout.addLayout(content_layout, 1)
    window.setCentralWidget(root)

    window.notification_overlay = NotificationOverlay(root)
    window._init_toast()
    window._init_trial_timer()
    window._switch_view("search")
    window._set_search_state("empty")

    # MED-02: Ensure StatsWorker thread is cleaned up on application exit
    original_close = window.closeEvent

    def _enhanced_close(event):
        if hasattr(window, "stats_view"):
            worker = getattr(window.stats_view, "worker", None)
            if worker and worker.isRunning():
                try:
                    worker.requestInterruption()
                except Exception:
                    pass
                try:
                    worker.quit()
                except Exception:
                    pass
                try:
                    worker.wait(500)
                except Exception:
                    pass
        original_close(event)

    window.closeEvent = _enhanced_close

def _resolve_path(obj, path: str | None):
    if not path:
        return obj
    current = obj
    for part in str(path).split("."):
        current = getattr(current, part, None)
        if current is None:
            return None
    return current


def _safe_connect(window, source_path: str, signal_name: str, target_path: str | None, slot_name: str) -> bool:
    source_obj = _resolve_path(window, source_path)
    target_obj = _resolve_path(window, target_path)
    if source_obj is None or target_obj is None:
        logger.warning(
            "wire_views: skipping %s.%s -> %s.%s (missing source/target)",
            source_path,
            signal_name,
            target_path or "window",
            slot_name,
        )
        return False
    signal = getattr(source_obj, signal_name, None)
    slot = getattr(target_obj, slot_name, None)
    if signal is None or slot is None:
        logger.warning(
            "wire_views: skipping %s.%s -> %s.%s (missing signal/slot)",
            source_path,
            signal_name,
            target_path or "window",
            slot_name,
        )
        return False
    try:
        signal.connect(slot)
        return True
    except Exception as exc:
        logger.error(
            "wire_views: failed %s.%s -> %s.%s: %s",
            source_path,
            signal_name,
            target_path or "window",
            slot_name,
            exc,
        )
        return False


_VIEW_WIRES: list[tuple[str, str, str | None, str]] = [
    ("search_view", "paste_requested", None, "_paste_clipboard"),
    ("search_view", "clear_history_requested", None, "_clear_search_history"),
    ("search_view", "download_requested", None, "_start_single_download"),
    ("search_view", "schedule_requested", None, "_add_current_to_queue"),
    ("search_view", "trim_toggle_requested", None, "_toggle_trim_options"),
    ("downloads_view", "filter_changed", None, "_set_downloads_filter"),
    ("downloads_view", "search_changed", None, "_refresh_downloads_list"),
    ("downloads_view", "queue_state_changed", None, "_set_queue_state_filter"),
    ("downloads_view", "page_changed", None, "_set_downloads_page"),
    ("downloads_view", "clear_completed_requested", None, "_clear_completed_history"),
    ("downloads_view", "export_txt_requested", None, "_export_history_txt"),
    ("downloads_view", "export_csv_requested", None, "_export_history_csv"),
    ("downloads_view", "queue_reorder_requested", None, "_on_queue_reorder_requested"),
    ("downloads_view", "list_scrolled", None, "_on_downloads_list_scrolled"),
    ("downloads_view", "list_range_changed", None, "_on_downloads_list_range_changed"),
    ("settings_view", "apply_requested", "settings_controller", "apply_settings_to_search"),
    ("settings_view", "dir_picker_requested", "settings_controller", "pick_settings_dir"),
    ("settings_view", "cookie_picker_requested", "settings_controller", "pick_cookies"),
    ("settings_view", "auto_cookie_requested", "settings_controller", "auto_import_cookies"),
    ("settings_view", "post_script_picker_requested", "settings_controller", "pick_post_download_script"),
    ("settings_view", "cookie_profile_save_requested", "settings_controller", "save_cookie_profile_from_ui"),
    ("settings_view", "cookie_profile_load_requested", "settings_controller", "load_selected_cookie_profile"),
    ("settings_view", "cookie_profile_delete_requested", "settings_controller", "delete_selected_cookie_profile"),
    ("settings_view", "update_ytdlp_requested", None, "_update_ytdlp"),
    ("settings_view", "check_app_updates_requested", None, "_check_app_updates_manual"),
    ("settings_view", "export_settings_requested", "settings_controller", "export_settings"),
    ("settings_view", "export_settings_qr_requested", "settings_controller", "export_settings_qr"),
    ("settings_view", "import_settings_requested", None, "_import_settings_from_file"),
    ("settings_view", "proxy_add_requested", "settings_controller", "add_proxy"),
    ("settings_view", "proxy_test_requested", "settings_controller", "test_proxy"),
    ("settings_view", "normalize_requested", None, "_normalize_downloads_folder"),
    ("settings_view", "mini_mode_requested", None, "_toggle_mini_mode"),
    ("settings_view", "sustainability_apply_requested", "settings_controller", "apply_sustainability"),
    ("settings_view", "ui_language_changed", "settings_controller", "set_ui_language"),
    ("settings_view", "bandwidth_rule_selected", "settings_controller", "load_selected_bandwidth_rule"),
    ("settings_view", "bandwidth_editor_changed", "settings_controller", "sync_bandwidth_schedule_rule_list"),
    ("settings_view", "bandwidth_new_rule_requested", "settings_controller", "prepare_new_bandwidth_rule"),
    ("settings_view", "bandwidth_save_rule_requested", "settings_controller", "save_bandwidth_rule_from_form"),
    ("settings_view", "bandwidth_remove_rule_requested", "settings_controller", "remove_selected_bandwidth_rule"),
    ("settings_view", "bandwidth_apply_schedule_requested", "settings_controller", "apply_bandwidth_schedule_rules"),
    ("settings_view", "bandwidth_reset_schedule_requested", "settings_controller", "reset_bandwidth_schedule_rules"),
    ("tools_view", "export_queue_requested", None, "_export_queue_to_file"),
    ("tools_view", "import_queue_requested", None, "_import_queue_from_file"),
    ("tools_view", "retry_failed_requested", None, "_retry_all_failed_queue_items"),
    ("tools_view", "optimize_queue_requested", None, "_auto_optimize_queue"),
    ("subscriptions_view", "newVideosReady", "download_controller", "download_subscription_videos"),
]


def wire_views(window):
    window.download_btn = window.search_view.download_btn
    window.adv_toggle_btn = getattr(window.search_view, "adv_toggle_btn", None)
    window.trim_btn = getattr(window.search_view, "trim_btn", None)
    window.schedule_btn = getattr(window.search_view, "schedule_btn", None)

    for source_path, signal_name, target_path, slot_name in _VIEW_WIRES:
        _safe_connect(window, source_path, signal_name, target_path, slot_name)

    # Keep current behavior: analyze signal provides URL while handler reads field state.
    window.search_view.analyze_requested.connect(lambda _url: window._start_analyze())
    
    def _handle_browser_analyze(url, context=None):
        window.search_view.url_input.setText(url)
        # TODO: Store context in window for the next analyze/download worker to use
        if context:
            logger.info(f"Browser handoff with context: {list(context.keys())}")
        window._switch_view('search')
        window._start_analyze()

    window.browser_view.analyze_requested.connect(_handle_browser_analyze)


    if hasattr(window.search_view, 'trim_view'):
        window.search_view.trim_view.saved.connect(window._on_trim_view_saved)
        window.search_view.trim_view.backRequested.connect(window._on_trim_view_back)
    window.tools_view.fetch_channel_requested.connect(lambda url: (window.search_view.url_input.setText(url), window._switch_view('search'), window._start_analyze()))

