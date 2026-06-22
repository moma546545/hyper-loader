
import os
os.environ.setdefault("QT_API", "pyside6")
import qtawesome as qta
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QLineEdit, QPushButton,
    QLabel, QStackedWidget, QButtonGroup, QComboBox, QFileDialog,
    QDateTimeEdit, QSpinBox, QCheckBox, QProgressBar, QTextEdit, QGridLayout, QSizePolicy, QSlider,
    QLayout, QBoxLayout
)
from PySide6.QtCore import Qt, QDateTime, Signal, QTimer
from PySide6.QtGui import QPainter, QColor, QLinearGradient, QBrush, QPen
from core.constants import VIDEO_QUALITIES, AUDIO_QUALITIES, VIDEO_FORMATS, AUDIO_FORMATS, SUBTITLE_OPTIONS, DOWNLOAD_CATEGORIES
from core.config import DEFAULT_SETTINGS, default_download_dir
from core.media_size import estimate_media_size_bytes, format_size_label
from ui.trim_dialog import TrimView
from ui.schedule_widget import SchedulePicker
from ui.views.base_view import BaseView
from ui.widgets import AnimatedButton, add_soft_shadow
from core.i18n import _
from ui.themes import get_theme

_FALLBACK_PROGRESS_STYLE = """
    QProgressBar {
        border: none;
        border-radius: 3px;
        background-color: rgba(255, 255, 255, 0.1);
    }
    QProgressBar::chunk {
        background-color: #6366F1;
        border-radius: 3px;
    }
"""


class LiquidProgressBar(QProgressBar):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._liquid_phase = 0.0
        self._target_value = 0.0
        self._animated_value = 0.0
        self._current_status = "idle"
        self._cached_theme = {}
        self._cached_theme_name = ""
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate_liquid)

    def _should_animate(self) -> bool:
        return self._current_status == "downloading" and self.isVisible()

    def _sync_timer_state(self):
        if self._should_animate():
            if not self._timer.isActive():
                # 30 FPS is enough for smooth UI and saves CPU
                self._timer.start(32)
            return
        if self._timer.isActive():
            self._timer.stop()

    def setValue(self, val):
        self._target_value = float(val)
        if self._current_status != "downloading" and self._animated_value != self._target_value:
            self._animated_value = self._target_value
            self.update()
        self._sync_timer_state()
        super().setValue(val)

    def set_status(self, status: str):
        """
        status: 'idle', 'downloading', 'paused', 'completed', 'error'
        """
        normalized = str(status or "idle").strip().lower()
        self._current_status = normalized
        if normalized != "downloading":
            # Ensure final state is drawn without animation gap
            self._animated_value = self._target_value
        self._sync_timer_state()
        self.update()

    def _animate_liquid(self):
        self._liquid_phase += 0.05
        if self._liquid_phase > 10.0:
            self._liquid_phase -= 10.0
            
        diff = self._target_value - self._animated_value
        if diff < 0:
            # Snap instantly if target goes backwards (avoids "going back" easing effect)
            self._animated_value = self._target_value
            self.update()
        else:
            self._animated_value += diff * 0.15
            if abs(diff) > 0.1:
                self.update()
            else:
                self._animated_value = self._target_value
                # Still update for gradient animation
                self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_timer_state()

    def hideEvent(self, event):
        super().hideEvent(event)
        if self._timer.isActive():
            self._timer.stop()

    def set_liquid_phase(self, phase: float):
        pass

    def _get_theme_cached(self) -> dict:
        theme_name = "Modern Dark"
        try:
            p = self.parent()
            if hasattr(p, "main_window") and hasattr(p.main_window, "theme"):
                theme_name = str(p.main_window.theme or "Modern Dark")
        except Exception:
            pass
        if theme_name != self._cached_theme_name:
            self._cached_theme_name = theme_name
            self._cached_theme = get_theme(theme_name)
        return self._cached_theme

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = self._get_theme_cached()
            
        bg = QColor(t.get("bg", "#111111"))
        border = QColor(t.get("border", "#333333"))

        # Smart Status Colors
        if self._current_status == 'completed':
            color1, color2 = QColor("#10B981"), QColor("#34D399")
        elif self._current_status == 'error':
            color1, color2 = QColor("#EF4444"), QColor("#F87171")
        elif self._current_status in {"paused", "idle"}:
            color1, color2 = QColor("#94A3B8"), QColor("#CBD5E1")
        else:
            color1 = QColor(t.get("accent", "#6366F1"))
            color2 = QColor(t.get("accent_2", "#8B5CF6"))

        w = self.width()
        h = self.height()
        radius = max(2, min(6, int(h / 2)))
        rect = self.rect().adjusted(1, 1, -1, -1)

        painter.setPen(QPen(border, 1))
        painter.setBrush(QBrush(bg))
        painter.drawRoundedRect(rect, radius, radius)

        mn = float(self.minimum())
        mx = float(self.maximum())
        if mx <= mn: return
        pct = max(0.0, min(1.0, (self._animated_value - mn) / (mx - mn)))
        fill_w = int(rect.width() * pct)
        if fill_w > 0:
            fill_rect = rect.adjusted(0, 0, -(rect.width() - fill_w), 0)
            
            import math
            shift = (math.sin(self._liquid_phase) + 1) / 2.0
            grad = QLinearGradient(0, 0, w, 0)
            grad.setColorAt(0.0, color1)
            grad.setColorAt(shift, color2)
            grad.setColorAt(1.0, color1)
            
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(grad))
            painter.drawRoundedRect(fill_rect, radius, radius)

            highlight = QLinearGradient(0, 0, 0, h)
            highlight.setColorAt(0.0, QColor(255, 255, 255, 50))
            highlight.setColorAt(1.0, QColor(255, 255, 255, 0))
            painter.setBrush(QBrush(highlight))
            painter.drawRoundedRect(fill_rect, radius, radius)

        # Draw percentage text
        text = f"{int(self._target_value)}%"
        painter.setPen(QPen(QColor("#FFFFFF")))
        
        # Adjust font size based on height, but keep it readable
        font = painter.font()
        font.setBold(True)
        font.setPixelSize(max(8, int(h * 0.7)))
        painter.setFont(font)
        
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)


def normalize_progress_status(status: str) -> str:
    value = str(status or "").strip().lower()
    if value in {"idle", "ready", "pending", "queued", "waiting"}:
        return "idle"
    if value in {"running", "downloading", "processing", "merging"}:
        return "downloading"
    if value in {"success", "completed", "complete"}:
        return "completed"
    if value in {"failed", "error", "cancelled"}:
        return "error"
    if value == "paused":
        return "paused"
    return "idle"


def create_status_progress_bar(
    status: str = "idle",
    value: int | float = 0,
    parent=None,
):
    normalized_status = normalize_progress_status(status)
    try:
        bar = LiquidProgressBar(parent)
        bar.set_status(normalized_status)
    except Exception:
        bar = QProgressBar(parent)
        bar.setTextVisible(False)
        bar.setStyleSheet(_FALLBACK_PROGRESS_STYLE)
    bar.setRange(0, 100)
    bar.setValue(max(0, min(100, int(round(float(value or 0))))))
    return bar

