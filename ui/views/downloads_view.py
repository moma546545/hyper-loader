
import os
os.environ.setdefault("QT_API", "pyside6")
import qtawesome as qta
from PySide6.QtCore import Qt, Signal, QRect, QPoint, QModelIndex
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QFrame,
    QLineEdit,
    QPushButton,
    QLabel,
    QScrollArea,
    QComboBox,
    QSizePolicy,
)

from core.i18n import _
from ui.models import DownloadListModel
from ui.views.base_view import BaseView
from ui.views.search_view import create_status_progress_bar


class DownloadCard(QFrame):
    pause_requested = Signal(int)
    cancel_requested = Signal(int)
    folder_requested = Signal(int)

    def __init__(self, title, size, status="downloading", theme=None, parent=None, queue_index: int = -1):
        super().__init__(parent)
        self._queue_index = int(queue_index)
        self.theme = theme or {}
        self.setObjectName("single_card")
        self.setFixedHeight(120)
        self.setStyleSheet(
            """
            QFrame#single_card {
                background-color: rgba(30, 30, 35, 180);
                border-radius: 16px;
                border: 1px solid rgba(255, 255, 255, 0.05);
            }
            QFrame#single_card:hover {
                background-color: rgba(45, 45, 55, 200);
                border: 1px solid rgba(99, 102, 241, 0.4);
            }
            """
        )

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(20)

        self.thumb = QLabel()
        self.thumb.setFixedSize(140, 80)
        self.thumb.setStyleSheet("background-color: rgba(0, 0, 0, 0.3); border-radius: 10px; border: 1px solid rgba(255, 255, 255, 0.05);")
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setText("[]")
        main_layout.addWidget(self.thumb)

        info_layout = QVBoxLayout()
        info_layout.setSpacing(6)
        self.title_lbl = QLabel(title)
        self.title_lbl.setStyleSheet("color: #E5E7EB; font-weight: 700; font-size: 15px;")
        self.title_lbl.setWordWrap(True)

        self.details_row = QHBoxLayout()
        self.speed_lbl = QLabel(f"{_('Speed:')} --")
        self.speed_lbl.setStyleSheet("color: #6366F1; font-weight: bold; font-size: 12px;")
        self.size_lbl = QLabel(f"{_('Size:')} {size}")
        self.size_lbl.setStyleSheet("color: #9CA3AF; font-size: 12px;")
        self.eta_lbl = QLabel(f"{_('ETA:')} --:--")
        self.eta_lbl.setStyleSheet("color: #9CA3AF; font-size: 12px;")
        
        self.details_row.addWidget(self.speed_lbl)
        self.details_row.addSpacing(15)
        self.details_row.addWidget(self.size_lbl)
        self.details_row.addSpacing(15)
        self.details_row.addWidget(self.eta_lbl)
        self.details_row.addSpacing(15)
        
        self.engine_lbl = QLabel("")
        self.engine_lbl.setStyleSheet("""
            color: #A78BFA;
            font-size: 10px;
            font-weight: 900;
            background-color: rgba(139, 92, 246, 0.15);
            border: 1px solid rgba(139, 92, 246, 0.3);
            border-radius: 4px;
            padding: 1px 6px;
            text-transform: uppercase;
        """)
        self.engine_lbl.hide()
        self.details_row.addWidget(self.engine_lbl)
        
        self.details_row.addStretch(1)

        self.progress = create_status_progress_bar(status=status, value=0 if status != "completed" else 100)
        self.progress.setFixedHeight(16)
        
        info_layout.addWidget(self.title_lbl)
        info_layout.addStretch(1)
        info_layout.addLayout(self.details_row)
        info_layout.addWidget(self.progress)
        main_layout.addLayout(info_layout, 1)

        actions_layout = QHBoxLayout()
        actions_layout.setSpacing(8)
        self.btn_pause = self.create_action_btn("fa5s.pause", "#F59E0B")
        self.btn_folder = self.create_action_btn("fa5s.folder-open", "#6366F1")
        self.btn_cancel = self.create_action_btn("fa5s.times", "#EF4444")
        self.btn_pause.clicked.connect(lambda: self.pause_requested.emit(self._queue_index))
        self.btn_folder.clicked.connect(lambda: self.folder_requested.emit(self._queue_index))
        self.btn_cancel.clicked.connect(lambda: self.cancel_requested.emit(self._queue_index))
        actions_layout.addWidget(self.btn_pause)
        actions_layout.addWidget(self.btn_folder)
        actions_layout.addWidget(self.btn_cancel)
        main_layout.addLayout(actions_layout)

    def create_action_btn(self, icon_name, hover_color):
        btn = QPushButton("")
        btn.setIcon(qta.icon(icon_name, color="#D1D5DB"))
        btn.setFixedSize(36, 36)
        btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn.setStyleSheet(
            f"""
            QPushButton {{
                background-color: rgba(255, 255, 255, 0.05);
                border-radius: 18px;
                border: 1px solid transparent;
            }}
            QPushButton:hover {{
                background-color: {hover_color}33;
                border: 1px solid {hover_color};
            }}
            """
        )
        return btn

    def set_engine(self, engine_name: str):
        if not engine_name:
            self.engine_lbl.hide()
            return
        self.engine_lbl.setText(engine_name)
        self.engine_lbl.show()

    def update_index(self, new_index: int):
        self._queue_index = int(new_index)


