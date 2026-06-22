
"""
ui/mini_mode.py — Compact Mini Player Window
A floating, always-on-top mini window that shows active download progress.
Inspired by Spotify Mini Player — beautiful, unobtrusive, draggable.
"""
try:
    from PySide6.QtCore import Qt, QTimer, Signal, QPoint
    from PySide6.QtGui import QColor, QPainter, QPainterPath
    from PySide6.QtWidgets import (
        QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
        QProgressBar, QApplication, QGraphicsDropShadowEffect
    )
except ImportError:
    from PyQt6.QtCore import Qt, QTimer, pyqtSignal as Signal, QPoint
    from PyQt6.QtGui import QColor, QPainter, QPainterPath
    from PyQt6.QtWidgets import (
        QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
        QProgressBar, QApplication, QGraphicsDropShadowEffect
    )


class MiniModeWindow(QWidget):
    """
    A compact floating window showing download progress.
    Always stays on top, draggable, click to restore main window.
    """
    showMainRequested = Signal()
    pauseRequested = Signal()
    cancelRequested = Signal()

    def __init__(self, theme_colors: dict = None):
        super().__init__(None)  # No parent so it floats independently
        self.t = theme_colors or self._default_colors()
        self._drag_pos = None
        self._active_title = "No active downloads"
        self._progress = 0
        self._speed = "--"
        self._eta = "--:--"
        self._total_active = 0
        self._pulse_offset = 0

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        # Increased size to accommodate the shadow margins (10px on each side)
        self.setFixedSize(360, 100)

        self._build_ui()
        self._position_bottom_right()

        # Pulse animation timer
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._tick_pulse)

    # ── UI Construction ───────────────────────────────────────────────────────

    def _clear_layout(self, layout=None):
        """Recursively remove widgets and nested layouts without orphaning the layout."""
        layout = layout or self.layout()
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            child_widget = item.widget()
            child_layout = item.layout()
            if child_layout is not None:
                self._clear_layout(child_layout)
                child_layout.deleteLater()
                continue
            if child_widget is not None:
                child_widget.deleteLater()

    def _build_ui(self):
        t = self.t
        
        outer = self.layout()
        if outer is None:
            outer = QHBoxLayout(self)
        else:
            self._clear_layout(outer)
        outer.setContentsMargins(15, 12, 15, 12)
        outer.setSpacing(12)

        # Apply a subtle shadow to the window itself
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(25)
        shadow.setColor(QColor(0, 0, 0, 150))
        shadow.setOffset(0, 8)
        self.setGraphicsEffect(shadow)

        # Icon with glassmorphism container
        icon_container = QWidget()
        icon_container.setFixedSize(40, 40)
        icon_layout = QVBoxLayout(icon_container)
        icon_layout.setContentsMargins(0, 0, 0, 0)
        
        self.icon_lbl = QLabel("⬇")
        self.icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_lbl.setStyleSheet(f"""
            QLabel {{
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, 
                    stop:0 {t.get('accent', '#6366F1')}, stop:1 {t.get('accent_2', '#8B5CF6')});
                color: #FFFFFF;
                border-radius: 12px;
                font-size: 16px;
                font-weight: bold;
            }}
        """)
        icon_layout.addWidget(self.icon_lbl)

        # Center column
        center = QVBoxLayout()
        center.setSpacing(4)
        center.setContentsMargins(0, 2, 0, 2)

        self.title_lbl = QLabel(self._active_title)
        self.title_lbl.setStyleSheet(
            f"color: {t.get('text', '#FFFFFF')}; font-size: 13px; font-weight: 700; background: transparent;"
        )
        self.title_lbl.setMaximumWidth(180)

        self.meta_lbl = QLabel(f"⚡ {self._speed}  ⏱ {self._eta}")
        self.meta_lbl.setStyleSheet(
            f"color: {t.get('muted', '#9CA3AF')}; font-size: 11px; font-weight: 600; background: transparent;"
        )

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(12)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{
                border: none; 
                border-radius: 3px;
                background-color: rgba(255, 255, 255, 0.1);
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {t.get('accent', '#6366F1')}, stop:1 {t.get('success', '#10B981')});
                border-radius: 3px;
            }}
        """)

        center.addWidget(self.title_lbl)
        center.addWidget(self.meta_lbl)
        center.addWidget(self.progress_bar)

        # Buttons column
        btns = QVBoxLayout()
        btns.setSpacing(6)
        btns.setContentsMargins(5, 0, 0, 0)
        
        btn_style = f"""
            QPushButton {{ 
                background-color: transparent; 
                color: {t.get('muted', '#9CA3AF')}; 
                border: none; 
                border-radius: 6px; 
                font-size: 14px; 
            }}
            QPushButton:hover {{ 
                background-color: rgba(255, 255, 255, 0.1); 
                color: {t.get('text', '#FFFFFF')}; 
            }}
        """

        self.expand_btn = QPushButton("⛶")
        self.expand_btn.setFixedSize(26, 26)
        self.expand_btn.setToolTip("Show Main Window")
        self.expand_btn.clicked.connect(self.showMainRequested.emit)
        self.expand_btn.setStyleSheet(btn_style)

        self.pause_btn = QPushButton("⏸")
        self.pause_btn.setFixedSize(26, 26)
        self.pause_btn.setToolTip("Pause All")
        self.pause_btn.clicked.connect(self.pauseRequested.emit)
        self.pause_btn.setStyleSheet(btn_style)

        self.cancel_btn = QPushButton("✕")
        self.cancel_btn.setFixedSize(26, 26)
        self.cancel_btn.setToolTip("Cancel Active")
        self.cancel_btn.clicked.connect(self.cancelRequested.emit)
        self.cancel_btn.setStyleSheet(btn_style + "QPushButton:hover { background-color: rgba(239, 68, 68, 0.18); color: #EF4444; }")

        self.close_mini_btn = QPushButton("✕")
        self.close_mini_btn.setFixedSize(26, 26)
        self.close_mini_btn.setToolTip("Hide Mini Mode")
        self.close_mini_btn.clicked.connect(self.hide)
        self.close_mini_btn.setStyleSheet(btn_style + f"QPushButton:hover {{ background-color: rgba(239, 68, 68, 0.2); color: #EF4444; }}")

        top_btns = QHBoxLayout()
        top_btns.setSpacing(4)
        top_btns.addWidget(self.expand_btn)
        top_btns.addWidget(self.close_mini_btn)
        
        btns.addLayout(top_btns)
        btns.addWidget(self.pause_btn, 0, Qt.AlignmentFlag.AlignRight)
        btns.addWidget(self.cancel_btn, 0, Qt.AlignmentFlag.AlignRight)

        outer.addWidget(icon_container)
        outer.addLayout(center, 1)
        outer.addLayout(btns)

    def _default_colors(self) -> dict:
        return {
            "bg": "#050D1A",
            "panel": "#0A1628",
            "panel_alt": "#0F1E35",
            "panel_soft": "#1A2B4A",
            "text": "#E8F4FF",
            "muted": "#6B8DB5",
            "accent": "#00F0FF",
            "accent_2": "#FF6B9D",
            "border": "#1A2E4A",
        }

    # ── Public API ────────────────────────────────────────────────────────────

    def update_progress(self, title: str, progress: float, speed: str, eta: str, total_active: int = 1):
        self._active_title = title[:38] + "…" if len(title) > 38 else title
        self._progress = max(0, min(100, int(progress)))
        self._speed = speed or "--"
        self._eta = eta or "--:--"
        self._total_active = total_active

        self.title_lbl.setText(
            f"[{total_active} active] {self._active_title}" if total_active > 1 else self._active_title
        )
        self.meta_lbl.setText(f"⚡ {self._speed}  ⏱ {self._eta}")
        self.progress_bar.setValue(self._progress)

        if total_active > 0 and not self._pulse_timer.isActive():
            self._pulse_timer.start(60)
        elif total_active == 0 and self._pulse_timer.isActive():
            self._pulse_timer.stop()

    def update_theme(self, theme_colors: dict):
        self.t = theme_colors
        self._build_ui()

    def set_idle(self):
        """Show idle state when no downloads are active."""
        self._pulse_timer.stop()
        self._total_active = 0
        self.title_lbl.setText("No active downloads")
        self.meta_lbl.setText("SnapDownloader ready")
        self.progress_bar.setValue(0)
        self.icon_lbl.setText("✓")

    # ── Painting ──────────────────────────────────────────────────────────────

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = self.t

        # Adjust for shadow margins
        margin = 10
        rect = self.rect().adjusted(margin, margin, -margin, -margin)

        path = QPainterPath()
        path.addRoundedRect(rect, 14, 14)
        
        # Semi-transparent background for glass effect
        bg_color = QColor(t.get("panel_alt", "#0F1E35"))
        bg_color.setAlpha(240)
        painter.fillPath(path, bg_color)
        
        # Subtle border
        pen = painter.pen()
        pen.setColor(QColor(255, 255, 255, 20))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawPath(path)
        
        painter.end()

    def _tick_pulse(self):
        """Pulse the icon when downloading."""
        if self._total_active > 0:
            self._pulse_offset = (self._pulse_offset + 10) % 360
            alpha = int(128 + 127 * abs(self._pulse_offset / 180.0 - 1.0))
            self.icon_lbl.setStyleSheet(f"""
                QLabel {{
                    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, 
                        stop:0 {self.t.get('accent', '#6366F1')}, stop:1 {self.t.get('accent_2', '#8B5CF6')});
                    color: #FFFFFF;
                    border-radius: 12px;
                    font-size: 16px;
                    font-weight: bold;
                    border: 2px solid rgba(255, 255, 255, {alpha / 255.0:.2f});
                }}
            """)

    # ── Drag ─────────────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    def mouseDoubleClickEvent(self, event):
        self.showMainRequested.emit()

    def _position_bottom_right(self):
        screen = QApplication.primaryScreen().geometry()
        x = screen.width() - self.width() - 20
        y = screen.height() - self.height() - 60
        self.move(x, y)