class SearchView(BaseView):
    # Signals to communicate to the controller/MainWindow
    analyze_requested = Signal(str)
    formats_requested = Signal(str)
    paste_requested = Signal()
    clear_history_requested = Signal()
    download_requested = Signal()
    schedule_requested = Signal()
    trim_toggle_requested = Signal()

    def __init__(self, main_window=None, parent=None):
        super().__init__(main_window, parent)
        self._settings_columns = 0
        self._player_layout_vertical = False
        self.setup_ui()
        self._setup_fade_animation()

    def _setup_fade_animation(self):
        from core.qt_compat import QGraphicsOpacityEffect, QPropertyAnimation, QEasingCurve
        self.opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.opacity_effect)
        self.fade_anim = QPropertyAnimation(self.opacity_effect, b"opacity")
        self.fade_anim.setDuration(400)
        self.fade_anim.setStartValue(0.0)
        self.fade_anim.setEndValue(1.0)
        self.fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def showEvent(self, event):
        super().showEvent(event)
        self.fade_anim.start()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Search Bar ──
        container = QFrame()
        container.setObjectName("search_bar_container")
        container.setStyleSheet("""
            QFrame#search_bar_container {
                background-color: rgba(30, 41, 59, 0.7);
                border-bottom: 1px solid rgba(255, 255, 255, 0.08);
                padding: 10px 0px;
            }
        """)
        add_soft_shadow(container, blur_radius=20, alpha=40)
        cl = QHBoxLayout(container)
        cl.setContentsMargins(20, 10, 20, 10)
        cl.setSpacing(15)

        # Fix layout flipping in RTL languages - removed to allow natural RTL order
        # container.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        
        self.url_input = QLineEdit()
        self.url_input.setObjectName("search_input")
        self.url_input.setStyleSheet("""
            QLineEdit {
                background-color: rgba(15, 23, 42, 0.6);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 14px;
                padding: 12px 20px;
                font-size: 15px;
                color: #F8FAFC;
            }
            QLineEdit:focus {
                border: 1px solid #3B82F6;
                background-color: rgba(15, 23, 42, 0.8);
            }
        """)
        self.url_input.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.url_input.setPlaceholderText(_("Paste video link or enter search keyword..."))
        self.url_input.returnPressed.connect(self._on_search_clicked)

        self.search_btn = AnimatedButton(_(" Search"))
        self.search_btn.setObjectName("search_btn")
        self.search_btn.setIcon(qta.icon('fa5s.search', color='white'))
        self.search_btn.setMinimumSize(100, 45)
        self.search_btn.setStyleSheet("""
            QPushButton {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #3B82F6, stop:1 #2563EB);
                color: white;
                border-radius: 14px;
                padding: 12px 24px;
                font-weight: 900;
                font-size: 15px;
                border: 1px solid rgba(255, 255, 255, 0.1);
            }
            QPushButton:hover {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2563EB, stop:1 #1D4ED8);
                border: 1px solid rgba(255, 255, 255, 0.3);
            }
        """)
        self.search_btn.clicked.connect(self._on_search_clicked)

        icon_btn_style = """
            QPushButton {
                background-color: rgba(30, 41, 59, 0.6);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 12px;
            }
            QPushButton:hover {
                background-color: rgba(51, 65, 85, 0.8);
                border: 1px solid rgba(255, 255, 255, 0.2);
            }
        """

        self.formats_btn = QPushButton("")
        self.formats_btn.setObjectName("action_button")
        self.formats_btn.setFixedSize(45, 45)
        self.formats_btn.setStyleSheet(icon_btn_style)
        self.formats_btn.setIcon(qta.icon('fa5s.cogs', color='#A1A1AA'))
        self.formats_btn.setToolTip(_("Formats and settings"))
        self.formats_btn.clicked.connect(self._on_formats_clicked)

        self.paste_btn = QPushButton("")
        self.paste_btn.setObjectName("action_button")
        self.paste_btn.setFixedSize(45, 45)
        self.paste_btn.setStyleSheet(icon_btn_style)
        self.paste_btn.setIcon(qta.icon('fa5s.clipboard', color='#A1A1AA'))
        self.paste_btn.setToolTip(_("Paste link"))
        self.paste_btn.clicked.connect(self.paste_requested.emit)

        self.clear_history_btn = QPushButton("")
        self.clear_history_btn.setObjectName("action_button")
        self.clear_history_btn.setFixedSize(45, 45)
        self.clear_history_btn.setStyleSheet(icon_btn_style)
        self.clear_history_btn.setIcon(qta.icon('fa5s.trash-alt', color='#A1A1AA'))
        self.clear_history_btn.setToolTip(_("Clear history"))
        self.clear_history_btn.clicked.connect(self.clear_history_requested.emit)

        cl.addWidget(self.url_input, 1)
        cl.addWidget(self.paste_btn)
        cl.addWidget(self.clear_history_btn)
        cl.addWidget(self.formats_btn)
        cl.addWidget(self.search_btn)

        # ── Search Stack ──
        self.search_stack = QStackedWidget()
        self.search_stack.addWidget(self._build_search_empty_state())
        self.search_stack.addWidget(self._build_search_single_state())
        
        self.trim_view = TrimView(self)
        self.search_stack.addWidget(self.trim_view)

        layout.addWidget(container)
        layout.addWidget(self.search_stack, 1)

    def _build_search_empty_state(self):
        frame = QFrame()
        frame.setObjectName("single_card")
        col = QVBoxLayout(frame)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        
        center_wrap = QWidget()
        center = QVBoxLayout(center_wrap)
        center.setContentsMargins(0, 0, 0, 0)
        center.setSpacing(15)
        
        self.empty_title = QLabel(_("Search and download your favorite videos"))
        self.empty_title.setObjectName("empty_title")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.empty_sub = QLabel(_("More than 900 websites supported"))
        self.empty_sub.setObjectName("empty_sub")
        self.empty_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        center.addStretch(1)
        center.addWidget(self.empty_title)
        center.addWidget(self.empty_sub)
        center.addStretch(1)
        
        col.addWidget(center_wrap, 1)
        return frame

    def _build_search_single_state(self):
        # Use QScrollArea to prevent any clipping on smaller screens
        from PySide6.QtWidgets import QScrollArea
        from PySide6.QtCore import Qt
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background-color: transparent; }")

        frame = QFrame()
        frame.setObjectName("main_root")

        self.search_main_layout = QVBoxLayout(frame)
        self.search_main_layout.setContentsMargins(15, 15, 15, 15)
        self.search_main_layout.setSpacing(20)

        # Main Column
        left_container = QWidget()
        left_col = QVBoxLayout(left_container)
        left_col.setContentsMargins(0, 0, 0, 0)
        left_col.setSpacing(15)

        # ─── 1. Video Info Card ───
        self.single_player_card = QFrame()
        self.single_player_card.setObjectName("single_card")
        self.single_player_card.setStyleSheet("""
            QFrame#single_card {
                background-color: rgba(30, 41, 59, 0.8);
                border-radius: 16px;
                border: 1px solid rgba(255, 255, 255, 0.08);
            }
        """)
        add_soft_shadow(self.single_player_card, blur_radius=25, alpha=50)
        self.player_layout = QHBoxLayout(self.single_player_card)
        self.player_layout.setContentsMargins(15, 15, 15, 15)
        self.player_layout.setSpacing(20)

        self.single_thumb = QLabel(_("Loading video preview..."))
        self.single_thumb.setObjectName("thumb_preview")
        self.single_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.single_thumb.setFixedSize(320, 180)
        self.single_thumb.setStyleSheet("""
            border-radius: 12px;
            background-color: rgba(15, 23, 42, 0.6);
            border: 1px solid rgba(255, 255, 255, 0.05);
        """)
        self.player_layout.addWidget(self.single_thumb)

        info_layout = QVBoxLayout()
        info_layout.setSpacing(10)

        self.single_title = QLabel(_("Video title"))
        self.single_title.setObjectName("single_title")
        self.single_title.setWordWrap(True)
        self.single_title.setStyleSheet("font-size: 22px; font-weight: 900; color: #F8FAFC; letter-spacing: 0.5px;")
        self.single_channel = QLabel(_("By channel"))
        self.single_channel.setObjectName("single_sub")
        self.single_channel.setStyleSheet("font-size: 15px; color: #94A3B8; font-weight: 500;")
        chips_row = QHBoxLayout()
        chips_row.setContentsMargins(0, 0, 0, 0)
        chips_row.setSpacing(10)
        self.single_status_chip = QLabel(_("LIVE"))
        self.single_status_chip.setObjectName("chip")
        self.single_status_chip.setStyleSheet("""
            background-color: rgba(239, 68, 68, 0.15);
            color: #EF4444;
            border: 1px solid rgba(239, 68, 68, 0.3);
            border-radius: 6px;
            padding: 4px 8px;
            font-weight: bold;
            font-size: 12px;
        """)
        self.single_status_chip.hide()
        self.single_category = QLabel(_("Category"))
        self.single_category.setObjectName("chip")
        self.single_category.setStyleSheet("""
            background-color: rgba(59, 130, 246, 0.15);
            color: #3B82F6;
            border: 1px solid rgba(59, 130, 246, 0.3);
            border-radius: 6px;
            padding: 4px 8px;
            font-weight: bold;
            font-size: 12px;
        """)
        self.single_category.hide()

        info_layout.addWidget(self.single_title)
        info_layout.addWidget(self.single_channel)
        chips_row.addWidget(self.single_status_chip)
        chips_row.addWidget(self.single_category)
        chips_row.addStretch(1)
        info_layout.addLayout(chips_row)
        info_layout.addStretch(1)

        self.player_layout.addLayout(info_layout, 1)
        left_col.addWidget(self.single_player_card)
        add_soft_shadow(self.single_player_card)

        # ─── 2. Settings Card ───
        self.single_settings_card = QFrame()
        self.single_settings_card.setObjectName("single_card")
        self.single_settings_card.setStyleSheet("""
            QFrame#single_card {
                background-color: rgba(30, 41, 59, 0.85);
                border-radius: 16px;
                border: 1px solid rgba(255, 255, 255, 0.08);
            }
        """)
        add_soft_shadow(self.single_settings_card, blur_radius=30, alpha=60)
        settings_layout = QVBoxLayout(self.single_settings_card)
        settings_layout.setContentsMargins(20, 20, 20, 20)
        settings_layout.setSpacing(15)

        # Segmented control بدل QTabBar
        self.mode_frame = QFrame()
        self.mode_frame.setObjectName("SegmentedControl")
        self.mode_frame.setStyleSheet("""
            QFrame#SegmentedControl {
                background-color: rgba(15, 23, 42, 0.6);
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.05);
            }
        """)
        mode_layout = QHBoxLayout(self.mode_frame)
        mode_layout.setContentsMargins(5, 5, 5, 5)
        mode_layout.setSpacing(5)

        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        modes = [
            ("Video", "fa5s.film"),
            ("Audio", "fa5s.music"),
            ("GIF", "fa5s.image"),
        ]
        
        mode_btn_style = """
            QPushButton {
                background-color: transparent;
                color: #94A3B8;
                border: none;
                border-radius: 8px;
                font-weight: 800;
                font-size: 15px;
            }
            QPushButton:hover {
                background-color: rgba(255, 255, 255, 0.05);
                color: #E2E8F0;
            }
            QPushButton:checked {
                background-color: rgba(59, 130, 246, 0.2);
                color: #60A5FA;
                border: 1px solid rgba(59, 130, 246, 0.4);
            }
        """

        for idx, (text, icon_name) in enumerate(modes):
            btn = QPushButton(f" {text}")
            btn.setIcon(qta.icon(icon_name, color="#A1A1AA"))
            btn.setCheckable(True)
            btn.setObjectName("SegmentedBtn")
            btn.setMinimumHeight(45)
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            btn.setProperty("mode_text", text)
            btn.setStyleSheet(mode_btn_style)
            self.mode_group.addButton(btn, idx)
            mode_layout.addWidget(btn)
            if idx == 0:
                btn.setChecked(True)
        self.mode_group.idClicked.connect(self._on_mode_changed)
        settings_layout.addWidget(self.mode_frame)

        self.quality_stack = QStackedWidget()
        self.quality_stack.addWidget(self._build_video_quality_widget())
        self.quality_stack.addWidget(self._build_audio_quality_widget())
        self.quality_stack.addWidget(self._build_gif_settings_widget())
        settings_layout.addWidget(self.quality_stack)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("border: none; background-color: #27272A; max-height: 1px;")
        settings_layout.addWidget(line)

        self.inputs_grid = QGridLayout()
        self.inputs_grid.setHorizontalSpacing(16)
        self.inputs_grid.setVerticalSpacing(15)

        self.format_combo = QComboBox()
        self.format_combo.setMinimumWidth(100)
        self.format_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.format_combo.currentTextChanged.connect(self._on_format_changed_refresh_sizes)

        self.subtitle_combo = QComboBox()
        self.subtitle_combo.addItems(SUBTITLE_OPTIONS)
        self.subtitle_combo.setCurrentText("None")
        self.subtitle_combo.setEditable(True)
        self.subtitle_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.subtitle_combo.setToolTip(_("Enter multiple subtitle languages separated by commas, for example: English,ar, or choose All"))
        self.subtitle_combo.setMinimumWidth(100)
        self.subtitle_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.category_combo = QComboBox()
        self.category_combo.addItems([_(name) for name in DOWNLOAD_CATEGORIES])
        self.category_combo.setMinimumWidth(100)
        self.category_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        save_container = QWidget()
        save_layout = QHBoxLayout(save_container)
        save_layout.setContentsMargins(0, 0, 0, 0)
        save_layout.setSpacing(10)
        
        self.out_dir_input = QLineEdit(default_download_dir())
        self.out_dir_input.setObjectName("path_input")
        self.out_dir_input.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.browse_btn = QPushButton(_("Browse"))
        self.browse_btn.setObjectName("action_button")
        self.browse_btn.clicked.connect(self._pick_out_dir)
        
        save_layout.addWidget(self.out_dir_input, 1)
        save_layout.addWidget(self.browse_btn)

        self.aria2_checkbox = QCheckBox(_("Accelerate (aria2)"))
        self.aria2_checkbox.setChecked(True)

        self.post_action_combo = QComboBox()
        self.post_action_combo.addItem(_("No action (Default)"), "none")
        self.post_action_combo.addItem(_("Open download folder"), "open_folder")
        self.post_action_combo.addItem(_("Play notification sound"), "play_sound")
        self.post_action_combo.addItem(_("Run custom script"), "run_script")
        self.post_action_combo.addItem(_("Transcribe audio to text"), "transcribe")
        self.post_action_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.speed_limit_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_limit_slider.setRange(0, 20000)
        self.speed_limit_slider.setSingleStep(100)
        self.speed_limit_slider.setPageStep(500)
        self.speed_limit_slider.setValue(0)
        self.speed_limit_value = QLabel(_("Unlimited"))
        self.speed_limit_value.setObjectName("single_sub")

        def _on_speed_limit_changed(value: int):
            if int(value) <= 0:
                self.speed_limit_value.setText(_("Unlimited"))
            else:
                self.speed_limit_value.setText(f"{int(value)} KB/s")

        self.speed_limit_slider.valueChanged.connect(_on_speed_limit_changed)
        _on_speed_limit_changed(self.speed_limit_slider.value())

        speed_row = QHBoxLayout()
        speed_row.setContentsMargins(0, 0, 0, 0)
        speed_row.setSpacing(10)
        speed_row.addWidget(self.speed_limit_slider, 1)
        speed_row.addWidget(self.speed_limit_value)

        post_script_row = QHBoxLayout()
        post_script_row.setContentsMargins(0, 0, 0, 0)
        post_script_row.setSpacing(10)
        self.post_script_input = QLineEdit()
        self.post_script_input.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.post_script_input.setPlaceholderText(_("Script path (.py/.ps1/.bat/.cmd)"))
        self.post_script_browse_btn = QPushButton(_("Browse Script"))
        self.post_script_browse_btn.setObjectName("action_button")
        self.post_script_browse_btn.clicked.connect(self._pick_post_script)
        post_script_row.addWidget(self.post_script_input, 1)
        post_script_row.addWidget(self.post_script_browse_btn)
        self.format_field = self._create_field_block(_("Format:"), self.format_combo)
        self.subtitle_field = self._create_field_block(_("Subtitle:"), self.subtitle_combo)
        self.category_field = self._create_field_block(_("Category:"), self.category_combo)
        self.save_to_field = self._create_field_block(_("Save to:"), save_container)
        self.aria2_field = self._create_field_block(_("Acceleration:"), self.aria2_checkbox)
        self.post_action_field = self._create_field_block(_("After Download:"), self.post_action_combo)
        self.speed_field = self._create_field_block(_("Download speed per file (KB/s):"), speed_row)
        self.post_script_field = self._create_field_block(_("Post-download script (optional):"), post_script_row)

        self._responsive_input_blocks = [
            (self.format_field, 1),
            (self.subtitle_field, 1),
            (self.category_field, 1),
            (self.save_to_field, "full"),
        ]
        settings_layout.addLayout(self.inputs_grid)

        self.schedule_picker = SchedulePicker(self, title=_("Download Schedule"))
        self.schedule_time_edit = self.schedule_picker.date_time_edit
        self.schedule_repeat_combo = self.schedule_picker.repeat_combo
        settings_layout.addWidget(self.schedule_picker)

        self.adv_toggle_btn = QPushButton(_("⚙ Advanced Options"))
        self.adv_toggle_btn.setObjectName("action_button")
        self.adv_toggle_btn.setCheckable(True)
        self.adv_toggle_btn.setMinimumHeight(40)
        self.adv_toggle_btn.setToolTip(_("Show advanced settings"))
        self.adv_toggle_btn.clicked.connect(self._toggle_advanced_options)
        settings_layout.addWidget(self.adv_toggle_btn)

        left_col.addWidget(self.single_settings_card)
        add_soft_shadow(self.single_settings_card)

        # ─── 3. Advanced Settings ───
        self.adv_container = QFrame()
        self.adv_container.setObjectName("playlist_row")
        adv_layout = QGridLayout(self.adv_container)
        adv_layout.setContentsMargins(15, 15, 15, 15)
        adv_layout.setHorizontalSpacing(15)
        adv_layout.setVerticalSpacing(12)

        self.start_input = QLineEdit()
        self.start_input.setPlaceholderText(_("HH:MM:SS"))
        self.end_input = QLineEdit()
        self.end_input.setPlaceholderText(_("HH:MM:SS"))
        range_layout = QHBoxLayout()
        range_layout.setContentsMargins(0, 0, 0, 0)
        range_layout.setSpacing(8)
        range_layout.addWidget(self.start_input)
        self.range_dash_label = QLabel("-")
        range_layout.addWidget(self.range_dash_label)
        range_layout.addWidget(self.end_input)

        self.concurrent_spin = QSpinBox()
        self.concurrent_spin.setRange(1, 10)
        self.concurrent_spin.setValue(int(DEFAULT_SETTINGS["max_concurrent"]))
        self.range_label = QLabel(_("Range:"), objectName="single_sub")
        adv_layout.addWidget(self.range_label, 0, 0)
        adv_layout.addLayout(range_layout, 0, 1)
        self.max_tasks_label = QLabel(_("Max Tasks:"), objectName="single_sub")
        adv_layout.addWidget(self.max_tasks_label, 0, 2)
        adv_layout.addWidget(self.concurrent_spin, 0, 3)
        adv_layout.addWidget(self.aria2_field, 1, 0, 1, 2)
        adv_layout.addWidget(self.post_action_field, 1, 2, 1, 2)
        adv_layout.addWidget(self.speed_field, 2, 0, 1, 4)
        adv_layout.addWidget(self.post_script_field, 3, 0, 1, 4)

        self.adv_container.hide()
        left_col.addWidget(self.adv_container)

        # Status and Progress
        self.status_container = QWidget()
        status_layout = QVBoxLayout(self.status_container)
        status_layout.setContentsMargins(5, 0, 5, 0)
        status_layout.setSpacing(5)
        
        self.status_row = QHBoxLayout()
        self.state_label = QLabel(_("جاهز"))
        self.state_label.setObjectName("single_sub")
        self.speed_label = QLabel(_("Speed: --"))
        self.speed_label.setObjectName("single_sub")
        self.eta_label = QLabel(_("ETA: --"))
        self.eta_label.setObjectName("single_sub")
        self.size_label = QLabel("--")
        self.size_label.setObjectName("single_sub")
        self.pre_download_size_label = QLabel("")
        self.pre_download_size_label.setObjectName("single_sub")
        self.pre_download_size_label.setStyleSheet("color: #F59E0B; font-weight: bold;")
        self.status_row.addWidget(self.state_label)
        self.status_row.addStretch(1)
        self.status_row.addWidget(self.pre_download_size_label)
        self.status_row.addWidget(self.speed_label)
        self.status_row.addWidget(self.eta_label)
        self.status_row.addWidget(self.size_label)

        self.progress_bar = LiquidProgressBar()
        self.progress_bar.setFixedHeight(16)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        
        status_layout.addLayout(self.status_row)
        status_layout.addWidget(self.progress_bar)
        self.status_container.hide() 

        left_col.addWidget(self.status_container)

        actions = QHBoxLayout()
        actions.setSpacing(12)

        self.trim_btn = QPushButton(_("Trim"))
        self.trim_btn.setObjectName("action_button")
        self.trim_btn.setMinimumHeight(48)
        self.trim_btn.clicked.connect(self._toggle_trim_options)

        self.schedule_btn = QPushButton(_("Schedule"))
        self.schedule_btn.setObjectName("action_button")
        self.schedule_btn.setMinimumHeight(48)
        self.schedule_btn.clicked.connect(self._on_schedule_clicked)

        self.download_btn = AnimatedButton(_("Download"))
        self.download_btn.setObjectName("action_download")
        self.download_btn.setMinimumHeight(52)
        self.download_btn.clicked.connect(self.download_requested.emit)

        actions.addWidget(self.trim_btn, 1)
        actions.addWidget(self.schedule_btn, 1)
        actions.addWidget(self.download_btn, 3)

        left_col.addStretch(1)
        left_col.addLayout(actions)

        # Debug Log
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFixedHeight(80)
        self.log_text.setObjectName("debug_log")
        left_col.addWidget(self.log_text)

        self.search_main_layout.addWidget(left_container, 1)

        scroll.setWidget(frame)
        self._on_mode_changed(0)
        self._apply_responsive_layout(force=True)
        self.refresh_theme_styles()
        return scroll

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_responsive_layout()

    def _create_field_block(self, label_text: str, content) -> QWidget:
        block = QWidget()
        block.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        block_layout = QVBoxLayout(block)
        block_layout.setContentsMargins(0, 0, 0, 0)
        block_layout.setSpacing(8)

        label = QLabel(label_text)
        label.setObjectName("single_sub")
        block._label = label
        block._label_key = str(label_text or "")
        block_layout.addWidget(label)

        if isinstance(content, QLayout):
            content_widget = QWidget()
            content_widget.setLayout(content)
            content_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            block_layout.addWidget(content_widget)
        else:
            if hasattr(content, "setSizePolicy"):
                content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            block_layout.addWidget(content)
        return block

    def _effective_content_width(self) -> int:
        frame = getattr(self, "single_settings_card", None)
        if frame is not None and frame.width() > 0:
            return int(frame.width())
        return int(self.width())

    def _apply_responsive_layout(self, force: bool = False):
        width = self._effective_content_width()
        columns = 3 if width >= 880 else 2 if width >= 620 else 1
        if force or columns != self._settings_columns:
            self._settings_columns = columns
            self._rebuild_inputs_grid(columns)

        use_vertical_player = width < 760
        if force or use_vertical_player != self._player_layout_vertical:
            self._player_layout_vertical = use_vertical_player
            direction = (
                QBoxLayout.Direction.TopToBottom
                if use_vertical_player
                else QBoxLayout.Direction.LeftToRight
            )
            self.player_layout.setDirection(direction)
            self.single_thumb.setAlignment(
                Qt.AlignmentFlag.AlignCenter
                if use_vertical_player
                else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )

    def _rebuild_inputs_grid(self, columns: int):
        while self.inputs_grid.count():
            self.inputs_grid.takeAt(0)

        for col in range(4):
            self.inputs_grid.setColumnStretch(col, 0)

        row = 0
        col = 0
        for widget, span in self._responsive_input_blocks:
            if span == "full":
                if col:
                    row += 1
                    col = 0
                self.inputs_grid.addWidget(widget, row, 0, 1, columns)
                row += 1
                continue

            self.inputs_grid.addWidget(widget, row, col)
            col += 1
            if col >= columns:
                row += 1
                col = 0

        for col in range(columns):
            self.inputs_grid.setColumnStretch(col, 1)

    def _build_video_quality_widget(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        label = QLabel(_("Video Quality"))
        label.setStyleSheet("color: #8B5CF6; font-size: 14px; font-weight: bold;")
        layout.addWidget(label)

        self.quality_group = QButtonGroup(self)
        self.quality_group.setExclusive(True)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        self.quality_radios = []
        for idx, quality in enumerate(VIDEO_QUALITIES):
            chip = QPushButton(quality)
            chip.setCheckable(True)
            chip.setProperty("base_quality", quality)
            chip.setMinimumHeight(54)
            chip.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            chip.setStyleSheet(self._chip_style())
            if quality == "1080p":
                chip.setChecked(True)
            self.quality_group.addButton(chip)
            self.quality_radios.append(chip)
            grid.addWidget(chip, idx // 4, idx % 4)
        self.quality_group.buttonClicked.connect(self._on_quality_chip_clicked)
        layout.addLayout(grid)
        layout.addStretch(1)
        return widget

    def _build_audio_quality_widget(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        label = QLabel(_("Audio Quality"))
        label.setStyleSheet("color: #8B5CF6; font-size: 14px; font-weight: bold;")
        layout.addWidget(label)

        self.audio_quality_group = QButtonGroup(self)
        self.audio_quality_group.setExclusive(True)
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        self.audio_quality_radios = []
        qualities = ["Original"] + AUDIO_QUALITIES
        for idx, quality in enumerate(qualities):
            chip = QPushButton(quality)
            chip.setCheckable(True)
            chip.setProperty("base_quality", quality)
            chip.setMinimumHeight(54)
            chip.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            chip.setStyleSheet(self._chip_style())
            if quality == "Original":
                chip.setChecked(True)
            self.audio_quality_group.addButton(chip)
            self.audio_quality_radios.append(chip)
            grid.addWidget(chip, idx // 3, idx % 3)
        self.audio_quality_group.buttonClicked.connect(self._on_quality_chip_clicked)
        layout.addLayout(grid)
        layout.addStretch(1)
        return widget

    def _build_gif_settings_widget(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        label = QLabel(_("GIF Settings"))
        label.setObjectName("section_title")
        layout.addWidget(label)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel(_("Start")))
        self.gif_start_input = QLineEdit("00:00")
        self.gif_start_input.setPlaceholderText(_("mm:ss"))
        self.gif_start_input.setFixedWidth(120)
        row1.addWidget(self.gif_start_input)
        row1.addSpacing(10)
        row1.addWidget(QLabel(_("End")))
        self.gif_end_input = QLineEdit("")
        self.gif_end_input.setPlaceholderText(_("mm:ss"))
        self.gif_end_input.setFixedWidth(120)
        row1.addWidget(self.gif_end_input)
        row1.addStretch(1)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel(_("FPS")))
        self.gif_fps_spin = QSpinBox()
        self.gif_fps_spin.setRange(5, 60)
        self.gif_fps_spin.setValue(15)
        self.gif_fps_spin.setFixedWidth(100)
        row2.addWidget(self.gif_fps_spin)
        row2.addSpacing(10)
        row2.addWidget(QLabel(_("Resolution")))
        self.gif_resolution_combo = QComboBox()
        self.gif_resolution_combo.addItems(["426x240", "640x360", "854x480", "1280x720"])
        self.gif_resolution_combo.setCurrentText("640x360")
        self.gif_resolution_combo.setFixedWidth(140)
        row2.addWidget(self.gif_resolution_combo)
        row2.addStretch(1)
        layout.addLayout(row2)

        return widget

    def _on_search_clicked(self):
        url = self.url_input.text().strip()
        if url:
            self.analyze_requested.emit(url)

    def _on_schedule_clicked(self):
        if hasattr(self, "schedule_picker"):
            self.schedule_picker.set_schedule_enabled(True)
        self.schedule_requested.emit()

    def _toggle_advanced_options(self):
        if self.adv_container.isVisible():
            self.adv_container.hide()
            if hasattr(self, "adv_toggle_btn"):
                self.adv_toggle_btn.setToolTip(_("Show advanced settings"))
                self.adv_toggle_btn.setChecked(False)
        else:
            self.adv_container.show()
            if hasattr(self, "adv_toggle_btn"):
                self.adv_toggle_btn.setToolTip(_("Hide advanced settings"))
                self.adv_toggle_btn.setChecked(True)

    def retranslate_ui(self):
        current_mode = self.current_mode_index()
        current_format = str(self.format_combo.currentText() or "")
        current_subtitle = str(self.subtitle_combo.currentText() or "")
        current_category = str(self.category_combo.currentText() or "")
        current_post_action = self.post_action_combo.currentData()
        current_repeat = self.schedule_repeat_combo.currentData()
        self.url_input.setPlaceholderText(_("Paste video link or enter search keyword..."))
        self.search_btn.setText(_(" Search"))
        self.formats_btn.setToolTip(_("Formats and settings"))
        self.paste_btn.setToolTip(_("Paste link"))
        self.clear_history_btn.setToolTip(_("Clear history"))
        self.empty_title.setText(_("Search and download your favorite videos"))
        self.empty_sub.setText(_("More than 900 websites supported"))
        if self.single_thumb.pixmap() is None:
            self.single_thumb.setText(_("Loading video preview..."))
        if not getattr(getattr(self.main_window, "preview_data", None), "get", None):
            self.single_title.setText(_("Video title"))
            self.single_channel.setText(_("By channel"))
        if not self.single_status_chip.isVisible():
            self.single_status_chip.setText(_("LIVE"))
        if not self.single_category.isVisible():
            self.single_category.setText(_("Category"))
        for btn in self.mode_group.buttons():
            source_text = str(btn.property("mode_text") or btn.text().strip())
            btn.setText(f" {_(source_text)}")
        self.subtitle_combo.setToolTip(_("Enter multiple subtitle languages separated by commas, for example: English,ar, or choose All"))
        self.browse_btn.setText(_("Browse"))
        self.aria2_checkbox.setText(_("Accelerate (aria2)"))
        self.speed_limit_value.setText(_("Unlimited") if int(self.speed_limit_slider.value() or 0) <= 0 else f"{int(self.speed_limit_slider.value())} KB/s")
        self.post_script_input.setPlaceholderText(_("Script path (.py/.ps1/.bat/.cmd)"))
        self.post_script_browse_btn.setText(_("Browse Script"))
        self.adv_toggle_btn.setText(_("⚙ Advanced Options"))
        self.adv_toggle_btn.setToolTip(_("Hide advanced settings") if self.adv_container.isVisible() else _("Show advanced settings"))
        self.start_input.setPlaceholderText(_("HH:MM:SS"))
        self.end_input.setPlaceholderText(_("HH:MM:SS"))
        self.range_label.setText(_("Range:"))
        self.max_tasks_label.setText(_("Max Tasks:"))
        self.trim_btn.setText(_("Trim"))
        self.schedule_btn.setText(_("Schedule"))
        if self.download_btn.isEnabled():
            if str(self.download_btn.text() or "").strip() in {"", "Download"}:
                self.download_btn.setText(_("Download"))
        else:
            current_download_text = str(self.download_btn.text() or "").strip()
            if current_download_text in {"Download", "Starting...", _("Download"), _("Starting...")}:
                self.download_btn.setText(_(current_download_text))
        self.state_label.setText(_("Ready") if str(self.state_label.text() or "").strip() in {"جاهز", "Ready"} else self.state_label.text())
        if str(self.speed_label.text() or "").startswith(("Speed:", _("Speed:"))):
            speed_value = str(self.speed_label.text() or "").split(":", 1)[-1].strip()
            self.speed_label.setText(f"{_('Speed:')} {speed_value}")
        if str(self.eta_label.text() or "").startswith(("ETA:", _("ETA:"))):
            eta_value = str(self.eta_label.text() or "").split(":", 1)[-1].strip()
            self.eta_label.setText(f"{_('ETA:')} {eta_value}")
        self.format_combo.blockSignals(True)
        self.subtitle_combo.blockSignals(True)
        self.category_combo.blockSignals(True)
        self.post_action_combo.blockSignals(True)
        self.schedule_repeat_combo.blockSignals(True)
        self._on_mode_changed(current_mode)
        if current_format:
            idx = self.format_combo.findText(current_format)
            if idx >= 0:
                self.format_combo.setCurrentIndex(idx)
        self.subtitle_combo.setCurrentText(current_subtitle)
        self.category_combo.clear()
        self.category_combo.addItems([_(name) for name in DOWNLOAD_CATEGORIES])
        if current_category:
            idx = self.category_combo.findText(current_category)
            if idx >= 0:
                self.category_combo.setCurrentIndex(idx)
        self.post_action_combo.clear()
        self.post_action_combo.addItem(_("No action (Default)"), "none")
        self.post_action_combo.addItem(_("Open download folder"), "open_folder")
        self.post_action_combo.addItem(_("Play notification sound"), "play_sound")
        self.post_action_combo.addItem(_("Run custom script"), "run_script")
        self.post_action_combo.addItem(_("Transcribe audio to text"), "transcribe")
        idx = self.post_action_combo.findData(current_post_action)
        self.post_action_combo.setCurrentIndex(max(0, idx))
        self.schedule_repeat_combo.clear()
        self.schedule_repeat_combo.addItem(_("No repeat"), "none")
        self.schedule_repeat_combo.addItem(_("Repeat daily"), "daily")
        self.schedule_repeat_combo.addItem(_("Repeat weekly"), "weekly")
        idx = self.schedule_repeat_combo.findData(current_repeat)
        self.schedule_repeat_combo.setCurrentIndex(max(0, idx))
        self.format_combo.blockSignals(False)
        self.subtitle_combo.blockSignals(False)
        self.category_combo.blockSignals(False)
        self.post_action_combo.blockSignals(False)
        self.schedule_repeat_combo.blockSignals(False)
        for widget in (
            self.format_field,
            self.subtitle_field,
            self.category_field,
            self.save_to_field,
            self.aria2_field,
            self.post_action_field,
            self.speed_field,
            self.post_script_field,
        ):
            label = getattr(widget, "_label", None)
            key = getattr(widget, "_label_key", "")
            if label is not None and key:
                label.setText(_(key))
        preview_data = getattr(self.main_window, "preview_data", {}) or {}
        if preview_data:
            channel = str(preview_data.get("channel", "--") or "--")
            views = preview_data.get("views", "0")
            self.single_channel.setText(_("By {channel}  •  {views} views").format(channel=channel, views=views))

    def _toggle_trim_options(self):
        self.trim_toggle_requested.emit()

    def _on_formats_clicked(self):
        url = self.url_input.text().strip()
        if url:
            self.formats_requested.emit(url)

    def _on_mode_changed(self, mode_index=None):
        if mode_index is None:
            mode_index = self.current_mode_index()
        if mode_index == 0:
            self.quality_stack.setCurrentIndex(0)
            self.format_combo.clear()
            self.format_combo.addItems(VIDEO_FORMATS)
        elif mode_index == 1:
            self.quality_stack.setCurrentIndex(1)
            self.format_combo.clear()
            self.format_combo.addItems(AUDIO_FORMATS)
        elif mode_index == 2:
            self.quality_stack.setCurrentIndex(2)
            self.format_combo.clear()
            self.format_combo.addItem("GIF")
        preview_data = getattr(self.main_window, "preview_data", None)
        if isinstance(preview_data, dict) and preview_data:
            self.update_quality_size_labels(preview_data)

    def current_mode_index(self) -> int:
        if hasattr(self, "mode_group"):
            checked = int(self.mode_group.checkedId())
            return checked if checked >= 0 else 0
        return 0

    def is_audio_mode(self) -> bool:
        return self.current_mode_index() == 1

    def _on_format_changed_refresh_sizes(self):
        preview_data = getattr(self.main_window, "preview_data", None)
        if preview_data:
            self.update_quality_size_labels(preview_data)

    def _on_quality_chip_clicked(self, _btn):
        preview_data = getattr(self.main_window, "preview_data", None)
        if preview_data:
            self.update_quality_size_labels(preview_data)

    def _chip_style(self) -> str:
        return (
            "QPushButton {"
            "background-color: rgba(30, 41, 59, 0.6);"
            "color: #CBD5E1;"
            "border: 1px solid rgba(255, 255, 255, 0.08);"
            "border-radius: 8px;"
            "padding: 7px 12px;"
            "font-weight: 800;"
            "font-size: 13px;"
            "}"
            "QPushButton:hover:!disabled {"
            "background-color: rgba(51, 65, 85, 0.8);"
            "border-color: rgba(255, 255, 255, 0.2);"
            "color: #F8FAFC;"
            "}"
            "QPushButton:checked:!disabled {"
            "background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #3B82F6, stop:1 #2563EB);"
            "color: white;"
            "border: 1px solid rgba(59, 130, 246, 0.5);"
            "}"
            "QPushButton:disabled {"
            "background-color: rgba(15, 23, 42, 0.3);"
            "color: rgba(255, 255, 255, 0.2);"
            "border: 1px dashed rgba(255, 255, 255, 0.05);"
            "}"
        )

    def update_quality_size_labels(self, preview_data: dict | None):
        data = preview_data if isinstance(preview_data, dict) else {}
        duration_seconds = int(data.get("duration_seconds") or 0)
        fmt = self.format_combo.currentText().strip() if hasattr(self, "format_combo") else ""
        groups = (
            (getattr(self, "quality_radios", []), "video"),
            (getattr(self, "audio_quality_radios", []), "audio"),
        )
        
        from core.media_size import _quality_height, _audio_kbps, _is_video_format, _is_audio_format, coerce_size_bytes
        formats = data.get("formats", [])
        max_h = 0
        max_abr = 0
        for f in formats:
            if isinstance(f, dict):
                if _is_video_format(f):
                    h = coerce_size_bytes(f.get("height"))
                    if h > max_h: max_h = h
                if _is_audio_format(f):
                    abr = float(f.get("abr") or f.get("tbr") or 0)
                    if abr > max_abr: max_abr = abr

        for buttons, mode in groups:
            for chip in list(buttons or []):
                base_quality = str(chip.property("base_quality") or chip.text()).splitlines()[0].strip()
                size_bytes, exact = estimate_media_size_bytes(
                    data,
                    duration_seconds=duration_seconds,
                    mode=mode,
                    quality=base_quality,
                    fmt=fmt,
                )
                
                is_available = True
                if formats:
                    if mode == "video" and max_h > 0:
                        q_h = _quality_height(base_quality)
                        if q_h > max_h and (q_h - max_h) >= 100:
                            is_available = False
                    elif mode == "audio" and max_abr > 0 and str(base_quality).strip().lower() != "original":
                        q_kbps = _audio_kbps(base_quality)
                        if q_kbps > max_abr and (q_kbps - max_abr) >= 32:
                            is_available = False

                if not is_available:
                    chip.setEnabled(False)
                    chip.setText(f"{base_quality}\n" + _("Unavailable"))
                    chip.setToolTip(_("Not available for this media"))
                else:
                    chip.setEnabled(True)
                    label = format_size_label(size_bytes, estimated=not exact, empty=_("Unknown size"))
                    chip.setText(f"{base_quality}\n{label}" if size_bytes > 0 else base_quality)
                    chip.setToolTip(_("Estimated size: {size}").format(size=label))
                    
                    # Update the prominent size label for the currently selected chip
                    if chip.isChecked():
                        if hasattr(self, "pre_download_size_label"):
                            self.pre_download_size_label.setText(_("Total Size: {size}").format(size=label))
                        if hasattr(self, "status_container"):
                            self.status_container.show()

    def _secondary_action_style(self) -> str:
        t = get_theme(getattr(self.main_window, "theme", "Modern Dark"))
        text = t.get("text", "#E5E7EB")
        border = t.get("border", "#4B5563")
        panel = t.get("panel", "#1F2430")
        bg2 = t.get("bg_2", "#151923")
        return (
            "QPushButton {"
            "background-color: transparent;"
            f"color: {text};"
            f"border: 1px solid {border};"
            "border-radius: 10px;"
            "padding: 10px 20px;"
            "font-weight: 700;"
            "}"
            "QPushButton:hover {"
            f"background-color: {panel};"
            f"border-color: {t.get('muted', border)};"
            "}"
            "QPushButton:pressed {"
            f"background-color: {bg2};"
            "}"
        )

    def refresh_theme_styles(self):
        t = get_theme(getattr(self.main_window, "theme", "Midnight Neon"))
        text = t.get("text", "#FFFFFF")
        muted = t.get("muted", "#9CA3AF")
        border = t.get("border", "rgba(255, 255, 255, 0.08)")
        accent = t.get("accent", "#00E5FF")
        panel = t.get("panel", "rgba(30, 30, 45, 0.60)")
        panel_alt = t.get("panel_alt", "rgba(40, 40, 60, 0.50)")
        success = t.get("success", "#00E676")

        if hasattr(self, "single_thumb"):
            self.single_thumb.setStyleSheet(f"border-radius: 12px; background-color: {panel_alt}; border: 1px solid {border};")
        if hasattr(self, "single_title"):
            self.single_title.setStyleSheet(f"font-size: 20px; font-weight: 800; color: {text};")
        if hasattr(self, "single_channel"):
            self.single_channel.setStyleSheet(f"color: {muted}; font-size: 14px; font-weight: 500;")
        if hasattr(self, "mode_group"):
            segmented_style = (
                "QPushButton#SegmentedBtn {"
                "background-color: transparent;"
                f"color: {muted};"
                "border: none;"
                "border-radius: 10px;"
                "font-weight: 800;"
                "font-size: 14px;"
                "padding: 8px;"
                "}"
                "QPushButton#SegmentedBtn:hover {"
                f"color: {text};"
                "background-color: rgba(255,255,255,0.05);"
                "}"
                "QPushButton#SegmentedBtn:checked {"
                f"background-color: {panel_alt};"
                f"color: {accent};"
                f"border: 1px solid {accent};"
                "}"
            )
            for btn in self.mode_group.buttons():
                btn.setStyleSheet(segmented_style)
        if hasattr(self, "aria2_checkbox"):
            self.aria2_checkbox.setStyleSheet(
                f"""
                QCheckBox {{
                    spacing: 8px;
                    color: {text};
                    font-weight: 600;
                }}
                QCheckBox::indicator {{
                    width: 36px;
                    height: 20px;
                    border-radius: 10px;
                    background: {panel_alt};
                    border: 1px solid {border};
                }}
                QCheckBox::indicator:checked {{
                    background: {success};
                    border: 1px solid {success};
                }}
                """
            )
        for chip in list(getattr(self, "quality_radios", [])) + list(getattr(self, "audio_quality_radios", [])):
            chip.setStyleSheet(self._chip_style())

        if hasattr(self, "log_text"):
            self.log_text.setStyleSheet(
                f"background-color: {t.get('bg', '#0d0d12')}; "
                f"border: 1px solid {border}; "
                f"border-radius: 6px; "
                f"padding: 5px; "
                f"color: {muted}; "
                f"font-family: monospace; "
                f"font-size: 11px;"
            )

    def _pick_out_dir(self):
        start_dir = self.get_out_dir() or default_download_dir()
        folder = QFileDialog.getExistingDirectory(self, _("Select download folder"), start_dir)
        if folder:
            self.set_out_dir(folder)

    def _pick_post_script(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            _("Select Post-Download Script"),
            "",
            _("Scripts (*.py *.ps1 *.bat *.cmd);;All Files (*)"),
        )
        if path and hasattr(self, "post_script_input"):
            self.post_script_input.setText(str(path))

    def show_empty_state(self):
        self.search_stack.setCurrentIndex(0)

    def show_single_state(self):
        self.search_stack.setCurrentIndex(1)

    def get_url(self) -> str:
        return str(self.url_input.text() if hasattr(self, "url_input") else "").strip()

    def set_url(self, url: str):
        if hasattr(self, "url_input"):
            self.url_input.setText(str(url or ""))

    def set_search_button(self, text: str | None = None, enabled: bool | None = None):
        if not hasattr(self, "search_btn"):
            return
        if text is not None:
            self.search_btn.setText(str(text))
        if enabled is not None:
            self.search_btn.setEnabled(bool(enabled))

    def set_download_button(self, text: str | None = None, enabled: bool | None = None):
        if not hasattr(self, "download_btn"):
            return
        if text is not None:
            self.download_btn.setText(str(text))
        if enabled is not None:
            self.download_btn.setEnabled(bool(enabled))

    def get_out_dir(self) -> str:
        return str(self.out_dir_input.text() if hasattr(self, "out_dir_input") else "").strip()

    def set_out_dir(self, out_dir: str):
        if hasattr(self, "out_dir_input"):
            self.out_dir_input.setText(str(out_dir or ""))

    def set_aria2_checked(self, checked: bool):
        if hasattr(self, "aria2_checkbox"):
            self.aria2_checkbox.setChecked(bool(checked))

    def update_progress(self, percent):
        value = max(0.0, min(100.0, float(percent or 0.0)))
        if hasattr(self, "status_container"):
            self.status_container.show()
        if hasattr(self, "progress_bar"):
            self.progress_bar.setValue(int(round(value)))
        if hasattr(self, "size_label"):
            self.size_label.setText(f"{value:.1f}%")

    def update_speed(self, speed_str, speed_val=None):
        if hasattr(self, "speed_label"):
            self.speed_label.setText(f"{_('Speed:')} {speed_str}")
        if hasattr(self, "speed_graph") and speed_val is not None:
            try:
                self.speed_graph.add_value(float(speed_val))
            except Exception:
                pass

    def update_eta(self, eta_str):
        if hasattr(self, "eta_label"):
            self.eta_label.setText(f"{_('ETA:')} {eta_str}")

    def update_status(self, status):
        s = str(status or "").lower().strip()
        if hasattr(self, "status_container"):
            self.status_container.show()
        if hasattr(self, "progress_bar"):
            if s in {"completed", "success"}:
                self.progress_bar.set_status("completed")
            elif s in {"error", "failed", "cancelled"}:
                self.progress_bar.set_status("error")
            elif s == "paused":
                self.progress_bar.set_status("paused")
            elif s in {"idle", "ready", "pending", "queued", "waiting"}:
                self.progress_bar.set_status("idle")
            else:
                self.progress_bar.set_status("downloading")
        if not hasattr(self, "state_label"):
            return
        if s in {"completed", "success"}:
            self.state_label.setText(_("Download completed successfully!"))
            self.state_label.setStyleSheet("color: #10B981; font-weight: bold;")
        elif s in {"error", "failed", "cancelled"}:
            self.state_label.setText(_("Download failed!"))
            self.state_label.setStyleSheet("color: #F43F5E; font-weight: bold;")
        elif s == "paused":
            self.state_label.setText(_("Download paused"))
            self.state_label.setStyleSheet("")
        elif s == "running":
            self.state_label.setText(_("Downloading..."))
            self.state_label.setStyleSheet("")



