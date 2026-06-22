import logging
import os
import re
import subprocess
import sys
import threading

from core.config import default_download_dir
from core.error_handler import ErrorHandler
from core.i18n import _

logger = logging.getLogger("SnapDownloader")


class HistoryPlaybackController:
    def __init__(self, window):
        self.window = window

    def _open_file_with_system(self, path: str) -> bool:
        target = os.path.abspath(str(path or "").strip())
        if not os.path.isfile(target):
            return False
        try:
            if sys.platform == "win32":
                os.startfile(target)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
            return True
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            self.window._warn(f"تعذر فتح الملف: {exc}")
            return False

    def find_best_file_path(self, item: dict) -> str:
        if not isinstance(item, dict):
            return ""
        raw_path = str(item.get("file_path", "") or "").strip()
        if raw_path and os.path.isfile(raw_path):
            return raw_path
        search_dirs = []
        out_dir_value = ""
        if hasattr(self.window, "search_view") and hasattr(self.window.search_view, "get_out_dir"):
            out_dir_value = self.window.search_view.get_out_dir()
        elif hasattr(self.window, "search_view") and hasattr(self.window.search_view, "out_dir_input"):
            out_dir_value = self.window.search_view.out_dir_input.text().strip()
        for candidate in [self.window.current_download_path, out_dir_value, default_download_dir()]:
            c = str(candidate or "").strip()
            if c and os.path.isdir(c) and c not in search_dirs:
                search_dirs.append(c)
        if not search_dirs:
            return ""
        title = str(item.get("title", "") or "").strip().lower()
        url = str(item.get("url", "") or "").strip().lower()
        ids = []
        for pattern in [
            r"\[([A-Za-z0-9_-]{6,})\]",
            r"[?&]v=([A-Za-z0-9_-]{6,})",
            r"youtu\.be/([A-Za-z0-9_-]{6,})",
        ]:
            for match in re.findall(pattern, f"{title} {url}"):
                if match not in ids:
                    ids.append(match)
        media_exts = {
            ".mp4",
            ".webm",
            ".mkv",
            ".avi",
            ".mov",
            ".flv",
            ".mp3",
            ".m4a",
            ".aac",
            ".wav",
            ".flac",
            ".ogg",
            ".wma",
        }
        max_candidates_per_dir = 2000
        title_tokens = [t for t in re.findall(r"[a-zA-Z0-9\u0600-\u06FF]+", title) if len(t) > 2][:6]
        for folder in search_dirs:
            try:
                files = []
                with os.scandir(folder) as entries:
                    for entry in entries:
                        if len(files) >= max_candidates_per_dir:
                            break
                        if not entry.is_file():
                            continue
                        ext = os.path.splitext(entry.name)[1].lower()
                        if ext in media_exts:
                            files.append(entry.path)
            except OSError:
                continue
            if ids:
                for fp in files:
                    name = os.path.basename(fp).lower()
                    if any(f"[{vid.lower()}]" in name or vid.lower() in name for vid in ids):
                        return fp
            if title_tokens:
                ranked = []
                for fp in files:
                    name = os.path.basename(fp).lower()
                    score = sum(1 for token in title_tokens if token in name)
                    if score > 0:
                        ranked.append((score, fp))
                if ranked:
                    ranked.sort(key=lambda x: (x[0], os.path.getmtime(x[1])), reverse=True)
                    return ranked[0][1]
        return ""

    def open_history_item_file(self, item: dict):
        def _on_ready(target: str):
            if target:
                item["file_path"] = target
                if self._open_file_with_system(target):
                    return
                ErrorHandler.show_warning(self.window, _("Error"), _("File not found!"))
                return
            ErrorHandler.show_warning(self.window, _("Error"), _("File not found!"))

        self.resolve_history_item_path_async(item, _on_ready)

    def open_history_item_folder(self, item: dict):
        def _on_ready(target: str):
            if target:
                item["file_path"] = target
                self.window._open_folder(target)
                return
            self.window._open_folder("")

        self.resolve_history_item_path_async(item, _on_ready)

    def resolve_history_item_path_async(self, item: dict, on_ready):
        if not callable(on_ready):
            return
        data = dict(item or {})
        with self.window._history_path_lock:
            self.window._history_path_request_seq += 1
            request_id = str(self.window._history_path_request_seq)
            self.window._history_path_callbacks[request_id] = on_ready

        def _resolve():
            target = ""
            try:
                target = self.find_best_file_path(data)
            except Exception as exc:
                logger.debug(f"[History] Failed to resolve history item path in background: {exc}")
            self.window.history_item_path_resolved_ui.emit(request_id, str(target or ""))

        threading.Thread(target=_resolve, daemon=True, name="HistoryPathResolver").start()

    def on_history_item_path_resolved(self, request_id: str, target: str):
        callback = None
        with self.window._history_path_lock:
            callback = self.window._history_path_callbacks.pop(str(request_id), None)
        if callback is None:
            return
        try:
            callback(str(target or ""))
        except Exception as exc:
            logger.debug(f"[History] Failed to execute history path callback: {exc}")
