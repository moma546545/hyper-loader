import os
import subprocess
import sys

from core.config import default_download_dir


class FileManagerController:
    def __init__(self, window):
        self.window = window

    def open_downloads_folder(self):
        target = (
            self.window.current_download_path
            or self.window.search_view.out_dir_input.text().strip()
            or default_download_dir()
        )
        folder = target if os.path.isdir(target) else os.path.dirname(target)
        folder = folder or os.getcwd()
        self.open_path_in_file_manager(folder)

    def open_folder(self, file_path: str):
        path = str(file_path or "").strip()
        if path and os.path.isfile(path):
            folder = os.path.dirname(path)
        elif path and os.path.isdir(path):
            folder = path
        else:
            folder = (
                self.window.current_download_path
                or self.window.search_view.out_dir_input.text().strip()
                or default_download_dir()
            )
        folder = folder if os.path.isdir(folder) else os.path.dirname(folder)
        folder = folder or os.getcwd()
        self.open_path_in_file_manager(folder)

    def open_queue_item_folder(self, item_index: int):
        try:
            idx = int(item_index)
        except Exception:
            self.open_folder("")
            return
        task = self.window.queue_manager.get_task(idx)
        if not isinstance(task, dict):
            self.open_folder("")
            return
        path = str(task.get("last_output_path") or task.get("out_dir") or "").strip()
        self.open_folder(path)

    def open_path_in_file_manager(self, folder: str):
        # Security: never pass unknown/invalid paths to shell openers.
        resolved = os.path.abspath(str(folder or "").strip())
        if not os.path.isdir(resolved):
            self.window._warn(f"المسار غير صالح أو غير موجود: {resolved}")
            return
        try:
            if sys.platform == "win32":
                os.startfile(resolved)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", resolved])
            else:
                subprocess.Popen(["xdg-open", resolved])
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            self.window._warn(f"تعذر فتح المجلد: {exc}")
