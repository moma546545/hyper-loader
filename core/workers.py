
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import shutil
from datetime import datetime
from contextlib import suppress

from .error_handler import format_background_task_error, is_timeout_exception
from .media_size import compact_formats
from .retry_utils import is_retryable_error_text, run_with_retries

try:
    from .cookie_importer import decrypt_cookie_file, is_encrypted_cookie_file, _harden_windows_file_permissions
except Exception:
    decrypt_cookie_file = None
    is_encrypted_cookie_file = None
    _harden_windows_file_permissions = None

try:
    from yt_dlp import YoutubeDL
except Exception:
    YoutubeDL = None

try:
    from PySide6.QtCore import QThread, Signal
except ImportError:
    from PyQt6.QtCore import QThread, pyqtSignal as Signal


logger = logging.getLogger("SnapDownloader.Workers")
_ANALYZE_RETRY_BACKOFF_SECONDS = (1.0, 2.0)
_FORMAT_PROBE_TIMEOUT_SECONDS = 90
_FFMPEG_CONVERSION_TIMEOUT_SECONDS = 1800
_FFMPEG_THUMBNAIL_TIMEOUT_SECONDS = 120
_MAX_COOKIE_FILE_BYTES = 20 * 1024 * 1024
_PLAYLIST_FALLBACK_TIMEOUT_SECONDS = 120
_PLAYLIST_FALLBACK_MAX_ENTRIES = max(
    50,
    min(1000, int(os.getenv("SNAPDOWNLOADER_ANALYZE_FALLBACK_PLAYLIST_MAX_ENTRIES", "250") or 250)),
)
_ENABLE_PLAYLIST_JSON_FALLBACK = str(
    os.getenv("SNAPDOWNLOADER_ENABLE_PLAYLIST_JSON_FALLBACK", "1")
).strip().lower() in {"1", "true", "yes", "on"}
_WORKER_DENIED_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
}


class _CommandResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = int(returncode)
        self.stdout = str(stdout or "")
        self.stderr = str(stderr or "")


def _is_safe_extra_arg_value(value: str) -> bool:
    text = str(value or "")
    if not text or len(text) > 2048:
        return False
    return "\r" not in text and "\n" not in text and "\x00" not in text


def _parse_safe_header(value: str) -> tuple[str, str] | None:
    text = str(value or "").strip()
    if ":" not in text:
        return None
    name, header_value = text.split(":", 1)
    name = name.strip()
    header_value = header_value.strip()
    if not name or not header_value:
        return None
    if not re.fullmatch(r"[A-Za-z0-9-]+", name):
        return None
    if name.lower() in _WORKER_DENIED_HEADERS or not _is_safe_extra_arg_value(header_value):
        return None
    return name, header_value


def _sanitize_worker_extra_args(args) -> list[str]:
    values = [str(value or "").strip() for value in (args or []) if str(value or "").strip()]
    safe_args: list[str] = []
    i = 0
    while i < len(values):
        arg = values[i]
        next_value = values[i + 1] if i + 1 < len(values) else ""

        def _consume(flag: str, value: str, *, validator=None):
            if not value:
                return False
            if validator is not None and not validator(value):
                return False
            safe_args.extend([flag, value])
            return True

        if arg in {"--proxy", "--user-agent", "--impersonate"}:
            if _consume(arg, next_value, validator=_is_safe_extra_arg_value):
                i += 2
                continue
        elif arg == "--extractor-args":
            parsed = _parse_safe_extractor_args(next_value)
            if parsed is not None:
                safe_args.extend(["--extractor-args", parsed["raw"]])
                i += 2
                continue
        elif arg.startswith("--proxy="):
            value = arg.split("=", 1)[1].strip()
            if _is_safe_extra_arg_value(value):
                safe_args.extend(["--proxy", value])
        elif arg.startswith("--user-agent="):
            value = arg.split("=", 1)[1].strip()
            if _is_safe_extra_arg_value(value):
                safe_args.extend(["--user-agent", value])
        elif arg.startswith("--impersonate="):
            value = arg.split("=", 1)[1].strip()
            if _is_safe_extra_arg_value(value) and re.fullmatch(r"[A-Za-z0-9._:-]+", value):
                safe_args.extend(["--impersonate", value])
        elif arg.startswith("--extractor-args="):
            parsed = _parse_safe_extractor_args(arg.split("=", 1)[1].strip())
            if parsed is not None:
                safe_args.extend(["--extractor-args", parsed["raw"]])
        elif arg == "--add-header":
            header = _parse_safe_header(next_value)
            if header is not None:
                safe_args.extend(["--add-header", f"{header[0]}:{header[1]}"])
                i += 2
                continue
        elif arg.startswith("--add-header="):
            header = _parse_safe_header(arg.split("=", 1)[1].strip())
            if header is not None:
                safe_args.extend(["--add-header", f"{header[0]}:{header[1]}"])
        elif arg in {"--sleep-interval", "--max-sleep-interval", "--sleep-requests"}:
            try:
                parsed = float(next_value)
            except Exception:
                parsed = -1.0
            if parsed >= 0:
                safe_args.extend([arg, f"{parsed:.2f}"])
                i += 2
                continue
        elif arg.startswith("--sleep-interval=") or arg.startswith("--max-sleep-interval=") or arg.startswith("--sleep-requests="):
            flag, raw_value = arg.split("=", 1)
            try:
                parsed = float(raw_value.strip())
            except Exception:
                parsed = -1.0
            if parsed >= 0:
                safe_args.extend([flag, f"{parsed:.2f}"])
        i += 1
    return safe_args