class _CardsListArea(QScrollArea):
    reorder_requested = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = None
        self._row_widgets = {}
        self._row_holders = {}
        self.setWidgetResizable(True)
        self.setObjectName("downloads_cards_area")
        self.setStyleSheet(
            """
            QScrollArea#downloads_cards_area { border: none; background-color: transparent; }
            QWidget#scroll_content { background-color: transparent; }
            """
        )
        self.scroll_content = QWidget()
        self.scroll_content.setObjectName("scroll_content")
        self.cards_layout = QVBoxLayout(self.scroll_content)
        self.cards_layout.setContentsMargins(0, 0, 10, 0)
        self.cards_layout.setSpacing(12)
        self.cards_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(self.scroll_content)

    def setModel(self, model):
        self._model = model
        if model is not None:
            for sig_name in ("modelReset", "layoutChanged", "rowsRemoved"):
                sig = getattr(model, sig_name, None)
                if sig is not None:
                    try:
                        sig.connect(self.clear_rows)
                    except Exception:
                        pass

    def model(self):
        return self._model

    def clear_rows(self):
        for holder in self._row_holders.values():
            self.cards_layout.removeWidget(holder)
            holder.deleteLater()
        self._row_widgets.clear()
        self._row_holders.clear()

    def _ensure_row(self, row: int):
        if row in self._row_holders:
            return self._row_holders[row]
        holder = QFrame(self.scroll_content)
        holder.setObjectName("download_row_holder")
        holder.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        holder_layout = QVBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setSpacing(0)
        self._row_holders[row] = holder
        self.cards_layout.addWidget(holder)
        return holder

    def setIndexWidget(self, index, widget):
        row = int(index.row()) if hasattr(index, "row") else int(index)
        holder = self._ensure_row(row)
        layout = holder.layout()
        old = self._row_widgets.get(row)
        if old is not None and old is not widget:
            layout.removeWidget(old)
            old.setParent(None)
        if widget is None:
            self._row_widgets.pop(row, None)
            return
        if widget.parent() is not holder:
            widget.setParent(holder)
        if layout.indexOf(widget) < 0:
            layout.addWidget(widget)
        self._row_widgets[row] = widget
        try:
            if bool(widget.property("is_empty_state")):
                h = self._target_empty_state_height()
                widget.setMinimumHeight(h)
                widget.setMaximumHeight(h)
            else:
                h = max(84, int(widget.sizeHint().height()))
            holder.setMinimumHeight(h)
            holder.setMaximumHeight(h)
        except Exception:
            pass
        # Ensure the widget is shown and layout updated
        widget.show()
        self._sync_empty_state_height()
        self.scroll_content.adjustSize()

    def indexWidget(self, index):
        row = int(index.row()) if hasattr(index, "row") else int(index)
        return self._row_widgets.get(row)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_empty_state_height()

    def _target_empty_state_height(self) -> int:
        margins = self.cards_layout.contentsMargins()
        viewport_h = int(self.viewport().height() or 0)
        available_h = viewport_h - int(margins.top()) - int(margins.bottom())
        return max(340, available_h)

    def _sync_empty_state_height(self):
        w = self._row_widgets.get(0)
        if w is None or not bool(w.property("is_empty_state")):
            return
        h = self._target_empty_state_height()
        w.setMinimumHeight(h)
        w.setMaximumHeight(h)
        holder = self._row_holders.get(0)
        if holder is not None:
            holder.setMinimumHeight(h)
            holder.setMaximumHeight(h)
        self.scroll_content.setMinimumHeight(h)

    def sizeHintForRow(self, row: int):
        w = self._row_widgets.get(int(row))
        if w is None:
            return 110
        try:
            return max(84, int(w.sizeHint().height()))
        except Exception:
            return max(84, int(w.height() or 110))

    def indexAt(self, point):
        if self._model is None:
            return QModelIndex()
        try:
            content_y = int(point.y() + self.verticalScrollBar().value())
            content_x = int(point.x())
            for row in sorted(self._row_holders.keys()):
                holder = self._row_holders[row]
                if holder is None:
                    continue
                g = holder.geometry()
                if QRect(g.x(), g.y(), g.width(), g.height()).contains(content_x, content_y):
                    return self._model.index(int(row), 0)
        except Exception:
            return QModelIndex()
        return QModelIndex()

    def visualRect(self, index):
        row = int(index.row()) if hasattr(index, "row") else int(index)
        w = self._row_widgets.get(row)
        if w is None:
            return QRect()
        pos = w.mapTo(self.viewport(), QPoint(0, 0))
        return QRect(pos, w.size())


