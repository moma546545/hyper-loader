import time

try:
    import yt_dlp
except Exception:
    yt_dlp = None

try:
    from PySide6.QtCore import QThread, Signal
except ImportError:
    from PyQt6.QtCore import QThread, pyqtSignal as Signal


class AnalyzerWorker(QThread):
    info_ready = Signal(dict)
    error_occurred = Signal(str)

    def __init__(self, url, parent=None):
        super().__init__(parent)
        self.url = str(url or "").strip()

    def _format_duration(self, seconds) -> str:
        try:
            total = int(seconds or 0)
        except Exception:
            return "--:--"
        if total <= 0:
            return "--:--"
        mins, secs = divmod(total, 60)
        hours, mins = divmod(mins, 60)
        if hours > 0:
            return f"{hours:02d}:{mins:02d}:{secs:02d}"
        return f"{mins:02d}:{secs:02d}"

    def run(self):
        if not self.url:
            self.error_occurred.emit("مشكلة في تحليل الرابط: الرابط مطلوب")
            return
        if yt_dlp is None:
            self.error_occurred.emit("مشكلة في تحليل الرابط: yt-dlp غير متاح")
            return

        ydl_opts = {
            "extract_flat": "in_playlist",
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": False,
            "socket_timeout": 30,
            "retries": 3,
            "extractor_retries": 2,
        }

        started_at = time.monotonic()
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(self.url, download=False)
            if not isinstance(info, dict):
                self.error_occurred.emit("مشكلة في تحليل الرابط: بيانات غير صالحة")
                return

            parsed_data = {
                "title": info.get("title", "فيديو غير معروف"),
                "thumbnail": info.get("thumbnail", ""),
                "duration": self._format_duration(info.get("duration")),
                "uploader": info.get("uploader", "قناة غير معروفة"),
                "formats": [],
                "is_playlist": isinstance(info.get("entries"), list),
                "elapsed_ms": int((time.monotonic() - started_at) * 1000),
            }

            if not parsed_data["is_playlist"]:
                for f in info.get("formats", []) or []:
                    if not isinstance(f, dict):
                        continue
                    if f.get("vcodec") == "none" and f.get("acodec") == "none":
                        continue
                    ext = f.get("ext", "mp4")
                    res = f.get("format_note", f.get("resolution", "audio"))
                    fmt_id = f.get("format_id")
                    size = f.get("filesize", 0) or f.get("filesize_approx", 0)
                    size_mb = f"{size / 1048576:.1f} MB" if size else "حجم غير معروف"
                    parsed_data["formats"].append(
                        {
                            "id": fmt_id,
                            "display": f"{res} - {ext} ({size_mb})",
                            "ext": ext,
                            "vcodec": f.get("vcodec", ""),
                            "acodec": f.get("acodec", ""),
                            "tbr": f.get("tbr"),
                            "vbr": f.get("vbr"),
                            "abr": f.get("abr"),
                            "fps": f.get("fps"),
                            "dynamic_range": f.get("dynamic_range", ""),
                            "filesize": f.get("filesize") or 0,
                            "filesize_approx": f.get("filesize_approx") or 0,
                        }
                    )

            self.info_ready.emit(parsed_data)
        except Exception as e:
            self.error_occurred.emit(f"مشكلة في تحليل الرابط: {str(e)}")