def _parse_safe_extractor_args(value: str) -> dict | None:
    text = str(value or "").strip()
    if not text or not _is_safe_extra_arg_value(text):
        return None
    match = re.fullmatch(r"([A-Za-z0-9_]+):([A-Za-z0-9_]+)=([A-Za-z0-9_,.-]+)", text)
    if match is None:
        return None
    extractor, key, payload = match.group(1).lower(), match.group(2).lower(), match.group(3)
    if extractor != "youtube":
        return None
    allowed_keys = {"player_client", "player_skip"}
    if key not in allowed_keys:
        return None
    values = [part.strip().lower() for part in payload.split(",") if part.strip()]
    if not values:
        return None
    allowed_player_clients = {"web", "web_safari", "android", "ios", "tv"}
    allowed_player_skip = {"webpage", "configs"}
    if key == "player_client" and any(item not in allowed_player_clients for item in values):
        return None
    if key == "player_skip" and any(item not in allowed_player_skip for item in values):
        return None
    unique_values: list[str] = []
    for item in values:
        if item not in unique_values:
            unique_values.append(item)
    return {"extractor": extractor, "key": key, "values": unique_values, "raw": f"{extractor}:{key}={','.join(unique_values)}"}


def _worker_extra_args_to_ytdlp_options(args) -> dict:
    values = _sanitize_worker_extra_args(args)
    options: dict = {}
    headers: dict[str, str] = {}
    i = 0
    while i < len(values):
        arg = values[i]
        next_value = values[i + 1] if i + 1 < len(values) else ""
        if arg == "--proxy" and next_value:
            options["proxy"] = next_value
            i += 2
            continue
        if arg == "--user-agent" and next_value:
            headers["User-Agent"] = next_value
            i += 2
            continue
        if arg == "--impersonate" and next_value:
            options["impersonate"] = next_value
            i += 2
            continue
        if arg == "--add-header" and next_value:
            parsed = _parse_safe_header(next_value)
            if parsed is not None:
                headers[parsed[0]] = parsed[1]
            i += 2
            continue
        if arg == "--extractor-args" and next_value:
            parsed = _parse_safe_extractor_args(next_value)
            if parsed is not None:
                options.setdefault("extractor_args", {}).setdefault(
                    parsed["extractor"], {}
                )[parsed["key"]] = list(parsed["values"])
            i += 2
            continue
        if arg == "--sleep-interval" and next_value:
            with suppress(Exception):
                options["sleep_interval"] = float(next_value)
            i += 2
            continue
        if arg == "--max-sleep-interval" and next_value:
            with suppress(Exception):
                options["max_sleep_interval"] = float(next_value)
            i += 2
            continue
        if arg == "--sleep-requests" and next_value:
            with suppress(Exception):
                options["sleep_interval_requests"] = float(next_value)
            i += 2
            continue
        i += 1
    if headers:
        options["http_headers"] = headers
    return options


