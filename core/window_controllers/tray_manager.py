import logging
import os
try:
    from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
    from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
    from PySide6.QtCore import QTimer
except ImportError:
    from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
    from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
    from PyQt6.QtCore import QTimer

from ..task_types import PENDING_TASK_STATUSES, SUCCESS_TASK_STATUSES

logger = logging.getLogger("SnapDownloader.TrayManager")


def _fallback_tray_icon() -> QIcon:
    pixmap = QPixmap(32, 32)
    pixmap.fill(QColor("#00D2FF"))
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QColor("#FFFFFF"))
        painter.setBrush(QColor("#10B981"))
        painter.drawEllipse(5, 5, 22, 22)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawLine(16, 9, 16, 22)
        painter.drawLine(11, 17, 16, 22)
        painter.drawLine(21, 17, 16, 22)
    finally:
        painter.end()
    return QIcon(pixmap)

class TrayManager:
    def __init__(self, window):
        self.window = window
        self.tray_icon = None
        self.tray_menu = None
        self._tray_progress_bucket = -1
        self._cleanup_done = False
        self._about_to_quit_connected = False

    def setup(self):
        self._cleanup_done = False
        icon_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "icons", "app.ico")
        if not os.path.exists(icon_path):
            icon_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "icons", "app.png")
            
        icon = QIcon(icon_path) if os.path.exists(icon_path) else _fallback_tray_icon()
        if icon.isNull():
            icon = _fallback_tray_icon()
        
        self.tray_icon = QSystemTrayIcon(icon, self.window)
        
        self.tray_menu = QMenu()
        self.tray_stats_action = QAction("Downloading: 0 | Queued: 0 | Completed: 0", self.window)
        self.tray_stats_action.setEnabled(False)
        self.tray_menu.addAction(self.tray_stats_action)
        self.tray_menu.addSeparator()
        
        show_action = QAction("Show SnapDownloader", self.window)
        show_action.triggered.connect(self._show_main_window)
        self.tray_menu.addAction(show_action)
        
        self.tray_pause_action = QAction("Pause All", self.window)
        self.tray_pause_action.triggered.connect(self.window._pause_queue_download)
        self.tray_menu.addAction(self.tray_pause_action)
        
        self.tray_resume_action = QAction("Resume All", self.window)
        self.tray_resume_action.triggered.connect(self.window._resume_queue_download)
        self.tray_menu.addAction(self.tray_resume_action)
        
        self.tray_mini_action = QAction("Mini Mode", self.window)
        self.tray_mini_action.triggered.connect(self.window._toggle_mini_mode)
        self.tray_menu.addAction(self.tray_mini_action)
        
        self.tray_menu.addSeparator()
        quit_action = QAction("Quit", self.window)
        quit_action.triggered.connect(self._quit_application)
        self.tray_menu.addAction(quit_action)
        
        self.tray_icon.setContextMenu(self.tray_menu)
        self.tray_icon.show()
        self.tray_icon.activated.connect(self._on_tray_activated)
        self._connect_about_to_quit()
        
        QTimer.singleShot(0, self.update_stats)

    def _connect_about_to_quit(self):
        app = QApplication.instance()
        if app is None or self._about_to_quit_connected:
            return
        try:
            app.aboutToQuit.connect(self._on_about_to_quit)
            self._about_to_quit_connected = True
        except Exception:
            logger.debug("Failed to connect tray aboutToQuit hook", exc_info=True)

    def _on_about_to_quit(self):
        setattr(self.window, "_quit_to_tray_bypass", True)
        setattr(self.window, "_app_is_quitting", True)
        self.cleanup()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_main_window()

    def _show_main_window(self):
        self.window.showNormal()
        try:
            self.window.raise_()
        except Exception:
            logger.debug("Failed to raise main window from tray", exc_info=True)
        self.window.activateWindow()

    def show_message(self, title: str, message: str, icon=QSystemTrayIcon.MessageIcon.Information, timeout: int = 1800):
        if not self.tray_icon or not self.tray_icon.isVisible():
            return
        self.tray_icon.showMessage(str(title or "SnapDownloader"), str(message or ""), icon, int(timeout))

    def update_stats(self):
        if not self.tray_menu:
            return
        active = self.window._active_workers_count()
        queued = sum(1 for t in self.window.queue_manager.items if str(t.get("status", "")).lower() in PENDING_TASK_STATUSES)
        completed = sum(1 for t in self.window.queue_manager.items if str(t.get("status", "")).lower() in SUCCESS_TASK_STATUSES)
        
        if hasattr(self, "tray_stats_action"):
            self.tray_stats_action.setText(f"Downloading: {active} | Queued: {queued} | Completed: {completed}")
        if hasattr(self, "tray_pause_action"):
            self.tray_pause_action.setEnabled(active > 0 and not self.window._queue_is_paused())
        if hasattr(self, "tray_resume_action"):
            self.tray_resume_action.setEnabled(queued > 0 or self.window._queue_is_paused())

    def notify_progress(self):
        self.update_stats()
        items = self.window.queue_manager.get_queue_items_snapshot()
        if not items:
            return
        total = len(items)
        completed = 0
        progress_sum = 0.0
        for item in items:
            status = str(item.get('status', 'pending')).lower()
            if status in {'success', 'failed', 'cancelled'}:
                completed += 1
                progress_sum += 100.0
                continue
            raw = float(item.get('progress', 0) or 0)
            progress_sum += max(0.0, min(100.0, raw))
        overall = int(round(progress_sum / max(1, total)))
        bucket = min(100, max(0, (overall // 10) * 10))
        if bucket < 10 or bucket == self._tray_progress_bucket:
            return
        self._tray_progress_bucket = bucket
        self.show_message(
            'SnapDownloader',
            f'تقدم التحميل: {overall}% ({completed}/{total})',
            QSystemTrayIcon.MessageIcon.Information,
            1400
        )

    def cleanup(self):
        if self._cleanup_done:
            return
        self._cleanup_done = True
        if self.tray_icon:
            try:
                self.tray_icon.activated.disconnect(self._on_tray_activated)
            except Exception:
                pass
            try:
                self.tray_icon.hide()
            except Exception:
                logger.debug("Failed to hide tray icon during cleanup", exc_info=True)
            try:
                self.tray_icon.setContextMenu(None)
            except Exception:
                logger.debug("Failed to detach tray menu during cleanup", exc_info=True)
            try:
                self.tray_icon.deleteLater()
            except Exception:
                logger.debug("Failed to schedule tray icon deletion", exc_info=True)
        if self.tray_menu is not None:
            try:
                self.tray_menu.deleteLater()
            except Exception:
                logger.debug("Failed to schedule tray menu deletion", exc_info=True)
        self.tray_icon = None
        self.tray_menu = None

    def _quit_application(self):
        # Explicit quit from tray should bypass close-to-tray behavior.
        setattr(self.window, "_quit_to_tray_bypass", True)
        setattr(self.window, "_app_is_quitting", True)
        self.window.close()
        app = QApplication.instance()
        if app is not None:
            try:
                app.quit()
            except Exception:
                logger.debug("Failed to quit application event loop from tray quit action", exc_info=True)
