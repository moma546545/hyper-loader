
import os
from PySide6.QtCore import Qt, Signal, QRect, QPoint, QUrl
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QPixmap, QDesktopServices
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QWidget, QStackedWidget, QLineEdit, QCheckBox, QGridLayout, QComboBox, QScrollArea
)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    HAS_MEDIA_PREVIEW = True
except Exception:
    QAudioOutput = None
    QMediaPlayer = None
    QVideoWidget = None
    HAS_MEDIA_PREVIEW = False

class RangeSlider(QWidget):
    rangeChanged = Signal(int, int)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(60)
        self.setMouseTracking(True)
        self._min = 0
        self._max = 100
        self._low = 0
        self._high = 100
        self._handle_radius = 12
        self._active_handle = None

    def setRange(self, minimum, maximum):
        self._min = minimum
        self._max = max(minimum + 1, maximum)
        self._low = minimum
        self._high = self._max
        self.update()

    def setValues(self, low, high):
        low = int(low)
        high = int(high)
        if self._max <= self._min:
            self._low = self._min
            self._high = self._max
            self.update()
            return
        low = max(self._min, min(low, self._max - 1))
        high = max(self._min + 1, min(high, self._max))
        if low >= high:
            low = max(self._min, min(low, self._max - 1))
            high = min(self._max, low + 1)
        self._low = low
        self._high = high
        self.update()

    def low(self): return self._low
    def high(self): return self._high

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        rect = self.rect()
        h = rect.height()
        w = rect.width()
        
        track_y = h // 2 - 6
        track_h = 12
        
        # Draw background track
        painter.setPen(Qt.PenStyle.NoPen)
        from ui.themes import get_theme
        t = get_theme(None) # Get current theme
        painter.setBrush(QColor(t['panel_soft']))
        painter.drawRoundedRect(self._handle_radius, track_y, w - 2*self._handle_radius, track_h, 6, 6)
        
        # Draw active track
        range_w = w - 2*self._handle_radius
        if self._max > self._min:
            x1 = self._handle_radius + int((self._low - self._min) / (self._max - self._min) * range_w)
            x2 = self._handle_radius + int((self._high - self._min) / (self._max - self._min) * range_w)
        else:
            x1, x2 = self._handle_radius, w - self._handle_radius
            
        painter.setBrush(QColor(t['accent']))
        painter.drawRoundedRect(x1, track_y, x2 - x1, track_h, 6, 6)
        
        # Draw tooltip above slider
        tooltip_w = 120
        tooltip_h = 24
        tooltip_x = x1 + (x2 - x1) // 2 - tooltip_w // 2
        tooltip_y = track_y - tooltip_h - 10
        
        # Draw tooltip bubble
        painter.setBrush(QColor(t['panel']))
        painter.drawRoundedRect(tooltip_x, tooltip_y, tooltip_w, tooltip_h, 2, 2)
        # Draw triangle pointing down
        polygon = [
            QPoint(tooltip_x + tooltip_w // 2 - 5, tooltip_y + tooltip_h),
            QPoint(tooltip_x + tooltip_w // 2 + 5, tooltip_y + tooltip_h),
            QPoint(tooltip_x + tooltip_w // 2, tooltip_y + tooltip_h + 6)
        ]
        painter.drawPolygon(polygon)
        
        # Tooltip text
        painter.setPen(QColor(t['text']))
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)
        
        def fmt(secs):
            m, s = divmod(int(secs), 60)
            h, m = divmod(m, 60)
            return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
            
        painter.drawText(tooltip_x, tooltip_y, tooltip_w, tooltip_h, Qt.AlignmentFlag.AlignCenter, f"{fmt(self._low)} to {fmt(self._high)}")

        # Draw handles
        painter.setPen(Qt.PenStyle.NoPen)
        # Create a radial gradient for 3D effect on handles
        for x in (x1, x2):
            painter.setBrush(QColor("#FFFFFF"))
            painter.drawEllipse(QPoint(x, track_y + track_h//2), self._handle_radius, self._handle_radius)
            
            painter.setBrush(QColor(0, 0, 0, 40))
            painter.drawEllipse(QPoint(x, track_y + track_h//2 + 1), self._handle_radius-1, self._handle_radius-1)
            painter.setBrush(QColor("#E0E0E0"))
            painter.drawEllipse(QPoint(x, track_y + track_h//2), self._handle_radius-2, self._handle_radius-2)

    def mousePressEvent(self, event):
        pos = event.pos()
        w = self.width()
        range_w = w - 2*self._handle_radius
        if self._max <= self._min: return
        x1 = self._handle_radius + int((self._low - self._min) / (self._max - self._min) * range_w)
        x2 = self._handle_radius + int((self._high - self._min) / (self._max - self._min) * range_w)
        
        if abs(pos.x() - x1) < self._handle_radius * 2:
            self._active_handle = 'low'
        elif abs(pos.x() - x2) < self._handle_radius * 2:
            self._active_handle = 'high'
        else:
            self._active_handle = None

    def mouseMoveEvent(self, event):
        if not self._active_handle:
            return
            
        w = self.width()
        range_w = w - 2*self._handle_radius
        x = max(self._handle_radius, min(event.pos().x(), w - self._handle_radius))
        val = self._min + (x - self._handle_radius) / range_w * (self._max - self._min)
        
        if self._active_handle == 'low':
            self._low = min(int(val), self._high - 1)
        else:
            self._high = max(int(val), self._low + 1)
            
        self.update()
        self.rangeChanged.emit(self._low, self._high)

    def mouseReleaseEvent(self, event):
        self._active_handle = None


class ToggleSwitch(QWidget):
    toggled = Signal(bool)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(44, 24)
        self._checked = False

    def isChecked(self):
        return self._checked

    def setChecked(self, checked):
        self._checked = checked
        self.update()
        self.toggled.emit(self._checked)

    def mouseReleaseEvent(self, event):
        self.setChecked(not self._checked)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        from ui.themes import get_theme
        t = get_theme(None)

        if self._checked:
            bg_color = QColor(t['success'])
            handle_x = self.width() - 22
        else:
            bg_color = QColor(t['muted'])
            handle_x = 2
            
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg_color)
        painter.drawRoundedRect(0, 0, self.width(), self.height(), 12, 12)
        
        painter.setBrush(QColor(t['text']))
        painter.drawEllipse(handle_x, 2, 20, 20)


class CustomTitleBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_dialog = parent
        self.setFixedHeight(30)
        self.setObjectName("title_bar")
        
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(5, 0, 0, 0)
        self.layout.setSpacing(0)
        
        self.title = QLabel("Trim Video")
        self.title.setObjectName("title_label")
        self.layout.addWidget(self.title)
        
        self.layout.addStretch(1)
        
        self.btn_minimize = self._create_button("—")
        self.btn_maximize = self._create_button("⬜")
        self.btn_close = self._create_button("✕")
        self.btn_close.setObjectName("title_btn_close")
        
        self.layout.addWidget(self.btn_minimize)
        self.layout.addWidget(self.btn_maximize)
        self.layout.addWidget(self.btn_close)
        
        self.btn_minimize.clicked.connect(self.parent_dialog.showMinimized)
        self.btn_maximize.clicked.connect(self._toggle_maximize_restore)
        self.btn_close.clicked.connect(self.parent_dialog.close)

    def _create_button(self, text):
        btn = QPushButton(text)
        btn.setFixedSize(30, 30)
        btn.setObjectName("title_btn")
        return btn
        
    def _toggle_maximize_restore(self):
        if self.parent_dialog.isMaximized():
            self.parent_dialog.showNormal()
            self.btn_maximize.setText("⬜")
        else:
            self.parent_dialog.showMaximized()
            self.btn_maximize.setText("🗗")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_position = event.globalPos() - self.parent_dialog.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton:
            self.parent_dialog.move(event.globalPos() - self.drag_position)
            event.accept()

class TrimDialog(QDialog):
    def __init__(self, task_data, parent=None):
        super().__init__(parent)
        self.task_data = task_data
        self.video_url = self._extract_video_url()
        self.thumb_url = self.task_data.get("thumbnail") or ""
        self._pending_reply = None
        self.net_manager = getattr(parent, "net_manager", None) or QNetworkAccessManager(self)
        self.duration = int(task_data.get("duration_seconds", 0))
        if self.duration == 0:
            self.duration = 60 * 60 # Fallback 1 hour
            
        self.setWindowTitle("Trim Video")
        self.setFixedSize(700, 480)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setObjectName("trim_dialog")
        
        # Use the global theme if possible, or modern dark colors
        from ui.themes import get_style_sheet
        self.setStyleSheet(get_style_sheet(getattr(parent, "theme", "Modern Dark")))
        self._build_ui()
        self._init_preview()
        
    def _extract_video_url(self) -> str:
        for key in ("webpage_url", "url", "original_url", "source_url", "video_url"):
            val = self.task_data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""

    def _open_video_url(self):
        if not self.video_url:
            return
        QDesktopServices.openUrl(QUrl(self.video_url))

    def _init_preview(self):
        if hasattr(self, "youtube_btn"):
            self.youtube_btn.setVisible(bool(self.video_url))
        if self.thumb_url:
            self._abort_pending_reply()
            request = QNetworkRequest(QUrl(self.thumb_url))
            self._pending_reply = self.net_manager.get(request)
            self._pending_reply.finished.connect(self._on_thumb_loaded_safe)
        else:
            self.thumb_label.setText("No Thumbnail")

    def _on_thumb_loaded(self, reply):
        data = reply.readAll()
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            scaled = pixmap.scaled(
                self.thumb_label.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.thumb_label.setPixmap(scaled)
            self.thumb_label.setText("")
        else:
            self.thumb_label.setText("No Thumbnail")
        reply.deleteLater()

    def _on_thumb_loaded_safe(self):
        reply = self._pending_reply
        self._pending_reply = None
        if reply is None:
            return
        try:
            self._on_thumb_loaded(reply)
        except RuntimeError:
            pass

    def _abort_pending_reply(self):
        reply = self._pending_reply
        self._pending_reply = None
        if reply is None:
            return
        try:
            reply.abort()
            reply.deleteLater()
        except RuntimeError:
            pass

    def closeEvent(self, event):
        self._abort_pending_reply()
        super().closeEvent(event)

    def _fmt(self, secs):
        m, s = divmod(int(secs), 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
        
    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        self.title_bar = CustomTitleBar(self)
        main_layout.addWidget(self.title_bar)
        
        content_layout = QHBoxLayout()
        content_layout.setContentsMargins(15, 15, 15, 15)
        content_layout.setSpacing(20)
        
        # ── Left Column (Video & Toggle) ──
        left_col = QVBoxLayout()
        left_col.setSpacing(15)
        
        video_card = QFrame()
        video_card.setFixedSize(300, 200)
        video_card.setObjectName("video_preview_card")
        vg = QGridLayout(video_card)
        vg.setContentsMargins(0, 0, 0, 0)
        vg.setSpacing(0)

        self.thumb_label = QLabel()
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_label.setFixedSize(300, 200)
        self.thumb_label.setObjectName("thumb_preview")
        self.thumb_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.thumb_label.mousePressEvent = lambda _e: self._open_video_url()
        vg.addWidget(self.thumb_label, 0, 0)

        self.youtube_btn = QPushButton("▶")
        self.youtube_btn.setFixedSize(28, 28)
        self.youtube_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.youtube_btn.setObjectName("youtube_play_btn")
        self.youtube_btn.clicked.connect(self._open_video_url)

        btn_wrap = QWidget()
        btn_wrap.setObjectName("transparent_widget")
        bw = QHBoxLayout(btn_wrap)
        bw.setContentsMargins(0, 0, 8, 8)
        bw.addStretch(1)
        bw.addWidget(self.youtube_btn, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        vg.addWidget(btn_wrap, 0, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        
        # Time Indicator under video
        time_lbl = QLabel(f"00:00 / {self._fmt(self.duration)}")
        time_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        time_lbl.setObjectName("time_indicator")
        
        left_col.addWidget(video_card)
        left_col.addWidget(time_lbl)
        
        # Toggle switch
        toggle_row = QHBoxLayout()
        toggle_lbl = QLabel("Enter start & end time manually")
        toggle_lbl.setObjectName("section_title")
        self.toggle_switch = ToggleSwitch()
        self.toggle_switch.toggled.connect(self._on_toggle)
        toggle_row.addWidget(toggle_lbl)
        toggle_row.addWidget(self.toggle_switch)
        
        left_col.addLayout(toggle_row)
        left_col.addStretch(1)
        
        # ── Right Column (Trim Controls) ──
        right_col = QVBoxLayout()
        right_col.setSpacing(15)
        
        header = QLabel("Trim #1")
        header.setObjectName("section_title")
        right_col.addWidget(header)
        
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setObjectName("h_separator")
        right_col.addWidget(line)
        
        # Stacked Widget for Slider vs Manual
        self.stack = QStackedWidget()
        self.stack.setFixedHeight(120)
        
        # Page 0: Slider
        slider_page = QWidget()
        sl_layout = QVBoxLayout(slider_page)
        sl_layout.setContentsMargins(10, 20, 10, 0)
        
        self.slider = RangeSlider()
        self.slider.setRange(0, self.duration)
        self.slider.rangeChanged.connect(self._on_slider_changed)
        sl_layout.addWidget(self.slider)
        
        sl_labels = QHBoxLayout()
        self.lbl_start = QLabel("00:00")
        self.lbl_start.setObjectName("single_sub")
        self.lbl_end = QLabel(self._fmt(self.duration))
        self.lbl_end.setObjectName("single_sub")
        sl_labels.addWidget(self.lbl_start)
        sl_labels.addStretch(1)
        sl_labels.addWidget(self.lbl_end)
        sl_layout.addLayout(sl_labels)
        
        self.stack.addWidget(slider_page)
        
        # Page 1: Manual Input
        manual_page = QWidget()
        man_layout = QVBoxLayout(manual_page)
        man_layout.setContentsMargins(0, 10, 0, 0)
        
        man_row = QHBoxLayout()
        man_row.setSpacing(20)
        
        start_col = QVBoxLayout()
        start_lbl = QLabel("Start:")
        start_lbl.setObjectName("single_sub")
        self.input_start = QLineEdit("00:00:00")
        self.input_start.setObjectName("trim_input")
        self.input_start.setFixedWidth(120)
        start_col.addWidget(start_lbl)
        start_col.addWidget(self.input_start)
        
        end_col = QVBoxLayout()
        end_lbl = QLabel("End:")
        end_lbl.setObjectName("single_sub")
        self.input_end = QLineEdit(self._fmt(self.duration))
        self.input_end.setObjectName("trim_input")
        self.input_end.setFixedWidth(120)
        end_col.addWidget(end_lbl)
        end_col.addWidget(self.input_end)
        
        man_row.addLayout(start_col)
        man_row.addLayout(end_col)
        man_row.addStretch(1)
        
        man_layout.addLayout(man_row)
        man_layout.addStretch(1)
        self.stack.addWidget(manual_page)
        
        right_col.addWidget(self.stack)
        
        # Title / Filename
        right_col.addSpacing(10)
        title_lbl = QLabel("Title / Filename:")
        title_lbl.setObjectName("single_sub")
        right_col.addWidget(title_lbl)
        
        self.input_title = QLineEdit(f"{self.task_data.get('title', 'Video')} (Trim)")
        self.input_title.setObjectName("trim_input")
        right_col.addWidget(self.input_title)
        
        right_col.addStretch(1)
        
        content_layout.addLayout(left_col, 2)
        content_layout.addLayout(right_col, 3)
        
        main_layout.addLayout(content_layout)
        
        # ── Bottom Buttons ──
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        
        btn_cancel = QPushButton("Cancel/Reset")
        btn_cancel.setFixedHeight(40)
        btn_cancel.setObjectName("trim_btn_danger")
        btn_cancel.clicked.connect(self.reject)
        
        btn_add = QPushButton("+ Add new trim")
        btn_add.setFixedHeight(40)
        btn_add.setObjectName("trim_btn_accent")
        
        btn_save = QPushButton("Save")
        btn_save.setFixedHeight(40)
        btn_save.setObjectName("trim_btn_success")
        btn_save.clicked.connect(self.accept)
        
        btn_layout.addWidget(btn_cancel, 1)
        btn_layout.addWidget(btn_add, 1)
        btn_layout.addWidget(btn_save, 1)
        
        main_layout.addLayout(btn_layout)

    def _on_toggle(self, checked):
        self.stack.setCurrentIndex(1 if checked else 0)

    def _on_slider_changed(self, low, high):
        self.lbl_start.setText(self._fmt(low))
        self.lbl_end.setText(self._fmt(high))
        self.input_start.setText(self._fmt(low))
        self.input_end.setText(self._fmt(high))

    def get_trim_data(self):
        return {
            "start": self.input_start.text(),
            "end": self.input_end.text(),
            "title": self.input_title.text()
        }


class TrimBlock(QWidget):
    deleteRequested = Signal(int)

    def __init__(self, index: int, duration: int, title: str, parent=None):
        super().__init__(parent)
        self.index = int(index)
        self.duration = int(duration)
        self._build_ui()
        self.set_duration(self.duration)
        self.set_title(title)
        self.set_manual_mode(False)
        self.set_range_seconds(0, self.duration)
        self._sync_header()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(10)
        self.header_lbl = QLabel("")
        self.header_lbl.setObjectName("section_title")
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.setFixedWidth(70)
        self.delete_btn.setObjectName("trim_btn_danger")
        self.delete_btn.clicked.connect(lambda: self.deleteRequested.emit(self.index))
        head.addWidget(self.header_lbl)
        head.addStretch(1)
        head.addWidget(self.delete_btn)
        outer.addLayout(head)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setObjectName("h_separator")
        outer.addWidget(line)

        self.stack = QStackedWidget()
        self.stack.setFixedHeight(120)

        slider_page = QWidget()
        sl = QVBoxLayout(slider_page)
        sl.setContentsMargins(10, 20, 10, 0)
        self.slider = RangeSlider()
        self.slider.rangeChanged.connect(self._on_slider_changed)
        sl.addWidget(self.slider)
        sl_labels = QHBoxLayout()
        self.lbl_start = QLabel("00:00")
        self.lbl_start.setObjectName("single_sub")
        self.lbl_end = QLabel("00:00")
        self.lbl_end.setObjectName("single_sub")
        sl_labels.addWidget(self.lbl_start)
        sl_labels.addStretch(1)
        sl_labels.addWidget(self.lbl_end)
        sl.addLayout(sl_labels)
        self.stack.addWidget(slider_page)

        manual_page = QWidget()
        ml = QVBoxLayout(manual_page)
        ml.setContentsMargins(0, 10, 0, 0)
        row = QHBoxLayout()
        row.setSpacing(20)
        sc = QVBoxLayout()
        slbl = QLabel("Start:")
        slbl.setObjectName("single_sub")
        self.input_start = QLineEdit("00:00:00")
        self.input_start.setObjectName("trim_input")
        self.input_start.setFixedWidth(120)
        sc.addWidget(slbl)
        sc.addWidget(self.input_start)

        ec = QVBoxLayout()
        elbl = QLabel("End:")
        elbl.setObjectName("single_sub")
        self.input_end = QLineEdit("00:00:00")
        self.input_end.setObjectName("trim_input")
        self.input_end.setFixedWidth(120)
        ec.addWidget(elbl)
        ec.addWidget(self.input_end)

        row.addLayout(sc)
        row.addLayout(ec)
        row.addStretch(1)
        ml.addLayout(row)
        ml.addStretch(1)
        self.stack.addWidget(manual_page)

        outer.addWidget(self.stack)

        title_lbl = QLabel("Title / Filename:")
        title_lbl.setObjectName("single_sub")
        outer.addWidget(title_lbl)
        self.input_title = QLineEdit("")
        self.input_title.setObjectName("trim_input")
        outer.addWidget(self.input_title)

    def set_index(self, index: int):
        self.index = int(index)
        self._sync_header()

    def set_duration(self, duration: int):
        self.duration = max(1, int(duration))
        self.slider.setRange(0, self.duration)

    def set_title(self, title: str):
        self.input_title.setText(str(title or ""))

    def set_manual_mode(self, manual: bool):
        self.stack.setCurrentIndex(1 if manual else 0)

    def show_delete(self, visible: bool):
        self.delete_btn.setVisible(bool(visible))

    def _sync_header(self):
        self.header_lbl.setText(f"Trim #{self.index + 1}")

    def _fmt(self, secs: int) -> str:
        m, s = divmod(int(secs), 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

    def _parse_time(self, value: str) -> int:
        text = str(value or "").strip()
        if not text:
            return 0
        parts = [p for p in text.split(":") if p != ""]
        try:
            nums = [int(p) for p in parts]
        except Exception:
            return 0
        if len(nums) == 2:
            mm, ss = nums
            return max(0, mm * 60 + ss)
        if len(nums) == 3:
            hh, mm, ss = nums
            return max(0, hh * 3600 + mm * 60 + ss)
        return 0

    def set_range_seconds(self, start_s: int, end_s: int):
        start_s = max(0, min(int(start_s), self.duration - 1))
        end_s = max(start_s + 1, min(int(end_s), self.duration))
        self.slider.setValues(start_s, end_s)
        self.lbl_start.setText(self._fmt(start_s))
        self.lbl_end.setText(self._fmt(end_s))
        self.input_start.setText(self._fmt(start_s))
        self.input_end.setText(self._fmt(end_s))

    def get_trim_seconds(self, manual_mode: bool) -> dict:
        if manual_mode:
            start_s = self._parse_time(self.input_start.text())
            end_s = self._parse_time(self.input_end.text())
        else:
            start_s = int(self.slider.low())
            end_s = int(self.slider.high())
        start_s = max(0, min(int(start_s), self.duration - 1))
        end_s = max(start_s + 1, min(int(end_s), self.duration))
        title = str(self.input_title.text() or "").strip()
        return {"start": start_s, "end": end_s, "title": title}

    def _on_slider_changed(self, low, high):
        self.lbl_start.setText(self._fmt(low))
        self.lbl_end.setText(self._fmt(high))
        self.input_start.setText(self._fmt(low))
        self.input_end.setText(self._fmt(high))


class TrimView(QWidget):
    saved = Signal(dict)
    backRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.task_data = {}
        self.video_url = ""
        self.stream_url = ""
        self.thumb_url = ""
        self._pending_reply = None
        self.net_manager = QNetworkAccessManager(self)
        self.duration = 0
        self.manual_mode = False
        self.media_player = None
        self.audio_output = None
        self._player_duration_ms = 0
        self._last_scrub_seconds = 0
        self._build_ui()
        self._init_media_player()

    def set_task(self, task_data: dict, net_manager=None):
        self.task_data = task_data or {}
        self.video_url = self._extract_video_url()
        self.stream_url = self._extract_stream_url()
        self.thumb_url = self.task_data.get("thumbnail") or ""
        self.duration = int(self.task_data.get("duration_seconds", 0) or 0)
        if self.duration <= 0:
            self.duration = 60 * 60
        if net_manager is not None:
            self.net_manager = net_manager

        self.manual_mode = False
        self.toggle_switch.setChecked(False)
        self._reset_trims()
        self._init_preview()

    def _extract_video_url(self) -> str:
        for key in ("webpage_url", "url", "original_url", "source_url", "video_url"):
            val = self.task_data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""

    def _extract_stream_url(self) -> str:
        for key in ("preview_stream_url", "stream_url", "source_url", "video_url"):
            val = self.task_data.get(key)
            if isinstance(val, str) and self._is_direct_media_url(val.strip()):
                return val.strip()
        return ""

    def _is_direct_media_url(self, value: str) -> bool:
        url = str(value or "").strip().lower()
        if not url.startswith(("http://", "https://")):
            return False
        blocked = ("youtube.com/watch", "youtu.be/", "youtube.com/shorts")
        if any(x in url for x in blocked):
            return False
        if any(x in url for x in (".m3u8", ".mp4", ".webm", ".mkv", ".mov", ".avi", ".mpd", ".m4a", ".mp3", ".aac", ".wav", ".ogg", ".flac")):
            return True
        return "mime=video" in url or "mime=audio" in url

    def _open_video_url(self):
        if not self.video_url:
            return
        QDesktopServices.openUrl(QUrl(self.video_url))

    def _fmt(self, secs):
        m, s = divmod(int(secs), 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

    def _clear_trims_ui(self):
        while self.trims_layout.count() > 0:
            item = self.trims_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _trim_blocks(self):
        blocks = []
        for i in range(self.trims_layout.count()):
            w = self.trims_layout.itemAt(i).widget()
            if isinstance(w, TrimBlock):
                blocks.append(w)
        return blocks

    def _reset_trims(self):
        self._clear_trims_ui()
        base_title = str(self.task_data.get("title", "Video")).strip() or "Video"
        block = TrimBlock(0, self.duration, f"{base_title} (Trim #1)", self)
        block.deleteRequested.connect(self._delete_trim)
        block.slider.rangeChanged.connect(lambda low, _high: self._seek_preview(low))
        self.trims_layout.addWidget(block)
        self._sync_delete_buttons()

    def _add_new_trim(self):
        blocks = self._trim_blocks()
        base_title = str(self.task_data.get("title", "Video")).strip() or "Video"
        idx = len(blocks)
        block = TrimBlock(idx, self.duration, f"{base_title} (Trim #{idx+1})", self)
        block.set_manual_mode(self.manual_mode)
        block.deleteRequested.connect(self._delete_trim)
        block.slider.rangeChanged.connect(lambda low, _high: self._seek_preview(low))
        self.trims_layout.addWidget(block)
        self._sync_delete_buttons()

    def _delete_trim(self, index: int):
        blocks = self._trim_blocks()
        if len(blocks) <= 1:
            return
        for b in blocks:
            if b.index == index:
                b.setParent(None)
                b.deleteLater()
                break
        blocks = self._trim_blocks()
        for i, b in enumerate(blocks):
            b.set_index(i)
            b.set_duration(self.duration)
            b.set_manual_mode(self.manual_mode)
        self._sync_delete_buttons()

    def _sync_delete_buttons(self):
        blocks = self._trim_blocks()
        for b in blocks:
            b.show_delete(len(blocks) > 1 and b.index > 0)

    def _on_toggle_manual(self, checked: bool):
        self.manual_mode = bool(checked)
        for b in self._trim_blocks():
            b.set_manual_mode(self.manual_mode)

    def _collect_trims(self):
        blocks = self._trim_blocks()
        result = []
        for b in blocks:
            b.set_duration(self.duration)
            result.append(b.get_trim_seconds(self.manual_mode))
        return result

    def _init_media_player(self):
        if not HAS_MEDIA_PREVIEW or not hasattr(self, "video_widget"):
            return
        self.audio_output = QAudioOutput(self)
        self.media_player = QMediaPlayer(self)
        self.media_player.setAudioOutput(self.audio_output)
        self.media_player.setVideoOutput(self.video_widget)
        self.media_player.positionChanged.connect(self._on_player_position_changed)
        self.media_player.durationChanged.connect(self._on_player_duration_changed)

    def _load_preview_source(self):
        self.video_stack.setCurrentIndex(1)
        self.thumb_label.setPixmap(QPixmap())
        self.thumb_label.setText("Preview unavailable")
        if self.media_player is None or not self.stream_url:
            return
        self.video_stack.setCurrentIndex(0)
        self.media_player.stop()
        self.media_player.setSource(QUrl(self.stream_url))
        self.media_player.pause()

    def _on_player_position_changed(self, position_ms: int):
        total = max(1, int(self._player_duration_ms or self.duration * 1000 or 1))
        self.preview_time_lbl.setText(f"{self._fmt(position_ms // 1000)} / {self._fmt(total // 1000)}")

    def _on_player_duration_changed(self, duration_ms: int):
        self._player_duration_ms = max(0, int(duration_ms or 0))
        if self._player_duration_ms > 0:
            self.preview_time_lbl.setText(f"00:00 / {self._fmt(self._player_duration_ms // 1000)}")

    def _seek_preview(self, seconds: int):
        self._last_scrub_seconds = max(0, int(seconds or 0))
        if self.media_player is not None and self.stream_url:
            self.media_player.setPosition(self._last_scrub_seconds * 1000)

    def _save(self):
        trims = self._collect_trims()
        first = trims[0] if trims else {"start": 0, "end": self.duration, "title": ""}
        payload = {
            "start": self._fmt(first.get("start", 0)),
            "end": self._fmt(first.get("end", self.duration)),
            "title": str(first.get("title", "") or "").strip(),
            "trims": trims,
        }
        self.saved.emit(payload)

    def _init_preview(self):
        self.youtube_btn.setVisible(bool(self.video_url))
        self.preview_time_lbl.setText(f"00:00 / {self._fmt(self.duration)}")
        self.preview_play_btn.setText("▶")
        self.preview_play_btn.setEnabled(bool(self.stream_url or self.video_url))
        self._load_preview_source()
        if not self.stream_url and self.thumb_url:
            self._abort_pending_reply()
            request = QNetworkRequest(QUrl(self.thumb_url))
            self._pending_reply = self.net_manager.get(request)
            self._pending_reply.finished.connect(self._on_thumb_loaded_safe)
        elif not self.stream_url:
            self.thumb_label.setText("Preview unavailable")
        self._seek_preview(0)

    def _on_thumb_loaded(self, reply):
        data = reply.readAll()
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            self.video_stack.setCurrentIndex(1)
            scaled = pixmap.scaled(
                self.thumb_label.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.thumb_label.setPixmap(scaled)
            self.thumb_label.setText("")
        else:
            self.thumb_label.setText("Preview disabled inside the app")
        reply.deleteLater()

    def _on_thumb_loaded_safe(self):
        reply = self._pending_reply
        self._pending_reply = None
        if reply is None:
            return
        try:
            self._on_thumb_loaded(reply)
        except RuntimeError:
            pass

    def _abort_pending_reply(self):
        reply = self._pending_reply
        self._pending_reply = None
        if reply is None:
            return
        try:
            reply.abort()
            reply.deleteLater()
        except RuntimeError:
            pass

    def _toggle_preview_play(self):
        if self.media_player is None or not self.stream_url:
            self._open_video_url()
            return
        if self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.media_player.pause()
            self.preview_play_btn.setText("▶")
        else:
            self.media_player.play()
            self.preview_play_btn.setText("❚❚")

    def closeEvent(self, event):
        self._abort_pending_reply()
        if self.media_player is not None:
            self.media_player.stop()
        super().closeEvent(event)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(15, 15, 15, 15)
        root.setSpacing(12)

        top_row = QHBoxLayout()
        self.back_btn = QPushButton("← رجوع")
        self.back_btn.setFixedHeight(34)
        self.back_btn.setFixedWidth(90)
        self.back_btn.setObjectName("action_button")
        self.back_btn.clicked.connect(self.backRequested.emit)
        top_row.addWidget(self.back_btn, 0, Qt.AlignmentFlag.AlignLeft)
        top_row.addStretch(1)
        root.addLayout(top_row)

        content = QHBoxLayout()
        content.setSpacing(20)

        left_col = QVBoxLayout()
        left_col.setSpacing(12)

        self.video_stack = QStackedWidget()
        self.video_stack.setFixedSize(320, 200)

        video_frame = QFrame()
        video_frame.setFixedSize(320, 200)
        video_frame.setObjectName("video_preview_card")
        tg = QGridLayout(video_frame)
        tg.setContentsMargins(0, 0, 0, 0)
        tg.setSpacing(0)

        if HAS_MEDIA_PREVIEW:
            self.video_widget = QVideoWidget(video_frame)
            self.video_widget.setMinimumSize(320, 200)
            tg.addWidget(self.video_widget, 0, 0)
        else:
            self.video_widget = QLabel("Preview unavailable")
            self.video_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            tg.addWidget(self.video_widget, 0, 0)

        self.video_stack.addWidget(video_frame)

        thumb_frame = QFrame()
        thumb_frame.setFixedSize(320, 200)
        thumb_frame.setObjectName("video_preview_card")
        thumb_layout = QGridLayout(thumb_frame)
        thumb_layout.setContentsMargins(0, 0, 0, 0)
        thumb_layout.setSpacing(0)

        self.thumb_label = QLabel("Preview unavailable")
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_label.setFixedSize(320, 200)
        self.thumb_label.setObjectName("thumb_preview")
        self.thumb_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.thumb_label.mousePressEvent = lambda _e: self._open_video_url()
        thumb_layout.addWidget(self.thumb_label, 0, 0)

        self.youtube_btn = QPushButton("▶")
        self.youtube_btn.setFixedSize(28, 28)
        self.youtube_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.youtube_btn.setObjectName("youtube_play_btn")
        self.youtube_btn.clicked.connect(self._open_video_url)

        btn_wrap = QWidget()
        btn_wrap.setObjectName("transparent_widget")
        bw = QHBoxLayout(btn_wrap)
        bw.setContentsMargins(0, 0, 8, 8)
        bw.addStretch(1)
        bw.addWidget(self.youtube_btn, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        thumb_layout.addWidget(btn_wrap, 0, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        self.video_stack.addWidget(thumb_frame)

        left_col.addWidget(self.video_stack)
        
        preview_controls = QHBoxLayout()
        self.preview_play_btn = QPushButton("▶")
        self.preview_play_btn.setFixedSize(40, 40)
        self.preview_play_btn.setObjectName("trim_btn_accent")
        self.preview_play_btn.clicked.connect(self._toggle_preview_play)
        
        self.preview_time_lbl = QLabel("00:00 / 00:00")
        self.preview_time_lbl.setObjectName("time_indicator")
        
        preview_controls.addWidget(self.preview_play_btn)
        preview_controls.addWidget(self.preview_time_lbl)
        preview_controls.addStretch(1)
        left_col.addLayout(preview_controls)
        
        toggle_row = QHBoxLayout()
        toggle_lbl = QLabel("Enter start & end time manually")
        toggle_lbl.setObjectName("section_title")
        self.toggle_switch = ToggleSwitch()
        self.toggle_switch.toggled.connect(self._on_toggle_manual)
        toggle_row.addWidget(toggle_lbl)
        toggle_row.addWidget(self.toggle_switch)
        left_col.addLayout(toggle_row)
        left_col.addStretch(1)
        
        # ── Right Column (Trims List) ──
        right_col = QVBoxLayout()
        right_col.setSpacing(10)
        
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setObjectName("transparent_widget")
        
        self.trims_container = QWidget()
        self.trims_layout = QVBoxLayout(self.trims_container)
        self.trims_layout.setContentsMargins(0, 0, 10, 0)
        self.trims_layout.setSpacing(20)
        self.trims_layout.addStretch(1)
        
        scroll.setWidget(self.trims_container)
        right_col.addWidget(scroll)
        
        # ── Bottom Action Buttons ──
        actions = QHBoxLayout()
        actions.setSpacing(10)
        
        btn_cancel = QPushButton("Cancel")
        btn_cancel.setFixedHeight(40)
        btn_cancel.setObjectName("trim_btn_danger")
        btn_cancel.clicked.connect(self.backRequested.emit)
        
        btn_add = QPushButton("+ Add new trim")
        btn_add.setFixedHeight(40)
        btn_add.setObjectName("trim_btn_accent")
        btn_add.clicked.connect(self._add_new_trim)
        
        btn_save = QPushButton("Save All Trims")
        btn_save.setFixedHeight(40)
        btn_save.setObjectName("trim_btn_success")
        btn_save.clicked.connect(self._save)
        
        actions.addWidget(btn_cancel, 1)
        actions.addWidget(btn_add, 1)
        actions.addWidget(btn_save, 1)
        
        right_col.addLayout(actions)
        
        content.addLayout(left_col, 2)
        content.addLayout(right_col, 3)
        root.addLayout(content)



