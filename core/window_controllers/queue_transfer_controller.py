import csv
import json
import os

from core.config import default_download_dir
from core.i18n import _
from core.qt_compat import QFileDialog, QMessageBox
from core.utils import sanitize_queue_items_for_safe_export


class QueueTransferController:
    def __init__(self, window):
        self.window = window

    def export_queue_to_file(self):
        items = self.window.queue_manager.get_queue_items_snapshot()
        if not items:
            self.window._warn(_("لا توجد عناصر في الطابور لتصديرها"))
            return
        default_name = "queue_items.json"
        default_path = os.path.join(os.getcwd(), default_name)
        path, selected_filter = QFileDialog.getSaveFileName(
            self.window,
            _("Export Queue"),
            default_path,
            "JSON Files (*.json);;CSV Files (*.csv);;Text Files (*.txt);;All Files (*)",
        )
        if not path:
            return
        export_format = self._infer_export_format(path, selected_filter)
        if export_format == "json":
            mode = self.choose_queue_export_mode()
            if not mode:
                return
            self._export_queue_json(path, items, mode)
            return
        if export_format == "csv":
            self._export_queue_csv(path, items)
            return
        self._export_queue_txt(path, items)

    def choose_queue_export_mode(self) -> str | None:
        msg = QMessageBox(self.window)
        msg.setWindowTitle(_("Export Queue"))
        msg.setIcon(QMessageBox.Icon.Question)
        msg.setText(_("اختر نوع تصدير الطابور"))
        msg.setInformativeText(
            _("Full Export يحافظ على الروابط الأصلية ويمكن استيراده لاحقًا.\n"
              "Safe Export ينقّح الروابط ويحذف البيانات الحساسة المحلية ومخصص للمشاركة أو المراجعة.")
        )
        full_btn = msg.addButton(_("Full Export"), QMessageBox.ButtonRole.AcceptRole)
        safe_btn = msg.addButton(_("Safe Export"), QMessageBox.ButtonRole.ActionRole)
        cancel_btn = msg.addButton(QMessageBox.StandardButton.Cancel)
        msg.setDefaultButton(full_btn)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked == full_btn:
            return "full"
        if clicked == safe_btn:
            return "safe"
        if clicked == cancel_btn:
            return None
        return None

    @staticmethod
    def _infer_export_format(path: str, selected_filter: str = "") -> str:
        ext = os.path.splitext(str(path or "").strip())[1].lower()
        if ext == ".csv":
            return "csv"
        if ext == ".txt":
            return "txt"
        if ext == ".json":
            return "json"
        normalized_filter = str(selected_filter or "").lower()
        if "csv" in normalized_filter:
            return "csv"
        if "text" in normalized_filter or "txt" in normalized_filter:
            return "txt"
        return "json"

    def _export_queue_json(self, path: str, items: list[dict], mode: str) -> None:
        if mode == "safe":
            payload = {
                "queue_items": sanitize_queue_items_for_safe_export(items),
                "export_mode": "safe",
                "importable": False,
            }
        else:
            payload = {
                "queue_items": items,
                "export_mode": "full",
                "importable": True,
            }
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            if mode == "safe":
                self.window._info(_("تم تصدير نسخة آمنة من الطابور إلى: {path}").format(path=path))
                self.window._append_log(f"تم تصدير نسخة آمنة من الطابور إلى {path}")
            else:
                self.window._info(_("تم تصدير الطابور بنجاح إلى: {path}").format(path=path))
                self.window._append_log(f"تم تصدير الطابور إلى {path}")
        except (OSError, ValueError, TypeError) as exc:
            self.window._warn(_("فشل تصدير الطابور: {err}").format(err=str(exc)))

    def _export_queue_csv(self, path: str, items: list[dict]) -> None:
        try:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["url", "title", "status", "mode", "format", "quality", "out_dir"])
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    writer.writerow(
                        [
                            str(item.get("url", "")).strip(),
                            str(item.get("title", "")).strip(),
                            str(item.get("status", "")).strip(),
                            str(item.get("mode", "")).strip(),
                            str(item.get("format", "")).strip(),
                            str(item.get("quality", "")).strip(),
                            str(item.get("out_dir", "")).strip(),
                        ]
                    )
            self.window._info(_("تم تصدير الطابور بصيغة CSV إلى: {path}").format(path=path))
            self.window._append_log(f"تم تصدير الطابور بصيغة CSV إلى {path}")
        except (OSError, ValueError, TypeError) as exc:
            self.window._warn(_("فشل تصدير الطابور: {err}").format(err=str(exc)))

    def _export_queue_txt(self, path: str, items: list[dict]) -> None:
        try:
            with open(path, "w", encoding="utf-8") as handle:
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    url = str(item.get("url", "")).strip()
                    if url:
                        handle.write(f"{url}\n")
            self.window._info(_("تم تصدير روابط الطابور بصيغة TXT إلى: {path}").format(path=path))
            self.window._append_log(f"تم تصدير روابط الطابور بصيغة TXT إلى {path}")
        except (OSError, ValueError, TypeError) as exc:
            self.window._warn(_("فشل تصدير الطابور: {err}").format(err=str(exc)))

    def import_queue_from_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self.window,
            _("Import Queue"),
            "",
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
        try:
            try:
                if os.path.getsize(path) > 5 * 1024 * 1024:
                    self.window._warn(_("ملف الطابور كبير جداً"))
                    return
            except OSError:
                pass
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                items = data.get("queue_items", [])
            elif isinstance(data, list):
                items = data
            else:
                raise ValueError("Invalid queue file format")
            if not isinstance(items, list):
                raise ValueError("Invalid queue file items")
            current_count = self.window.queue_manager.get_item_count()
            capacity = max(0, int(self.window.queue_manager.MAX_QUEUE_SIZE) - int(current_count))
            if capacity <= 0:
                self.window._warn(_("الطابور ممتلئ"))
                return
            normalized = []
            dropped = 0
            for raw in items[:5000]:
                if len(normalized) >= capacity:
                    break
                payload = self._normalize_import_item(raw)
                if payload is None:
                    dropped += 1
                    continue
                normalized.append(payload)
            if not normalized:
                self.window._warn(_("لا توجد عناصر صالحة في ملف الطابور"))
                return
            self.window.queue_manager.add_tasks(normalized)
            self.window._save_session()
            self.window._refresh_downloads_list()
            message = _("تم استيراد {count} عنصر إلى الطابور").format(count=len(normalized))
            if dropped:
                message = message + _(" (تم تجاهل {count} عنصر غير صالح)").format(count=dropped)
            self.window._info(message)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self.window._warn(_("فشل استيراد الطابور: {err}").format(err=str(exc)))

    def _normalize_import_item(self, raw: dict | object) -> dict | None:
        if not isinstance(raw, dict):
            return None
        url = str(raw.get("url", "") or "").strip()
        if not url or len(url) > 4096 or not self.window._is_allowed_bulk_url(url):
            return None
        out_dir = str(raw.get("out_dir") or raw.get("download_path") or "").strip()
        if not out_dir:
            out_dir = default_download_dir()
        status = str(raw.get("status", "pending") or "pending").strip().lower()
        if status in {"running", "downloading", "processing", "merging"}:
            status = "pending"
        if status not in {"pending", "queued", "waiting", "paused", "failed", "cancelled"}:
            status = "pending"
        try:
            scheduled_at = float(raw.get("scheduled_at") or 0)
        except (TypeError, ValueError):
            scheduled_at = 0.0
        if scheduled_at < 0:
            scheduled_at = 0.0
        try:
            retry_count = int(raw.get("retry_count") or 0)
        except (TypeError, ValueError):
            retry_count = 0
        try:
            next_retry_at = float(raw.get("next_retry_at") or 0)
        except (TypeError, ValueError):
            next_retry_at = 0.0
        if next_retry_at < 0:
            next_retry_at = 0.0
        mode = str(raw.get("mode") or raw.get("download_type") or "").strip().lower()
        if mode not in {"video", "audio"}:
            mode = "video"
        return {
            "url": url,
            "title": str(raw.get("title", "") or "").strip(),
            "thumbnail": str(raw.get("thumbnail", "") or "").strip(),
            "mode": mode,
            "format": str(raw.get("format", "MP4") or "MP4").strip(),
            "quality": str(raw.get("quality", "1080p") or "1080p").strip(),
            "subtitle": str(raw.get("subtitle", "None") or "None").strip(),
            "out_dir": out_dir,
            "status": status,
            "scheduled_at": scheduled_at,
            "retry_count": max(0, retry_count),
            "next_retry_at": next_retry_at,
            "video_id": str(raw.get("video_id", "") or "").strip(),
            "file_hash": str(raw.get("file_hash", "") or "").strip(),
            "post_action": str(raw.get("post_action", "none") or "none").strip() or "none",
            "post_download_script": str(raw.get("post_download_script", "") or "").strip(),
            "embed_subs": bool(raw.get("embed_subs", True)),
            "split_chapters": bool(raw.get("split_chapters", False)),
            "whisper_fallback": bool(raw.get("whisper_fallback", False)),
            "sponsorblock_enabled": bool(raw.get("sponsorblock_enabled", False)),
            "rename_template": str(raw.get("rename_template", "Default") or "Default").strip() or "Default",
            "use_native_engine": bool(raw.get("use_native_engine", False)),
        }
