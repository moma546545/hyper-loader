
"""
ui/subscriptions_view.py — Smart Channel Subscriptions UI Panel
Premium panel for managing YouTube channel subscriptions.
Pluggable into the main QStackedWidget as a new "📺 Subscriptions" tab.
"""
from datetime import datetime

try:
    from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QSize, Qt, QTimer, Signal, QEvent
    from PySide6.QtGui import QColor, QPainter, QPen
    from PySide6.QtWidgets import (
        QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel,
        QPushButton, QLineEdit, QListView, QComboBox, QSpinBox,
        QStyledItemDelegate, QAbstractItemView, QToolTip,
    )
except ImportError:
    from PyQt6.QtCore import QAbstractListModel, QModelIndex, QRect, QSize, Qt, pyqtSignal as Signal, QTimer, QEvent
    from PyQt6.QtGui import QColor, QPainter, QPen
    from PyQt6.QtWidgets import (
        QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel,
        QPushButton, QLineEdit, QListView, QComboBox, QSpinBox,
        QStyledItemDelegate, QAbstractItemView, QToolTip,
    )

from core.channel_subscriptions import subscription_manager, ChannelSubscription
from core.i18n import _
from core.error_handler import ErrorHandler
from core.utils import redact_url
from ui.themes import get_theme

SUBSCRIPTION_ROLE = Qt.ItemDataRole.UserRole
EMPTY_STATE_ROLE = Qt.ItemDataRole.UserRole + 1


class SubscriptionListModel(QAbstractListModel):
    EMPTY_ROW_HEIGHT = 88
    ITEM_ROW_HEIGHT = 74

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: list[ChannelSubscription] = []

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self._items) if self._items else 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if not self._items:
            if role == EMPTY_STATE_ROLE:
                return True
            if role == Qt.ItemDataRole.DisplayRole:
                return _("No subscriptions yet. Paste a channel URL above, then press Subscribe to create your first auto-download rule.")
            if role == Qt.ItemDataRole.SizeHintRole:
                return QSize(0, self.EMPTY_ROW_HEIGHT)
            return None

        item = self._items[index.row()]
        if role == SUBSCRIPTION_ROLE:
            return item
        if role == Qt.ItemDataRole.DisplayRole:
            return item.name
        if role == Qt.ItemDataRole.SizeHintRole:
            return QSize(0, self.ITEM_ROW_HEIGHT)
        return None

    def set_subscriptions(self, subscriptions: list[ChannelSubscription]):
        self.beginResetModel()
        self._items = list(subscriptions or [])
        self.endResetModel()


