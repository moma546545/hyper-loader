import csv
import json
import logging
import os

from core.database import count_history, delete_history, fetch_history, get_all_stats
from core.qt_compat import QFileDialog
from core.task_types import DownloadHistoryEntry
from core.utils import redact_url

logger = logging.getLogger("SnapDownloader")
EXPORT_PAGE_SIZE = 20000
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")


class HistoryDataController:
    def __init__(self, window):
        self.window = window

    @staticmethod
    def normalize_history_mode(mode_value: str) -> str:
        mode = str(mode_value or "").strip().lower()
        if mode in {"audio", "صوت"}:
            return "audio"
        return "video"

    def fetch_db_history(self, status: str = None, limit: int = 2500, offset: int = 0) -> list[DownloadHistoryEntry]:
        rows = fetch_history(status=status, limit=limit, offset=offset)
        normalized: list[DownloadHistoryEntry] = []
        for row in rows:
            file_path = str(row.get("file_path", "") or "").strip()
            current_status = str(row.get("status", "success") or "success").strip().lower()
            if file_path and current_status == "success" and not os.path.exists(file_path):
                current_status = "missing"
            normalized.append(
                {
                    "timestamp": row.get("timestamp", ""),
                    "title": row.get("title", ""),
                    "url": row.get("url", ""),
                    "mode": self.normalize_history_mode(row.get("mode", "video")),
                    "format": row.get("format", "--"),
                    "quality": row.get("quality", "--"),
                    "size": row.get("size_text", "--"),
                    "thumbnail": row.get("thumbnail", ""),
                    "file_path": file_path,
                    "status": current_status,
                    "message": row.get("message", ""),
                    "attempts": row.get("attempts", 1),
                    "error": row.get("error", ""),
                }
            )
        return normalized

    @staticmethod
    def _csv_safe_cell(value) -> str:
        text = str(value or "")
        if text.startswith(CSV_FORMULA_PREFIXES):
            return "'" + text
        return text

    def _fetch_all_db_history_for_export(self) -> list[DownloadHistoryEntry]:
        items: list[DownloadHistoryEntry] = []
        offset = 0
        while True:
            page = self.fetch_db_history(limit=EXPORT_PAGE_SIZE, offset=offset)
            if not page:
                break
            items.extend(page)
            if len(page) < EXPORT_PAGE_SIZE:
                break
            offset += EXPORT_PAGE_SIZE
        return items

    def load_stats(self):
        try:
            self.window.stats["download_history"] = self.fetch_db_history(limit=250)
            db_stats = get_all_stats()
            self.window.stats["total_videos"] = int(db_stats.get("total_videos", 0))
            self.window.stats["total_audios"] = int(db_stats.get("total_audios", 0))
        except (TypeError, ValueError, RuntimeError, OSError) as exc:
            self.window._append_log(f"تعذر تحميل الإحصائيات من قاعدة البيانات: {exc}")
            self.window.stats["download_history"] = []

    def clear_completed_history(self):
        try:
            before = count_history("success")
            delete_history("success")
            self.window.stats["download_history"] = self.fetch_db_history(limit=250)
            self.window._save_stats()
            self.window._append_log(f"تم حذف {before} عنصر مكتمل من السجل")
            self.window._refresh_downloads_list()
        except (RuntimeError, OSError, ValueError, TypeError) as exc:
            self.window._warn(f"فشل حذف السجل المكتمل: {exc}")

    def export_history_csv(self):
        path, _ = QFileDialog.getSaveFileName(
            self.window,
            "Export Links as CSV",
            "links_export.csv",
            "CSV Files (*.csv)",
        )
        if not path:
            return
        
        is_completed_view = getattr(self.window, "downloads_filter", "active") == "completed"
        if is_completed_view:
            items = self._fetch_all_db_history_for_export()
        else:
            items = self.window.queue_manager.get_queue_items_snapshot()

        try:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["url", "title", "status"])
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    url = str(item.get("url", "")).strip()
                    if not url:
                        continue
                    title = str(item.get("title", ""))
                    status = str(item.get("status", ""))
                    writer.writerow(
                        [
                            self._csv_safe_cell(url),
                            self._csv_safe_cell(title),
                            self._csv_safe_cell(status),
                        ]
                    )
            self.window._info("تم تصدير الروابط بنجاح")
            self.window._append_log(f"تم تصدير الروابط إلى {path}")
        except (OSError, ValueError, TypeError) as exc:
            self.window._warn(f"فشل التصدير: {exc}")

    def export_history_txt(self):
        path, _ = QFileDialog.getSaveFileName(
            self.window,
            "Export Links as TXT",
            "links_export.txt",
            "Text Files (*.txt)",
        )
        if not path:
            return
        
        is_completed_view = getattr(self.window, "downloads_filter", "active") == "completed"
        if is_completed_view:
            items = self._fetch_all_db_history_for_export()
        else:
            items = self.window.queue_manager.get_queue_items_snapshot()

        try:
            with open(path, "w", encoding="utf-8") as handle:
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    url = str(item.get("url", "")).strip()
                    if url:
                        handle.write(f"{url}\n")
            self.window._info("تم تصدير الروابط بنجاح")
            self.window._append_log(f"تم تصدير الروابط إلى {path}")
        except (OSError, ValueError, TypeError) as exc:
            self.window._warn(f"فشل التصدير: {exc}")
