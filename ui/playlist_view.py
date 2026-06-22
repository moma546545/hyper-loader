
"""
ui/playlist_view.py — Dedicated Playlist Management UI
Provides advanced playlist fetching, selection, and batch download capabilities.
"""
import re
import os
import urllib.request
from collections import deque
from contextlib import suppress
from core.constants import VIDEO_FORMATS, AUDIO_FORMATS, SUBTITLE_OPTIONS
from core.i18n import _
from core.media_size import estimate_media_size_bytes, format_size_label
from ui.schedule_widget import SchedulePicker
from ui.models import PlaylistListModel

try:
    from PySide6.QtCore import Qt, Signal, QTimer, QUrl, QPoint
    from PySide6.QtGui import QPixmap
    from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
    from PySide6.QtWidgets import (
        QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel,
        QPushButton, QLineEdit, QListView,
        QComboBox, QCheckBox,
        QGridLayout
    )
except ImportError:
    from PyQt6.QtCore import Qt, pyqtSignal as Signal, QTimer, QUrl, QPoint
    from PyQt6.QtGui import QPixmap
    from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
    from PyQt6.QtWidgets import (
        QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel,
        QPushButton, QLineEdit, QListView,
        QComboBox, QCheckBox,
        QGridLayout
    )

class PlaylistView(QWidget):
    """
    Dedicated view for analyzing and downloading playlists.
    Signals:
        analyzeRequested(str) - Emitted when user wants to fetch a playlist
        downloadRequested(list) - Emitted with a list of task dicts to download
    """
    analyzeRequested = Signal(str)
    forceAnalyzeRequested = Signal(str)
    downloadRequested = Signal(list)

    def __init__(self, parent=None, net_manager=None):
        super().__init__(parent)
        self.playlist_items = []
        self.playlist_rows = {}
        self.thumbnail_cache = {}
        self._thumbnail_waiters: dict[str, list[QLabel]] = {}
        self._thumbnail_inflight: set[str] = set()
        self.net_manager = net_manager or QNetworkAccessManager(self)
        self._visible_buffer_rows = 18
        self._visible_refresh_timer = QTimer(self)
        self._visible_refresh_timer.setSingleShot(True)
        self._visible_refresh_timer.timeout.connect(self._refresh_visible_rows)
        self._metrics_refresh_timer = QTimer(self)
        self._metrics_refresh_timer.setSingleShot(True)
        self._metrics_refresh_timer.timeout.connect(self._refresh_metrics)
        self._new_entry_ids: set = set()   # highlighted after diff
        self._removed_entry_ids: set = set()
        self._current_playlist_url = ""
        self._selected_item_count = 0
        self._selected_size_bytes = 0
        self._total_estimated_size_bytes = 0
        self._deferred_playlist_queue = deque()
        self._deferred_playlist_payload = {}
        self._deferred_playlist_new_entry_ids: set = set()
        self._deferred_playlist_removed_entry_ids: set = set()
        self._deferred_playlist_total = 0
        self._deferred_playlist_timer = QTimer(self)
        self._deferred_playlist_timer.setSingleShot(True)
        self._deferred_playlist_timer.timeout.connect(self._consume_deferred_playlist_chunk)
        self._pending_size_label_rows: set[int] = set()
        self._size_label_refresh_timer = QTimer(self)
        self._size_label_refresh_timer.setSingleShot(True)
        self._size_label_refresh_timer.timeout.connect(self._flush_pending_row_size_labels)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # Header
        hdr = QHBoxLayout()
        self.title_label = QLabel(_("Playlist Manager"))
        self.title_label.setObjectName("single_title")
        self.desc_label = QLabel(_("Fetch full playlists, select specific videos, and download in batch"))
        self.desc_label.setObjectName("single_sub")
        hdr.addWidget(self.title_label)
        hdr.addStretch(1)
        layout.addLayout(hdr)
        layout.addWidget(self.desc_label)

        # Input Area
        input_container = QFrame()
        input_container.setObjectName("search_bar_container")
        input_container.setStyleSheet("""
            QFrame#search_bar_container {
                background-color: rgba(30, 41, 59, 0.7);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 16px;
            }
        """)
        input_container.setFixedHeight(72)
        input_layout = QHBoxLayout(input_container)
        input_layout.setContentsMargins(16, 0, 16, 0)
        input_layout.setSpacing(8)

        self.url_input = QLineEdit()
        self.url_input.setObjectName("search_input")
        self.url_input.setStyleSheet("""
            QLineEdit {
                background-color: rgba(15, 23, 42, 0.6);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 12px;
                padding: 10px 16px;
                font-size: 14px;
                color: #F8FAFC;
            }
            QLineEdit:focus {
                border: 1px solid #3B82F6;
                background-color: rgba(15, 23, 42, 0.8);
            }
        """)
        self.url_input.setPlaceholderText(_("Paste YouTube/SoundCloud Playlist URL here..."))
        self.url_input.returnPressed.connect(self._on_analyze_clicked)

        btn_style = """
            QPushButton {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #3B82F6, stop:1 #2563EB);
                color: white;
                border-radius: 12px;
                padding: 10px 20px;
                font-weight: 800;
                font-size: 14px;
                border: 1px solid rgba(255, 255, 255, 0.1);
            }
            QPushButton:hover {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #2563EB, stop:1 #1D4ED8);
            }
            QPushButton:disabled {
                background-color: rgba(59, 130, 246, 0.3);
                color: rgba(255, 255, 255, 0.5);
            }
        """
        
        self.analyze_btn = QPushButton(_("Analyze Playlist"))
        self.analyze_btn.setObjectName("search_btn")
        self.analyze_btn.setStyleSheet(btn_style)
        self.analyze_btn.setMinimumHeight(44)
        self.analyze_btn.clicked.connect(self._on_analyze_clicked)

        self.force_analyze_btn = QPushButton(_("Force Reanalyze"))
        self.force_analyze_btn.setObjectName("secondary_btn")
        self.force_analyze_btn.setStyleSheet(btn_style.replace("#3B82F6", "#475569").replace("#2563EB", "#334155").replace("#1D4ED8", "#1E293B"))
        self.force_analyze_btn.setMinimumHeight(44)
        self.force_analyze_btn.clicked.connect(self._on_force_analyze_clicked)

        input_layout.addWidget(self.url_input, 1)
        input_layout.addWidget(self.analyze_btn)
        input_layout.addWidget(self.force_analyze_btn)
        layout.addWidget(input_container)

        # Global Settings & Select All Header
        settings_frame = QFrame()
        settings_frame.setObjectName("playlist_header")
        settings_frame.setStyleSheet("""
            QFrame#playlist_header {
                background-color: rgba(30, 41, 59, 0.5);
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-radius: 12px;
            }
            QComboBox {
                background-color: rgba(15, 23, 42, 0.8);
                border: 1px solid rgba(255, 255, 255, 0.1);
                border-radius: 8px;
                padding: 6px 12px;
                color: #E2E8F0;
                font-weight: 600;
            }
            QComboBox:hover {
                border-color: rgba(255, 255, 255, 0.3);
            }
            QCheckBox {
                spacing: 8px;
                color: #F8FAFC;
                font-weight: 700;
                font-size: 13px;
            }
        """)
        settings_layout = QHBoxLayout(settings_frame)
        settings_layout.setContentsMargins(16, 12, 16, 12)
        settings_layout.setSpacing(12)

        self.select_all_cb = QCheckBox(_("Select All"))
        self.select_all_cb.toggled.connect(self._toggle_select_all)
        self.select_all_cb.setEnabled(False)

        self.global_format = QComboBox()
        self.global_format.addItems(VIDEO_FORMATS + AUDIO_FORMATS)
        self.global_format.setCurrentText("WAV")
        self.global_format.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.global_format.setMinimumWidth(100)
        self.global_format.currentTextChanged.connect(self._on_global_format_changed)
        
        self.global_quality = QComboBox()
        self._refresh_global_quality_options("WAV")
        self.global_quality.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.global_quality.setMinimumWidth(100)
        self.global_quality.currentTextChanged.connect(self._apply_global_quality)

        self.global_subtitle = QComboBox()
        self.global_subtitle.addItems(SUBTITLE_OPTIONS)
        self.global_subtitle.setCurrentText("None")
        self.global_subtitle.setEditable(True)
        self.global_subtitle.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.global_subtitle.setToolTip(_("اكتب أكثر من لغة مفصولة بفاصلة مثل: English,ar أو اختر All"))
        self.global_subtitle.setMinimumWidth(100)

        self.status_lbl = QLabel(_("Ready"))
        self.status_lbl.setObjectName("single_sub")
        self.status_lbl.setMinimumWidth(120)
        self.total_size_lbl = QLabel(_("Total Size: --"))
        self.total_size_lbl.setObjectName("single_sub")
        self.total_size_lbl.setMinimumWidth(150)

        self.format_lbl = QLabel(_("Format:"))
        self.quality_lbl = QLabel(_("Quality:"))
        self.subtitle_lbl = QLabel(_("Subtitles:"))

        settings_layout.addWidget(self.select_all_cb)
        settings_layout.addWidget(self.format_lbl)
        settings_layout.addWidget(self.global_format)
        settings_layout.addWidget(self.quality_lbl)
        settings_layout.addWidget(self.global_quality)
        settings_layout.addWidget(self.subtitle_lbl)
        settings_layout.addWidget(self.global_subtitle)
        settings_layout.addStretch(1)
        settings_layout.addWidget(self.total_size_lbl)
        settings_layout.addWidget(self.status_lbl)

        layout.addWidget(settings_frame)

        self.schedule_picker = SchedulePicker(self, title=_("Playlist Schedule"), compact=True)
        layout.addWidget(self.schedule_picker)

        # List Widget for Videos
        self.list_widget = QListView()
        self.list_model = PlaylistListModel(self.playlist_items)
        self.list_widget.setModel(self.list_model)
        self.list_widget.setObjectName("playlist_list")
        self.list_widget.setFrameShape(QFrame.Shape.NoFrame)
        self.list_widget.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_widget.verticalScrollBar().valueChanged.connect(lambda _value: self._schedule_visible_refresh())
        self.list_widget.viewport().installEventFilter(self)
        
        # Give list widget maximum possible space in the layout
        layout.addWidget(self.list_widget, stretch=14)

        # Footer
        footer = QHBoxLayout()
        self.download_btn = QPushButton(f"{_('Download Selected')} (0/0)")
        self.download_btn.setObjectName("action_download")
        self.download_btn.setEnabled(False)
        self.download_btn.setMinimumWidth(280)
        self.download_btn.setFixedHeight(54)
        self.download_btn.setStyleSheet("""
            QPushButton {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #10B981, stop:1 #059669);
                color: white;
                border-radius: 14px;
                font-weight: 900;
                font-size: 16px;
                letter-spacing: 0.5px;
                border: 1px solid rgba(255, 255, 255, 0.2);
            }
            QPushButton:hover:enabled {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #059669, stop:1 #047857);
                border: 1px solid rgba(255, 255, 255, 0.4);
            }
            QPushButton:disabled {
                background-color: rgba(16, 185, 129, 0.2);
                color: rgba(255, 255, 255, 0.4);
                border: 1px solid rgba(255, 255, 255, 0.05);
            }
        """)
        self.download_btn.clicked.connect(self._on_download_clicked)
        
        footer.addStretch(1)
        footer.addWidget(self.download_btn)
        layout.addLayout(footer)

    def retranslate_ui(self):
        self.title_label.setText(_("Playlist Manager"))
        self.desc_label.setText(_("Fetch full playlists, select specific videos, and download in batch"))
        self.url_input.setPlaceholderText(_("Paste YouTube/SoundCloud Playlist URL here..."))
        if str(self.analyze_btn.text() or "").strip() in {"Analyze Playlist", _("Analyze Playlist"), "Analyzing...", _("Analyzing...")}:
            self.analyze_btn.setText(_("Analyze Playlist") if self.analyze_btn.isEnabled() else _("Analyzing..."))
        self.force_analyze_btn.setText(_("Force Reanalyze"))
        self.select_all_cb.setText(_("Select All"))
        self.global_subtitle.setToolTip(_("Enter multiple subtitle languages separated by commas, for example: English,ar, or choose All"))
        self.format_lbl.setText(_("Format:"))
        self.quality_lbl.setText(_("Quality:"))
        self.subtitle_lbl.setText(_("Subtitles:"))
        if hasattr(self, "schedule_picker"):
            self.schedule_picker.title_label.setText(_("Playlist Schedule"))
        self.total_size_lbl.setText(_("Total Size: --") if str(self.total_size_lbl.text() or "").strip() in {"Total Size: --", "الحجم الكلي: --"} else self.total_size_lbl.text())
        if str(self.status_lbl.text() or "").strip() in {"Ready", "جاهز"}:
            self.status_lbl.setText(_("Ready"))
        self.download_btn.setText(f"{_('Download Selected')} ({self._selected_item_count}/{len(self.playlist_items)})")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _on_analyze_clicked(self):
        url = self.url_input.text().strip()
        if not url:
            return
        self.analyze_btn.setEnabled(False)
        self.force_analyze_btn.setEnabled(False)
        self.analyze_btn.setText(_("Analyzing..."))
        self.status_lbl.setText(_("Fetching playlist data..."))
        self.analyzeRequested.emit(url)

    def _on_force_analyze_clicked(self):
        url = self.url_input.text().strip()
        if not url:
            return
        self.analyze_btn.setEnabled(False)
        self.force_analyze_btn.setEnabled(False)
        self.analyze_btn.setText(_("Analyzing..."))
        self.status_lbl.setText(_("Force reanalyze requested..."))
        self.forceAnalyzeRequested.emit(url)

    def set_loading_state(self, is_loading: bool):
        if is_loading:
            self.analyze_btn.setEnabled(False)
            self.force_analyze_btn.setEnabled(False)
            self.analyze_btn.setText(_("Analyzing..."))
            self.status_lbl.setText(_("Fetching playlist data..."))
        else:
            self.analyze_btn.setEnabled(True)
            self.force_analyze_btn.setEnabled(True)
            self.analyze_btn.setText(_("Analyze Playlist"))
            self.status_lbl.setText(_("Ready"))

    def _clear_list(self):
        self._cancel_deferred_playlist_append()
        self._pending_size_label_rows.clear()
        if self._size_label_refresh_timer.isActive():
            self._size_label_refresh_timer.stop()
        self._thumbnail_waiters.clear()
        self._thumbnail_inflight.clear()
        self.playlist_items.clear()
        self.list_model.update_items(self.playlist_items)
        self._clear_row_widgets()
        self._reset_cached_metrics()
        self._new_entry_ids = set()
        self._removed_entry_ids = set()
        self.select_all_cb.setEnabled(False)
        self.select_all_cb.setChecked(False)
        self.download_btn.setEnabled(False)
        self.download_btn.setText(f"{_('Download Selected')} (0/0)")
        self.total_size_lbl.setText(_("Total Size: --"))

    def prepare_for_playlist_fetch(self, url: str, *, preserve_existing: bool = False) -> bool:
        normalized_url = str(url or "").strip()
        same_playlist = bool(normalized_url) and normalized_url == self._current_playlist_url
        can_preserve = bool(preserve_existing and same_playlist and self.playlist_items)
        if not can_preserve:
            self._clear_list()
        self._current_playlist_url = normalized_url
        if can_preserve:
            self.status_lbl.setText(_("Reanalyzing playlist..."))
        return can_preserve

    @staticmethod
    def _entry_id(item: dict) -> str:
        if not isinstance(item, dict):
            return ""
        return str(item.get("id") or item.get("entry_id") or item.get("video_id") or "").strip()

    def remove_entries_by_ids(self, entry_ids: set | None) -> int:
        ids = {str(eid).strip() for eid in (entry_ids or set()) if str(eid).strip()}
        if not ids or not self.playlist_items:
            return 0
        remove_indexes = [
            idx for idx, item in enumerate(self.playlist_items)
            if self._entry_id(item) in ids
        ]
        removed_count = len(remove_indexes)
        if removed_count <= 0:
            return 0
        self._removed_entry_ids = self._removed_entry_ids | ids
        self.list_model.remove_rows(remove_indexes)
        self._clear_row_widgets()
        self._recalculate_cached_metrics()
        has_items = bool(self.playlist_items)
        self.select_all_cb.setEnabled(has_items)
        self.download_btn.setEnabled(has_items)
        self.select_all_cb.blockSignals(True)
        self.select_all_cb.setChecked(has_items and self._selected_item_count == len(self.playlist_items))
        self.select_all_cb.blockSignals(False)
        self._schedule_visible_refresh()
        self._schedule_metrics_refresh()
        return removed_count

    def set_playlist_data(
        self,
        payload: dict,
        items: list,
        *,
        new_entry_ids: set | None = None,
        removed_entry_ids: set | None = None,
        reset: bool = True,
    ):
        if reset:
            self._clear_list()
        else:
            self.remove_entries_by_ids(removed_entry_ids or set())
        self._new_entry_ids = self._new_entry_ids | set(new_entry_ids or set())
        if self._should_defer_playlist_append(items):
            self._start_deferred_playlist_append(
                payload,
                items,
                new_entry_ids=new_entry_ids,
                removed_entry_ids=removed_entry_ids,
            )
            return
        self.append_playlist_items(payload, items)
        if not self.playlist_items:
            self.status_lbl.setText(_("Playlist is empty or invalid."))
            return
        self.finalize_playlist_data(
            payload,
            len(self.playlist_items),
            new_entry_ids=new_entry_ids,
            removed_entry_ids=removed_entry_ids,
        )

    def append_playlist_items(self, payload: dict, items: list, *, new_entry_ids: set | None = None):
        if new_entry_ids is not None:
            self._new_entry_ids = self._new_entry_ids | set(new_entry_ids)
        next_items = []
        for raw in items or []:
            if not isinstance(raw, dict):
                continue
            item = raw
            self._ensure_item_state(item)
            next_items.append(item)
        if not next_items:
            return
        self.list_model.append_items(next_items)
        self._selected_item_count += sum(1 for item in next_items if bool(item.get("_selected", True)))
        self._selected_size_bytes += sum(
            int(item.get("estimated_size_bytes", 0) or 0)
            for item in next_items
            if bool(item.get("_selected", True))
        )
        self._total_estimated_size_bytes += sum(int(item.get("estimated_size_bytes", 0) or 0) for item in next_items)
        self.select_all_cb.setEnabled(True)
        self.download_btn.setEnabled(True)
        title = str((payload or {}).get("title") or "").strip()
        if title:
            self.status_lbl.setText(_("Loading playlist: {title}").format(title=title))
        self._schedule_visible_refresh()
        self._schedule_metrics_refresh()

    def _deferred_append_threshold(self) -> int:
        try:
            raw = int(os.environ.get("SNAPDOWNLOADER_PLAYLIST_DEFER_THRESHOLD", "1200") or 1200)
        except Exception:
            raw = 1200
        return max(400, min(50000, raw))

    def _deferred_append_chunk_size(self) -> int:
        try:
            raw = int(os.environ.get("SNAPDOWNLOADER_PLAYLIST_DEFER_CHUNK", "300") or 300)
        except Exception:
            raw = 300
        return max(100, min(5000, raw))

    def _should_defer_playlist_append(self, items: list | None) -> bool:
        return len(items or []) >= self._deferred_append_threshold()

    def _cancel_deferred_playlist_append(self):
        if self._deferred_playlist_timer.isActive():
            self._deferred_playlist_timer.stop()
        self._deferred_playlist_queue.clear()
        self._deferred_playlist_payload = {}
        self._deferred_playlist_new_entry_ids = set()
        self._deferred_playlist_removed_entry_ids = set()
        self._deferred_playlist_total = 0

    def _start_deferred_playlist_append(
        self,
        payload: dict,
        items: list,
        *,
        new_entry_ids: set | None = None,
        removed_entry_ids: set | None = None,
    ):
        self._cancel_deferred_playlist_append()
        prepared = [item for item in (items or []) if isinstance(item, dict)]
        self._deferred_playlist_queue = deque(prepared)
        self._deferred_playlist_payload = dict(payload or {})
        self._deferred_playlist_new_entry_ids = set(new_entry_ids or set())
        self._deferred_playlist_removed_entry_ids = set(removed_entry_ids or set())
        self._deferred_playlist_total = len(prepared)
        if self._deferred_playlist_total <= 0:
            self.status_lbl.setText(_("Playlist is empty or invalid."))
            return
        self._consume_deferred_playlist_chunk()

    def _consume_deferred_playlist_chunk(self):
        if not self._deferred_playlist_queue:
            self.finalize_playlist_data(
                self._deferred_playlist_payload,
                len(self.playlist_items),
                new_entry_ids=self._deferred_playlist_new_entry_ids,
                removed_entry_ids=self._deferred_playlist_removed_entry_ids,
            )
            self._cancel_deferred_playlist_append()
            return

        chunk_size = self._deferred_append_chunk_size()
        chunk = [self._deferred_playlist_queue.popleft() for _ in range(min(chunk_size, len(self._deferred_playlist_queue)))]
        self.append_playlist_items(self._deferred_playlist_payload, chunk)
        loaded = len(self.playlist_items)
        total = max(loaded, int(self._deferred_playlist_total or 0))
        title = str(self._deferred_playlist_payload.get("title") or "").strip()
        if title:
            self.status_lbl.setText(_("Loading playlist: {title} ({loaded}/{total})").format(title=title, loaded=loaded, total=total))
        else:
            self.status_lbl.setText(_("Loading playlist ({loaded}/{total})").format(loaded=loaded, total=total))

        if self._deferred_playlist_queue:
            self._deferred_playlist_timer.start(0)

    def finalize_playlist_data(
        self,
        payload: dict,
        item_count: int = 0,
        *,
        new_entry_ids: set | None = None,
        removed_entry_ids: set | None = None,
    ):
        if new_entry_ids is not None:
            self._new_entry_ids = self._new_entry_ids | set(new_entry_ids)
        if removed_entry_ids is not None:
            self._removed_entry_ids = self._removed_entry_ids | set(removed_entry_ids)
        playlist_url = str((payload or {}).get("url") or (payload or {}).get("webpage_url") or "").strip()
        if playlist_url:
            self._current_playlist_url = playlist_url
        count = int(item_count or len(self.playlist_items))
        new_count = len(self._new_entry_ids)
        removed_count = len(self._removed_entry_ids)
        if count <= 0:
            self.status_lbl.setText(_("Playlist is empty or invalid."))
            return
        if new_count > 0 and removed_count > 0:
            status_text = _(
                "Playlist synced ({count} items, +{new} new, -{removed} removed)"
            ).format(count=count, new=new_count, removed=removed_count)
        elif new_count > 0:
            status_text = _(
                "Playlist loaded ({count} items, {new} new since last fetch)"
            ).format(count=count, new=new_count)
        elif removed_count > 0:
            status_text = _(
                "Playlist synced ({count} items, {removed} removed since last fetch)"
            ).format(count=count, removed=removed_count)
        else:
            status_text = _(
                "Playlist loaded successfully ({count} items)"
            ).format(count=count)
        self.status_lbl.setText(status_text)
        self.select_all_cb.blockSignals(True)
        self.select_all_cb.setChecked(bool(self.playlist_items) and self._selected_item_count == len(self.playlist_items))
        self.select_all_cb.blockSignals(False)
        self._schedule_visible_refresh()
        self._refresh_metrics()

    def _schedule_metrics_refresh(self):
        if not self._metrics_refresh_timer.isActive():
            self._metrics_refresh_timer.start(0)

    def _refresh_metrics(self):
        self._update_download_button()
        self._update_total_size()

    def _reset_cached_metrics(self):
        self._selected_item_count = 0
        self._selected_size_bytes = 0
        self._total_estimated_size_bytes = 0

    def _recalculate_cached_metrics(self):
        selected_count = 0
        selected_size = 0
        total_size = 0
        for item in self.playlist_items:
            size_bytes = int(item.get("estimated_size_bytes", 0) or 0)
            total_size += size_bytes
            if bool(item.get("_selected", True)):
                selected_count += 1
                selected_size += size_bytes
        self._selected_item_count = selected_count
        self._selected_size_bytes = selected_size
        self._total_estimated_size_bytes = total_size

    def _estimate_item_size_bytes(self, item: dict) -> int:
        fmt = str(item.get("_format") or "MP4")
        quality = str(item.get("_quality") or "1080p")
        mode = "audio" if fmt in AUDIO_FORMATS else "video"
        size_bytes, exact = estimate_media_size_bytes(
            item,
            duration_seconds=int(item.get("duration_seconds", 0) or 0),
            mode=mode,
            quality=quality,
            fmt=fmt,
        )
        item["size_is_estimate"] = not exact
        return size_bytes

    @staticmethod
    def _item_size_bytes(item: dict) -> int:
        return int(item.get("estimated_size_bytes", 0) or 0)

    def _ensure_item_state(self, item: dict):
        fmt = str(item.get("_format") or self.global_format.currentText() or "MP4").strip() or "MP4"
        qualities = self._quality_options_for_format(fmt)
        quality = str(item.get("_quality") or self.global_quality.currentText() or "").strip()
        if quality not in qualities:
            quality = qualities[0]
        item["_format"] = fmt
        item["_quality"] = quality
        item["_selected"] = bool(item.get("_selected", True))
        item["estimated_size_bytes"] = self._estimate_item_size_bytes(item)

    def _clear_row_widgets(self):
        for row_index in list(self.playlist_rows.keys()):
            self._release_row_widget(row_index)
        self.playlist_rows.clear()

    def _add_item_card(self, row_index: int, row_data: dict):
        model_index = self.list_model.index(row_index, 0)
        if not model_index.isValid():
            return
        card = QFrame()
        card.setObjectName("playlist_row")
        card.setFixedHeight(104)
        card.setStyleSheet("""
            QFrame#playlist_row {
                background-color: rgba(30, 41, 59, 0.6);
                border-radius: 14px;
                border: 1px solid rgba(255, 255, 255, 0.05);
                margin: 4px;
            }
            QFrame#playlist_row:hover {
                background-color: rgba(51, 65, 85, 0.8);
                border-color: rgba(255, 255, 255, 0.15);
            }
        """)
        # Highlight entries new since last fetch
        entry_id = str(
            row_data.get("id") or row_data.get("entry_id") or row_data.get("video_id") or ""
        ).strip()
        if entry_id and entry_id in self._new_entry_ids:
            card.setStyleSheet(card.styleSheet() + 
                "QFrame#playlist_row { border-left: 4px solid #F59E0B; background-color: rgba(245,158,11,0.1); }"
            )
        h = QHBoxLayout(card)
        h.setContentsMargins(12, 8, 12, 8)
        h.setSpacing(14)

        cb = QCheckBox()
        cb.setChecked(bool(row_data.get("_selected", True)))
        cb.toggled.connect(lambda checked, idx=row_index: self._on_row_selection_changed(idx, checked))

        thumb_lbl = QLabel()
        thumb_lbl.setObjectName("thumb_preview")
        thumb_lbl.setFixedSize(160, 90)
        thumb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb_lbl.setStyleSheet("""
            background-color: #0F172A;
            border-radius: 8px;
            border: 1px solid rgba(255, 255, 255, 0.1);
            font-size: 24px;
        """)
        
        # Async thumbnail loading
        thumb_url = row_data.get("thumbnail", "")
        if thumb_url in self.thumbnail_cache:
            thumb_lbl.setPixmap(self.thumbnail_cache[thumb_url])
        else:
            thumb_lbl.setText("🎬")
            if thumb_url:
                self._async_load_thumbnail(thumb_url, thumb_lbl)

        title_lbl = QLabel(row_data.get("title", _("Unknown Title")))
        title_lbl.setObjectName("playlist_title")
        title_lbl.setWordWrap(True)
        title_lbl.setStyleSheet("font-size: 15px; font-weight: 700; color: #F8FAFC; letter-spacing: 0.3px;")
        live_status = str(row_data.get("live_status", "") or "").strip().lower()
        live_badge_text = ""
        if bool(row_data.get("is_live", False)) or live_status in {"is_live", "live"}:
            live_badge_text = _("LIVE")
        elif bool(row_data.get("was_live", False)) or live_status == "was_live":
            live_badge_text = _("WAS LIVE")
        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(4)
        title_col.addWidget(title_lbl)
        if live_badge_text:
            live_badge = QLabel(live_badge_text)
            live_badge.setObjectName("chip")
            live_badge.setStyleSheet(
                "font-size: 10px; font-weight: 700; padding: 2px 8px; "
                "border-radius: 8px; background-color: rgba(239, 68, 68, 0.14); color: #EF4444;"
            )
            title_col.addWidget(live_badge, 0, Qt.AlignmentFlag.AlignLeft)

        dur_lbl = QLabel(self._format_duration(row_data.get("duration_seconds", 0)))
        dur_lbl.setObjectName("single_sub")
        size_lbl = QLabel("--")
        size_lbl.setObjectName("single_sub")

        fmt = QComboBox()
        fmt.addItems(VIDEO_FORMATS + AUDIO_FORMATS)
        fmt.setCurrentText(str(row_data.get("_format") or self.global_format.currentText()))
        fmt.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        fmt.setFixedWidth(140)

        quality = QComboBox()
        quality.addItems(self._quality_options_for_format(fmt.currentText()))
        desired_quality = str(row_data.get("_quality") or self.global_quality.currentText()).strip()
        if desired_quality in [quality.itemText(i) for i in range(quality.count())]:
            quality.setCurrentText(desired_quality)
        quality.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        quality.setFixedWidth(140)
        
        meta_col = QVBoxLayout()
        meta_col.setContentsMargins(0, 0, 0, 0)
        meta_col.setSpacing(2)
        meta_col.addWidget(dur_lbl, 0, Qt.AlignmentFlag.AlignRight)
        meta_col.addWidget(size_lbl, 0, Qt.AlignmentFlag.AlignRight)

        h.addWidget(cb)
        h.addWidget(thumb_lbl)
        h.addLayout(title_col, 1)
        h.addLayout(meta_col)
        h.addWidget(fmt)
        h.addWidget(quality)

        self.list_widget.setIndexWidget(model_index, card)

        row_ref = {
            "widget": card,
            "check": cb,
            "format": fmt,
            "quality": quality,
            "item": row_data,
            "size_lbl": size_lbl,
        }
        fmt.currentTextChanged.connect(lambda val, idx=row_index, q=quality: self._on_row_format_changed(idx, val, q))
        quality.currentTextChanged.connect(lambda val, idx=row_index: self._on_row_quality_changed(idx, val))
        self._refresh_row_size_label(row_index)
        self.playlist_rows[row_index] = row_ref

    def _release_row_widget(self, row_index: int):
        row_ref = self.playlist_rows.pop(row_index, None)
        if not row_ref:
            return
        widget = row_ref.get("widget")
        model_index = self.list_model.index(row_index, 0)
        if model_index.isValid():
            self.list_widget.setIndexWidget(model_index, None)
        if widget is not None:
            widget.deleteLater()

    def _schedule_visible_refresh(self):
        if not self._visible_refresh_timer.isActive():
            self._visible_refresh_timer.start(0)

    def _visible_materialize_budget(self) -> int:
        try:
            raw = int(os.environ.get("SNAPDOWNLOADER_PLAYLIST_VISIBLE_BUDGET", "24") or 24)
        except Exception:
            raw = 24
        return max(4, min(200, raw))

    def _size_label_refresh_budget(self) -> int:
        try:
            raw = int(os.environ.get("SNAPDOWNLOADER_PLAYLIST_SIZE_LABEL_BUDGET", "32") or 32)
        except Exception:
            raw = 32
        return max(4, min(500, raw))

    def _queue_row_size_refresh(self, row_index: int):
        if not isinstance(row_index, int):
            return
        if row_index < 0 or row_index >= len(self.playlist_items):
            return
        self._pending_size_label_rows.add(row_index)
        if not self._size_label_refresh_timer.isActive():
            self._size_label_refresh_timer.start(0)

    def _queue_row_size_refresh_many(self, row_indexes):
        has_any = False
        for row_index in row_indexes or []:
            if not isinstance(row_index, int):
                continue
            if row_index < 0 or row_index >= len(self.playlist_items):
                continue
            self._pending_size_label_rows.add(row_index)
            has_any = True
        if has_any and not self._size_label_refresh_timer.isActive():
            self._size_label_refresh_timer.start(0)

    def _flush_pending_row_size_labels(self):
        if not self._pending_size_label_rows:
            return
        budget = self._size_label_refresh_budget()
        batch = sorted(self._pending_size_label_rows)[:budget]
        for row_index in batch:
            self._pending_size_label_rows.discard(row_index)
            self._refresh_row_size_label(row_index)
        if self._pending_size_label_rows:
            self._size_label_refresh_timer.start(0)

    def _visible_row_range(self):
        total = len(self.playlist_items)
        if total <= 0:
            return 0, -1
        viewport = self.list_widget.viewport()
        top_index = self.list_widget.indexAt(QPoint(8, 8))
        bottom_index = self.list_widget.indexAt(QPoint(8, max(8, viewport.height() - 8)))
        first = top_index.row() if top_index.isValid() else 0
        if bottom_index.isValid():
            last = bottom_index.row()
        else:
            visible_guess = max(8, int(viewport.height() / 94) + 2)
            last = min(total - 1, first + visible_guess)
        first = max(0, first - self._visible_buffer_rows)
        last = min(total - 1, last + self._visible_buffer_rows)
        return first, last

    def _refresh_visible_rows(self):
        start, end = self._visible_row_range()
        if end < start:
            self._clear_row_widgets()
            return
        desired = set(range(start, end + 1))
        existing = set(self.playlist_rows.keys())
        for row_index in sorted(existing - desired):
            self._release_row_widget(row_index)
        missing = sorted(desired - existing)
        budget = self._visible_materialize_budget()
        for row_index in missing[:budget]:
            self._add_item_card(row_index, self.playlist_items[row_index])
        if len(missing) > budget:
            self._schedule_visible_refresh()

    def _async_load_thumbnail(self, url: str, label: QLabel):
        thumb_url = str(url or "").strip()
        if not thumb_url:
            return
        if thumb_url in self.thumbnail_cache:
            with suppress(Exception):
                label.setPixmap(self.thumbnail_cache[thumb_url])
            return
        waiters = self._thumbnail_waiters.setdefault(thumb_url, [])
        waiters.append(label)
        if thumb_url in self._thumbnail_inflight:
            return
        self._thumbnail_inflight.add(thumb_url)
        request = QNetworkRequest(QUrl(url))
        reply = self.net_manager.get(request)
        reply.finished.connect(lambda: self._on_thumbnail_loaded(reply, thumb_url))

    def _on_thumbnail_loaded(self, reply: QNetworkReply, url: str):
        labels = list(self._thumbnail_waiters.pop(url, []))
        self._thumbnail_inflight.discard(url)
        cached = None
        if reply.error() == QNetworkReply.NetworkError.NoError:
            data = reply.readAll()
            pixmap = QPixmap()
            if pixmap.loadFromData(data):
                scaled = pixmap.scaled(160, 90, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                self.thumbnail_cache[url] = scaled
                cached = scaled
        if cached is not None:
            for label in labels:
                with suppress(Exception):
                    if label is not None:
                        label.setPixmap(cached)
        reply.deleteLater()

    def _format_duration(self, seconds: int) -> str:
        if not seconds: return "--:--"
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h > 0: return f"{h}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    # ── Global Settings Logic ────────────────────────────────────────────────

    def _quality_options_for_format(self, fmt: str) -> list:
        if fmt in AUDIO_FORMATS:
            return ["320kbps", "256kbps", "192kbps", "128kbps", "96kbps"]
        return ["8K", "4K", "1440p", "1080p", "720p", "480p", "360p"]

    def _refresh_global_quality_options(self, fmt: str):
        self.global_quality.blockSignals(True)
        self.global_quality.clear()
        self.global_quality.addItems(self._quality_options_for_format(fmt))
        self.global_quality.setCurrentIndex(0)
        self.global_quality.blockSignals(False)

    def _on_global_format_changed(self, value: str):
        self._refresh_global_quality_options(value)
        selected_quality = self.global_quality.currentText()
        qualities = self._quality_options_for_format(value)
        next_quality = selected_quality if selected_quality in qualities else qualities[0]
        selected_count = 0
        selected_size = 0
        total_size = 0
        for item in self.playlist_items:
            item["_format"] = value
            item["_quality"] = next_quality
            item["_selected"] = bool(item.get("_selected", True))
            size_bytes = self._estimate_size_bytes(
                item.get("duration_seconds", 0),
                value,
                next_quality,
            )
            item["estimated_size_bytes"] = size_bytes
            total_size += int(size_bytes)
            if item["_selected"]:
                selected_count += 1
                selected_size += int(size_bytes)
        for row_index, row in list(self.playlist_rows.items()):
            row["format"].blockSignals(True)
            row["format"].setCurrentText(value)
            row["format"].blockSignals(False)
            row["quality"].blockSignals(True)
            row["quality"].clear()
            row["quality"].addItems(qualities)
            row["quality"].setCurrentText(str(self.playlist_items[row_index].get("_quality") or qualities[0]))
            row["quality"].blockSignals(False)
        self._queue_row_size_refresh_many(self.playlist_rows.keys())
        self._selected_item_count = selected_count
        self._selected_size_bytes = selected_size
        self._total_estimated_size_bytes = total_size
        self._schedule_metrics_refresh()

    def _apply_global_quality(self, value: str):
        if not value:
            return
        options_cache: dict[str, list] = {}
        selected_count = 0
        selected_size = 0
        total_size = 0
        for item in self.playlist_items:
            fmt = str(item.get("_format") or self.global_format.currentText() or "MP4").strip() or "MP4"
            item["_format"] = fmt
            options = options_cache.get(fmt)
            if options is None:
                options = self._quality_options_for_format(fmt)
                options_cache[fmt] = options
            if value in options:
                next_quality = value
            else:
                current_quality = str(item.get("_quality") or "").strip()
                next_quality = current_quality if current_quality in options else options[0]
            item["_quality"] = next_quality
            item["_selected"] = bool(item.get("_selected", True))
            size_bytes = self._estimate_size_bytes(
                item.get("duration_seconds", 0),
                fmt,
                next_quality,
            )
            item["estimated_size_bytes"] = size_bytes
            total_size += int(size_bytes)
            if item["_selected"]:
                selected_count += 1
                selected_size += int(size_bytes)
        for row_index, row in list(self.playlist_rows.items()):
            q_combo = row["quality"]
            if value in [q_combo.itemText(i) for i in range(q_combo.count())]:
                q_combo.blockSignals(True)
                q_combo.setCurrentText(value)
                q_combo.blockSignals(False)
                self._queue_row_size_refresh(row_index)
        self._selected_item_count = selected_count
        self._selected_size_bytes = selected_size
        self._total_estimated_size_bytes = total_size
        self._schedule_metrics_refresh()

    def _on_row_format_changed(self, row_index: int, value: str, quality_combo: QComboBox):
        item = self.playlist_items[row_index]
        old_size_bytes = self._item_size_bytes(item)
        item["_format"] = value
        quality_combo.blockSignals(True)
        quality_combo.clear()
        options = self._quality_options_for_format(value)
        quality_combo.addItems(options)
        next_quality = item.get("_quality") if item.get("_quality") in options else options[0]
        quality_combo.setCurrentText(next_quality)
        quality_combo.blockSignals(False)
        item["_quality"] = next_quality
        self._ensure_item_state(item)
        new_size_bytes = self._item_size_bytes(item)
        delta = new_size_bytes - old_size_bytes
        self._total_estimated_size_bytes += delta
        if bool(item.get("_selected", True)):
            self._selected_size_bytes += delta
        self._refresh_row_size_label(row_index)
        self._schedule_metrics_refresh()

    def _on_row_quality_changed(self, row_index: int, value: str):
        item = self.playlist_items[row_index]
        old_size_bytes = self._item_size_bytes(item)
        item["_quality"] = str(value or "").strip()
        self._ensure_item_state(item)
        new_size_bytes = self._item_size_bytes(item)
        delta = new_size_bytes - old_size_bytes
        self._total_estimated_size_bytes += delta
        if bool(item.get("_selected", True)):
            self._selected_size_bytes += delta
        self._refresh_row_size_label(row_index)
        self._schedule_metrics_refresh()

    def _on_row_selection_changed(self, row_index: int, checked: bool):
        item = self.playlist_items[row_index]
        was_selected = bool(item.get("_selected", True))
        now_selected = bool(checked)
        if was_selected == now_selected:
            return
        item["_selected"] = now_selected
        size_bytes = self._item_size_bytes(item)
        self._selected_item_count += 1 if now_selected else -1
        self._selected_size_bytes += size_bytes if now_selected else -size_bytes
        self._schedule_metrics_refresh()

    def _toggle_select_all(self, checked: bool):
        for item in self.playlist_items:
            item["_selected"] = bool(checked)
        for row in self.playlist_rows.values():
            row["check"].blockSignals(True)
            row["check"].setChecked(checked)
            row["check"].blockSignals(False)
        self._selected_item_count = len(self.playlist_items) if checked else 0
        self._selected_size_bytes = self._total_estimated_size_bytes if checked else 0
        self._schedule_metrics_refresh()

    def _update_download_button(self):
        total = len(self.playlist_items)
        self.download_btn.setText(f"{_('Download Selected')} ({self._selected_item_count}/{total})")
        self.download_btn.setEnabled(self._selected_item_count > 0)

    def _estimate_size_bytes(self, duration_seconds: int, fmt: str, quality: str) -> int:
        seconds = int(duration_seconds or 0)
        if seconds <= 0:
            return 0
        fmt_text = str(fmt or "").strip()
        q_text = str(quality or "").strip().lower()
        if fmt_text in AUDIO_FORMATS:
            m = re.search(r"(\d+)", q_text)
            kbps = int(m.group(1)) if m else 192
            return int((seconds * kbps * 1000) / 8)
        if "8k" in q_text:
            mbps = 32
        elif "4k" in q_text or "2160" in q_text:
            mbps = 20
        elif "1440" in q_text:
            mbps = 12
        elif "1080" in q_text:
            mbps = 8
        elif "720" in q_text:
            mbps = 5
        elif "480" in q_text:
            mbps = 2.5
        else:
            mbps = 1.5
        return int((seconds * mbps * 1_000_000) / 8)

    def _format_size(self, size_bytes: int, *, estimated: bool = True) -> str:
        return format_size_label(size_bytes, estimated=estimated, empty="--")

    def _format_item_size(self, item: dict) -> str:
        return self._format_size(
            int(item.get("estimated_size_bytes", 0) or 0),
            estimated=bool(item.get("size_is_estimate", True)),
        )

    def _refresh_row_size_label(self, row_index: int):
        if not (0 <= row_index < len(self.playlist_items)):
            return
        item = self.playlist_items[row_index]
        row = self.playlist_rows.get(row_index)
        if row and row.get("size_lbl"):
            row["size_lbl"].setText(self._format_item_size(item))

    def _update_total_size(self):
        self.total_size_lbl.setText(_("Total Size: {value}").format(value=self._format_size(self._selected_size_bytes)))

    def _on_download_clicked(self):
        selected_items = [item for item in self.playlist_items if bool(item.get("_selected", True))]
        if not selected_items:
            return
            
        tasks = []
        subtitle = self.global_subtitle.currentText()
        schedule_settings = self.schedule_picker.get_schedule_settings() if hasattr(self, "schedule_picker") else {}
        for item in selected_items:
            fmt = str(item.get("_format") or self.global_format.currentText())
            quality = str(item.get("_quality") or self.global_quality.currentText()).replace(" kbps", "kbps")
            
            task = {
                "url": item.get("url", ""),
                "title": item.get("title", "Unknown"),
                "thumbnail": item.get("thumbnail", ""),
                "duration_seconds": int(item.get("duration_seconds", 0)),
                "is_live": bool(item.get("is_live", False)),
                "was_live": bool(item.get("was_live", False)),
                "live_status": str(item.get("live_status", "") or ""),
                "format": fmt,
                "quality": quality,
                "subtitle": subtitle,
                "scheduled_at": float(schedule_settings.get("scheduled_at", 0) or 0),
                "schedule_repeat": str(schedule_settings.get("schedule_repeat", "none") or "none"),
                "estimated_size_bytes": int(item.get("estimated_size_bytes", 0) or 0),
                "size_bytes": int(item.get("estimated_size_bytes", 0) or 0),
                "size": self._format_item_size(item),
                "size_text": self._format_item_size(item),
                "size_is_estimate": bool(item.get("size_is_estimate", True)),
                "status": "pending",
                "retries": 3
            }
            tasks.append(task)
            
        self.downloadRequested.emit(tasks)

    def eventFilter(self, watched, event):
        if watched is self.list_widget.viewport():
            event_type = event.type()
            if event_type in {event.Type.Resize, event.Type.Paint, event.Type.Wheel}:
                self._schedule_visible_refresh()
        return super().eventFilter(watched, event)



