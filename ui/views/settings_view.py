
from PySide6.QtCore import Signal, QTime, Qt
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QFrame,
    QLineEdit,
    QPushButton,
    QLabel,
    QCheckBox,
    QSpinBox,
    QComboBox,
    QScrollArea,
    QTimeEdit,
    QListWidget,
    QTextEdit,
    QGridLayout,
    QGroupBox,
    QStackedWidget,
    QSizePolicy,
)

from core.config import DEFAULT_SETTINGS, default_download_dir
from core.cookie_importer import get_available_browsers
from core.i18n import _, i18n
from core.proxy_manager import proxy_manager
from core.smart_rename import TEMPLATES as RENAME_TEMPLATES
from core.sustainability import ACTIONS as SUSTAINABILITY_ACTIONS
from ui.views.base_view import BaseView
from ui.views.pipeline_editor import PipelineEditor


class BandwidthCalendarWidget(QWidget):
    scheduleChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._buttons: list[QPushButton] = []
        self._slots: list[dict] = [{"limit_kbps": 0, "selected": False} for _ in range(24)]
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        control_row = QHBoxLayout()
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(0, 1024 * 1024)
        self.limit_spin.setSuffix(" KB/s")
        self.limit_spin.setSpecialValueText(_("Unlimited"))
        self.apply_btn = QPushButton(_("Apply To Selected Hours"))
        self.apply_btn.setObjectName("action_schedule")
        self.apply_btn.clicked.connect(self._apply_limit_to_selected)
        self.clear_btn = QPushButton(_("Clear Selection"))
        self.clear_btn.setObjectName("action_trim")
        self.clear_btn.clicked.connect(self.clear_selection)
        control_row.addWidget(self.limit_spin)
        control_row.addWidget(self.apply_btn)
        control_row.addWidget(self.clear_btn)
        control_row.addStretch(1)
        root.addLayout(control_row)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        for hour in range(24):
            btn = QPushButton(self._slot_text(hour))
            btn.setCheckable(True)
            btn.setMinimumHeight(44)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(lambda checked=False, idx=hour: self._toggle_slot(idx))
            self._buttons.append(btn)
            grid.addWidget(btn, hour // 6, hour % 6)
        root.addLayout(grid)

        self.summary_label = QLabel(_("No hourly overrides configured"))
        self.summary_label.setObjectName("single_sub")
        self.summary_label.setWordWrap(True)
        root.addWidget(self.summary_label)
        self._refresh_buttons()

    def _slot_text(self, hour: int) -> str:
        next_hour = (hour + 1) % 24
        return f"{hour:02d}:00-{next_hour:02d}:00"

    def _toggle_slot(self, hour: int):
        self._slots[hour]["selected"] = bool(self._buttons[hour].isChecked())
        self._refresh_buttons()
        self.scheduleChanged.emit()

    def _apply_limit_to_selected(self):
        limit = int(self.limit_spin.value())
        updated = False
        for slot in self._slots:
            if slot["selected"]:
                slot["limit_kbps"] = limit
                updated = True
        if updated:
            self._refresh_buttons()
            self.scheduleChanged.emit()

    def clear_selection(self):
        for slot in self._slots:
            slot["selected"] = False
        self._refresh_buttons()

    def selected_hours(self) -> list[int]:
        return [index for index, slot in enumerate(self._slots) if bool(slot["selected"])]

    def export_schedule(self) -> list[dict]:
        rules = []
        start_hour = None
        current_limit = None
        for hour in range(25):
            slot_limit = None
            if hour < 24:
                slot_limit = int(self._slots[hour]["limit_kbps"] or 0)
            if start_hour is None and hour < 24:
                start_hour = hour
                current_limit = slot_limit
                continue
            if hour < 24 and slot_limit == current_limit:
                continue
            if start_hour is not None:
                end_hour = hour
                rules.append(
                    {
                        "start": f"{start_hour:02d}:00",
                        "end": "24:00" if end_hour >= 24 else f"{end_hour:02d}:00",
                        "limit_kbps": int(current_limit or 0),
                        "label": f"{start_hour:02d}:00-{0 if end_hour >= 24 else end_hour:02d}:00",
                    }
                )
            start_hour = hour if hour < 24 else None
            current_limit = slot_limit
        return rules

    def import_schedule(self, rules: list[dict]):
        self._slots = [{"limit_kbps": 0, "selected": False} for _ in range(24)]
        for rule in rules or []:
            start_text = str(rule.get("start", "00:00") or "00:00")
            end_text = str(rule.get("end", "00:00") or "00:00")
            try:
                start_hour = int(start_text.split(":")[0])
                end_hour = 24 if end_text == "24:00" else int(end_text.split(":")[0])
                limit_kbps = max(0, int(rule.get("limit_kbps", 0) or 0))
            except Exception:
                continue
            for hour in range(max(0, start_hour), min(24, end_hour)):
                self._slots[hour]["limit_kbps"] = limit_kbps
        self._refresh_buttons()

    def _refresh_buttons(self):
        configured = 0
        for hour, slot in enumerate(self._slots):
            btn = self._buttons[hour]
            selected = bool(slot["selected"])
            limit = int(slot["limit_kbps"] or 0)
            btn.blockSignals(True)
            btn.setChecked(selected)
            btn.setText(f"{self._slot_text(hour)}\n{limit if limit else '∞'} KB/s")
            if limit > 0:
                configured += 1
            if selected and limit > 0:
                btn.setStyleSheet("background-color: rgba(99, 102, 241, 0.28); border: 1px solid rgba(99, 102, 241, 0.55);")
            elif selected:
                btn.setStyleSheet("background-color: rgba(16, 185, 129, 0.22); border: 1px solid rgba(16, 185, 129, 0.55);")
            elif limit > 0:
                btn.setStyleSheet("background-color: rgba(99, 102, 241, 0.16); border: 1px solid rgba(99, 102, 241, 0.28);")
            else:
                btn.setStyleSheet("")
            btn.blockSignals(False)
        self.summary_label.setText(
            _("Configured hourly limits: {count}/24").format(count=configured)
            if configured
            else _("No hourly overrides configured")
        )

class SettingsView(BaseView):
    apply_requested = Signal()
    dir_picker_requested = Signal()
    cookie_picker_requested = Signal()
    auto_cookie_requested = Signal()
    post_script_picker_requested = Signal()
    cookie_profile_save_requested = Signal()
    cookie_profile_load_requested = Signal()
    cookie_profile_delete_requested = Signal()
    update_ytdlp_requested = Signal()
    proxy_test_requested = Signal()
    proxy_add_requested = Signal()
    normalize_requested = Signal()
    mini_mode_requested = Signal()
    sustainability_apply_requested = Signal()
    export_settings_requested = Signal()
    export_settings_qr_requested = Signal()
    import_settings_requested = Signal()
    check_app_updates_requested = Signal()
    ui_language_changed = Signal(str)
    bandwidth_rule_selected = Signal(int)
    bandwidth_editor_changed = Signal()
    bandwidth_new_rule_requested = Signal()
    bandwidth_save_rule_requested = Signal()
    bandwidth_remove_rule_requested = Signal()
    bandwidth_apply_schedule_requested = Signal()
    bandwidth_reset_schedule_requested = Signal()

    def __init__(self, main_window=None, parent=None):
        super().__init__(main_window, parent)
        self.setup_ui()

    def _create_page(self, layout_builder_func):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("background: transparent;")
        
        inner_widget = QWidget()
        inner_widget.setStyleSheet("""
            QWidget {
                background: transparent;
            }
            QGroupBox {
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 8px;
                margin-top: 15px;
                font-weight: bold;
                padding-top: 15px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
                color: #A1A1AA;
            }
        """)
        inner_layout = QVBoxLayout(inner_widget)
        inner_layout.setContentsMargins(20, 20, 20, 20)
        inner_layout.setSpacing(15)
        
        layout_builder_func(inner_layout)
        inner_layout.addStretch(1)
        
        scroll.setWidget(inner_widget)
        layout.addWidget(scroll)
        
        return page

    def setup_ui(self):
        def _disable_wheel_scroll(widget):
            main = getattr(self, "main_window", None)
            wheel_filter = getattr(main, "wheel_filter", None)
            if wheel_filter is not None:
                widget.installEventFilter(wheel_filter)

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(20)

        # 1. Sidebar (Navigation Menu)
        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(220)
        self.sidebar.setObjectName("settings_sidebar")
        self.sidebar.setStyleSheet("""
            QListWidget#settings_sidebar {
                background-color: rgba(30, 30, 35, 160);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 12px;
                padding: 10px 5px;
                outline: none;
            }
            QListWidget#settings_sidebar::item {
                color: #A1A1AA;
                padding: 12px 15px;
                border-radius: 8px;
                margin: 4px 5px;
                font-size: 14px;
                font-weight: bold;
            }
            QListWidget#settings_sidebar::item:hover {
                background-color: rgba(255, 255, 255, 0.08);
                color: #E4E4E7;
            }
            QListWidget#settings_sidebar::item:selected {
                background-color: rgba(99, 102, 241, 0.25);
                border-left: 4px solid #6366F1;
                color: #ffffff;
            }
        """)

        # 2. Right Panel
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(15)
        
        header_layout = QHBoxLayout()
        self.header_title = QLabel(_("Settings"))
        self.header_title.setStyleSheet("font-size: 24px; font-weight: bold; color: white;")
        header_layout.addWidget(self.header_title)
        
        self.apply_btn = QPushButton(_("Apply Settings"))
        self.apply_btn.setObjectName("action_download")
        self.apply_btn.clicked.connect(self.apply_requested.emit)
        self.apply_btn.setMinimumHeight(35)
        self.apply_btn.setFixedWidth(150)
        header_layout.addStretch()
        header_layout.addWidget(self.apply_btn)
        
        right_layout.addLayout(header_layout)

        self.stacked_widget = QStackedWidget()
        self.stacked_widget.setObjectName("settings_stacked")
        self.stacked_widget.setStyleSheet("""
            QStackedWidget#settings_stacked {
                background-color: rgba(30, 30, 35, 140);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 12px;
            }
        """)
        right_layout.addWidget(self.stacked_widget)
        
        main_layout.addWidget(self.sidebar)
        main_layout.addWidget(right_panel, stretch=1)

        # Connect sidebar to stacked widget
        self.sidebar.currentRowChanged.connect(self.stacked_widget.setCurrentIndex)

        # Common inputs initialization
        self.settings_out_dir = QLineEdit(default_download_dir())
        self.settings_out_dir.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.pick_dir_btn = QPushButton(_("Choose Folder"))
        self.pick_dir_btn.setObjectName("action_schedule")
        self.pick_dir_btn.clicked.connect(self.dir_picker_requested.emit)

        self.settings_rename_template = QComboBox()
        self.settings_rename_template.addItems(list(RENAME_TEMPLATES.keys()))
        _disable_wheel_scroll(self.settings_rename_template)
        
        self.settings_language_combo = QComboBox()
        _disable_wheel_scroll(self.settings_language_combo)
        for code, label in i18n.available_languages().items():
            self.settings_language_combo.addItem(label, code)
        self.settings_language_combo.currentIndexChanged.connect(
            lambda _=None: self.ui_language_changed.emit(str(self.settings_language_combo.currentData() or ""))
        )

        self.settings_retries = QSpinBox()
        self.settings_retries.setRange(1, 7)
        self.settings_retries.setValue(int(DEFAULT_SETTINGS["retries"]))
        _disable_wheel_scroll(self.settings_retries)
        
        self.settings_retry_delay = QSpinBox()
        self.settings_retry_delay.setRange(1, 120)
        self.settings_retry_delay.setValue(int(DEFAULT_SETTINGS["auto_retry_delay_seconds"]))
        self.settings_retry_delay.setPrefix(_("Retry Delay (sec): "))
        _disable_wheel_scroll(self.settings_retry_delay)
        
        self.settings_queue_retry_limit = QSpinBox()
        self.settings_queue_retry_limit.setRange(0, 8)
        self.settings_queue_retry_limit.setValue(int(DEFAULT_SETTINGS["queue_auto_retry_limit"]))
        self.settings_queue_retry_limit.setPrefix(_("Queue Auto Retry: "))
        _disable_wheel_scroll(self.settings_queue_retry_limit)

        self.settings_queue_priority = QComboBox()
        self.settings_queue_priority.addItem(_("FIFO (افتراضي)"), "fifo")
        self.settings_queue_priority.addItem(_("الأصغر أولاً"), "smallest_first")
        preferred_priority = str(DEFAULT_SETTINGS.get("queue_priority", "fifo"))
        idx_priority = self.settings_queue_priority.findData(preferred_priority)
        if idx_priority >= 0:
            self.settings_queue_priority.setCurrentIndex(idx_priority)
        _disable_wheel_scroll(self.settings_queue_priority)
        
        self.settings_concurrent = QSpinBox()
        self.settings_concurrent.setRange(1, 10)
        self.settings_concurrent.setValue(int(DEFAULT_SETTINGS["max_concurrent"]))
        self.settings_concurrent.setPrefix(_("Max Concurrent Downloads: "))
        _disable_wheel_scroll(self.settings_concurrent)
        
        self.settings_search_history_limit = QSpinBox()
        self.settings_search_history_limit.setRange(5, 500)
        self.settings_search_history_limit.setValue(int(DEFAULT_SETTINGS["search_history_limit"]))
        self.settings_search_history_limit.setPrefix(_("Search History Limit: "))
        _disable_wheel_scroll(self.settings_search_history_limit)

        self.settings_search_history_ttl_days = QSpinBox()
        self.settings_search_history_ttl_days.setRange(1, 365)
        self.settings_search_history_ttl_days.setValue(int(DEFAULT_SETTINGS["search_history_ttl_days"]))
        self.settings_search_history_ttl_days.setPrefix(_("Search History TTL: "))
        self.settings_search_history_ttl_days.setSuffix(_(" days"))
        _disable_wheel_scroll(self.settings_search_history_ttl_days)

        self.settings_thumbnail_cache_max = QSpinBox()
        self.settings_thumbnail_cache_max.setRange(50, 2000)
        self.settings_thumbnail_cache_max.setValue(int(DEFAULT_SETTINGS["thumbnail_cache_max"]))

        self.settings_clean_metadata = QCheckBox(_("AI Metadata Cleaner (Remove [Official], (2024), etc. from titles)"))
        self.settings_clean_metadata.setChecked(bool(DEFAULT_SETTINGS.get("clean_metadata", True)))
        self.settings_thumbnail_cache_max.setPrefix(_("Thumbnail Cache: "))
        self.settings_thumbnail_cache_max.setSuffix(_(" items"))
        _disable_wheel_scroll(self.settings_thumbnail_cache_max)

        self.settings_aria2 = QCheckBox(_("استخدام aria2 للتسريع"))
        self.settings_aria2.setChecked(True)

        self.settings_use_ytdlp_api = QCheckBox(_("استخدام yt-dlp API (تجريبي)"))
        self.settings_use_ytdlp_api.setChecked(bool(DEFAULT_SETTINGS.get("use_ytdlp_api", False)))
        
        self.settings_use_native_engine = QCheckBox(_("استخدام المحرك الأصيل الذكي (Native Smart Engine)"))
        self.settings_use_native_engine.setChecked(bool(DEFAULT_SETTINGS.get("use_native_engine", True)))

        self.settings_storage_guard = QCheckBox(_("إيقاف التحميل مؤقتاً عند انخفاض المساحة"))
        self.settings_storage_min_free_gb = QSpinBox()
        self.settings_storage_min_free_gb.setRange(1, 500)
        self.settings_storage_min_free_gb.setValue(int(DEFAULT_SETTINGS["storage_min_free_gb"]))
        self.settings_storage_min_free_gb.setPrefix(_("الحد الأدنى للمساحة الحرة: "))
        self.settings_storage_min_free_gb.setSuffix(" GB")
        _disable_wheel_scroll(self.settings_storage_min_free_gb)
        
        self.settings_system_theme_sync = QCheckBox(_("مزامنة الثيم مع إعدادات ويندوز"))
        self.settings_bandwidth_scheduler = QCheckBox(_("تفعيل جدول تحديد السرعة"))
        self.settings_bandwidth_scheduler_summary = QLabel("")
        self.settings_bandwidth_scheduler_summary.setObjectName("single_sub")
        self.bandwidth_calendar = BandwidthCalendarWidget()
        self.bandwidth_calendar.scheduleChanged.connect(self.bandwidth_editor_changed.emit)
        
        self.settings_bandwidth_rule_list = QListWidget()
        self.settings_bandwidth_rule_list.setMinimumHeight(110)
        self.settings_bandwidth_rule_list.currentRowChanged.connect(self.bandwidth_rule_selected.emit)

        self.settings_bandwidth_rule_start = QTimeEdit()
        self.settings_bandwidth_rule_start.setDisplayFormat("HH:mm")
        self.settings_bandwidth_rule_start.setTime(QTime(8, 0))
        _disable_wheel_scroll(self.settings_bandwidth_rule_start)

        self.settings_bandwidth_rule_end = QTimeEdit()
        self.settings_bandwidth_rule_end.setDisplayFormat("HH:mm")
        self.settings_bandwidth_rule_end.setTime(QTime(12, 0))
        _disable_wheel_scroll(self.settings_bandwidth_rule_end)

        self.settings_bandwidth_rule_limit = QSpinBox()
        self.settings_bandwidth_rule_limit.setRange(0, 1024 * 1024)
        self.settings_bandwidth_rule_limit.setSuffix(" KB/s")
        self.settings_bandwidth_rule_limit.setSpecialValueText(_("0 KB/s (غير محدود)"))
        _disable_wheel_scroll(self.settings_bandwidth_rule_limit)

        self.settings_bandwidth_rule_label = QLineEdit()
        self.settings_bandwidth_rule_label.setPlaceholderText(_("اسم القاعدة (اختياري)"))

        new_bandwidth_rule_btn = QPushButton(_("قاعدة جديدة"))
        new_bandwidth_rule_btn.setObjectName("action_trim")
        new_bandwidth_rule_btn.clicked.connect(self.bandwidth_new_rule_requested.emit)

        save_bandwidth_rule_btn = QPushButton(_("حفظ داخل المحرر"))
        save_bandwidth_rule_btn.setObjectName("action_schedule")
        save_bandwidth_rule_btn.clicked.connect(self.bandwidth_save_rule_requested.emit)

        remove_bandwidth_rule_btn = QPushButton(_("حذف المحددة"))
        remove_bandwidth_rule_btn.setObjectName("action_trim")
        remove_bandwidth_rule_btn.clicked.connect(self.bandwidth_remove_rule_requested.emit)

        self.settings_bandwidth_schedule_editor = QTextEdit()
        self.settings_bandwidth_schedule_editor.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.settings_bandwidth_schedule_editor.setPlaceholderText(
            '[{"start": "08:00", "end": "12:00", "limit_kbps": 2048, "label": "Morning"}]'
        )
        self.settings_bandwidth_schedule_editor.setMinimumHeight(150)
        self.settings_bandwidth_schedule_editor.textChanged.connect(self.bandwidth_editor_changed.emit)

        self.settings_bandwidth_schedule_status = QLabel("")
        self.settings_bandwidth_schedule_status.setObjectName("single_sub")

        self.pipeline_editor = PipelineEditor(self)

        apply_bandwidth_schedule_btn = QPushButton(_("تطبيق جدول السرعة"))
        apply_bandwidth_schedule_btn.setObjectName("action_schedule")
        apply_bandwidth_schedule_btn.clicked.connect(self.bandwidth_apply_schedule_requested.emit)

        reset_bandwidth_schedule_btn = QPushButton(_("استعادة الجدول الافتراضي"))
        reset_bandwidth_schedule_btn.setObjectName("action_trim")
        reset_bandwidth_schedule_btn.clicked.connect(self.bandwidth_reset_schedule_requested.emit)
        
        self.settings_cookies = QLineEdit()
        self.settings_cookies.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.settings_cookies.setPlaceholderText(_("Path to cookies.txt (optional)"))
        pick_cookies_btn = QPushButton(_("Browse Cookies"))
        pick_cookies_btn.setObjectName("icon_btn")
        pick_cookies_btn.setFixedWidth(100)
        pick_cookies_btn.clicked.connect(self.cookie_picker_requested.emit)
        
        browsers_available = get_available_browsers()
        auto_cookie_btn = QPushButton(
            _("🍪 Auto-Import Cookies ({browsers})").format(browsers=", ".join(browsers_available) or _("None found"))
        )
        auto_cookie_btn.setObjectName("action_trim")
        auto_cookie_btn.setEnabled(bool(browsers_available))
        auto_cookie_btn.clicked.connect(self.auto_cookie_requested.emit)

        self.settings_cookie_profile_combo = QComboBox()
        self.settings_cookie_profile_combo.setEditable(False)
        self.settings_cookie_profile_combo.addItem(_("Default (no profile)"))
        _disable_wheel_scroll(self.settings_cookie_profile_combo)

        self.settings_cookie_profile_name = QLineEdit()
        self.settings_cookie_profile_name.setPlaceholderText(_("Cookie profile name"))

        save_cookie_profile_btn = QPushButton(_("Save Profile"))
        save_cookie_profile_btn.setObjectName("action_schedule")
        save_cookie_profile_btn.clicked.connect(self.cookie_profile_save_requested.emit)
        load_cookie_profile_btn = QPushButton(_("Load Profile"))
        load_cookie_profile_btn.setObjectName("action_trim")
        load_cookie_profile_btn.clicked.connect(self.cookie_profile_load_requested.emit)
        delete_cookie_profile_btn = QPushButton(_("Delete Profile"))
        delete_cookie_profile_btn.setObjectName("action_trim")
        delete_cookie_profile_btn.clicked.connect(self.cookie_profile_delete_requested.emit)

        self.settings_post_download_script = QLineEdit()
        self.settings_post_download_script.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.settings_post_download_script.setPlaceholderText(_("Path to post-download script (optional)"))
        pick_post_script_btn = QPushButton(_("Browse Script"))
        pick_post_script_btn.setObjectName("icon_btn")
        pick_post_script_btn.setFixedWidth(120)
        pick_post_script_btn.clicked.connect(self.post_script_picker_requested.emit)

        self.settings_embed_subs = QCheckBox(_("Embed Subtitles in Video"))
        self.settings_embed_subs.setChecked(True)
        self.settings_hard_burn_subs = QCheckBox(_("Hard-burn subtitles into video (re-encode)"))
        self.settings_whisper_fallback_enabled = QCheckBox(_("🎙 Generate Subtitles with Whisper (Fallback)"))
        self.settings_split_chapters = QCheckBox(_("Split Videos by Chapters (YouTube)"))
        self.settings_verify_checksum = QCheckBox(_("Verify File Integrity (SHA-256) after Download"))
        self.settings_virus_scan_after_download = QCheckBox(_("Scan Downloaded Files with Windows Defender"))
        self.settings_sponsorblock_enabled = QCheckBox(_("✂️ Remove Sponsors automatically (SponsorBlock)"))
        self.settings_normalize_audio_postprocess = QCheckBox(_("🎧 Normalize Audio Post-Processing (EBU R128)"))
        self.settings_auto_categorize_downloads = QCheckBox(_("Auto-organize downloads after completion"))
        self.settings_auto_categorize_mode = QComboBox()
        self.settings_auto_categorize_mode.addItem(_("By media type"), "mode")
        self.settings_auto_categorize_mode.addItem(_("By file extension"), "extension")
        self.settings_auto_categorize_mode.addItem(_("Media type + extension"), "mode_then_extension")
        _disable_wheel_scroll(self.settings_auto_categorize_mode)

        self.merge_group = QGroupBox(_("Advanced Merging"))
        self.merge_group.setCheckable(True)
        self.merge_group.setChecked(False)
        merge_layout = QGridLayout(self.merge_group)

        self.settings_video_codec = QComboBox()
        self.settings_video_codec.addItems([
            "copy", 
            "h264", "h265", 
            "h264_nvenc", "hevc_nvenc", 
            "h264_qsv", "hevc_qsv", 
            "h264_amf", "hevc_amf",
            "av1", "libaom-av1", "av1_nvenc", "av1_qsv", "av1_amf"
        ])
        self.settings_video_crf = QSpinBox()
        self.settings_video_crf.setRange(0, 51)
        self.settings_video_crf.setValue(23)
        self.settings_audio_codec = QComboBox()
        self.settings_audio_codec.addItems(["copy", "aac", "opus"])
        self.settings_audio_bitrate = QLineEdit("192k")
        self.settings_merge_hw_encoder = QComboBox()
        self.settings_merge_hw_encoder.addItems(["off", "auto", "nvenc", "qsv", "amf"])
        self.settings_merge_force_reencode = QCheckBox(_("Force re-encode for incompatible streams"))
        self.settings_merge_video_preset = QComboBox()
        self.settings_merge_video_preset.addItems(["p1", "p2", "p3", "p4", "p5", "p6", "p7"])

        merge_layout.addWidget(QLabel(_("Video Codec:")), 0, 0)
        merge_layout.addWidget(self.settings_video_codec, 0, 1)
        merge_layout.addWidget(QLabel(_("Video CRF (Quality):")), 1, 0)
        merge_layout.addWidget(self.settings_video_crf, 1, 1)
        merge_layout.addWidget(QLabel(_("Audio Codec:")), 2, 0)
        merge_layout.addWidget(self.settings_audio_codec, 2, 1)
        merge_layout.addWidget(QLabel(_("Audio Bitrate:")), 3, 0)
        merge_layout.addWidget(self.settings_audio_bitrate, 3, 1)
        merge_layout.addWidget(QLabel(_("Hardware Encoder:")), 4, 0)
        merge_layout.addWidget(self.settings_merge_hw_encoder, 4, 1)
        merge_layout.addWidget(QLabel(_("Encoder Preset:")), 5, 0)
        merge_layout.addWidget(self.settings_merge_video_preset, 5, 1)
        merge_layout.addWidget(self.settings_merge_force_reencode, 6, 0, 1, 2)

        self.proxy_enabled_cb = QCheckBox(_("Enable Proxy"))
        self.proxy_enabled_cb.setChecked(proxy_manager.is_enabled())
        self.proxy_enabled_cb.toggled.connect(lambda v: proxy_manager.set_enabled(v))
        self.proxy_input = QLineEdit()
        self.proxy_input.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.proxy_input.setPlaceholderText(_("http://1.2.3.4:8080 or socks5://user:pass@host:port"))
        self.proxy_input.setText(proxy_manager.get_current_proxy() or "")
        add_proxy_btn = QPushButton(_("Add Proxy"))
        add_proxy_btn.setObjectName("action_trim")
        add_proxy_btn.clicked.connect(self.proxy_add_requested.emit)
        test_proxy_btn = QPushButton(_("Test Proxy"))
        test_proxy_btn.setObjectName("action_schedule")
        test_proxy_btn.clicked.connect(self.proxy_test_requested.emit)

        self.sustainability_combo = QComboBox()
        self.sustainability_combo.addItems([f"{k} — {v}" for k, v in SUSTAINABILITY_ACTIONS.items()])
        _disable_wheel_scroll(self.sustainability_combo)
        self.sustainability_spin = QSpinBox()
        self.sustainability_spin.setRange(0, 3600)
        self.sustainability_spin.setPrefix(_("Delay: "))
        self.sustainability_spin.setSuffix(" s")
        _disable_wheel_scroll(self.sustainability_spin)
        apply_sustainability_btn = QPushButton(_("Apply After-Queue Action"))
        apply_sustainability_btn.setObjectName("action_trim")
        apply_sustainability_btn.clicked.connect(self.sustainability_apply_requested.emit)

        normalize_btn = QPushButton(_("🎧 Normalize All Audio in Downloads Folder"))
        normalize_btn.setObjectName("action_schedule")
        normalize_btn.clicked.connect(self.normalize_requested.emit)

        export_btn = QPushButton(_("Export Settings"))
        export_btn.setObjectName("action_trim")
        export_btn.clicked.connect(self.export_settings_requested.emit)
        export_qr_btn = QPushButton(_("Export Settings QR"))
        export_qr_btn.setObjectName("action_trim")
        export_qr_btn.clicked.connect(self.export_settings_qr_requested.emit)
        import_btn = QPushButton(_("Import Settings"))
        import_btn.setObjectName("action_schedule")
        import_btn.clicked.connect(self.import_settings_requested.emit)

        update_ytdlp_btn = QPushButton(_("Check for yt-dlp Updates"))
        update_ytdlp_btn.setObjectName("action_trim")
        update_ytdlp_btn.clicked.connect(self.update_ytdlp_requested.emit)

        update_app_btn = QPushButton(_("Check for App Updates"))
        update_app_btn.setObjectName("action_download")
        update_app_btn.clicked.connect(self.check_app_updates_requested.emit)
        
        mini_btn = QPushButton(_("🪟 Toggle Mini Mode"))
        mini_btn.setObjectName("action_schedule")
        mini_btn.clicked.connect(self.mini_mode_requested.emit)

        # Build Pages
        
        # 1. General & Appearance Page
        def build_general(layout):
            layout.addWidget(QLabel(_("UI Language:")))
            layout.addWidget(self.settings_language_combo)
            layout.addWidget(QLabel(_("File Naming Template:")))
            layout.addWidget(self.settings_rename_template)
            layout.addWidget(self.settings_system_theme_sync)
            layout.addWidget(self.settings_search_history_limit)
            layout.addWidget(self.settings_search_history_ttl_days)
            layout.addWidget(self.settings_thumbnail_cache_max)
            layout.addWidget(self.settings_clean_metadata)
            
        page_general = self._create_page(build_general)
        self.stacked_widget.addWidget(page_general)
        self.sidebar.addItem("🎨 " + _("General & Appearance"))

        # 2. Download Settings Page
        def build_download(layout):
            layout.addWidget(QLabel(_("Download Path:")))
            dir_layout = QHBoxLayout()
            dir_layout.addWidget(self.settings_out_dir)
            dir_layout.addWidget(self.pick_dir_btn)
            layout.addLayout(dir_layout)
            layout.addWidget(self.settings_concurrent)
            layout.addWidget(self.settings_retries)
            layout.addWidget(self.settings_retry_delay)
            layout.addWidget(self.settings_queue_retry_limit)
            layout.addWidget(QLabel(_("Queue Priority:")))
            layout.addWidget(self.settings_queue_priority)
            layout.addWidget(self.settings_storage_guard)
            layout.addWidget(self.settings_storage_min_free_gb)
            
        page_download = self._create_page(build_download)
        self.stacked_widget.addWidget(page_download)
        self.sidebar.addItem("⬇️ " + _("Download Settings"))

        # 3. Advanced Features Page
        def build_advanced(layout):
            layout.addWidget(self.settings_aria2)
            layout.addWidget(self.settings_use_ytdlp_api)
            layout.addWidget(self.settings_use_native_engine)
            layout.addWidget(self.settings_embed_subs)
            layout.addWidget(self.settings_hard_burn_subs)
            layout.addWidget(self.settings_whisper_fallback_enabled)
            layout.addWidget(self.settings_split_chapters)
            layout.addWidget(self.settings_verify_checksum)
            layout.addWidget(self.settings_virus_scan_after_download)
            layout.addWidget(self.settings_sponsorblock_enabled)
            layout.addWidget(self.settings_normalize_audio_postprocess)
            layout.addWidget(self.settings_auto_categorize_downloads)
            layout.addWidget(self.settings_auto_categorize_mode)
            
            layout.addWidget(QLabel(_("Cookies File (Netscape format):")))
            cr = QHBoxLayout()
            cr.addWidget(self.settings_cookies)
            cr.addWidget(pick_cookies_btn)
            layout.addLayout(cr)
            layout.addWidget(auto_cookie_btn)
            
            self.settings_cookies_from_browser = QComboBox()
            self.settings_cookies_from_browser.addItems(["none", "chrome", "firefox", "edge", "opera", "brave", "safari", "vivaldi"])
            layout.addWidget(QLabel(_("Import Cookies from Browser:")))
            layout.addWidget(self.settings_cookies_from_browser)

            layout.addWidget(QLabel(_("Cookie Profiles:")))
            layout.addWidget(self.settings_cookie_profile_combo)
            cpn = QHBoxLayout()
            cpn.addWidget(self.settings_cookie_profile_name)
            cpn.addWidget(save_cookie_profile_btn)
            cpn.addWidget(load_cookie_profile_btn)
            cpn.addWidget(delete_cookie_profile_btn)
            layout.addLayout(cpn)
            layout.addWidget(QLabel(_("Post-Download Script (Default for new tasks):")))
            psr = QHBoxLayout()
            psr.addWidget(self.settings_post_download_script)
            psr.addWidget(pick_post_script_btn)
            layout.addLayout(psr)
            layout.addWidget(QLabel(_("Post-Processing Pipeline:")))
            layout.addWidget(self.pipeline_editor)
            
            layout.addWidget(self.merge_group)

        page_advanced = self._create_page(build_advanced)
        self.stacked_widget.addWidget(page_advanced)
        self.sidebar.addItem("⚙️ " + _("Advanced Features"))

        # 4. Proxy & Bandwidth Page
        def build_proxy(layout):
            layout.addWidget(QLabel(_("🔐 Proxy Manager:")))
            layout.addWidget(self.proxy_enabled_cb)
            proxy_row = QHBoxLayout()
            proxy_row.addWidget(self.proxy_input)
            proxy_row.addWidget(add_proxy_btn)
            proxy_row.addWidget(test_proxy_btn)
            layout.addLayout(proxy_row)
            
            layout.addWidget(self.settings_bandwidth_scheduler)
            layout.addWidget(self.settings_bandwidth_scheduler_summary)
            layout.addWidget(QLabel(_("24-Hour Bandwidth Calendar:")))
            layout.addWidget(self.bandwidth_calendar)
            bandwidth_editor_hint = QLabel(_("اختر قاعدة من القائمة لتعديلها أو اترك التحديد فارغًا لإضافة قاعدة جديدة."))
            bandwidth_editor_hint.setObjectName("single_sub")
            layout.addWidget(bandwidth_editor_hint)
            layout.addWidget(self.settings_bandwidth_rule_list)
            
            bandwidth_rule_form = QGridLayout()
            bandwidth_rule_form.addWidget(QLabel(_("من")), 0, 0)
            bandwidth_rule_form.addWidget(self.settings_bandwidth_rule_start, 0, 1)
            bandwidth_rule_form.addWidget(QLabel(_("إلى")), 0, 2)
            bandwidth_rule_form.addWidget(self.settings_bandwidth_rule_end, 0, 3)
            bandwidth_rule_form.addWidget(QLabel(_("الحد")), 1, 0)
            bandwidth_rule_form.addWidget(self.settings_bandwidth_rule_limit, 1, 1)
            bandwidth_rule_form.addWidget(QLabel(_("الاسم")), 1, 2)
            bandwidth_rule_form.addWidget(self.settings_bandwidth_rule_label, 1, 3)
            layout.addLayout(bandwidth_rule_form)
            
            bandwidth_rule_actions = QHBoxLayout()
            bandwidth_rule_actions.addWidget(new_bandwidth_rule_btn)
            bandwidth_rule_actions.addWidget(save_bandwidth_rule_btn)
            bandwidth_rule_actions.addWidget(remove_bandwidth_rule_btn)
            layout.addLayout(bandwidth_rule_actions)
            
            layout.addWidget(QLabel(_("قواعد جدول السرعة (JSON):")))
            layout.addWidget(self.settings_bandwidth_schedule_editor)
            layout.addWidget(self.settings_bandwidth_schedule_status)
            
            bandwidth_schedule_actions = QHBoxLayout()
            bandwidth_schedule_actions.addWidget(apply_bandwidth_schedule_btn)
            bandwidth_schedule_actions.addWidget(reset_bandwidth_schedule_btn)
            layout.addLayout(bandwidth_schedule_actions)

        page_proxy = self._create_page(build_proxy)
        self.stacked_widget.addWidget(page_proxy)
        self.sidebar.addItem("🌐 " + _("Proxy & Bandwidth"))

        # 5. System & Tools Page
        def build_system(layout):
            layout.addWidget(QLabel(_("🔋 After-Queue Power Action:")))
            sus_layout = QHBoxLayout()
            sus_layout.addWidget(self.sustainability_combo)
            sus_layout.addWidget(self.sustainability_spin)
            layout.addLayout(sus_layout)
            layout.addWidget(apply_sustainability_btn)
            
            layout.addWidget(normalize_btn)
            
            sys_actions_layout = QGridLayout()
            sys_actions_layout.addWidget(export_btn, 0, 0)
            sys_actions_layout.addWidget(export_qr_btn, 0, 1)
            sys_actions_layout.addWidget(import_btn, 0, 2)
            sys_actions_layout.addWidget(update_ytdlp_btn, 1, 0)
            sys_actions_layout.addWidget(update_app_btn, 1, 1)
            sys_actions_layout.addWidget(mini_btn, 1, 2)
            layout.addLayout(sys_actions_layout)

        page_system = self._create_page(build_system)
        self.stacked_widget.addWidget(page_system)
        self.sidebar.addItem("🛠️ " + _("System & Tools"))

        # Set initial tab
        self.sidebar.setCurrentRow(0)

    def set_bandwidth_scheduler_checked(self, checked: bool):
        if hasattr(self, "settings_bandwidth_scheduler"):
            self.settings_bandwidth_scheduler.setChecked(bool(checked))

    def set_bandwidth_scheduler_summary(self, text: str):
        if hasattr(self, "settings_bandwidth_scheduler_summary"):
            self.settings_bandwidth_scheduler_summary.setText(str(text or ""))

    def set_bandwidth_schedule_status(self, text: str):
        if hasattr(self, "settings_bandwidth_schedule_status"):
            self.settings_bandwidth_schedule_status.setText(str(text or ""))

    def get_bandwidth_schedule_editor_text(self) -> str:
        if not hasattr(self, "settings_bandwidth_schedule_editor"):
            return ""
        return str(self.settings_bandwidth_schedule_editor.toPlainText() or "")

    def set_bandwidth_schedule_editor_text(self, text: str, block_signals: bool = True):
        if not hasattr(self, "settings_bandwidth_schedule_editor"):
            return
        if block_signals:
            self.settings_bandwidth_schedule_editor.blockSignals(True)
        self.settings_bandwidth_schedule_editor.setPlainText(str(text or ""))
        if block_signals:
            self.settings_bandwidth_schedule_editor.blockSignals(False)
        if hasattr(self, "bandwidth_calendar"):
            try:
                import json

                self.bandwidth_calendar.import_schedule(list(json.loads(str(text or "") or "[]")))
            except Exception:
                pass

    def set_bandwidth_rule_list_items(self, items: list[str], selected_index: int | None = None, block_signals: bool = True):
        if not hasattr(self, "settings_bandwidth_rule_list"):
            return
        if block_signals:
            self.settings_bandwidth_rule_list.blockSignals(True)
        self.settings_bandwidth_rule_list.clear()
        for item in items or []:
            self.settings_bandwidth_rule_list.addItem(str(item))
        if block_signals:
            self.settings_bandwidth_rule_list.blockSignals(False)
        if selected_index is None or selected_index < 0:
            return
        try:
            self.settings_bandwidth_rule_list.setCurrentRow(int(selected_index))
        except Exception:
            return

    def clear_bandwidth_rule_selection(self, block_signals: bool = True):
        if not hasattr(self, "settings_bandwidth_rule_list"):
            return
        if block_signals:
            self.settings_bandwidth_rule_list.blockSignals(True)
        self.settings_bandwidth_rule_list.clearSelection()
        if block_signals:
            self.settings_bandwidth_rule_list.blockSignals(False)

    def get_bandwidth_rule_form(self) -> dict:
        if not hasattr(self, "settings_bandwidth_rule_start"):
            return {"start": "08:00", "end": "12:00", "limit_kbps": 0, "label": ""}
        return {
            "start": self.settings_bandwidth_rule_start.time().toString("HH:mm"),
            "end": self.settings_bandwidth_rule_end.time().toString("HH:mm") if hasattr(self, "settings_bandwidth_rule_end") else "12:00",
            "limit_kbps": int(self.settings_bandwidth_rule_limit.value()) if hasattr(self, "settings_bandwidth_rule_limit") else 0,
            "label": str(self.settings_bandwidth_rule_label.text() if hasattr(self, "settings_bandwidth_rule_label") else "").strip(),
        }

    def set_bandwidth_rule_form(self, rule: dict | None = None):
        if not hasattr(self, "settings_bandwidth_rule_start"):
            return
        value = rule or {"start": "08:00", "end": "12:00", "limit_kbps": 0, "label": ""}
        start_time = QTime.fromString(str(value.get("start", "08:00")), "HH:mm")
        end_time = QTime.fromString(str(value.get("end", "12:00")), "HH:mm")
        if not start_time.isValid():
            start_time = QTime(8, 0)
        if not end_time.isValid():
            end_time = QTime(12, 0)
        self.settings_bandwidth_rule_start.setTime(start_time)
        if hasattr(self, "settings_bandwidth_rule_end"):
            self.settings_bandwidth_rule_end.setTime(end_time)
        if hasattr(self, "settings_bandwidth_rule_limit"):
            self.settings_bandwidth_rule_limit.setValue(max(0, int(value.get("limit_kbps", 0) or 0)))
        if hasattr(self, "settings_bandwidth_rule_label"):
            self.settings_bandwidth_rule_label.setText(str(value.get("label", "") or ""))

    def autosave_controls(self) -> list:
        controls = []
        for name in (
            "settings_out_dir",
            "settings_retries",
            "settings_retry_delay",
            "settings_queue_retry_limit",
            "settings_queue_priority",
            "settings_concurrent",
            "settings_search_history_limit",
            "settings_search_history_ttl_days",
            "settings_thumbnail_cache_max",
            "settings_clean_metadata",
            "settings_storage_guard",
            "settings_storage_min_free_gb",
            "settings_system_theme_sync",
            "settings_bandwidth_scheduler",
            "settings_aria2",
            "settings_cookies",
            "settings_cookie_profile_combo",
            "settings_cookie_profile_name",
            "settings_post_download_script",
            "pipeline_editor",
            "settings_embed_subs",
            "settings_hard_burn_subs",
            "settings_whisper_fallback_enabled",
            "settings_rename_template",
            "settings_split_chapters",
            "settings_verify_checksum",
            "settings_virus_scan_after_download",
            "settings_sponsorblock_enabled",
            "settings_normalize_audio_postprocess",
            "settings_auto_categorize_downloads",
            "settings_auto_categorize_mode",
            "settings_use_ytdlp_api",
            "settings_use_native_engine",
            "proxy_enabled_cb",
            "proxy_input",
            "sustainability_combo",
            "sustainability_spin",
            "merge_group",
            "settings_video_codec",
            "settings_video_crf",
            "settings_audio_codec",
            "settings_audio_bitrate",
        ):
            if hasattr(self, name):
                controls.append(getattr(self, name))
        return controls

    def get_out_dir(self) -> str:
        if not hasattr(self, "settings_out_dir"):
            return ""
        return str(self.settings_out_dir.text() or "").strip()

    def set_out_dir(self, path: str):
        if hasattr(self, "settings_out_dir"):
            self.settings_out_dir.setText(str(path or "").strip())

    def get_proxy_text(self) -> str:
        if not hasattr(self, "proxy_input"):
            return ""
        return str(self.proxy_input.text() or "").strip()

    def get_sustainability_config(self) -> dict:
        action = ""
        if hasattr(self, "sustainability_combo"):
            action = str(self.sustainability_combo.currentText() or "").split(" — ")[0].strip()
        delay_seconds = 60
        if hasattr(self, "sustainability_spin"):
            delay_seconds = int(self.sustainability_spin.value())
        return {
            "action": action,
            "delay_seconds": delay_seconds,
        }

    def get_form_settings(self) -> dict:
        queue_priority = "fifo"
        if hasattr(self, "settings_queue_priority"):
            queue_priority = str(self.settings_queue_priority.currentData() or "fifo")

        rename_template = "Default"
        if hasattr(self, "settings_rename_template"):
            rename_template = str(self.settings_rename_template.currentText() or "Default")

        ui_language = ""
        if hasattr(self, "settings_language_combo"):
            ui_language = str(self.settings_language_combo.currentData() or "")

        return {
            "out_dir": str(self.settings_out_dir.text() if hasattr(self, "settings_out_dir") else "").strip(),
            "retries": int(self.settings_retries.value()) if hasattr(self, "settings_retries") else int(DEFAULT_SETTINGS.get("retries", 3)),
            "auto_retry_delay_seconds": int(self.settings_retry_delay.value()) if hasattr(self, "settings_retry_delay") else int(DEFAULT_SETTINGS.get("auto_retry_delay_seconds", 5)),
            "queue_auto_retry_limit": int(self.settings_queue_retry_limit.value()) if hasattr(self, "settings_queue_retry_limit") else int(DEFAULT_SETTINGS.get("queue_auto_retry_limit", 0)),
            "queue_priority": queue_priority,
            "max_concurrent": int(self.settings_concurrent.value()) if hasattr(self, "settings_concurrent") else int(DEFAULT_SETTINGS.get("max_concurrent", 1)),
            "search_history_limit": int(self.settings_search_history_limit.value()) if hasattr(self, "settings_search_history_limit") else int(DEFAULT_SETTINGS.get("search_history_limit", 50)),
            "search_history_ttl_days": int(self.settings_search_history_ttl_days.value()) if hasattr(self, "settings_search_history_ttl_days") else int(DEFAULT_SETTINGS.get("search_history_ttl_days", 30)),
            "thumbnail_cache_max": int(self.settings_thumbnail_cache_max.value()) if hasattr(self, "settings_thumbnail_cache_max") else int(DEFAULT_SETTINGS.get("thumbnail_cache_max", 200)),
            "clean_metadata": bool(self.settings_clean_metadata.isChecked()) if hasattr(self, "settings_clean_metadata") else bool(DEFAULT_SETTINGS.get("clean_metadata", True)),
            "storage_guard_enabled": bool(self.settings_storage_guard.isChecked()) if hasattr(self, "settings_storage_guard") else bool(DEFAULT_SETTINGS.get("storage_guard_enabled", False)),
            "storage_min_free_gb": int(self.settings_storage_min_free_gb.value()) if hasattr(self, "settings_storage_min_free_gb") else int(DEFAULT_SETTINGS.get("storage_min_free_gb", 2)),
            "system_theme_sync_enabled": bool(self.settings_system_theme_sync.isChecked()) if hasattr(self, "settings_system_theme_sync") else True,
            "bandwidth_scheduler_enabled": bool(self.settings_bandwidth_scheduler.isChecked()) if hasattr(self, "settings_bandwidth_scheduler") else False,
            "bandwidth_schedule_grid": self.bandwidth_calendar.export_schedule() if hasattr(self, "bandwidth_calendar") else [],
            "cookies_path": str(self.settings_cookies.text() if hasattr(self, "settings_cookies") else "").strip(),
            "cookie_profile_name": str(self.settings_cookie_profile_combo.currentText() if hasattr(self, "settings_cookie_profile_combo") else "").strip(),
            "cookies_from_browser": str(self.settings_cookies_from_browser.currentText() if hasattr(self, "settings_cookies_from_browser") else "none").strip().lower() or "none",
            "post_download_script": str(self.settings_post_download_script.text() if hasattr(self, "settings_post_download_script") else "").strip(),
            "post_process_pipeline": self.pipeline_editor.pipeline() if hasattr(self, "pipeline_editor") else [],
            "embed_subs": bool(self.settings_embed_subs.isChecked()) if hasattr(self, "settings_embed_subs") else True,
            "hard_burn_subs": bool(self.settings_hard_burn_subs.isChecked()) if hasattr(self, "settings_hard_burn_subs") else bool(DEFAULT_SETTINGS.get("hard_burn_subs", False)),
            "whisper_fallback": bool(self.settings_whisper_fallback_enabled.isChecked()) if hasattr(self, "settings_whisper_fallback_enabled") else False,
            "split_chapters": bool(self.settings_split_chapters.isChecked()) if hasattr(self, "settings_split_chapters") else False,
            "verify_checksum": bool(self.settings_verify_checksum.isChecked()) if hasattr(self, "settings_verify_checksum") else False,
            "virus_scan_after_download": bool(self.settings_virus_scan_after_download.isChecked()) if hasattr(self, "settings_virus_scan_after_download") else bool(DEFAULT_SETTINGS.get("virus_scan_after_download", False)),
            "sponsorblock_enabled": bool(self.settings_sponsorblock_enabled.isChecked()) if hasattr(self, "settings_sponsorblock_enabled") else bool(DEFAULT_SETTINGS.get("sponsorblock_enabled", False)),
            "normalize_audio_postprocess": bool(self.settings_normalize_audio_postprocess.isChecked()) if hasattr(self, "settings_normalize_audio_postprocess") else bool(DEFAULT_SETTINGS.get("normalize_audio_postprocess", False)),
            "auto_categorize_downloads": bool(self.settings_auto_categorize_downloads.isChecked()) if hasattr(self, "settings_auto_categorize_downloads") else bool(DEFAULT_SETTINGS.get("auto_categorize_downloads", False)),
            "auto_categorize_mode": str(
                (
                    self.settings_auto_categorize_mode.currentData()
                    if hasattr(self, "settings_auto_categorize_mode")
                    else DEFAULT_SETTINGS.get("auto_categorize_mode", "off")
                )
                or (
                    self.settings_auto_categorize_mode.currentText()
                    if hasattr(self, "settings_auto_categorize_mode")
                    else DEFAULT_SETTINGS.get("auto_categorize_mode", "off")
                )
                or DEFAULT_SETTINGS.get("auto_categorize_mode", "off")
            ).strip(),
            "rename_template": rename_template,
            "use_ytdlp_api": bool(self.settings_use_ytdlp_api.isChecked()) if hasattr(self, "settings_use_ytdlp_api") else bool(DEFAULT_SETTINGS.get("use_ytdlp_api", False)),
            "use_native_engine": bool(self.settings_use_native_engine.isChecked()) if hasattr(self, "settings_use_native_engine") else bool(DEFAULT_SETTINGS.get("use_native_engine", True)),
            "use_aria2": bool(self.settings_aria2.isChecked()) if hasattr(self, "settings_aria2") else True,
            "proxy_enabled": bool(self.proxy_enabled_cb.isChecked()) if hasattr(self, "proxy_enabled_cb") else False,
            "proxy": str(self.proxy_input.text() if hasattr(self, "proxy_input") else "").strip(),
            "sustainability_action": str(self.sustainability_combo.currentText() if hasattr(self, "sustainability_combo") else "").split(" — ")[0].strip(),
            "sustainability_delay_seconds": int(self.sustainability_spin.value()) if hasattr(self, "sustainability_spin") else 60,
            "ui_language": ui_language,
            "custom_merge_enabled": bool(self.merge_group.isChecked()) if hasattr(self, "merge_group") else False,
            "custom_merge_video_codec": str(self.settings_video_codec.currentText()) if hasattr(self, "settings_video_codec") else "copy",
            "custom_merge_video_crf": int(self.settings_video_crf.value()) if hasattr(self, "settings_video_crf") else 23,
            "custom_merge_audio_codec": str(self.settings_audio_codec.currentText()) if hasattr(self, "settings_audio_codec") else "aac",
            "custom_merge_audio_bitrate": str(self.settings_audio_bitrate.text() if hasattr(self, "settings_audio_bitrate") else "192k").strip(),
            "custom_merge_hw_encoder": str(self.settings_merge_hw_encoder.currentText()) if hasattr(self, "settings_merge_hw_encoder") else "off",
            "custom_merge_force_reencode": bool(self.settings_merge_force_reencode.isChecked()) if hasattr(self, "settings_merge_force_reencode") else False,
            "custom_merge_video_preset": str(self.settings_merge_video_preset.currentText()) if hasattr(self, "settings_merge_video_preset") else "p5",
        }

    def apply_form_settings(self, settings: dict, block_signals: bool = True):
        if not isinstance(settings, dict):
            return

        def _maybe_block(widget, flag: bool):
            if widget is None:
                return
            try:
                widget.blockSignals(bool(flag))
            except Exception:
                return

        out_dir = str(settings.get("out_dir", "") or "")
        if hasattr(self, "settings_out_dir"):
            _maybe_block(self.settings_out_dir, block_signals)
            self.settings_out_dir.setText(out_dir)
            _maybe_block(self.settings_out_dir, False)

        if hasattr(self, "settings_retries"):
            _maybe_block(self.settings_retries, block_signals)
            self.settings_retries.setValue(max(1, int(settings.get("retries", self.settings_retries.value()) or self.settings_retries.value())))
            _maybe_block(self.settings_retries, False)

        if hasattr(self, "settings_retry_delay"):
            _maybe_block(self.settings_retry_delay, block_signals)
            self.settings_retry_delay.setValue(max(1, int(settings.get("auto_retry_delay_seconds", self.settings_retry_delay.value()) or self.settings_retry_delay.value())))
            _maybe_block(self.settings_retry_delay, False)

        if hasattr(self, "settings_queue_retry_limit"):
            _maybe_block(self.settings_queue_retry_limit, block_signals)
            self.settings_queue_retry_limit.setValue(max(0, int(settings.get("queue_auto_retry_limit", self.settings_queue_retry_limit.value()) or self.settings_queue_retry_limit.value())))
            _maybe_block(self.settings_queue_retry_limit, False)

        if hasattr(self, "settings_queue_priority"):
            _maybe_block(self.settings_queue_priority, block_signals)
            desired = str(settings.get("queue_priority", "") or "")
            idx = self.settings_queue_priority.findData(desired)
            if idx >= 0:
                self.settings_queue_priority.setCurrentIndex(idx)
            _maybe_block(self.settings_queue_priority, False)

        if hasattr(self, "settings_concurrent"):
            _maybe_block(self.settings_concurrent, block_signals)
            self.settings_concurrent.setValue(max(1, int(settings.get("max_concurrent", self.settings_concurrent.value()) or self.settings_concurrent.value())))
            _maybe_block(self.settings_concurrent, False)

        if hasattr(self, "settings_search_history_limit"):
            _maybe_block(self.settings_search_history_limit, block_signals)
            self.settings_search_history_limit.setValue(max(5, int(settings.get("search_history_limit", self.settings_search_history_limit.value()) or self.settings_search_history_limit.value())))
            _maybe_block(self.settings_search_history_limit, False)

        if hasattr(self, "settings_search_history_ttl_days"):
            _maybe_block(self.settings_search_history_ttl_days, block_signals)
            self.settings_search_history_ttl_days.setValue(max(1, int(settings.get("search_history_ttl_days", self.settings_search_history_ttl_days.value()) or self.settings_search_history_ttl_days.value())))
            _maybe_block(self.settings_search_history_ttl_days, False)

        if hasattr(self, "settings_thumbnail_cache_max"):
            _maybe_block(self.settings_thumbnail_cache_max, block_signals)
            self.settings_thumbnail_cache_max.setValue(max(50, int(settings.get("thumbnail_cache_max", self.settings_thumbnail_cache_max.value()) or self.settings_thumbnail_cache_max.value())))
            _maybe_block(self.settings_thumbnail_cache_max, False)

        if hasattr(self, "settings_clean_metadata"):
            _maybe_block(self.settings_clean_metadata, block_signals)
            self.settings_clean_metadata.setChecked(bool(settings.get("clean_metadata", self.settings_clean_metadata.isChecked())))
            _maybe_block(self.settings_clean_metadata, False)

        if hasattr(self, "settings_storage_guard"):
            _maybe_block(self.settings_storage_guard, block_signals)
            self.settings_storage_guard.setChecked(bool(settings.get("storage_guard_enabled", self.settings_storage_guard.isChecked())))
            _maybe_block(self.settings_storage_guard, False)

        if hasattr(self, "settings_storage_min_free_gb"):
            _maybe_block(self.settings_storage_min_free_gb, block_signals)
            self.settings_storage_min_free_gb.setValue(max(1, int(settings.get("storage_min_free_gb", self.settings_storage_min_free_gb.value()) or self.settings_storage_min_free_gb.value())))
            _maybe_block(self.settings_storage_min_free_gb, False)

        if hasattr(self, "settings_system_theme_sync"):
            _maybe_block(self.settings_system_theme_sync, block_signals)
            self.settings_system_theme_sync.setChecked(bool(settings.get("system_theme_sync_enabled", self.settings_system_theme_sync.isChecked())))
            _maybe_block(self.settings_system_theme_sync, False)

        if hasattr(self, "settings_bandwidth_scheduler"):
            _maybe_block(self.settings_bandwidth_scheduler, block_signals)
            self.settings_bandwidth_scheduler.setChecked(bool(settings.get("bandwidth_scheduler_enabled", self.settings_bandwidth_scheduler.isChecked())))
            _maybe_block(self.settings_bandwidth_scheduler, False)

        if hasattr(self, "bandwidth_calendar"):
            self.bandwidth_calendar.import_schedule(list(settings.get("bandwidth_schedule_grid", []) or []))

        if hasattr(self, "settings_cookies"):
            _maybe_block(self.settings_cookies, block_signals)
            self.settings_cookies.setText(str(settings.get("cookies_path", self.settings_cookies.text()) or "").strip())
            _maybe_block(self.settings_cookies, False)

        if hasattr(self, "settings_cookie_profile_name"):
            _maybe_block(self.settings_cookie_profile_name, block_signals)
            self.settings_cookie_profile_name.setText(str(settings.get("cookie_profile_name", self.settings_cookie_profile_name.text()) or "").strip())
            _maybe_block(self.settings_cookie_profile_name, False)

        if hasattr(self, "settings_cookie_profile_combo"):
            _maybe_block(self.settings_cookie_profile_combo, block_signals)
            desired = str(settings.get("cookie_profile_name", "") or "").strip()
            if desired:
                idx = self.settings_cookie_profile_combo.findText(desired)
                if idx >= 0:
                    self.settings_cookie_profile_combo.setCurrentIndex(idx)
            _maybe_block(self.settings_cookie_profile_combo, False)

        if hasattr(self, "settings_cookies_from_browser"):
            desired = str(settings.get("cookies_from_browser", "") or "").strip().lower()
            if desired:
                _maybe_block(self.settings_cookies_from_browser, block_signals)
                idx = self.settings_cookies_from_browser.findText(desired)
                if idx >= 0:
                    self.settings_cookies_from_browser.setCurrentIndex(idx)
                _maybe_block(self.settings_cookies_from_browser, False)

        if hasattr(self, "settings_post_download_script"):
            _maybe_block(self.settings_post_download_script, block_signals)
            self.settings_post_download_script.setText(str(settings.get("post_download_script", self.settings_post_download_script.text()) or "").strip())
            _maybe_block(self.settings_post_download_script, False)

        if hasattr(self, "pipeline_editor"):
            self.pipeline_editor.set_pipeline(list(settings.get("post_process_pipeline", []) or []), emit_signal=False)

        if hasattr(self, "settings_embed_subs"):
            _maybe_block(self.settings_embed_subs, block_signals)
            self.settings_embed_subs.setChecked(bool(settings.get("embed_subs", self.settings_embed_subs.isChecked())))
            _maybe_block(self.settings_embed_subs, False)

        if hasattr(self, "settings_hard_burn_subs"):
            _maybe_block(self.settings_hard_burn_subs, block_signals)
            self.settings_hard_burn_subs.setChecked(
                bool(
                    settings.get(
                        "hard_burn_subs",
                        self.settings_hard_burn_subs.isChecked(),
                    )
                )
            )
            _maybe_block(self.settings_hard_burn_subs, False)

        if hasattr(self, "settings_whisper_fallback_enabled"):
            _maybe_block(self.settings_whisper_fallback_enabled, block_signals)
            self.settings_whisper_fallback_enabled.setChecked(bool(settings.get("whisper_fallback", self.settings_whisper_fallback_enabled.isChecked())))
            _maybe_block(self.settings_whisper_fallback_enabled, False)

        if hasattr(self, "settings_sponsorblock_enabled"):
            _maybe_block(self.settings_sponsorblock_enabled, block_signals)
            self.settings_sponsorblock_enabled.setChecked(bool(settings.get("sponsorblock_enabled", self.settings_sponsorblock_enabled.isChecked())))
            _maybe_block(self.settings_sponsorblock_enabled, False)

        if hasattr(self, "settings_normalize_audio_postprocess"):
            _maybe_block(self.settings_normalize_audio_postprocess, block_signals)
            self.settings_normalize_audio_postprocess.setChecked(
                bool(
                    settings.get(
                        "normalize_audio_postprocess",
                        self.settings_normalize_audio_postprocess.isChecked(),
                    )
                )
            )
            _maybe_block(self.settings_normalize_audio_postprocess, False)

        if hasattr(self, "settings_auto_categorize_downloads"):
            _maybe_block(self.settings_auto_categorize_downloads, block_signals)
            self.settings_auto_categorize_downloads.setChecked(
                bool(
                    settings.get(
                        "auto_categorize_downloads",
                        self.settings_auto_categorize_downloads.isChecked(),
                    )
                )
            )
            _maybe_block(self.settings_auto_categorize_downloads, False)

        if hasattr(self, "settings_auto_categorize_mode"):
            _maybe_block(self.settings_auto_categorize_mode, block_signals)
            desired = str(
                settings.get(
                    "auto_categorize_mode",
                    DEFAULT_SETTINGS.get("auto_categorize_mode", "off"),
                )
                or DEFAULT_SETTINGS.get("auto_categorize_mode", "off")
            ).strip()
            idx = -1
            if hasattr(self.settings_auto_categorize_mode, "findData"):
                idx = self.settings_auto_categorize_mode.findData(desired)
            if idx < 0 and hasattr(self.settings_auto_categorize_mode, "findText"):
                idx = self.settings_auto_categorize_mode.findText(desired)
            if idx >= 0:
                self.settings_auto_categorize_mode.setCurrentIndex(idx)
            _maybe_block(self.settings_auto_categorize_mode, False)

        if hasattr(self, "settings_split_chapters"):
            _maybe_block(self.settings_split_chapters, block_signals)
            self.settings_split_chapters.setChecked(bool(settings.get("split_chapters", self.settings_split_chapters.isChecked())))
            _maybe_block(self.settings_split_chapters, False)

        if hasattr(self, "settings_verify_checksum"):
            _maybe_block(self.settings_verify_checksum, block_signals)
            self.settings_verify_checksum.setChecked(bool(settings.get("verify_checksum", self.settings_verify_checksum.isChecked())))
            _maybe_block(self.settings_verify_checksum, False)

        if hasattr(self, "settings_virus_scan_after_download"):
            _maybe_block(self.settings_virus_scan_after_download, block_signals)
            self.settings_virus_scan_after_download.setChecked(
                bool(
                    settings.get(
                        "virus_scan_after_download",
                        self.settings_virus_scan_after_download.isChecked(),
                    )
                )
            )
            _maybe_block(self.settings_virus_scan_after_download, False)

        if hasattr(self, "settings_rename_template"):
            _maybe_block(self.settings_rename_template, block_signals)
            desired = str(settings.get("rename_template", "") or "")
            idx = self.settings_rename_template.findText(desired)
            if idx >= 0:
                self.settings_rename_template.setCurrentIndex(idx)
            _maybe_block(self.settings_rename_template, False)

        if hasattr(self, "settings_use_ytdlp_api"):
            _maybe_block(self.settings_use_ytdlp_api, block_signals)
            self.settings_use_ytdlp_api.setChecked(bool(settings.get("use_ytdlp_api", self.settings_use_ytdlp_api.isChecked())))
            _maybe_block(self.settings_use_ytdlp_api, False)

        if hasattr(self, "settings_use_native_engine"):
            _maybe_block(self.settings_use_native_engine, block_signals)
            self.settings_use_native_engine.setChecked(bool(settings.get("use_native_engine", self.settings_use_native_engine.isChecked())))
            _maybe_block(self.settings_use_native_engine, False)

        if hasattr(self, "settings_aria2"):
            _maybe_block(self.settings_aria2, block_signals)
            self.settings_aria2.setChecked(bool(settings.get("use_aria2", self.settings_aria2.isChecked())))
            _maybe_block(self.settings_aria2, False)

        if hasattr(self, "proxy_enabled_cb"):
            _maybe_block(self.proxy_enabled_cb, block_signals)
            self.proxy_enabled_cb.setChecked(bool(settings.get("proxy_enabled", self.proxy_enabled_cb.isChecked())))
            _maybe_block(self.proxy_enabled_cb, False)

        if hasattr(self, "proxy_input"):
            _maybe_block(self.proxy_input, block_signals)
            self.proxy_input.setText(str(settings.get("proxy", self.proxy_input.text()) or "").strip())
            _maybe_block(self.proxy_input, False)

        if hasattr(self, "sustainability_spin"):
            _maybe_block(self.sustainability_spin, block_signals)
            self.sustainability_spin.setValue(max(0, int(settings.get("sustainability_delay_seconds", self.sustainability_spin.value()) or self.sustainability_spin.value())))
            _maybe_block(self.sustainability_spin, False)

        if hasattr(self, "merge_group"):
            _maybe_block(self.merge_group, block_signals)
            self.merge_group.setChecked(bool(settings.get("custom_merge_enabled", self.merge_group.isChecked())))
            _maybe_block(self.merge_group, False)

        if hasattr(self, "settings_video_codec"):
            desired = str(settings.get("custom_merge_video_codec", "") or "")
            idx = self.settings_video_codec.findText(desired)
            if idx >= 0:
                self.settings_video_codec.setCurrentIndex(idx)

        if hasattr(self, "settings_video_crf"):
            self.settings_video_crf.setValue(max(0, min(51, int(settings.get("custom_merge_video_crf", self.settings_video_crf.value()) or self.settings_video_crf.value()))))

        if hasattr(self, "settings_audio_codec"):
            desired = str(settings.get("custom_merge_audio_codec", "") or "")
            idx = self.settings_audio_codec.findText(desired)
            if idx >= 0:
                self.settings_audio_codec.setCurrentIndex(idx)

        if hasattr(self, "settings_audio_bitrate"):
            self.settings_audio_bitrate.setText(str(settings.get("custom_merge_audio_bitrate", self.settings_audio_bitrate.text()) or "").strip())

        if hasattr(self, "settings_merge_hw_encoder"):
            desired = str(settings.get("custom_merge_hw_encoder", "") or "")
            idx = self.settings_merge_hw_encoder.findText(desired)
            if idx >= 0:
                self.settings_merge_hw_encoder.setCurrentIndex(idx)

        if hasattr(self, "settings_merge_video_preset"):
            desired = str(settings.get("custom_merge_video_preset", "") or "")
            idx = self.settings_merge_video_preset.findText(desired)
            if idx >= 0:
                self.settings_merge_video_preset.setCurrentIndex(idx)

        if hasattr(self, "settings_merge_force_reencode"):
            _maybe_block(self.settings_merge_force_reencode, block_signals)
            self.settings_merge_force_reencode.setChecked(
                bool(settings.get("custom_merge_force_reencode", self.settings_merge_force_reencode.isChecked()))
            )
            _maybe_block(self.settings_merge_force_reencode, False)

        if hasattr(self, "settings_language_combo"):
            desired = str(settings.get("ui_language", "") or "")
            if desired:
                _maybe_block(self.settings_language_combo, block_signals)
                idx = self.settings_language_combo.findData(desired)
                if idx >= 0:
                    self.settings_language_combo.setCurrentIndex(idx)
                _maybe_block(self.settings_language_combo, False)

    def set_cookie_profile_items(self, names: list[str], selected_name: str = "", block_signals: bool = True):
        if not hasattr(self, "settings_cookie_profile_combo"):
            return
        combo = self.settings_cookie_profile_combo
        if block_signals:
            combo.blockSignals(True)
        combo.clear()
        combo.addItem(_("Default (no profile)"))
        for name in names or []:
            text = str(name or "").strip()
            if text:
                combo.addItem(text)
        desired = str(selected_name or "").strip()
        if desired:
            idx = combo.findText(desired)
            if idx >= 0:
                combo.setCurrentIndex(idx)
        if block_signals:
            combo.blockSignals(False)

    def retranslate_ui(self):
        self.header_title.setText(_("Settings"))
        self.apply_btn.setText(_("Apply Settings"))
        self.pick_dir_btn.setText(_("Choose Folder"))
        self.settings_retry_delay.setPrefix(_("Retry Delay (sec): "))
        self.settings_queue_retry_limit.setPrefix(_("Queue Auto Retry: "))
        self.settings_concurrent.setPrefix(_("Max Concurrent Downloads: "))
        self.settings_search_history_limit.setPrefix(_("Search History Limit: "))
        self.settings_search_history_ttl_days.setPrefix(_("Search History TTL: "))
        self.settings_search_history_ttl_days.setSuffix(_(" days"))
        self.settings_thumbnail_cache_max.setPrefix(_("Thumbnail Cache: "))
        self.settings_thumbnail_cache_max.setSuffix(_(" items"))
        self.settings_aria2.setText(_("Use aria2 for acceleration"))
        self.settings_use_ytdlp_api.setText(_("Use yt-dlp API (Experimental)"))
        self.settings_use_native_engine.setText(_("Use Native Smart Engine"))
        self.settings_storage_guard.setText(_("Pause downloads when free disk space is low"))
        self.settings_storage_min_free_gb.setPrefix(_("Minimum free space: "))
        self.settings_system_theme_sync.setText(_("Sync theme with Windows settings"))
        self.settings_bandwidth_scheduler.setText(_("Enable bandwidth scheduler"))
        self.settings_bandwidth_rule_limit.setSpecialValueText(_("0 KB/s (Unlimited)"))
        self.settings_bandwidth_rule_label.setPlaceholderText(_("Rule name (optional)"))
        self.settings_cookies.setPlaceholderText(_("Path to cookies.txt (optional)"))
        self.settings_cookie_profile_name.setPlaceholderText(_("Cookie profile name"))
        self.settings_post_download_script.setPlaceholderText(_("Path to post-download script (optional)"))
        self.settings_embed_subs.setText(_("Embed Subtitles in Video"))
        self.settings_hard_burn_subs.setText(_("Hard-burn subtitles into video (re-encode)"))
        self.settings_whisper_fallback_enabled.setText(_("Generate subtitles with Whisper (fallback)"))
        self.settings_split_chapters.setText(_("Split videos by chapters"))
        self.settings_verify_checksum.setText(_("Verify file integrity (SHA-256) after download"))
        self.settings_virus_scan_after_download.setText(_("Scan downloaded files with Windows Defender"))
        self.settings_sponsorblock_enabled.setText(_("Remove sponsors automatically"))
        self.settings_normalize_audio_postprocess.setText(_("Normalize audio after download"))
        self.settings_auto_categorize_downloads.setText(_("Auto-organize downloads after completion"))
        self.merge_group.setTitle(_("Advanced Merging"))
        self.settings_merge_force_reencode.setText(_("Force re-encode for incompatible streams"))
        self.proxy_enabled_cb.setText(_("Enable Proxy"))
        self.proxy_input.setPlaceholderText(_("http://1.2.3.4:8080 or socks5://user:pass@host:port"))
        self.sustainability_spin.setPrefix(_("Delay: "))
        icons_and_keys = [
            ("🎨 ", "General & Appearance"),
            ("⬇️ ", "Download Settings"),
            ("⚙️ ", "Advanced Features"),
            ("🌐 ", "Proxy & Bandwidth"),
            ("🛠️ ", "System & Tools"),
        ]
        for index, (icon, key) in enumerate(icons_and_keys):
            item = self.sidebar.item(index)
            if item is not None:
                item.setText(icon + _(key))
        current_language = self.settings_language_combo.currentData()
        self.settings_language_combo.blockSignals(True)
        self.settings_language_combo.clear()
        for code, label in i18n.available_languages().items():
            self.settings_language_combo.addItem(label, code)
        idx = self.settings_language_combo.findData(current_language)
        self.settings_language_combo.setCurrentIndex(max(0, idx))
        self.settings_language_combo.blockSignals(False)
        current_priority = self.settings_queue_priority.currentData()
        self.settings_queue_priority.blockSignals(True)
        self.settings_queue_priority.clear()
        self.settings_queue_priority.addItem(_("FIFO (Default)"), "fifo")
        self.settings_queue_priority.addItem(_("Smallest First"), "smallest_first")
        idx = self.settings_queue_priority.findData(current_priority)
        self.settings_queue_priority.setCurrentIndex(max(0, idx))
        self.settings_queue_priority.blockSignals(False)
        current_browser = self.settings_cookies_from_browser.currentText()
        self.settings_cookies_from_browser.blockSignals(True)
        self.settings_cookies_from_browser.clear()
        self.settings_cookies_from_browser.addItems(["none", "chrome", "firefox", "edge", "opera", "brave", "safari", "vivaldi"])
        idx = self.settings_cookies_from_browser.findText(current_browser)
        self.settings_cookies_from_browser.setCurrentIndex(max(0, idx))
        self.settings_cookies_from_browser.blockSignals(False)
        current_profile = self.settings_cookie_profile_combo.currentText()
        self.settings_cookie_profile_combo.blockSignals(True)
        if self.settings_cookie_profile_combo.count() > 0:
            self.settings_cookie_profile_combo.setItemText(0, _("Default (no profile)"))
        idx = self.settings_cookie_profile_combo.findText(current_profile)
        if idx >= 0:
            self.settings_cookie_profile_combo.setCurrentIndex(idx)
        self.settings_cookie_profile_combo.blockSignals(False)
