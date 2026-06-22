import sys
import platform as _platform
import logging
from PySide6.QtWidgets import QWidget, QLabel, QHBoxLayout, QGraphicsOpacityEffect, QApplication
from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve, QRect, QPoint

try:
    import win11toast as _win11toast
    _HAS_WIN11_TOAST = True
except ImportError:
    _win11toast = None
    _HAS_WIN11_TOAST = False

try:
    _IS_WINDOWS_11 = (
        sys.platform == "win32"
        and _HAS_WIN11_TOAST
        and int(_platform.version().split('.')[2] or 0) >= 22000
    )
except Exception:
    _IS_WINDOWS_11 = False

class ToastWidget(QWidget):
    active_toasts = []  # To stack multiple toasts eventually if needed

    def __init__(self, parent, message: str, level: str = "info", duration_ms=3000):
        top_window = parent.window() if parent else None
        super().__init__(top_window)
        
        self.message = message
        self.level = level
        self.duration_ms = duration_ms
        self._hiding = False
        
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        
        self.setup_ui()
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide_toast)
        
    def setup_ui(self):
        # Enhanced modern styling with glassmorphism
        bg_col = "rgba(30, 30, 35, 245)"
        border_col = "rgba(255, 255, 255, 0.1)"
        text_col = "#E5E7EB"
        icon_text = "ℹ"
        accent_col = "#6366F1"
        
        if self.level == "error":
            bg_col = "rgba(40, 20, 20, 245)"
            border_col = "rgba(239, 68, 68, 0.3)"
            accent_col = "#EF4444"
            icon_text = "✖"
        elif self.level == "warning":
            bg_col = "rgba(40, 35, 15, 245)"
            border_col = "rgba(245, 158, 11, 0.3)"
            accent_col = "#F59E0B"
            icon_text = "⚠"
        elif self.level == "success":
            bg_col = "rgba(20, 40, 25, 245)"
            border_col = "rgba(16, 185, 129, 0.3)"
            accent_col = "#10B981"
            icon_text = "✔"

        # Using a QFrame for the background to apply styling
        self.bg_frame = QWidget(self)
        self.bg_frame.setObjectName("toast_bg")
        self.bg_frame.setStyleSheet(f"""
            QWidget#toast_bg {{
                background-color: {bg_col};
                border: 1px solid {border_col};
                border-left: 4px solid {accent_col};
                border-radius: 8px;
            }}
        """)
        
        # Move layout to the background frame
        bg_layout = QHBoxLayout(self.bg_frame)
        bg_layout.setContentsMargins(16, 12, 16, 12)
        bg_layout.setSpacing(12)
        
        # Main widget layout just holds the frame
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(self.bg_frame)
        
        icon = QLabel(icon_text)
        icon.setStyleSheet(f"""
            font-size: 16px; 
            font-weight: bold; 
            color: {accent_col};
            background: transparent;
            border: none;
        """)
        
        label = QLabel(self.message)
        label.setWordWrap(True)
        label.setStyleSheet(f"""
            font-size: 14px;
            font-weight: 500;
            color: {text_col};
            background: transparent;
            border: none;
        """)
        
        bg_layout.addWidget(icon)
        bg_layout.addWidget(label, 1) # Give label stretch priority
        
        # Add a subtle shadow
        from PySide6.QtWidgets import QGraphicsDropShadowEffect
        from PySide6.QtGui import QColor
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setColor(QColor(0, 0, 0, 100))
        shadow.setOffset(0, 4)
        self.bg_frame.setGraphicsEffect(shadow)
        
        self.adjustSize()
        self.opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self.opacity_effect)
        self.opacity_effect.setOpacity(0.0)
        self.pos_anim = QPropertyAnimation(self, b"pos", self)
        self.pos_anim.setDuration(400)
        self.pos_anim.setEasingCurve(QEasingCurve.Type.OutBack)
        self.op_anim = QPropertyAnimation(self.opacity_effect, b"opacity", self)
        self.op_anim.setDuration(300)
        self.op_anim.finished.connect(self._on_fade_done)

    def show_toast(self):
        self._hiding = False
        self._hide_timer.stop()
        self.show()
        parent_rect = self.parent().geometry() if self.parent() else QApplication.primaryScreen().availableGeometry()
        
        margins = 20
        # Determine stacking offset if there are active toasts
        stack_offset = len(ToastWidget.active_toasts) * (self.height() + 10)
        
        target_pos = QPoint(
            parent_rect.right() - self.width() - margins,
            parent_rect.bottom() - self.height() - margins - stack_offset
        )
        start_pos = QPoint(target_pos.x(), target_pos.y() + 50)
        
        self.setGeometry(QRect(start_pos, self.size()))
        self.pos_anim.stop()
        self.pos_anim.setStartValue(start_pos)
        self.pos_anim.setEndValue(target_pos)
        self.pos_anim.setDirection(QPropertyAnimation.Direction.Forward)

        self.op_anim.stop()
        self.op_anim.setStartValue(0.0)
        self.op_anim.setEndValue(1.0)
        self.op_anim.setDirection(QPropertyAnimation.Direction.Forward)
        
        self.pos_anim.start()
        self.op_anim.start()
        
        ToastWidget.active_toasts.append(self)
        self._hide_timer.start(max(0, int(self.duration_ms)))

    def _on_fade_done(self):
        """Called when opacity animation finishes. Clean up if we were hiding."""
        if self._hiding:
            if self in ToastWidget.active_toasts:
                ToastWidget.active_toasts.remove(self)
            self.close()
            self.deleteLater()

    def hide_toast(self):
        if self._hiding:
            return
        self._hiding = True
        self._hide_timer.stop()
        if self in ToastWidget.active_toasts:
            ToastWidget.active_toasts.remove(self)
            
        self.pos_anim.stop()
        self.op_anim.stop()
        self.pos_anim.setDirection(QPropertyAnimation.Direction.Backward)
        self.op_anim.setDirection(QPropertyAnimation.Direction.Backward)
        self.pos_anim.start()
        self.op_anim.start()

class ToastManager:
    @staticmethod
    def show(parent, message: str, level: str="info", duration: int=3500):
        text = str(message or "").strip()
        if not text:
            return
            
        title = "VidDownloader"
        if level == "success": title = "Success"
        elif level == "warn" or level == "warning": title = "Warning"
        elif level == "error": title = "Error"
        else: title = "Information"
            
        if _IS_WINDOWS_11:
            import threading
            def _show_win11_toast():
                try:
                    _win11toast.toast(title, text, app_id="VidDownloader")
                except Exception:
                    pass
            threading.Thread(target=_show_win11_toast, daemon=True).start()
            return
                
        try:
            toast = ToastWidget(parent, message, level, duration)
            toast.show_toast()
        except Exception as e:
            logging.getLogger("ToastManager").error(f"Failed to show toast: {e}")