class _PreparedCookieFile:
    def __init__(self, path: str):
        self._source_path = str(path or "").strip()
        self._prepared_path: str | None = None
        self._temp_path = ""
        self._temp_signature: tuple[int, int] | None = None

    def prepare(self) -> str:
        if self._prepared_path is not None:
            return self._prepared_path
        if not self._source_path:
            self._prepared_path = ""
            return ""
        full = os.path.abspath(self._source_path)
        if not os.path.isfile(full):
            logger.warning("تم تجاهل cookies file غير صالح أثناء التحليل")
            self._prepared_path = ""
            return ""
        try:
            if os.path.getsize(full) > _MAX_COOKIE_FILE_BYTES:
                logger.warning("تم تجاهل cookies file لأن حجمه كبير جداً أثناء التحليل")
                self._prepared_path = ""
                return ""
        except Exception as exc:
            logger.warning(f"تعذر التحقق من حجم cookies file أثناء التحليل: {exc}")
            self._prepared_path = ""
            return ""

        if is_encrypted_cookie_file is not None and decrypt_cookie_file is not None:
            try:
                if is_encrypted_cookie_file(full):
                    plain = decrypt_cookie_file(full)
                    fd, tmp = tempfile.mkstemp(prefix="viddl_worker_cookies_", suffix=".txt")
                    try:
                        if os.name != "nt":
                            with suppress(OSError):
                                os.fchmod(fd, 0o600)
                        if _harden_windows_file_permissions is not None:
                            with suppress(Exception):
                                _harden_windows_file_permissions(tmp)
                        with os.fdopen(fd, "wb") as handle:
                            fd = None
                            handle.write(plain)
                    finally:
                        if fd is not None:
                            with suppress(OSError):
                                os.close(fd)
                    with suppress(Exception):
                        os.chmod(tmp, 0o600)
                    self._temp_path = tmp
                    try:
                        st = os.stat(tmp, follow_symlinks=False)
                        self._temp_signature = (int(st.st_dev), int(st.st_ino))
                    except Exception:
                        self._temp_signature = None
                    self._prepared_path = tmp
                    return tmp
            except Exception as exc:
                logger.warning(f"تعذر فك تشفير cookies file أثناء التحليل: {exc}")
                self._prepared_path = ""
                return ""

        self._prepared_path = full
        return full

    def cleanup(self) -> None:
        if not self._temp_path:
            return
        temp_path = self._temp_path
        expected_sig = self._temp_signature
        fd = None
        try:
            try:
                st = os.stat(temp_path, follow_symlinks=False)
                current_sig = (int(st.st_dev), int(st.st_ino))
            except Exception:
                current_sig = None
            if expected_sig is not None and current_sig is not None and current_sig != expected_sig:
                logger.warning("تم تخطي تنظيف ملف كوكيز مؤقت بسبب تغيّر هوية الملف (TOCTOU guard).")
                return
            open_flags = os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                open_flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(temp_path, open_flags)
            if expected_sig is not None:
                try:
                    fd_sig = os.fstat(fd)
                    if (int(fd_sig.st_dev), int(fd_sig.st_ino)) != expected_sig:
                        logger.warning("تم إيقاف مسح ملف كوكيز مؤقت لأن واصف الملف لا يطابق التوقيع المتوقع.")
                        return
                except Exception:
                    return
            size = max(0, int(os.fstat(fd).st_size))
            if size > 0:
                chunk = b"\x00" * min(size, 1024 * 1024)
                remaining = size
                while remaining > 0:
                    written = os.write(fd, chunk[: min(len(chunk), remaining)])
                    if written <= 0:
                        break
                    remaining -= written
        except Exception:
            pass
        finally:
            if fd is not None:
                with suppress(Exception):
                    os.close(fd)
        with suppress(Exception):
            os.remove(temp_path)
        self._temp_path = ""
        self._temp_signature = None
        self._prepared_path = None


def _terminate_process(process) -> None:
    if process is None:
        return
    with suppress(Exception):
        process.terminate()
        process.wait(timeout=2)
        return
    with suppress(Exception):
        process.kill()


