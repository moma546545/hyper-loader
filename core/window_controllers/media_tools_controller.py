import os

from core.audio_normalizer import normalize_folder
from core.config import default_download_dir
from core.qt_compat import QFileDialog, QMessageBox
from core.workers import ConversionWorker


class MediaToolsController:
    def __init__(self, window):
        self.window = window

    def pick_conv_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self.window,
            "Select Media File",
            "",
            "Media Files (*.mp4 *.mkv *.avi *.mp3 *.wav);;All Files (*)",
        )
        if path:
            self.window.tools_view.conv_input.setText(path)

    def start_conversion(self):
        if self.window._conversion_worker is not None and self.window._conversion_worker.isRunning():
            QMessageBox.information(self.window, "Busy", "A conversion is already in progress.")
            return
        input_path = self.window.tools_view.conv_input.text()
        if not input_path or not os.path.exists(input_path):
            QMessageBox.warning(self.window, "Error", "Please select a valid file.")
            return

        fmt = self.window.tools_view.conv_fmt.currentText().lower()
        output_path = os.path.splitext(input_path)[0] + f"_converted.{fmt}"
        worker = ConversionWorker(input_path, fmt, output_path, self.window)
        self.window._conversion_worker = worker
        self.window._append_log(f"Conversion started: {input_path} -> {output_path}")
        self.window._set_status("جاري التحويل")

        def _on_conversion_done(success: bool, final_path: str, error: str):
            self.window._conversion_worker = None
            if success:
                QMessageBox.information(self.window, "Done", f"Conversion finished successfully.\n{final_path}")
                self.window._append_log(f"Conversion finished: {final_path}")
                self.window._set_status("اكتمل التحويل")
                return
            msg = str(error or "FFmpeg conversion failed.").strip()
            QMessageBox.critical(self.window, "Error", msg)
            self.window._append_log(f"[Conversion ERROR] {msg}")
            self.window._set_status("فشل التحويل")

        worker.finished.connect(_on_conversion_done)
        worker.start()

    def fetch_channel(self):
        url = self.window.tools_view.chan_url.text().strip()
        if not url:
            return
        self.window.search_view.url_input.setText(url)
        self.window._switch_view("search")
        self.window._start_analyze()

    def normalize_downloads_folder(self):
        if self.window._normalize_folder_running:
            self.window._warn("يوجد تطبيع صوتي جارٍ بالفعل")
            return
        self.window._normalize_folder_running = True
        if hasattr(self.window, "search_view") and hasattr(self.window.search_view, "get_out_dir"):
            out_dir = self.window.search_view.get_out_dir() or default_download_dir()
        else:
            out_dir = self.window.search_view.out_dir_input.text().strip() or default_download_dir()
        self.window._info("🎧 Normalizing audio levels in downloads folder...")
        normalize_folder(
            folder=out_dir,
            progress_callback=self.window._append_log,
            done_callback=lambda ok, total: (
                setattr(self.window, "_normalize_folder_running", False),
                self.window._info(f"✅ Normalized {ok}/{total} files"),
            ),
        )