class SubscriptionItemDelegate(QStyledItemDelegate):
    checkRequested = Signal(object)
    removeRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._theme_name = "Modern Dark"
        self._padding = 8
        self._radius = 12
        self._button_size = QSize(32, 32)
        self._button_gap = 8
        self._hovered_row = -1
        self._hovered_button = None

    def set_theme_name(self, theme_name: str | None):
        self._theme_name = str(theme_name or "").strip() or "Modern Dark"

    def set_hover_state(self, row: int = -1, button: str | None = None):
        next_row = int(row)
        next_button = str(button) if button else None
        if next_row == self._hovered_row and next_button == self._hovered_button:
            return
        self._hovered_row = next_row
        self._hovered_button = next_button
        parent = self.parent()
        if parent is not None and hasattr(parent, "viewport"):
            parent.viewport().update()

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        theme = get_theme(self._theme_name)

        if bool(index.data(EMPTY_STATE_ROLE)):
            self._paint_empty_state(painter, option, str(index.data(Qt.ItemDataRole.DisplayRole) or ""), theme)
            painter.restore()
            return

        sub = index.data(SUBSCRIPTION_ROLE)
        if sub is None:
            painter.restore()
            return

        card_rect = option.rect.adjusted(2, 1, -2, -1)
        painter.setPen(Qt.PenStyle.NoPen)
        card_bg = QColor(theme["panel"])
        card_bg.setAlpha(180 if index.row() == self._hovered_row else 145)
        painter.setBrush(card_bg)
        painter.drawRoundedRect(card_rect, self._radius, self._radius)

        border_color = QColor(theme["accent"] if index.row() == self._hovered_row else theme["border"])
        border_color.setAlpha(120 if index.row() == self._hovered_row else max(24, border_color.alpha()))
        border_pen = QPen(border_color)
        border_pen.setWidth(1)
        painter.setPen(border_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(card_rect, self._radius, self._radius)

        title_rect, meta_rect, check_rect, remove_rect = self._layout_parts(option.rect, option.direction)
        title_pen = QColor(theme["text"])
        muted_pen = QColor(theme["muted"])

        painter.setPen(title_pen)
        title_font = painter.font()
        title_font.setBold(True)
        title_font.setPointSize(max(10, title_font.pointSize()))
        painter.setFont(title_font)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignVCenter, str(sub.name or ""))

        painter.setPen(muted_pen)
        meta_font = painter.font()
        meta_font.setBold(False)
        meta_font.setPointSize(max(9, meta_font.pointSize() - 1))
        painter.setFont(meta_font)
        painter.drawText(
            meta_rect,
            Qt.AlignmentFlag.AlignLeading | Qt.AlignmentFlag.AlignVCenter,
            self._meta_text(sub),
        )

        self._paint_icon_button(
            painter,
            check_rect,
            label="↻",
            bg=self._tinted(theme["success"], 48 if self._is_hovered_button(index.row(), "check") else 28),
            border=self._tinted(theme["success"], 155 if self._is_hovered_button(index.row(), "check") else 90),
            fg=QColor(theme["success"]),
        )
        self._paint_icon_button(
            painter,
            remove_rect,
            label="✕",
            bg=self._tinted(theme["danger"], 52 if self._is_hovered_button(index.row(), "remove") else 28),
            border=self._tinted(theme["danger"], 155 if self._is_hovered_button(index.row(), "remove") else 90),
            fg=QColor(theme["danger"]),
        )
        painter.restore()

    def editorEvent(self, event, model, option, index):
        if bool(index.data(EMPTY_STATE_ROLE)):
            return False
        event_type = getattr(event, "type", lambda: None)()
        if event_type == QEvent.Type.MouseMove:
            pos_fn = getattr(event, "position", None)
            hover_pos = pos_fn().toPoint() if callable(pos_fn) else event.pos()
            self.set_hover_state(index.row(), self.button_at(option.rect, hover_pos, option.direction))
            return False
        if event_type not in {QEvent.Type.MouseButtonRelease, QEvent.Type.MouseButtonDblClick}:
            return False
        pos_fn = getattr(event, "position", None)
        click_pos = pos_fn().toPoint() if callable(pos_fn) else event.pos()
        sub = index.data(SUBSCRIPTION_ROLE)
        if sub is None:
            return False
        button = self.button_at(option.rect, click_pos, option.direction)
        if button == "check":
            self.checkRequested.emit(sub)
            return True
        if button == "remove":
            self.removeRequested.emit(str(sub.url or ""))
            return True
        return False

    def helpEvent(self, event, view, option, index):
        if bool(index.data(EMPTY_STATE_ROLE)):
            QToolTip.showText(event.globalPos(), _("Paste a channel or playlist URL above to add your first subscription."), view)
            return True
        button = self.button_at(option.rect, event.pos(), option.direction)
        tip = self.tooltip_for_button(button)
        if tip:
            QToolTip.showText(event.globalPos(), tip, view)
            return True
        QToolTip.hideText()
        return False

    def button_at(self, outer_rect: QRect, pos, direction: Qt.LayoutDirection = Qt.LayoutDirection.LeftToRight) -> str | None:
        _, _, check_rect, remove_rect = self._layout_parts(outer_rect, direction)
        if check_rect.contains(pos):
            return "check"
        if remove_rect.contains(pos):
            return "remove"
        return None

    @staticmethod
    def tooltip_for_button(button: str | None) -> str:
        if button == "check":
            return _("Check this subscription now")
        if button == "remove":
            return _("Remove this subscription")
        return ""

    def _paint_empty_state(self, painter, option, text: str, theme: dict):
        rect = option.rect.adjusted(4, 2, -4, -2)
        painter.setPen(Qt.PenStyle.NoPen)
        empty_bg = QColor(theme["panel_soft"])
        empty_bg.setAlpha(110)
        painter.setBrush(empty_bg)
        painter.drawRoundedRect(rect, self._radius, self._radius)
        accent = QColor(theme["accent"])
        accent.setAlpha(140)
        accent_pen = QPen(accent)
        accent_pen.setWidth(2)
        painter.setPen(accent_pen)
        painter.drawLine(rect.left() + 14, rect.center().y(), rect.left() + 46, rect.center().y())
        painter.drawLine(rect.left() + 30, rect.center().y() - 16, rect.left() + 30, rect.center().y() + 16)

        text_rect = rect.adjusted(62, 10, -14, -10)
        title_rect = QRect(text_rect.left(), text_rect.top(), text_rect.width(), 24)
        subtitle_rect = QRect(text_rect.left(), title_rect.bottom() + 4, text_rect.width(), text_rect.height() - 28)

        title_font = painter.font()
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(theme["text"]))
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, _("Start your first subscription"))

        subtitle_font = painter.font()
        subtitle_font.setBold(False)
        painter.setFont(subtitle_font)
        painter.setPen(QColor(theme["muted"]))
        painter.drawText(subtitle_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap, text)

    def _paint_icon_button(self, painter, rect: QRect, *, label: str, bg, border, fg):
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, 8, 8)
        border_pen = QPen(border)
        border_pen.setWidth(1)
        painter.setPen(border_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, 8, 8)
        painter.setPen(fg)
        btn_font = painter.font()
        btn_font.setBold(True)
        painter.setFont(btn_font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

    def _layout_parts(self, outer_rect: QRect, direction: Qt.LayoutDirection = Qt.LayoutDirection.LeftToRight):
        is_rtl = direction == Qt.LayoutDirection.RightToLeft
        card_rect = outer_rect.adjusted(self._padding, self._padding // 2, -self._padding, -self._padding // 2)
        
        if is_rtl:
            remove_rect = QRect(
                card_rect.left() + 12,
                card_rect.center().y() - self._button_size.height() // 2,
                self._button_size.width(),
                self._button_size.height(),
            )
            check_rect = QRect(
                remove_rect.right() + self._button_gap,
                remove_rect.top(),
                self._button_size.width(),
                self._button_size.height(),
            )
            text_left = check_rect.right() + 12
            title_rect = QRect(text_left, card_rect.top() + 8, max(40, card_rect.right() - 12 - text_left), 22)
            meta_rect = QRect(text_left, title_rect.bottom() + 4, max(40, card_rect.right() - 12 - text_left), 20)
        else:
            remove_rect = QRect(
                card_rect.right() - self._button_size.width() - 12,
                card_rect.center().y() - self._button_size.height() // 2,
                self._button_size.width(),
                self._button_size.height(),
            )
            check_rect = QRect(
                remove_rect.left() - self._button_gap - self._button_size.width(),
                remove_rect.top(),
                self._button_size.width(),
                self._button_size.height(),
            )
            text_right = check_rect.left() - 12
            title_rect = QRect(card_rect.left() + 12, card_rect.top() + 8, max(40, text_right - (card_rect.left() + 12)), 22)
            meta_rect = QRect(card_rect.left() + 12, title_rect.bottom() + 4, max(40, text_right - (card_rect.left() + 12)), 20)
            
        return title_rect, meta_rect, check_rect, remove_rect

    def _is_hovered_button(self, row: int, button: str) -> bool:
        return self._hovered_row == row and self._hovered_button == button

    @staticmethod
    def _tinted(color_value: str, alpha: int) -> QColor:
        color = QColor(color_value)
        color.setAlpha(max(0, min(255, int(alpha))))
        return color

    @staticmethod
    def _meta_text(sub: ChannelSubscription) -> str:
        last_check_str = (
            datetime.fromtimestamp(sub.last_check).strftime("%Y-%m-%d %H:%M")
            if float(sub.last_check or 0) > 0 else _("Never")
        )
        return _("{fmt} • {quality} • Every {interval}h • Last: {last} • {seen} seen").format(
            fmt=str(sub.format or "").upper(),
            quality=sub.quality,
            interval=sub.check_interval_h,
            last=last_check_str,
            seen=len(sub.known_ids),
        )



class SubscriptionsView(QWidget):
    """
    Full subscriptions management panel.
    Signals:
        newVideosReady(list[str]) — emitted when new video URLs are found
    """
    newVideosReady = Signal(list, dict)
    _refresh_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.theme_name = getattr(parent, "theme", "Modern Dark")
        self._build_ui()
        self._refresh_requested.connect(self._refresh_list)
        self._refresh_list()

        # Poll UI every 60s to show updated last-check times
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_list)
        self._refresh_timer.start(60_000)

        # Set callback for background detections
        subscription_manager.set_callback(self._on_new_videos_found)
        subscription_manager.start()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        # Header
        hdr = QHBoxLayout()
        self.title_label = QLabel(_("Smart Channel Subscriptions"))
        self.title_label.setObjectName("single_title")
        self.subtitle_label = QLabel(_("Auto-downloads new videos from subscribed channels"))
        self.subtitle_label.setObjectName("single_sub")
        hdr.addWidget(self.title_label)
        hdr.addStretch(1)
        layout.addLayout(hdr)
        layout.addWidget(self.subtitle_label)

        # ── Add Subscription Form ─────────────────────────────────────────────
        form = QFrame()
        form.setObjectName("playlist_header")
        form_layout = QVBoxLayout(form)
        form_layout.setContentsMargins(12, 10, 12, 10)
        form_layout.setSpacing(8)

        row1 = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText(_("YouTube Channel / Playlist URL …"))
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText(_("Friendly name (optional)"))
        row1.addWidget(self.url_input, 3)
        row1.addWidget(self.name_input, 2)

        row2 = QHBoxLayout()
        self.fmt_combo = QComboBox()
        self.fmt_combo.addItems(["MP4", "MKV", "MP3", "M4A", "WEBM"])
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(["1080p", "720p", "480p", "360p"])
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 168)
        self.interval_spin.setValue(24)
        self.interval_spin.setPrefix(_("Check every "))
        self.interval_spin.setSuffix(_(" h"))
        self.max_spin = QSpinBox()
        self.max_spin.setRange(1, 50)
        self.max_spin.setValue(5)
        self.max_spin.setPrefix(_("Max "))
        self.max_spin.setSuffix(_(" per check"))
        self.add_btn = QPushButton(_("＋ Subscribe"))
        self.add_btn.setObjectName("action_download")
        self.add_btn.setFixedWidth(120)
        self.add_btn.clicked.connect(self._add_subscription)
        self.format_label = QLabel(_("Format:"))
        row2.addWidget(self.format_label)
        row2.addWidget(self.fmt_combo)
        self.quality_label = QLabel(_("Quality:"))
        row2.addWidget(self.quality_label)
        row2.addWidget(self.quality_combo)
        row2.addWidget(self.interval_spin)
        row2.addWidget(self.max_spin)
        row2.addStretch(1)
        row2.addWidget(self.add_btn)

        form_layout.addLayout(row1)
        form_layout.addLayout(row2)
        layout.addWidget(form)

        # ── Subscription List ──────────────────────────────────────────────────
        list_hdr = QHBoxLayout()
        self.active_subscriptions_label = QLabel(_("Active Subscriptions"), objectName="section_title")
        list_hdr.addWidget(self.active_subscriptions_label)
        list_hdr.addStretch(1)
        self.check_all_btn = QPushButton(_("🔄 Check All Now"))
        self.check_all_btn.setObjectName("action_trim")
        self.check_all_btn.clicked.connect(self._check_all_now)
        list_hdr.addWidget(self.check_all_btn)
        layout.addLayout(list_hdr)

        self.subs_model = SubscriptionListModel(self)
        self.subs_delegate = SubscriptionItemDelegate(self)
        self.subs_delegate.set_theme_name(self.theme_name)
        self.subs_delegate.checkRequested.connect(self._check_subscription)
        self.subs_delegate.removeRequested.connect(self._remove_subscription)

        self.subs_list = SubscriptionListView(self.subs_delegate, self)
        self.subs_list.setObjectName("subscriptions_list")
        self.subs_list.setModel(self.subs_model)
        self.subs_list.setItemDelegate(self.subs_delegate)
        self.subs_list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.subs_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.subs_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.subs_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.subs_list.setSpacing(4)
        self.subs_list.setMouseTracking(True)
        self.subs_list.setStyleSheet(
            """
            QListView#subscriptions_list {
                background: transparent;
                border: none;
                outline: none;
            }
            QListView#subscriptions_list::item {
                border: none;
            }
            """
        )
        layout.addWidget(self.subs_list, 1)

    # ── Logic ─────────────────────────────────────────────────────────────────

    def _add_subscription(self):
        url = self.url_input.text().strip()
        if not url:
            return
        name = self.name_input.text().strip() or url[:40]
        subscription_manager.add(
            url=url,
            name=name,
            fmt=self.fmt_combo.currentText().lower(),
            quality=self.quality_combo.currentText(),
            check_interval_h=self.interval_spin.value(),
            max_downloads=self.max_spin.value(),
        )
        self.url_input.clear()
        self.name_input.clear()
        self._refresh_list()

    def _check_all_now(self):
        for sub in subscription_manager.get_all():
            subscription_manager.check_now(sub)

    def _check_subscription(self, sub: ChannelSubscription):
        if sub is None:
            return
        subscription_manager.check_now(sub)

    def _remove_subscription(self, url: str):
        if not ErrorHandler.confirm(
            self,
            _("Remove Subscription"),
            _("Remove subscription?\n{url}").format(url=redact_url(url)),
        ):
            return
        subscription_manager.remove(url)
        self._refresh_list()

    def _refresh_list(self):
        subs = subscription_manager.get_all()
        self.subs_model.set_subscriptions(subs)
        self.subs_list.sync_current_index()

    def update_theme(self, theme_name: str):
        self.theme_name = str(theme_name or "").strip() or self.theme_name
        self.subs_delegate.set_theme_name(self.theme_name)
        self.subs_list.viewport().update()

    def retranslate_ui(self):
        self.title_label.setText(_("Smart Channel Subscriptions"))
        self.subtitle_label.setText(_("Auto-downloads new videos from subscribed channels"))
        self.url_input.setPlaceholderText(_("YouTube Channel / Playlist URL …"))
        self.name_input.setPlaceholderText(_("Friendly name (optional)"))
        self.interval_spin.setPrefix(_("Check every "))
        self.interval_spin.setSuffix(_(" h"))
        self.max_spin.setPrefix(_("Max "))
        self.max_spin.setSuffix(_(" per check"))
        self.add_btn.setText(_("＋ Subscribe"))
        self.format_label.setText(_("Format:"))
        self.quality_label.setText(_("Quality:"))
        self.active_subscriptions_label.setText(_("Active Subscriptions"))
        self.check_all_btn.setText(_("🔄 Check All Now"))
        self.subs_list.viewport().update()

    def focus_add_form(self):
        self.url_input.setFocus()
        self.url_input.selectAll()

    def _on_new_videos_found(self, sub: ChannelSubscription, new_urls: list):
        """Called from background thread — emit signal so Qt handles it safely."""
        self.newVideosReady.emit(new_urls, sub.to_dict())
        self._refresh_requested.emit()