class _InterruptibleWorkerThread(QThread):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop_event = threading.Event()
        self._process_lock = threading.Lock()
        self._current_process = None

    def _is_stop_requested(self) -> bool:
        return self._stop_event.is_set() or self.isInterruptionRequested()

    def _set_current_process(self, process) -> None:
        with self._process_lock:
            self._current_process = process

    def _clear_current_process(self, process=None) -> None:
        with self._process_lock:
            if process is None or self._current_process is process:
                self._current_process = None

    def request_stop(self) -> None:
        self._stop_event.set()
        self.requestInterruption()
        with self._process_lock:
            process = self._current_process
        if process is not None:
            _terminate_process(process)

    def stop(self) -> None:
        self.request_stop()

    def wait_for_stop(self, timeout_ms: int = 5000) -> bool:
        if not self.isRunning():
            return True
        if QThread.currentThread() is self:
            return False
        try:
            return bool(self.wait(max(0, int(timeout_ms or 0))))
        except Exception:
            return False

    def _run_process(self, command, *, timeout_seconds: float):
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = None
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
            self._set_current_process(process)
            deadline = time.monotonic() + max(1.0, float(timeout_seconds or 1.0))
            while True:
                if self._is_stop_requested():
                    _terminate_process(process)
                    return _CommandResult(130, "", "Worker cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _terminate_process(process)
                    raise TimeoutError("Worker timed out")
                try:
                    stdout, stderr = process.communicate(timeout=min(0.5, remaining))
                    return _CommandResult(process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    continue
        finally:
            self._clear_current_process(process)


class AnalyzeWorker(_InterruptibleWorkerThread):
    finished = Signal(bool, str, dict, list)
    playlist_chunk = Signal(dict, list)

    def __init__(self, url: str, cookies_file: str = "", extra_args=None):
        super().__init__()
        self.raw_input = str(url or "").strip()
        self.url, self._is_keyword_search = self._normalize_analyze_target(self.raw_input)
        self.cookies_file = str(cookies_file or "").strip()
        self.extra_args = _sanitize_worker_extra_args(extra_args)

    @staticmethod
    def _normalize_analyze_target(value: str) -> tuple[str, bool]:
        text = str(value or "").strip()
        if not text:
            return "", False
        lowered = text.lower()
        if "://" in text or lowered.startswith(("ytsearch:", "ytsearch1:", "ytsearch10:")):
            return text, False
        if lowered.startswith(("www.", "youtube.com/", "m.youtube.com/", "youtu.be/")):
            return text, False
        # Accept plain search keywords by mapping to yt-dlp search syntax.
        return f"ytsearch1:{text}", True

    @staticmethod
    def _first_playlist_entry(data: dict):
        entries = data.get("entries")
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if isinstance(entry, dict):
                return entry
        return None

    def _build_single_payload(self, data: dict):
        if not isinstance(data, dict):
            data = {}
        webpage_url = str(data.get("webpage_url") or "").strip()
        stream_url = str(data.get("url") or "").strip()
        video_id = str(data.get("id") or "").strip()
        if not webpage_url:
            if stream_url.startswith("http"):
                webpage_url = stream_url
            elif video_id:
                webpage_url = f"https://www.youtube.com/watch?v={video_id}"
            elif not self._is_keyword_search:
                webpage_url = self.url
        payload = {
            "kind": "single",
            "title": data.get("title") or "بدون عنوان",
            "channel": data.get("uploader") or data.get("channel") or "--",
            "views": f"{data.get('view_count'):,}" if isinstance(data.get("view_count"), int) else "--",
            "duration_seconds": int(data.get("duration") or 0),
            "thumbnail": data.get("thumbnail") or "",
            "categories": data.get("categories", []),
            "webpage_url": webpage_url,
            "stream_url": stream_url,
            "is_live": bool(data.get("is_live", False)),
            "was_live": bool(data.get("was_live", False)),
            "live_status": str(data.get("live_status") or ""),
            "video_id": video_id,
        }
        preview_stream_url = self._select_preview_stream_url(data.get("formats"))
        if preview_stream_url:
            payload["preview_stream_url"] = preview_stream_url
        if data.get("filesize"):
            payload["filesize"] = data.get("filesize")
        if data.get("filesize_approx"):
            payload["filesize_approx"] = data.get("filesize_approx")
        formats = compact_formats(data.get("formats"))
        if formats:
            payload["formats"] = formats
        return payload

    @staticmethod
    def _select_preview_stream_url(formats: list | tuple | None) -> str:
        candidates = []
        for fmt in formats or []:
            if not isinstance(fmt, dict):
                continue
            url = str(fmt.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            vcodec = str(fmt.get("vcodec") or "").lower()
            acodec = str(fmt.get("acodec") or "").lower()
            if vcodec in {"", "none"} or acodec in {"", "none"}:
                continue
            ext = str(fmt.get("ext") or "").lower()
            height = int(fmt.get("height") or 0)
            preference = 0 if ext == "mp4" else 1
            low_res_bonus = 0 if 0 < height <= 480 else 1
            candidates.append((low_res_bonus, preference, max(0, height), url))
        if not candidates:
            return ""
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        return str(candidates[0][3] or "")

    def _run_json(self, command, timeout_seconds: float = 120):
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = None
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
            self._set_current_process(process)
            deadline = time.monotonic() + max(1.0, float(timeout_seconds or 120))
            check_interval = 0.5
            while True:
                if self._is_stop_requested():
                    _terminate_process(process)
                    return _CommandResult(130, "", "تم إلغاء تحليل الرابط.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _terminate_process(process)
                    return _CommandResult(124, "", "انتهت مهلة تحليل الرابط. حاول مرة أخرى أو جرّب رابط أقصر.")
                try:
                    stdout, stderr = process.communicate(timeout=min(check_interval, remaining))
                    return _CommandResult(process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    continue
        except Exception as exc:
            _terminate_process(process)
            return _CommandResult(
                1,
                "",
                format_background_task_error(exc, "تحليل الرابط"),
            )
        finally:
            self._clear_current_process(process)

    def _run_json_with_retries(self, command, timeout_seconds: float):
        def _attempt(_attempt_number: int, _total_attempts: int):
            result = self._run_json(command, timeout_seconds=timeout_seconds)
            if result.returncode in {0, 130, 124}:
                return result
            error_text = result.stderr.strip() or result.stdout.strip()
            if is_retryable_error_text(error_text):
                raise RuntimeError(error_text or "retryable analyze error")
            return result

        try:
            return run_with_retries(
                "analyze worker",
                _attempt,
                _ANALYZE_RETRY_BACKOFF_SECONDS,
                should_retry_exception=lambda exc: (
                    is_timeout_exception(exc) or is_retryable_error_text(str(exc))
                ),
                logger=logger,
                sleep_func=time.sleep,
                should_abort=self._is_stop_requested,
                abort_error_factory=lambda: InterruptedError("تم إلغاء تحليل الرابط."),
            )
        except InterruptedError:
            return _CommandResult(130, "", "تم إلغاء تحليل الرابط.")
        except Exception as exc:
            return _CommandResult(1, "", format_background_task_error(exc, "تحليل الرابط"))

    def _is_playlist(self, data: dict):
        has_entries = isinstance(data.get("entries"), list) and len(data.get("entries", [])) > 0
        data_type = str(data.get("_type", "")).lower()
        extractor_key = str(data.get("extractor_key", "")).lower()
        extractor_name = str(data.get("extractor", "")).lower()
        has_playlist_fields = any(k in data for k in ["playlist_count", "playlist_id", "playlist_title"])
        return (
            data_type in {"playlist", "multi_video"}
            or has_entries
            or has_playlist_fields
            or ("playlist" in extractor_key)
            or ("playlist" in extractor_name)
        )

    def _normalize_playlist(self, data: dict):
        normalized = []
        entries = data.get("entries", []) if isinstance(data, dict) else []
        for index, entry in enumerate(entries, start=1):
            normalized_entry = self._normalize_playlist_entry(entry, index)
            if normalized_entry:
                normalized.append(normalized_entry)
        return normalized

    def _normalize_playlist_entry(self, entry: dict, index: int):
        if not isinstance(entry, dict):
            return None
        video_id = entry.get("id") or entry.get("url") or ""
        webpage_url = entry.get("webpage_url") or ""
        if webpage_url:
            actual_url = webpage_url
        elif isinstance(video_id, str) and video_id.startswith("http"):
            actual_url = video_id
        else:
            actual_url = f"https://www.youtube.com/watch?v={video_id}" if video_id else ""
        if not actual_url:
            return None
        thumb = entry.get("thumbnail") or ""
        if not thumb and isinstance(video_id, str) and video_id and "http" not in video_id:
            thumb = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        payload = {
            "index": index,
            "id": str(video_id or "").strip(),
            "entry_id": str(video_id or "").strip(),
            "video_id": str(video_id or "").strip(),
            "title": entry.get("title") or f"Item {index}",
            "url": actual_url,
            "thumbnail": thumb,
            "duration_seconds": int(entry.get("duration") or 0),
            "is_live": bool(entry.get("is_live", False)),
            "was_live": bool(entry.get("was_live", False)),
            "live_status": str(entry.get("live_status") or ""),
        }
        if entry.get("filesize"):
            payload["filesize"] = entry.get("filesize")
        if entry.get("filesize_approx"):
            payload["filesize_approx"] = entry.get("filesize_approx")
        formats = compact_formats(entry.get("formats"))
        if formats:
            payload["formats"] = formats
        return payload

    def _playlist_ytdlp_options(self, safe_cookies: str) -> dict:
        opts = {
            "extract_flat": "in_playlist",
            "lazy_playlist": True,
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": False,
            "socket_timeout": 30,
            "retries": 10,
            "extractor_retries": 5,
        }
        if safe_cookies:
            opts["cookiefile"] = safe_cookies
        opts.update(_worker_extra_args_to_ytdlp_options(self.extra_args))
        return opts

    def _run_playlist_incremental(self, safe_cookies: str):
        if YoutubeDL is None:
            return None
        try:
            with YoutubeDL(self._playlist_ytdlp_options(safe_cookies)) as ydl:
                data = ydl.extract_info(self.url, download=False)
        except Exception as exc:
            logger.debug(f"تعذر استخدام yt-dlp API لتحليل البلاي ليست: {exc}")
            err_str = str(exc).lower()
            if any(k in err_str for k in ["sign in", "bot", "cookie", "private", "unavailable", "captcha", "login", "drm", "geo-restricted", "age-restricted", "premium"]):
                return False, str(exc), {"kind": "playlist", "title": "Error"}
            return None
        if not isinstance(data, dict) or not self._is_playlist(data):
            return None

        payload = {
            "kind": "playlist",
            "title": data.get("title") or "Playlist",
            "url": str(data.get("webpage_url") or self.url or "").strip(),
            "webpage_url": str(data.get("webpage_url") or self.url or "").strip(),
        }
        entries = data.get("entries") or []
        total_items = 0
        chunk: list[dict] = []
        for raw_entry in entries:
            if self._is_stop_requested():
                return False, "تم إلغاء تحليل الرابط.", payload
            total_items += 1
            normalized = self._normalize_playlist_entry(raw_entry, total_items)
            if normalized is None:
                continue
            chunk.append(normalized)
            if len(chunk) >= 100:
                self.playlist_chunk.emit(payload, list(chunk))
                chunk.clear()
        if chunk:
            self.playlist_chunk.emit(payload, list(chunk))
        if total_items <= 0:
            return False, "تم فتح البلاي ليست لكن بدون عناصر قابلة للعرض.", payload
        return True, f"تم تحليل {total_items} عنصر", payload

    def _single_ytdlp_options(self, safe_cookies: str) -> dict:
        opts = {
            "extract_flat": False,
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
            "retries": 10,
            "extractor_retries": 5,
        }
        if safe_cookies:
            opts["cookiefile"] = safe_cookies
        opts.update(_worker_extra_args_to_ytdlp_options(self.extra_args))
        return opts

    def _run_single_incremental(self, safe_cookies: str):
        if YoutubeDL is None:
            return None
        try:
            with YoutubeDL(self._single_ytdlp_options(safe_cookies)) as ydl:
                data = ydl.extract_info(self.url, download=False)
        except Exception as exc:
            logger.debug(f"تعذر استخدام yt-dlp API لتحليل الرابط المفرد: {exc}")
            err_str = str(exc).lower()
            if any(k in err_str for k in ["sign in", "bot", "cookie", "private", "unavailable", "captcha", "login", "drm", "geo-restricted", "age-restricted", "premium"]):
                return False, str(exc), {"kind": "single"}
            return None
            
        if not isinstance(data, dict):
            return None

        if self._is_keyword_search and self._is_playlist(data):
            first = self._first_playlist_entry(data)
            if first is None:
                return False, "لم يتم العثور على نتائج للبحث.", {"kind": "single"}
            data = first

        payload = self._build_single_payload(data)
        return True, "تم تحليل الرابط بنجاح", payload

    def run(self):
        if not self.url:
            self.finished.emit(False, "الرابط مطلوب", {}, [])
            return
        prepared_cookies = _PreparedCookieFile(self.cookies_file)
        try:
            is_playlist_url = "list=" in self.url or "/playlist" in self.url
            safe_cookies = prepared_cookies.prepare()
            if is_playlist_url:
                streamed = self._run_playlist_incremental(safe_cookies)
                if streamed is not None:
                    success, message, payload = streamed
                    self.finished.emit(bool(success), message, payload or {}, [])
                    return
                if not _ENABLE_PLAYLIST_JSON_FALLBACK:
                    self.finished.emit(
                        False,
                        "تعذر التحليل التدريجي للبلاي ليست، وتم تعطيل fallback JSON لهذا الإصدار.",
                        {"kind": "playlist", "url": self.url},
                        [],
                    )
                    return
            else:
                streamed = self._run_single_incremental(safe_cookies)
                if streamed is not None:
                    success, message, payload = streamed
                    self.finished.emit(bool(success), message, payload or {}, [])
                    return
            command = [sys.executable, "-m", "yt_dlp", "--no-warnings"]
            if safe_cookies:
                command.extend(["--cookies", safe_cookies])
            if self.extra_args:
                command.extend(self.extra_args)
            if is_playlist_url:
                command.extend(
                    [
                        "--flat-playlist",
                        "--playlist-end",
                        str(_PLAYLIST_FALLBACK_MAX_ENTRIES),
                        "--dump-single-json",
                        self.url,
                    ]
                )
            else:
                command.extend(["-J", self.url])
            timeout_seconds = _PLAYLIST_FALLBACK_TIMEOUT_SECONDS if is_playlist_url else 60
            result = self._run_json_with_retries(command, timeout_seconds=timeout_seconds)
        finally:
            prepared_cookies.cleanup()
        if self._is_stop_requested() or result.returncode == 130:
            self.finished.emit(False, "تم إلغاء تحليل الرابط.", {}, [])
            return
        if result.returncode != 0:
            msg = result.stderr.strip() or result.stdout.strip() or "تعذر تحليل الرابط"
            self.finished.emit(False, msg, {}, [])
            return
        try:
            data = json.loads(result.stdout or "{}")
        except Exception:
            self.finished.emit(False, "البيانات المسترجعة غير صالحة", {}, [])
            return
        if not isinstance(data, dict):
            self.finished.emit(False, "البيانات المسترجعة غير صالحة", {}, [])
            return
        if self._is_keyword_search and self._is_playlist(data):
            first = self._first_playlist_entry(data)
            if first is None:
                self.finished.emit(False, "لم يتم العثور على نتائج للبحث.", {}, [])
                return
            self.finished.emit(True, "تم تحليل الرابط بنجاح", self._build_single_payload(first), [])
            return
        if self._is_playlist(data):
            items = self._normalize_playlist(data)
            if not items:
                self.finished.emit(False, "تم فتح البلاي ليست لكن بدون عناصر قابلة للعرض.", {}, [])
                return
            payload = {
                "kind": "playlist",
                "title": data.get("title") or "Playlist",
                "url": str(data.get("webpage_url") or self.url or "").strip(),
                "webpage_url": str(data.get("webpage_url") or self.url or "").strip(),
            }
            self.finished.emit(True, f"تم تحليل {len(items)} عنصر", payload, items)
            return
        payload = self._build_single_payload(data)
        self.finished.emit(True, "تم تحليل الرابط بنجاح", payload, [])


class FormatProbeWorker(_InterruptibleWorkerThread):
    finished = Signal(bool, str, str)

    def __init__(self, url: str, parent=None, cookies_file: str = "", extra_args=None):
        super().__init__(parent)
        self.url = str(url or "").strip()
        self.cookies_file = str(cookies_file or "").strip()
        self.extra_args = _sanitize_worker_extra_args(extra_args)

    def run(self):
        if not self.url:
            self.finished.emit(False, "", "الرابط مطلوب")
            return
        prepared_cookies = _PreparedCookieFile(self.cookies_file)
        try:
            cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings"]
            safe_cookies = prepared_cookies.prepare()
            if safe_cookies:
                cmd.extend(["--cookies", safe_cookies])
            if self.extra_args:
                cmd.extend(self.extra_args)
            cmd.extend(["-F", self.url])
            proc = self._run_process(cmd, timeout_seconds=_FORMAT_PROBE_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            self.finished.emit(False, "", format_background_task_error(exc, "فحص الصيغ"))
            return
        except Exception as exc:
            self.finished.emit(False, "", format_background_task_error(exc, "فحص الصيغ"))
            return
        finally:
            prepared_cookies.cleanup()
        if proc.returncode == 130:
            self.finished.emit(False, "", "تم إلغاء فحص الصيغ")
            return
        if proc.returncode != 0:
            self.finished.emit(False, "", (proc.stderr or proc.stdout or "تعذر جلب الصيغ").strip())
            return
        self.finished.emit(True, (proc.stdout or "").strip(), "")


class ConversionWorker(_InterruptibleWorkerThread):
    finished = Signal(bool, str, str)

    ALLOWED_FORMATS = {"mp4", "mkv", "avi", "mov", "mp3", "m4a", "flac", "wav", "webm", "ogg", "gif"}

    def __init__(self, input_path: str, output_format: str, output_path: str, parent=None):
        super().__init__(parent)
        self.input_path = os.path.abspath(str(input_path or "").strip())
        self.output_format = str(output_format or "").strip().lower()
        self.output_path = os.path.abspath(str(output_path or "").strip())

    def _build_command(self) -> list[str]:
        if not self.input_path or not os.path.isfile(self.input_path):
            raise FileNotFoundError("Input file not found.")
        if self.output_format not in self.ALLOWED_FORMATS:
            raise ValueError("Unsupported output format.")
        if not self.output_path:
            raise ValueError("Missing output path.")
        ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
        return [ffmpeg_bin, "-y", "-i", self.input_path, self.output_path]

    def run(self):
        try:
            cmd = self._build_command()
        except Exception as exc:
            self.finished.emit(False, self.output_path, str(exc))
            return
        try:
            proc = self._run_process(cmd, timeout_seconds=_FFMPEG_CONVERSION_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            self.finished.emit(False, self.output_path, format_background_task_error(exc, "تحويل الملف"))
            return
        except Exception as exc:
            self.finished.emit(False, self.output_path, format_background_task_error(exc, "تحويل الملف"))
            return
        if proc.returncode == 130:
            self.finished.emit(False, self.output_path, "تم إلغاء تحويل الملف")
            return
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "FFmpeg conversion failed.").strip()
            self.finished.emit(False, self.output_path, err)
            return
        if self.output_path and not os.path.exists(self.output_path):
            self.finished.emit(False, self.output_path, "Output file was not created.")
            return
        self.finished.emit(True, self.output_path, "")


class ThumbnailExtractWorker(_InterruptibleWorkerThread):
    finished = Signal(bool, str, str)

    def __init__(self, input_path: str, time_value: str, output_path: str, parent=None):
        super().__init__(parent)
        self.input_path = os.path.abspath(str(input_path or "").strip())
        self.time_value = str(time_value or "").strip() or "00:10"
        self.output_path = os.path.abspath(str(output_path or "").strip())

    def _build_command(self) -> list[str]:
        if not self.input_path or not os.path.isfile(self.input_path):
            raise FileNotFoundError("Input file not found.")
        if not self.output_path:
            raise ValueError("Missing output path.")
        ffmpeg_bin = shutil.which("ffmpeg") or "ffmpeg"
        return [
            ffmpeg_bin,
            "-y",
            "-ss",
            self.time_value,
            "-i",
            self.input_path,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            self.output_path,
        ]

    def run(self):
        try:
            cmd = self._build_command()
        except Exception as exc:
            self.finished.emit(False, self.output_path, str(exc))
            return
        try:
            proc = self._run_process(cmd, timeout_seconds=_FFMPEG_THUMBNAIL_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            self.finished.emit(False, self.output_path, format_background_task_error(exc, "استخراج الصورة المصغرة"))
            return
        except Exception as exc:
            self.finished.emit(False, self.output_path, format_background_task_error(exc, "استخراج الصورة المصغرة"))
            return
        if proc.returncode == 130:
            self.finished.emit(False, self.output_path, "تم إلغاء استخراج الصورة المصغرة")
            return
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "FFmpeg thumbnail extraction failed.").strip()
            self.finished.emit(False, self.output_path, err)
            return
        if self.output_path and not os.path.exists(self.output_path):
            self.finished.emit(False, self.output_path, "Output file was not created.")
            return
        if self.output_path and os.path.getsize(self.output_path) <= 0:
            self.finished.emit(False, self.output_path, "Output file is empty.")
            return
        self.finished.emit(True, self.output_path, "")
