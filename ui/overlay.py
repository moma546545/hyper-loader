
"""
ui/overlay.py - Professional Notification Overlay Widget

Provides a customizable, animated overlay for showing notifications (info, success, warning, error).
"""
import logging

try:
    from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, QPoint
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QApplication
except ImportError:
    from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, QPoint
    from PyQt6.QtGui import QIcon
    from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QApplication

logger = logging.getLogger("SnapDownloader.Overlay")

STYLES = {
    "base": """
        QFrame#overlay_frame {
            background-color: rgba(35, 39, 42, 0.95);
            border-radius: 12px;
            border: 1px solid #4f545c;
        }
        QLabel#overlay_title {
            font-size: 16px;
            font-weight: bold;
            color: #ffffff;
        }
        QLabel#overlay_message {
            font-size: 13px;
            color: #dcddde;
        }
    """,
    "icon_colors": {
        "success": "#2ecc71",
        "info": "#3498db",
        "warning": "#f1c40f",
        "error": "#e74c3c",
    }
}

ICONS = {
    "success": "✅",
    "info": "ℹ️",
    "warning": "⚠️",
    "error": "❌",
}

class NotificationOverlay(QFrame):
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("overlay_frame")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumWidth(350)
        self.setMaximumWidth(450)
        self.hide()

        self._build_ui()
        self.setStyleSheet(STYLES["base"])
        self.animation = QPropertyAnimation(self, b"pos")
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.animation.setDuration(400)
        self.animation.finished.connect(self._on_hide_animation_done)

    def _on_hide_animation_done(self):
        """Only hide if the animation ended off-screen (i.e. hide animation, not show)."""
        if self.pos().y() < 0:
            self.hide()

    def _build_ui(self):
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(15)

        self.icon_label = QLabel("")
        self.icon_label.setObjectName("overlay_icon")
        self.icon_label.setFixedSize(32, 32)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        text_layout = QVBoxLayout()
        text_layout.setSpacing(4)
        self.title_label = QLabel("")
        self.title_label.setObjectName("overlay_title")
        self.title_label.setWordWrap(True)
        self.message_label = QLabel("")
        self.message_label.setObjectName("overlay_message")
        self.message_label.setWordWrap(True)
        text_layout.addWidget(self.title_label)
        text_layout.addWidget(self.message_label)
        text_layout.addStretch(1)

        main_layout.addWidget(self.icon_label)
        main_layout.addLayout(text_layout)

    def show_message(self, title: str, message: str, level: str = "info", duration_ms: int = 4000):
        """Displays the notification with animation."""
        self.title_label.setText(title)
        self.message_label.setText(message)

        icon_char = ICONS.get(level, "ℹ️")
        icon_color = STYLES["icon_colors"].get(level, "#3498db")
        self.icon_label.setText(icon_char)
        self.icon_label.setStyleSheet(f"font-size: 20px; color: {icon_color};")

        self.adjustSize()
        self.show_animated()

        if duration_ms > 0:
            QTimer.singleShot(duration_ms, self.hide_animated)

    def show_animated(self):
        self.animation.stop()
        self.show()
        parent_size = self.parent().size()
        self.move(int((parent_size.width() - self.width()) / 2), -self.height())
        self.animation.setStartValue(self.pos())
        self.animation.setEndValue(QPoint(int((parent_size.width() - self.width()) / 2), 20))
        self.animation.start()

    def hide_animated(self):
        self.animation.stop()
        self.animation.setStartValue(self.pos())
        self.animation.setEndValue(QPoint(int((self.parent().width() - self.width()) / 2), -self.height()))
        self.animation.start()

