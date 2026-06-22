try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QMenu
except ImportError:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QMenu

from core.i18n import _, i18n
import qtawesome as qta


def build_title_bar(window):
    frame = QFrame()
    frame.setObjectName("title_bar")
    frame.setFixedHeight(40)

    row = QHBoxLayout(frame)
    row.setContentsMargins(16, 0, 16, 0)
    row.setSpacing(10)

    title_label = QLabel("SnapDownloader")
    title_label.setObjectName("title_label")
    title_label.setStyleSheet("font-weight: bold; margin-right: 15px;")
    row.addWidget(title_label)

    # --- Menus ---
    menu_style = """
    QMenu {
        background-color: #1E1E22;
        border: 1px solid #27272A;
        border-radius: 6px;
        padding: 5px;
    }
    QMenu::item {
        padding: 8px 24px 8px 8px;
        border-radius: 4px;
        color: #E4E4E7;
    }
    QMenu::item:selected {
        background-color: #6366F1;
        color: white;
    }
    """
    
    def _create_menu_button(text: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("title_menu_btn")
        btn.setStyleSheet(
            "background: transparent; border: none; padding: 5px 10px; color: #A1A1AA; font-size: 13px;"
        )
        return btn

    def _set_check_icon(action, checked: bool):
        icon_name = "fa5s.check-square" if checked else "fa5s.square"
        action.setIcon(qta.icon(icon_name, color="#A1A1AA"))

    # 1. File Menu
    file_btn = _create_menu_button(_("File"))
    file_menu = QMenu(frame)
    file_menu.setStyleSheet(menu_style)
    file_menu.addAction(qta.icon('fa5s.window-minimize', color='#A1A1AA'), _("Minimize"), lambda: window.showMinimized())
    file_menu.addSeparator()
    file_menu.addAction(qta.icon('fa5s.star', color='#A1A1AA'), _("New Subscription"), lambda: getattr(window, '_show_subscriptions', lambda: None)())
    file_menu.addAction(qta.icon('fa5s.file-import', color='#A1A1AA'), _("Import"), lambda: getattr(window, '_bulk_import', lambda: None)())
    file_menu.addAction(qta.icon('fa5s.file-export', color='#A1A1AA'), _("Export Queue"), lambda: getattr(window, '_export_queue_to_file', lambda: None)())
    file_menu.addAction(qta.icon('fa5s.file-import', color='#A1A1AA'), _("Import Queue"), lambda: getattr(window, '_import_queue_from_file', lambda: None)())
    file_menu.addSeparator()
    file_menu.addAction(qta.icon('fa5s.sign-out-alt', color='#A1A1AA'), _("Quit"), lambda: getattr(window, '_close_window', lambda: None)())
    file_btn.setMenu(file_menu)
    row.addWidget(file_btn)

    # 2. Downloads Menu
    dl_btn = _create_menu_button(_("Downloads"))
    dl_menu = QMenu(frame)
    dl_menu.setStyleSheet(menu_style)
    dl_menu.addAction(qta.icon('fa5s.pause', color='#A1A1AA'), _("Pause All Downloads"), lambda: getattr(window, '_pause_queue_download', lambda: None)())
    dl_menu.addAction(qta.icon('fa5s.play', color='#A1A1AA'), _("Resume All Downloads"), lambda: getattr(window, '_resume_queue_download', lambda: None)())
    dl_menu.addAction(qta.icon('fa5s.redo', color='#A1A1AA'), _("Restart All Failed Downloads"), lambda: getattr(window, '_retry_all_failed_queue_items', lambda: None)())
    dl_menu.addSeparator()
    dl_menu.addAction(qta.icon('fa5s.broom', color='#A1A1AA'), _("Clear Completed History"), lambda: getattr(window, '_clear_completed_history', lambda: None)())
    dl_menu.addAction(qta.icon('fa5s.trash', color='#A1A1AA'), _("Clear Finished/Failed Downloads"), lambda: getattr(window, '_clear_finished_or_failed_downloads', lambda: None)())
    dl_menu.addAction(qta.icon('fa5s.times-circle', color='#A1A1AA'), _("Cancel All Downloads (In Progress)"), lambda: getattr(window, '_cancel_all_active_downloads', lambda: None)())
    dl_btn.setMenu(dl_menu)
    row.addWidget(dl_btn)

    # 3. Language Menu
    lang_btn = _create_menu_button(_("Language"))
    lang_menu = QMenu(frame)
    lang_menu.setStyleSheet(menu_style)
    current_lang = str(getattr(window, "ui_language", "en") or "en").strip().lower()
    available_languages = i18n.available_languages()
    for code in ("en", "ar", "es", "fr"):
        if code not in available_languages:
            continue
        action = lang_menu.addAction(available_languages[code])
        action.setCheckable(True)
        action.setChecked(code == current_lang)
        _set_check_icon(action, code == current_lang)
        action.triggered.connect(
            lambda checked=False, lang_code=code: getattr(window, "_set_ui_language_from_menu", lambda _code: None)(lang_code)
        )
    lang_btn.setMenu(lang_menu)
    row.addWidget(lang_btn)

    # 4. Tools Menu
    tools_btn = _create_menu_button(_("Tools"))
    tools_menu = QMenu(frame)
    tools_menu.setStyleSheet(menu_style)
    tools_menu.addAction(qta.icon('fa5s.download', color='#A1A1AA'), _("Downloads Folder"), lambda: getattr(window, '_open_downloads_folder', lambda: None)())
    try:
        from core.utils import get_app_data_dir
        app_data_dir = get_app_data_dir()
    except Exception:
        app_data_dir = ""
    tools_menu.addAction(
        qta.icon('fa5s.folder-open', color='#A1A1AA'),
        _("Open App Data Directory"),
        lambda: getattr(window, '_open_path_in_file_manager', lambda _path: None)(app_data_dir),
    )
    tools_menu.addAction(qta.icon('fa5s.tools', color='#A1A1AA'), _("Open Tools Page"), lambda: getattr(window, '_switch_view', lambda _key: None)("tools"))
    tools_menu.addSeparator()
    tools_menu.addAction(qta.icon('fa5s.sync-alt', color='#A1A1AA'), _("Update yt-dlp"), lambda: getattr(getattr(window, 'update_controller', None), 'update_ytdlp_manual', lambda: None)())
    tools_menu.addAction(qta.icon('fa5s.arrow-circle-down', color='#A1A1AA'), _("Check App Updates"), lambda: getattr(getattr(window, 'update_controller', None), 'check_updates_manual', lambda: None)())
    tools_menu.addSeparator()
    tools_menu.addAction(qta.icon('fa5s.file-export', color='#A1A1AA'), _("Export/Backup Subscriptions"), lambda: getattr(window, '_export_subscriptions', lambda: None)())
    tools_menu.addAction(qta.icon('fa5s.file-import', color='#A1A1AA'), _("Import/Restore Subscriptions"), lambda: getattr(window, '_import_subscriptions', lambda: None)())
    tools_menu.addSeparator()
    tools_menu.addAction(qta.icon('fa5s.globe', color='#A1A1AA'), _("yt-dlp Releases (web)"), lambda: getattr(window, '_open_web_url', lambda _url: None)("https://github.com/yt-dlp/yt-dlp/releases"))
    tools_menu.addAction(qta.icon('fa5s.globe', color='#A1A1AA'), _("Install ffmpeg (web)"), lambda: getattr(window, '_open_web_url', lambda _url: None)("https://www.gyan.dev/ffmpeg/builds/"))
    tools_btn.setMenu(tools_menu)
    row.addWidget(tools_btn)

    row.addStretch(1)

    theme_btn = QPushButton(_("Theme"))
    theme_btn.setObjectName("title_btn")
    theme_btn.setFixedSize(56, 30)
    theme_btn.setToolTip(_("Toggle Theme"))
    theme_btn.clicked.connect(window._toggle_theme)

    mode_btn = QPushButton(_("Mode"))
    mode_btn.setObjectName("title_btn")
    mode_btn.setFixedSize(56, 30)
    mode_btn.setToolTip(_("Toggle Dark/Light"))
    mode_btn.clicked.connect(window._toggle_dark_light_mode)

    min_btn = QPushButton("—")
    min_btn.setObjectName("title_btn")
    min_btn.setFixedSize(30, 30)
    min_btn.clicked.connect(window.showMinimized)

    max_btn = QPushButton("□")
    max_btn.setObjectName("title_btn")
    max_btn.setFixedSize(30, 30)
    max_btn.setToolTip(_("Maximize / Restore"))
    max_btn.clicked.connect(lambda: window.showNormal() if window.isMaximized() else window.showMaximized())

    close_btn = QPushButton("X")
    close_btn.setObjectName("title_btn_close")
    close_btn.setFixedSize(30, 30)
    close_btn.clicked.connect(window._close_window)

    row.addWidget(mode_btn)
    row.addWidget(theme_btn)
    row.addWidget(min_btn)
    row.addWidget(max_btn)
    row.addWidget(close_btn)

    window._is_tracking = False
    window._start_pos = None

    def mousePressEvent(event):
        if event.button() == Qt.MouseButton.LeftButton:
            window._is_tracking = True
            window._start_pos = event.globalPosition().toPoint() - window.pos()
            event.accept()

    def mouseMoveEvent(event):
        if window._is_tracking:
            window.move(event.globalPosition().toPoint() - window._start_pos)
            event.accept()

    def mouseReleaseEvent(event):
        if event.button() == Qt.MouseButton.LeftButton:
            window._is_tracking = False
            event.accept()

    def mouseDoubleClickEvent(event):
        if event.button() == Qt.MouseButton.LeftButton:
            if window.isMaximized():
                window.showNormal()
            else:
                window.showMaximized()
            event.accept()

    frame.mousePressEvent = mousePressEvent
    frame.mouseMoveEvent = mouseMoveEvent
    frame.mouseReleaseEvent = mouseReleaseEvent
    frame.mouseDoubleClickEvent = mouseDoubleClickEvent
    return frame