class DownloadsView(BaseView):
    filter_changed = Signal(str)
    search_changed = Signal(str)
    queue_state_changed = Signal(str)
    page_changed = Signal(str)
    clear_completed_requested = Signal()
    export_txt_requested = Signal()
    export_csv_requested = Signal()
    list_scrolled = Signal(int)
    list_range_changed = Signal(int, int)
    queue_reorder_requested = Signal(int, int)
    history_filters_changed = Signal()

    def __init__(self, main_window=None, parent=None):
        super().__init__(main_window, parent)
        self.downloads_dash_specs = []
        self.filter_buttons = {}
        self.filter_button_texts = {}
        self.media_filter_buttons = {}
        self._active_media_filter = "all"
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 12)
        layout.setSpacing(8)

        header_layout = QVBoxLayout()
        header_layout.setSpacing(4)
        dash_layout = QHBoxLayout()
        dash_layout.setSpacing(8)
        self.stat_active = self.create_stat_card(_("Downloading"), "0", "#8B5CF6", "fa5s.cloud-download-alt")
        self.stat_completed = self.create_stat_card(_("Completed"), "0", "#10B981", "fa5s.check-circle")
        self.stat_queued = self.create_stat_card(_("Queued"), "0", "#F59E0B", "fa5s.clock")
        self.downloads_dash_specs = [
            (self.stat_active, "accent"),
            (self.stat_completed, "gold"),
            (self.stat_queued, "warning"),
        ]
        dash_layout.addWidget(self.stat_active)
        dash_layout.addWidget(self.stat_completed)
        dash_layout.addWidget(self.stat_queued)
        dash_layout.addStretch(1)
        header_layout.addLayout(dash_layout)
        layout.addLayout(header_layout)

        filters_row = QHBoxLayout()
        filters_row.setSpacing(8)
        filters = [
            ("completed", _("Completed downloads")),
            ("active", _("Active")),
            ("queued", _("Queued")),
            ("scheduled", _("Scheduled")),
        ]
        for key, text in filters:
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setChecked(key == "completed")
            btn.setFixedHeight(32)
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.clicked.connect(lambda _=False, k=key: self._on_filter_clicked(k))
            btn.setStyleSheet(
                """
                QPushButton {
                    background-color: transparent;
                    color: #94A3B8;
                    border: 1px solid #27272A;
                    border-radius: 17px;
                    padding: 0px 20px;
                    font-weight: bold;
                }
                QPushButton:checked {
                    background-color: rgba(139, 92, 246, 0.2);
                    color: #A78BFA;
                    border: 1px solid #8B5CF6;
                }
                QPushButton:hover:!checked {
                    background-color: rgba(255, 255, 255, 0.05);
                    color: #FFFFFF;
                }
                """
            )
            self.filter_buttons[key] = btn
            self.filter_button_texts[key] = {
                "completed": "Completed downloads",
                "active": "Active",
                "queued": "Queued",
                "scheduled": "Scheduled",
            }[key]
            filters_row.addWidget(btn)
        filters_row.addStretch(1)

        self.clear_btn = QPushButton(f" {_('Clear completed')}")
        self.clear_btn.setIcon(qta.icon("fa5s.trash-alt", color="#EF4444"))
        self.clear_btn.setObjectName("action_button")
        self.clear_btn.setStyleSheet("color: #F87171; border: 1px solid rgba(239, 68, 68, 0.3);")
        self.clear_btn.clicked.connect(self.clear_completed_requested.emit)
        filters_row.addWidget(self.clear_btn)

        # Hidden compatibility controls used by app logic.
        self.queue_state_combo = QComboBox()
        self.queue_state_combo.addItems([_("All"), _("Queued"), _("Downloading"), _("Paused"), _("Failed"), _("Cancelled")])
        self.queue_state_combo.currentTextChanged.connect(self.queue_state_changed.emit)
        self.queue_state_combo.hide()
        filters_row.addWidget(self.queue_state_combo)

        self.page_combo = QComboBox()
        self.page_combo.currentTextChanged.connect(self.page_changed.emit)
        self.page_combo.hide()
        filters_row.addWidget(self.page_combo)

        self.history_mode_combo = QComboBox()
        self.history_mode_combo.addItem(_("All Types"), "all")
        self.history_mode_combo.addItem(_("Video Only"), "video")
        self.history_mode_combo.addItem(_("Audio Only"), "audio")
        self.history_mode_combo.currentIndexChanged.connect(self.history_filters_changed.emit)
        self.history_mode_combo.hide()
        filters_row.addWidget(self.history_mode_combo)

        self.history_format_combo = QComboBox()
        self.history_format_combo.addItem(_("All Formats"), "all")
        self.history_format_combo.addItem("MP4", "MP4")
        self.history_format_combo.addItem("MKV", "MKV")
        self.history_format_combo.addItem("MP3", "MP3")
        self.history_format_combo.addItem("M4A", "M4A")
        self.history_format_combo.currentIndexChanged.connect(self.history_filters_changed.emit)
        self.history_format_combo.hide()
        filters_row.addWidget(self.history_format_combo)

        self.history_date_combo = QComboBox()
        self.history_date_combo.addItem(_("All Time"), "all")
        self.history_date_combo.addItem(_("Last 24 hours"), "24h")
        self.history_date_combo.addItem(_("Last 7 days"), "7d")
        self.history_date_combo.addItem(_("Last 30 days"), "30d")
        self.history_date_combo.currentIndexChanged.connect(self.history_filters_changed.emit)
        self.history_date_combo.hide()
        filters_row.addWidget(self.history_date_combo)

        self.export_csv_btn = QPushButton(_(" Export CSV"))
        self.export_csv_btn.setObjectName("action_trim")
        self.export_csv_btn.setIcon(qta.icon("fa5s.file-csv", color="#A1A1AA"))
        self.export_csv_btn.clicked.connect(self.export_csv_requested.emit)
        filters_row.addWidget(self.export_csv_btn)

        self.export_txt_btn = QPushButton(_(" Export TXT"))
        self.export_txt_btn.setObjectName("action_trim")
        self.export_txt_btn.setIcon(qta.icon("fa5s.file-alt", color="#A1A1AA"))
        self.export_txt_btn.clicked.connect(self.export_txt_requested.emit)
        filters_row.addWidget(self.export_txt_btn)
        layout.addLayout(filters_row)

        media_filters_row = QHBoxLayout()
        media_filters_row.setSpacing(8)
        self.media_filter_label = QLabel(_("Type:"))
        self.media_filter_label.setStyleSheet("color: #9CA3AF; font-size: 12px; font-weight: bold;")
        media_filters_row.addWidget(self.media_filter_label)
        media_filters = [
            ("all", "All"),
            ("video", "Video"),
            ("audio", "Audio"),
        ]
        for key, text_key in media_filters:
            btn = QPushButton(_(text_key))
            btn.setCheckable(True)
            btn.setChecked(key == self._active_media_filter)
            btn.setFixedHeight(28)
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            btn.clicked.connect(lambda _=False, k=key: self._on_media_filter_clicked(k))
            btn.setStyleSheet(
                """
                QPushButton {
                    background-color: rgba(255, 255, 255, 0.04);
                    color: #A1A1AA;
                    border: 1px solid rgba(255, 255, 255, 0.08);
                    border-radius: 14px;
                    padding: 0px 14px;
                    font-size: 12px;
                    font-weight: bold;
                }
                QPushButton:checked {
                    background-color: rgba(99, 102, 241, 0.22);
                    color: #FFFFFF;
                    border: 1px solid rgba(99, 102, 241, 0.45);
                }
                """
            )
            self.media_filter_buttons[key] = btn
            media_filters_row.addWidget(btn)
        media_filters_row.addStretch(1)
        layout.addLayout(media_filters_row)

        self.search_input = QLineEdit()
        self.search_input.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self.search_input.setPlaceholderText(_("Search downloads..."))
        self.search_input.setMinimumWidth(260)
        self.search_input.setMaximumWidth(380)
        self.search_input.setFixedHeight(38)
        self.search_input.addAction(qta.icon("fa5s.search", color="#A1A1AA"), QLineEdit.ActionPosition.LeadingPosition)
        self.search_input.setStyleSheet("""
            QLineEdit {
                background-color: rgba(30, 30, 35, 200);
                border: 1px solid rgba(99, 102, 241, 0.22);
                border-radius: 19px;
                padding: 0 15px;
                color: #FFFFFF;
            }
            QLineEdit:focus {
                border: 1px solid rgba(99, 102, 241, 0.55);
                background-color: rgba(45, 45, 55, 210);
            }
        """)
        self.search_input.textChanged.connect(self.search_changed.emit)
        self.downloads_search_input = self.search_input
        search_row = QHBoxLayout()
        search_row.setSpacing(0)
        search_row.addStretch(1)
        search_row.addWidget(self.search_input)
        layout.addLayout(search_row)

        self.downloads_list = _CardsListArea()
        self.downloads_model = DownloadListModel([])
        self.downloads_list.setModel(self.downloads_model)
        scroll_bar = self.downloads_list.verticalScrollBar()
        scroll_bar.valueChanged.connect(self.list_scrolled.emit)
        scroll_bar.rangeChanged.connect(self.list_range_changed.emit)
        self.downloads_list.reorder_requested.connect(self.queue_reorder_requested.emit)
        layout.addWidget(self.downloads_list, 1)

    def create_stat_card(self, title, value, color, icon_name):
        card = QFrame()
        card.setFixedSize(164, 74)
        card.setStyleSheet(
            f"""
            QFrame {{
                background-color: rgba(30, 30, 35, 180);
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.05);
                border-left: 4px solid {color};
            }}
            """
        )
        
        # Add drop shadow to stat cards
        from PySide6.QtWidgets import QGraphicsDropShadowEffect
        from PySide6.QtGui import QColor
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(15)
        shadow.setColor(QColor(0, 0, 0, 60))
        shadow.setOffset(0, 4)
        card.setGraphicsEffect(shadow)

        vbox = QVBoxLayout(card)
        vbox.setContentsMargins(12, 9, 12, 9)
        top_row = QHBoxLayout()
        lbl_title = QLabel(title)
        lbl_title.setStyleSheet("color: #9CA3AF; font-size: 12px; font-weight: bold; background: transparent; border: none;")
        icon = QLabel()
        icon.setPixmap(qta.icon(icon_name, color=color).pixmap(16, 16))
        icon.setStyleSheet("background: transparent; border: none;")
        top_row.addWidget(lbl_title)
        top_row.addStretch()
        top_row.addWidget(icon)
        lbl_val = QLabel(value)
        lbl_val.setProperty("stat_value_label", True)
        lbl_val.setStyleSheet(f"color: {color}; font-size: 22px; font-weight: 900; background: transparent; border: none;")
        vbox.addLayout(top_row)
        vbox.addWidget(lbl_val)
        card._title_label = lbl_title
        card._title_key = str(title or "")
        return card

    def _on_filter_clicked(self, key):
        self.filter_changed.emit(key)

    def _on_media_filter_clicked(self, key: str):
        self._active_media_filter = str(key or "all").strip().lower() or "all"
        self.set_active_media_filter(self._active_media_filter)
        self.history_filters_changed.emit()

    def set_active_filter(self, key: str):
        target = str(key or "")
        for name, btn in (self.filter_buttons or {}).items():
            if btn is not None:
                btn.setChecked(str(name) == target)

    def set_active_media_filter(self, key: str):
        target = str(key or "all").strip().lower() or "all"
        self._active_media_filter = target
        for name, btn in (self.media_filter_buttons or {}).items():
            if btn is not None:
                btn.setChecked(str(name) == target)
        if hasattr(self, "history_mode_combo"):
            idx = self.history_mode_combo.findData(target)
            if idx >= 0:
                self.history_mode_combo.blockSignals(True)
                self.history_mode_combo.setCurrentIndex(idx)
                self.history_mode_combo.blockSignals(False)

    def set_media_filter_counts(self, all_count: int, video_count: int, audio_count: int):
        counts = {
            "all": int(all_count or 0),
            "video": int(video_count or 0),
            "audio": int(audio_count or 0),
        }
        for key, btn in (self.media_filter_buttons or {}).items():
            count = counts.get(key, 0)
            label_key = "All" if key == "all" else "Video" if key == "video" else "Audio"
            btn.setText(f"{_(label_key)} ({count})")

    def get_search_query(self) -> str:
        return str(self.downloads_search_input.text() if hasattr(self, "downloads_search_input") else "").strip()

    def get_history_filters(self) -> dict:
        mode = str(getattr(self, "_active_media_filter", "all") or "all")
        fmt = str(self.history_format_combo.currentData() or "all") if hasattr(self, "history_format_combo") else "all"
        date = str(self.history_date_combo.currentData() or "all") if hasattr(self, "history_date_combo") else "all"
        return {"mode": mode, "format": fmt, "date": date}

    def set_queue_state_visible(self, visible: bool):
        if hasattr(self, "queue_state_combo"):
            self.queue_state_combo.setVisible(bool(visible))

    def set_page_options(self, total_pages: int, current_page: int):
        if not hasattr(self, "page_combo"):
            return
        pages = max(1, int(total_pages or 1))
        current = max(1, min(int(current_page or 1), pages))
        if pages <= 1:
            self.page_combo.hide()
            return
        self.page_combo.blockSignals(True)
        self.page_combo.clear()
        for p in range(1, pages + 1):
            self.page_combo.addItem(str(p))
        self.page_combo.setCurrentText(str(current))
        self.page_combo.blockSignals(False)
        self.page_combo.setVisible(True)

    def _set_stat_value(self, card: QFrame, value: int):
        labels = card.findChildren(QLabel)
        if len(labels) >= 2:
            labels[-1].setText(str(int(value or 0)))

    def set_dashboard_counts(self, active: int, queued: int, completed: int, failed: int):
        if hasattr(self, "stat_active"):
            self._set_stat_value(self.stat_active, int(active or 0))
        if hasattr(self, "stat_completed"):
            self._set_stat_value(self.stat_completed, int(completed or 0))
        if hasattr(self, "stat_queued"):
            self._set_stat_value(self.stat_queued, int(queued or 0))

    def retranslate_ui(self):
        for key, btn in self.filter_buttons.items():
            source_text = str(self.filter_button_texts.get(key, key))
            btn.setText(_(source_text))
        if hasattr(self, "clear_btn"):
            self.clear_btn.setText(f" {_('Clear completed')}")
        if hasattr(self, "export_csv_btn"):
            self.export_csv_btn.setText(_(" Export CSV"))
        if hasattr(self, "export_txt_btn"):
            self.export_txt_btn.setText(_(" Export TXT"))
        if hasattr(self, "search_input"):
            self.search_input.setPlaceholderText(_("Search downloads..."))
        if hasattr(self, "media_filter_label"):
            self.media_filter_label.setText(_("Type:"))
        if hasattr(self, "queue_state_combo"):
            current = self.queue_state_combo.currentIndex()
            self.queue_state_combo.blockSignals(True)
            self.queue_state_combo.clear()
            self.queue_state_combo.addItems([_("All"), _("Queued"), _("Downloading"), _("Paused"), _("Failed"), _("Cancelled")])
            self.queue_state_combo.setCurrentIndex(max(0, current))
            self.queue_state_combo.blockSignals(False)
        if hasattr(self, "history_mode_combo"):
            current = self.history_mode_combo.currentData()
            self.history_mode_combo.blockSignals(True)
            self.history_mode_combo.clear()
            self.history_mode_combo.addItem(_("All Types"), "all")
            self.history_mode_combo.addItem(_("Video Only"), "video")
            self.history_mode_combo.addItem(_("Audio Only"), "audio")
            idx = self.history_mode_combo.findData(current)
            self.history_mode_combo.setCurrentIndex(max(0, idx))
            self.history_mode_combo.blockSignals(False)
        current_media_filter = getattr(self, "_active_media_filter", "all")
        current_media_counts = {
            key: btn.text() for key, btn in self.media_filter_buttons.items()
        }
        for key, btn in self.media_filter_buttons.items():
            label_key = "All" if key == "all" else "Video" if key == "video" else "Audio"
            suffix = ""
            current_text = str(current_media_counts.get(key, "") or "")
            if "(" in current_text and current_text.endswith(")"):
                suffix = current_text[current_text.rfind("("):]
            btn.setText(f"{_(label_key)} {suffix}".strip())
        self.set_active_media_filter(current_media_filter)
        if hasattr(self, "history_format_combo"):
            current = self.history_format_combo.currentData()
            self.history_format_combo.blockSignals(True)
            self.history_format_combo.clear()
            self.history_format_combo.addItem(_("All Formats"), "all")
            self.history_format_combo.addItem("MP4", "MP4")
            self.history_format_combo.addItem("MKV", "MKV")
            self.history_format_combo.addItem("MP3", "MP3")
            self.history_format_combo.addItem("M4A", "M4A")
            idx = self.history_format_combo.findData(current)
            self.history_format_combo.setCurrentIndex(max(0, idx))
            self.history_format_combo.blockSignals(False)
        if hasattr(self, "history_date_combo"):
            current = self.history_date_combo.currentData()
            self.history_date_combo.blockSignals(True)
            self.history_date_combo.clear()
            self.history_date_combo.addItem(_("All Time"), "all")
            self.history_date_combo.addItem(_("Last 24 hours"), "24h")
            self.history_date_combo.addItem(_("Last 7 days"), "7d")
            self.history_date_combo.addItem(_("Last 30 days"), "30d")
            idx = self.history_date_combo.findData(current)
            self.history_date_combo.setCurrentIndex(max(0, idx))
            self.history_date_combo.blockSignals(False)
        for card in (getattr(self, "stat_active", None), getattr(self, "stat_completed", None), getattr(self, "stat_queued", None)):
            if card is None:
                continue
            title_label = getattr(card, "_title_label", None)
            title_key = getattr(card, "_title_key", "")
            if title_label is not None and title_key:
                title_label.setText(_(title_key))

    def apply_theme(self, theme_data: dict, get_theme_func):
        t = get_theme_func(theme_data["theme"])
        self.setStyleSheet(
            f"""
            QFrame#download_row_holder {{
                background: transparent;
                border: none;
            }}
            """
        )
        for btn in self.filter_buttons.values():
            btn.setStyleSheet(
                f"""
                QPushButton {{
                    background-color: transparent;
                    color: {t['muted']};
                    border: 1px solid {t['border']};
                    border-radius: 17px;
                    padding: 0px 20px;
                    font-weight: bold;
                }}
                QPushButton:checked {{
                    background-color: rgba(139, 92, 246, 0.2);
                    color: {t['accent_2']};
                    border: 1px solid {t['accent_2']};
                }}
                QPushButton:hover:!checked {{
                    background-color: {t['panel']};
                    color: {t['text']};
                }}
                """
            )