class SubscriptionListView(QListView):
    def __init__(self, delegate: SubscriptionItemDelegate, parent=None):
        super().__init__(parent)
        self._delegate = delegate

    def mouseMoveEvent(self, event):
        index = self.indexAt(event.pos())
        if index.isValid():
            button = self._delegate.button_at(self.visualRect(index), event.pos())
            self._delegate.set_hover_state(index.row(), button)
            self.setCursor(Qt.CursorShape.PointingHandCursor if button else Qt.CursorShape.ArrowCursor)
            self.setToolTip(self._delegate.tooltip_for_button(button))
        else:
            self._delegate.set_hover_state(-1, None)
            self.unsetCursor()
            self.setToolTip("")
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._delegate.set_hover_state(-1, None)
        self.unsetCursor()
        self.setToolTip("")
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        index = self.currentIndex()
        model = self.model()
        is_empty = (not index.isValid()) or bool(model.data(index, EMPTY_STATE_ROLE))
        key = event.key()

        if key in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            if is_empty:
                parent = self.parent()
                if parent is not None and hasattr(parent, "focus_add_form"):
                    parent.focus_add_form()
            else:
                sub = model.data(index, SUBSCRIPTION_ROLE)
                if sub is not None:
                    self._delegate.checkRequested.emit(sub)
            event.accept()
            return

        if key in {Qt.Key.Key_Delete, Qt.Key.Key_Backspace} and not is_empty:
            sub = model.data(index, SUBSCRIPTION_ROLE)
            if sub is not None:
                self._delegate.removeRequested.emit(str(sub.url or ""))
            event.accept()
            return

        super().keyPressEvent(event)

    def sync_current_index(self):
        model = self.model()
        if model is None or model.rowCount() <= 0:
            return
        current = self.currentIndex()
        if current.isValid() and current.row() < model.rowCount():
            return
        self.setCurrentIndex(model.index(0, 0))
