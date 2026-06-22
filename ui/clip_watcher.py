
# ui/clip_watcher.py
import re
try:
    from PySide6.QtCore import Qt, QTimer, Signal, QPoint, QPropertyAnimation, QEasingCurve
    from PySide6.QtGui import QColor, QPainter, QBrush, QPen
    from PySide6.QtWidgets import (
        QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, 
        QApplication, QGraphicsDropShadowEffect, QFrame
    )
except ImportError:
    from PyQt6.QtCore import Qt, QTimer, pyqtSignal as Signal, QPoint, QPropertyAnimation, QEasingCurve
    from PyQt6.QtGui import QColor, QPainter, QBrush, QPen
    from PyQt6.QtWidgets import (
        QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, 
        QApplication, QGraphicsDropShadowEffect, QFrame
    )
from ui.themes import get_theme

_URL_PATTERN = re.compile(
    r'^https?://'
    r'(?:[a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,}'
    r'(?:/[^\s]*)?$'
)

class ClipWatcher(QWidget):
    downloadRequested = Signal(str)
    analyzeRequested = Signal(str)

    def __init__(self, theme_name="Modern Dark"):
        super().__init__()
        self.theme_name = theme_name
        self.current_url = ""
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        
        self._init_ui()
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.hide_animated)
        
        self.check_timer = QTimer(self)
        self.check_timer.timeout.connect(self._check_clipboard)
        self.last_clipboard = ""

    def _init_ui(self):
        self.main_frame = QFrame(self)
        self.main_frame.setObjectName("snap_bar")
        
        layout = QHBoxLayout(self.main_frame)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(10)
        
        icon = QLabel("✨")
        txt = QLabel("Link Detected!")
        
        self.btn_dl = QPushButton("Download Now")
        self.btn_dl.clicked.connect(self._on_download)
        
        self.btn_close = QPushButton("✕")
        self.btn_close.setObjectName("secondary")
        self.btn_close.setFixedWidth(24)
        self.btn_close.clicked.connect(self.hide_animated)
        
        layout.addWidget(icon)
        layout.addWidget(txt)
        layout.addStretch(1)
        layout.addWidget(self.btn_dl)
        layout.addWidget(self.btn_close)
        
        self.setFixedSize(300, 54)
        
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(15)
        shadow.setColor(QColor(0, 0, 0, 150))
        shadow.setOffset(0, 2)
        self.main_frame.setGraphicsEffect(shadow)
        self._apply_theme()

    def _apply_theme(self):
        t = get_theme(self.theme_name)
        self.main_frame.setStyleSheet(f"""
            QFrame#snap_bar {{
                background-color: {t['panel_alt']}f2;
                border: 1px solid {t['border']};
                border-radius: 10px;
            }}
            QLabel {{
                color: {t['text']};
                font-weight: 700;
                font-size: 11px;
                background: transparent;
            }}
            QPushButton {{
                background-color: {t['accent']};
                color: {t['bg']};
                border: none;
                border-radius: 6px;
                padding: 6px 10px;
                font-weight: 700;
                font-size: 10px;
            }}
            QPushButton:hover {{
                background-color: {t['accent_2']};
            }}
            QPushButton#secondary {{
                background-color: {t['panel']};
                color: {t['text']};
                border: 1px solid {t['border']};
            }}
            QPushButton#secondary:hover {{
                border: 1px solid {t['accent']};
                color: {t['accent']};
            }}
        """)

    def update_theme(self, theme_name: str):
        self.theme_name = theme_name or self.theme_name
        self._apply_theme()

    def _on_download(self):
        self.downloadRequested.emit(self.current_url)
        self.hide_animated()

    def _check_clipboard(self):
        cb = QApplication.clipboard().text().strip()
        if cb == self.last_clipboard or not cb or len(cb) > 2048:
            return
        self.last_clipboard = cb
        
        # Simple URL detection
        if _URL_PATTERN.match(cb):
            self.current_url = cb
            self.show_animated()

    def show_animated(self):
        self.hide_timer.stop()
        if hasattr(self, 'anim') and self.anim is not None:
            self.anim.stop()
        screen = QApplication.primaryScreen().geometry()
        target_x = screen.width() - self.width() - 20
        target_y = 40
        self.move(target_x, target_y - 20)
        
        self.anim = QPropertyAnimation(self, b"pos")
        self.anim.setDuration(400)
        self.anim.setStartValue(QPoint(target_x, target_y - 20))
        self.anim.setEndValue(QPoint(target_x, target_y))
        self.anim.setEasingCurve(QEasingCurve.Type.OutBack)
        
        self.show()
        self.anim.start()
        self.hide_timer.start(8000)

    def hide_animated(self):
        self.hide_timer.stop()
        if hasattr(self, 'anim') and self.anim is not None:
            self.anim.stop()
            try:
                self.anim.finished.disconnect()
            except RuntimeError:
                pass
        curr_pos = self.pos()
        self.anim = QPropertyAnimation(self, b"pos")
        self.anim.setDuration(300)
        self.anim.setStartValue(curr_pos)
        self.anim.setEndValue(QPoint(curr_pos.x(), curr_pos.y() - 30))
        self.anim.setEasingCurve(QEasingCurve.Type.InQuad)
        self.anim.finished.connect(self.hide)
        self.anim.start()

    def start(self):
        self.check_timer.start(2000)

    def stop(self):
        self.check_timer.stop()



