
# ui/stats_view.py
import logging

try:
    from PySide6.QtCore import Qt, QPoint, QThread, Signal
    from PySide6.QtGui import (
        QPainter, QPen, QBrush, QColor, QLinearGradient, QPolygon
    )
    from PySide6.QtWidgets import (
        QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame
    )
except ImportError:
    from PyQt6.QtCore import Qt, QPoint, QThread, pyqtSignal as Signal
    from PyQt6.QtGui import (
        QPainter, QPen, QBrush, QColor, QLinearGradient, QPolygon
    )
    from PyQt6.QtWidgets import (
        QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame
    )

from ui.themes import get_theme
from core.i18n import _
from core.database import get_all_stats, get_history_stats_snapshot

logger = logging.getLogger("SnapDownloader.Stats")

class StatsWorker(QThread):
    finished = Signal(dict)

    def run(self):
        results = {
            "total_bytes": 0,
            "peak_kbps": 0.0,
            "success_rate": 0.0,
            "chart_data": [],
            "total_videos": 0,
            "total_audios": 0,
            "history_count": 0,
        }
        try:
            stats = get_all_stats()
            snapshot = get_history_stats_snapshot(days=7)
            total_count = int(snapshot.get("total_count", 0) or 0)
            success_count = int(snapshot.get("success_count", 0) or 0)

            # Calculations
            total_bytes = int(snapshot.get("total_bytes", 0) or 0)
            peak_kbps = float(stats.get("peak_speed_kbps", 0) or 0)
            success_rate = (success_count / float(total_count)) * 100.0 if total_count > 0 else 0.0
            total_videos = int(stats.get("total_videos", 0) or 0)
            total_audios = int(stats.get("total_audios", 0) or 0)

            # Chart data calculation
            chart_data = list(snapshot.get("chart_data", []) or [])

            results.update({
                "total_bytes": total_bytes,
                "peak_kbps": peak_kbps,
                "success_rate": success_rate,
                "chart_data": chart_data,
                "total_videos": total_videos,
                "total_audios": total_audios,
                "history_count": success_count,
            })
        except Exception as exc:
            logger.warning(f"[StatsWorker] Failed to build stats: {exc}")
        finally:
            self.finished.emit(results)



class PremiumChart(QWidget):
    def __init__(self, theme_name="Modern Dark"):
        super().__init__()
        self.theme_name = theme_name
        self.data = []
        self.setMinimumHeight(150)
        self._cache_key = None
        self._cache = {}

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        t = get_theme(self.theme_name)
        w, h = self.width(), self.height()
        key = (self.theme_name, int(w), int(h), tuple(int(x or 0) for x in (self.data or [])))
        if key != self._cache_key:
            self._cache_key = key
            rows = 5
            grid_lines = [int(h * i / (rows - 1)) for i in range(rows)]
            data = list(self.data or [])
            points = []
            if len(data) >= 2 and w > 0 and h > 0:
                step = w / (len(data) - 1)
                for i, val in enumerate(data):
                    px = int(i * step)
                    py = int(h - (float(val) / 100.0 * h))
                    points.append(QPoint(px, py))
            pen_grid = QPen(QColor(t["border"]))
            pen_grid.setWidth(1)
            pen_grid.setStyle(Qt.PenStyle.DashLine)
            pen_path = QPen(QColor(t["accent"]))
            pen_path.setWidth(4)
            pen_path.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            gradient = QLinearGradient(0, 0, 0, h)
            gradient.setColorAt(0, QColor(t["accent"] + "88"))
            gradient.setColorAt(1, QColor(t["accent"] + "00"))
            poly_points = [QPoint(0, h)] + points + [QPoint(w, h)] if points else []
            self._cache = {
                "grid_lines": grid_lines,
                "pen_grid": pen_grid,
                "pen_path": pen_path,
                "fill_brush": QBrush(gradient),
                "points": points,
                "poly": QPolygon(poly_points) if poly_points else QPolygon(),
                "dot_brush": QBrush(QColor(t["text"])),
            }
        
        # Grid lines
        painter.setPen(self._cache["pen_grid"])
        for y in self._cache["grid_lines"]:
            painter.drawLine(0, y, w, y)
            
        # Draw area
        points = self._cache["points"]
        if not points:
            painter.setPen(QPen(QColor(t["muted"])))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, _("Loading stats..."))
            return
            
        # Path for the line
        path_pen = self._cache["pen_path"]
        painter.setPen(path_pen)
        
        # Area fill
        painter.setBrush(self._cache["fill_brush"])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(self._cache["poly"])
        
        # Line
        painter.setPen(path_pen)
        for i in range(len(points) - 1):
            painter.drawLine(points[i], points[i+1])
            
        # Dots
        painter.setBrush(self._cache["dot_brush"])
        painter.setPen(path_pen)
        for p in points:
            painter.drawEllipse(p, 5, 5)

class StatsView(QWidget):
    def __init__(self, theme_name="Modern Dark"):
        super().__init__()
        self.theme_name = theme_name
        self.worker = None
        self._init_ui()
        self.refresh()

    def _init_ui(self):
        t = get_theme(self.theme_name)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(25)
        
        self.title_label = QLabel(_("Insights Dashboard"))
        self.title_label.setStyleSheet(f"font-size: 24px; font-weight: 900; color: {t['text']};")
        layout.addWidget(self.title_label)
        
        cards_layout = QHBoxLayout()
        cards_layout.setSpacing(15)
        
        self.card_total = self._create_card(_("Total Downloaded"), "-- GB", "⬇")
        self.card_speed = self._create_card(_("Peak Speed"), "-- MB/s", "⚡")
        self.card_success = self._create_card(_("Success Rate"), "--%", "✅")
        
        cards_layout.addWidget(self.card_total)
        cards_layout.addWidget(self.card_speed)
        cards_layout.addWidget(self.card_success)
        layout.addLayout(cards_layout)
        
        # Chart Section
        chart_container = QFrame()
        chart_container.setStyleSheet(f"background: {t['panel_alt']}; border: 1px solid {t['border']}; border-radius: 20px;")
        chart_layout = QVBoxLayout(chart_container)
        chart_layout.setContentsMargins(20, 20, 20, 20)
        
        self.chart_title_label = QLabel(_("The Pulse (Activity Trends)"))
        self.chart_title_label.setStyleSheet(f"font-size: 14px; font-weight: 800; color: {t['accent_2']};")
        chart_layout.addWidget(self.chart_title_label)
        
        self.chart = PremiumChart(self.theme_name)
        chart_layout.addWidget(self.chart)
        
        layout.addWidget(chart_container)
        
        self.history_title_label = QLabel(_("Recent Download Activity"))
        self.history_title_label.setStyleSheet(f"font-size: 16px; font-weight: 800; color: {t['text']}; margin-top: 10px;")
        layout.addWidget(self.history_title_label)
        
        self.history_summary = QLabel("")
        self.history_summary.setStyleSheet(f"font-size: 12px; color: {t['muted']};")
        layout.addWidget(self.history_summary)

    def _create_card(self, title, value, icon):
        t = get_theme(self.theme_name)
        card = QFrame()
        card.setStyleSheet(f"background: {t['panel']}; border: 1px solid {t['border']}; border-radius: 16px;")
        l = QVBoxLayout(card)
        l.setContentsMargins(15, 15, 15, 15)
        
        row1 = QHBoxLayout()
        row1.addWidget(QLabel(icon))
        row1.addStretch(1)
        title_label = QLabel(title)
        row1.addWidget(title_label)
        
        val = QLabel(value)
        val.setStyleSheet(f"font-size: 20px; font-weight: 900; color: {t['accent']}; margin-top: 10px;")
        
        l.addLayout(row1)
        l.addWidget(val)
        card._value_label = val
        card._title_label = title_label
        return card

    def refresh(self):
        # M-08: Run stats calculation in a background thread to keep UI responsive
        if self.worker and self.worker.isRunning():
            return # Don't start a new worker if one is already running
        self.worker = StatsWorker()
        self.worker.finished.connect(self._on_stats_ready)
        self.worker.finished.connect(self._dispose_worker)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.start()

    def closeEvent(self, event):
        worker = self.worker
        self.worker = None
        if worker is not None and worker.isRunning():
            try:
                worker.requestInterruption()
            except Exception:
                pass
            try:
                worker.quit()
            except Exception:
                pass
            try:
                worker.wait(500)
            except Exception:
                pass
        if worker is not None:
            try:
                worker.deleteLater()
            except Exception:
                pass
        super().closeEvent(event)

    def _on_stats_ready(self, results: dict):
        self.card_total._value_label.setText(self._format_bytes(results.get("total_bytes", 0)))
        self.card_speed._value_label.setText(self._format_speed(results.get("peak_kbps", 0)))
        self.card_success._value_label.setText(f"{results.get('success_rate', 0.0):.1f}%")
        self.chart.data = results.get("chart_data", [])
        self.chart.update()
        self._update_history_summary(
            results.get("history_count", 0),
            results.get("total_videos", 0),
            results.get("total_audios", 0)
        )

    def _dispose_worker(self, _results: dict):
        self.worker = None

    def _format_bytes(self, value: int) -> str:
        b = float(value or 0)
        units = ["B", "KB", "MB", "GB", "TB"]
        idx = 0
        while b >= 1024.0 and idx < len(units) - 1:
            b /= 1024.0
            idx += 1
        if idx == 0:
            return f"{int(b)} {units[idx]}"
        return f"{b:.1f} {units[idx]}"

    def _format_speed(self, kbps: float) -> str:
        v = float(kbps or 0.0)
        if v <= 0:
            return "0 MB/s"
        mb_s = v / 1024.0
        return f"{mb_s:.1f} MB/s"

    def _update_history_summary(self, history_count: int, total_videos: int, total_audios: int):
        text = _(
            "Completed downloads: {count}  •  Videos: {videos}  •  Audios: {audios}"
        ).format(count=history_count, videos=total_videos, audios=total_audios)
        self.history_summary.setText(text)

    def retranslate_ui(self):
        self.title_label.setText(_("Insights Dashboard"))
        self.chart_title_label.setText(_("The Pulse (Activity Trends)"))
        self.history_title_label.setText(_("Recent Download Activity"))
        if hasattr(self, "card_total"):
            self.card_total._title_label.setText(_("Total Downloaded"))
        if hasattr(self, "card_speed"):
            self.card_speed._title_label.setText(_("Peak Speed"))
        if hasattr(self, "card_success"):
            self.card_success._title_label.setText(_("Success Rate"))



