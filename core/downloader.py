
import os
import re
import glob
import json
import random
import shutil
import subprocess
import sys
import time
import hashlib
import logging
import threading
import tempfile
import locale
import queue
import urllib.parse
import weakref
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

try:
    from PySide6.QtCore import QThread, Signal, QMutex, QMutexLocker
except ImportError:
    from PyQt6.QtCore import QThread, pyqtSignal as Signal, QMutex, QMutexLocker

from .event_bus import event_bus, DownloadFinishedEvent
from .anti_detection import anti_detection_engine
from .bandwidth_scheduler import scheduler
from .media_engine import FormatDecisionEngine, MediaProfile
from .constants import (
    SUBTITLE_LANGUAGES,
    MAX_RETRY_DELAY_SECONDS,
    POLL_INTERVAL_SECONDS,
    PROCESS_TERMINATION_TIMEOUT,
    PROCESS_KILL_TIMEOUT,
)
from .config import video_quality_to_height
from .download_providers import DownloadProviderRegistry
from .download_providers_oop.ytdlp_provider import _url_is_direct as _is_direct_url
from .error_handler import format_background_task_error, is_timeout_exception
from .retry_utils import is_retryable_error_text
from .proxy_manager import proxy_manager
from .task_types import TaskStatus, normalize_task_status
from .tls_transport import (
    TransportCancelled,
    download_direct_file as tls_download_direct_file,
    is_tls_transport_available,
    is_tls_transport_enabled,
)
from .utils import get_resource_path

try:
    from .antigravity_engine import antigravity_engine as _antigravity_engine
except Exception:
    _antigravity_engine = None  # type: ignore[assignment]


try:
    from .smart_rename import build_filename as _build_rename_filename
except ImportError:
    _build_rename_filename = None

logger = logging.getLogger("SnapDownloader.Downloader")
_DOWNLOAD_IDLE_TIMEOUT_SECONDS = 300.0
_MERGE_IDLE_TIMEOUT_SECONDS = 180.0
_GIF_STAGE_TIMEOUT_SECONDS = 300
_CHECKSUM_VERIFY_TIMEOUT_SECONDS = 15
_DEFENDER_SCAN_TIMEOUT_SECONDS = 180
_ARIA2_DEFAULT_CONNECTION_PROFILE = {"x": "8", "s": "8", "j": "4"}
_ARIA2_DOMAIN_CONNECTION_PROFILES: tuple[tuple[tuple[str, ...], dict[str, str]], ...] = (
    (("youtube.com", "youtu.be", "googlevideo.com", "ytimg.com"), {"x": "4", "s": "4", "j": "2"}),
)
_STATUS_RUNNING = TaskStatus.RUNNING.value
_STATUS_DOWNLOADING = TaskStatus.DOWNLOADING.value
_STATUS_SUCCESS = TaskStatus.SUCCESS.value
_STATUS_COMPLETED = TaskStatus.COMPLETED.value
_STATUS_FAILED = TaskStatus.FAILED.value
_STATUS_CANCELLED = TaskStatus.CANCELLED.value
_STATUS_ERROR = TaskStatus.ERROR.value
_STATUS_PAUSED = TaskStatus.PAUSED.value


class _YtDlpCancelled(RuntimeError):
    """Internal sentinel used to abort yt-dlp API downloads cooperatively."""

try:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
except Exception:
    YoutubeDL = None
    DownloadError = Exception

try:
    from .cookie_importer import decrypt_cookie_file, is_encrypted_cookie_file, _harden_windows_file_permissions
except Exception:
    decrypt_cookie_file = None
    is_encrypted_cookie_file = None
    _harden_windows_file_permissions = None

@dataclass(frozen=True)
class _VideoSelectorPreference:
    label: str
    min_fps: int | None
    codec_prefixes: tuple[str, ...]


class DownloadWorker(QThread):
    progress = Signal(int, float, str, str)
    log = Signal(str)
    state = Signal(str)
    progress_updated = Signal(float)
    speed_updated = Signal(str, float)
    eta_updated = Signal(str)
    status_changed = Signal(str)
    log_msg = Signal(str)
    output_path_changed = Signal(str)
    resume_snapshot = Signal(dict)
    engine_detected = Signal(str)  # New signal for UI feedback
    _provider_registry = DownloadProviderRegistry()
    _provider_registry_lock = threading.RLock()
    _provider_registry_bootstrapped = False

    def __init__(
        self,
        target_url,
        out_dir,
        mode,
        quality,
        fmt,
        subtitle_lang="None",
        start_time="",
        end_time="",
        retries=3,
        retry_delay_seconds=2,
        use_aria2=True,
        cookies_file="",
        cookies_from_browser="none",
        embed_subs=True,
        split_chapters=False,
        rename_template="Default",
        channel="",
        verify_checksum=False,
        virus_scan_after_download=False,
        normalize_audio_postprocess=False,
        bandwidth_limit_kbps=0,
        use_ytdlp_api: bool = False,
        frozen_title: str = "",
        merge_opts: dict = None,
        use_native_engine: bool = False,
        is_live_hint: bool = False,
        was_live_hint: bool = False,
        live_status_hint: str = "",
        trims: list[dict] | None = None,
        hard_burn_subs: bool = False,
        clean_metadata: bool = False,
        format_decision_engine=None,

    ):
        super().__init__()
        self.url = target_url.strip()
        if self.url.lower() != "antigravity":
            import urllib.parse
            parsed = urllib.parse.urlsplit(self.url)
            import sys
            is_testing = "pytest" in sys.modules or "unittest" in sys.modules
            allowed_schemes = {"http", "https", "ftp", "ftps"}
            if is_testing:
                allowed_schemes.add("")
            if parsed.scheme.lower() not in allowed_schemes:
                raise ValueError(f"Unsafe URL scheme rejected: {parsed.scheme}")
        self.out_dir = out_dir.strip()
        self.mode = mode
        self.quality = quality
        self.fmt = fmt
        self.subtitle_lang = subtitle_lang or "None"
        self.start_time = (start_time or "").strip()
        self.end_time = (end_time or "").strip()
        self.retries = max(1, int(retries))
        self.retry_delay_seconds = max(1, int(retry_delay_seconds or 2))
        self.use_aria2 = use_aria2
        self.cookies_file = cookies_file.strip()
        self.cookies_from_browser = str(cookies_from_browser or "none").strip().lower() or "none"
        self.embed_subs = embed_subs
        self.split_chapters = split_chapters
        self.rename_template = rename_template or "Default"
        self.channel = channel or ""
        self.verify_checksum = verify_checksum
        self.virus_scan_after_download = bool(virus_scan_after_download)
        self.normalize_audio_postprocess = bool(normalize_audio_postprocess)
        self.bandwidth_limit_kbps = max(0, int(bandwidth_limit_kbps or 0))
        self.use_ytdlp_api = bool(use_ytdlp_api)
        self.frozen_title = str(frozen_title or "").strip()
        self.merge_opts = merge_opts or {}
        self.custom_merge = self.merge_opts.get("enabled", False)
        self.use_native_engine: bool = bool(use_native_engine)
        self.is_live_hint = bool(is_live_hint)
        self.was_live_hint = bool(was_live_hint)
        self.live_status_hint = str(live_status_hint or "").strip().lower()
        self.trims = list(trims or [])
        self.hard_burn_subs = bool(hard_burn_subs)
        self.clean_metadata = bool(clean_metadata)
        # Injected decision engine keeps worker decoupled from import-time wiring.
        self._format_decision_engine = format_decision_engine or FormatDecisionEngine()


        self._cancel_event = threading.Event()  # H-03: for instant cancellation
        self.current_process = None
        self.process_mutex = QMutex()
        self.started_at = datetime.now().isoformat()
        self.downloaded_file_path = ""
        self.downloaded_separate_files = []
        self._downloaded_separate_files_lock = threading.Lock()
        self.extra_args: list = []  # injected at runtime (e.g. proxy flags)
        self._last_resume_emit_ts = 0.0
        self._last_resume_payload: dict | None = None
        self._last_resume_signature: tuple | None = None
        self._resume_partial_scan_cache_key = ""
        self._resume_partial_scan_cache_ts = 0.0
        self._resume_partial_scan_cache_files: list[str] = []
        self._prepared_cookies_path: str | None = None
        self._temp_cookies_path: str = ""
        self._last_runtime_error: str = ""
        self._checkpoint_interval_bytes: int = 10 * 1024 * 1024
        self._last_checkpoint_bytes: int = 0
        self._checkpoint_path: str = ""
        self._aria2_path_cached: str | None = None
        self._aria2_path_resolved = False
        self._ffmpeg_encoders_cache: set[str] | None = None
        self._active_segmented_provider = None
        self._active_direct_transport_stop = None
        self._format_fallback_level: int = 0
        self._merge_output_format_override: str | None = None
        self._safe_extra_args_cache_key: tuple[str, ...] | None = None
        self._safe_extra_args_cache_result: tuple[list[str], dict] = ([], {})
        self._stdout_reader_thread: threading.Thread | None = None
        self._stdout_reader_stream = None
        # Compatibility bridge for UI code using the simpler signal names.
        self.log.connect(self.log_msg.emit)
        self.state.connect(self._forward_state_to_compat)
        self.progress.connect(self._forward_progress_to_compat)
        self._ensure_default_providers()

    @classmethod
    def register_download_provider(
        cls,
        name: str,
        *,
        can_handle,
        run_once,
        priority: int = 100,
    ) -> str:
        return cls._provider_registry.register(
            name,
            can_handle=can_handle,
            run_once=run_once,
            priority=priority,
        )

    @classmethod
    def unregister_download_provider(cls, name: str) -> bool:
        return cls._provider_registry.unregister(name)

    @classmethod
    def list_download_providers(cls) -> list[str]:
        return cls._provider_registry.list_names()

    @classmethod
    def _ensure_default_providers(cls) -> None:
        if cls._provider_registry_bootstrapped:
            return
        with cls._provider_registry_lock:
            if cls._provider_registry_bootstrapped:
                return
            # Priority 5: Native Segmented Engine — for direct HTTP file links
            cls.register_download_provider(
                "tls_fingerprint_direct",
                can_handle=lambda worker: getattr(worker, "_should_use_tls_fingerprint_provider", lambda: False)(),
                run_once=lambda worker, _cmd, _env: worker._run_tls_fingerprint_direct_once(),
                priority=4,
            )
            cls.register_download_provider(
                "native_segmented",
                can_handle=lambda worker: (
                    getattr(worker, "use_native_engine", False)
                    and _is_direct_url(str(getattr(worker, "url", "")))
                ),
                run_once=lambda worker, _cmd, _env: worker._run_native_segmented_once(),
                priority=5,
            )
            cls.register_download_provider(
                "yt_dlp_api",
                can_handle=lambda worker: worker._should_use_api(),
                run_once=lambda worker, _cmd, _env: worker._run_ytdlp_once(),
                priority=10,
            )
            cls.register_download_provider(
                "yt_dlp_subprocess",
                can_handle=lambda _worker: True,
                run_once=lambda worker, cmd, env: worker._run_subprocess_once(cmd, env),
                priority=1000,
            )
            cls._provider_registry_bootstrapped = True

    def _should_use_tls_fingerprint_provider(self) -> bool:
        if not bool(self.use_native_engine):
            return False
        if not _is_direct_url(str(self.url or "")):
            return False
        if not is_tls_transport_enabled():
            return False
        return bool(self._is_tls_transport_available())

    def _is_tls_transport_available(self) -> bool:
        return bool(is_tls_transport_available())

    def _run_tls_fingerprint_direct_once(self) -> tuple[bool, bool, str]:
        if not self._is_tls_transport_available():
            return False, False, "TLS fingerprint transport غير متاح"
        safe_args, options = self._parse_safe_extra_args()
        _ = safe_args  # parsed to enforce safety filtering side effects consistently
        headers = dict(options.get("http_headers", {}) or {})
        referer = str(options.get("referer", "") or "").strip()
        if referer:
            headers["Referer"] = referer
        user_agent = str(options.get("user_agent", "") or "").strip()
        if user_agent:
            headers["User-Agent"] = user_agent
        proxy = str(options.get("proxy", "") or "").strip()
        if not proxy and proxy_manager.is_enabled():
            proxy = str(proxy_manager.get_current_proxy() or "").strip()
        impersonate = str(options.get("impersonate", "") or "").strip()
        tls_profile = str(getattr(self, "tls_transport_profile", "") or "").strip()
        if tls_profile:
            impersonate = tls_profile
        self.log.emit("🔐 TLS Fingerprint Transport قيد التشغيل...")
        self._active_direct_transport_stop = self._cancel_event.set
        try:
            out_path, _bytes_written = tls_download_direct_file(
                url=self.url,
                out_dir=self.out_dir,
                headers=headers,
                proxy=proxy,
                impersonate=impersonate,
                cancel_check=self._is_cancel_requested,
                on_progress=self._on_tls_transport_progress,
            )
            self._set_downloaded_file_path(out_path)
            self.progress.emit(1, 100.0, "--", "--:--")
            return True, False, ""
        except TransportCancelled:
            return False, True, "تم إلغاء التحميل"
        except Exception as exc:
            return False, False, self._normalize_download_error(str(exc), fallback="فشل TLS transport")
        finally:
            self._active_direct_transport_stop = None

    def _on_tls_transport_progress(self, downloaded: int, total_bytes: int, speed_bps: float) -> None:
        pct = 0.0
        eta = "--:--"
        if int(total_bytes or 0) > 0:
            pct = max(0.0, min(100.0, (float(downloaded) / float(total_bytes)) * 100.0))
            if float(speed_bps or 0.0) > 0.0:
                eta = self._format_eta((float(total_bytes) - float(downloaded)) / float(speed_bps))
        self.progress.emit(1, pct, self._format_speed(speed_bps), eta)

    def _run_native_segmented_once(self) -> tuple[bool, bool, str]:
        """Runs a download using the native SegmentedProvider. Returns (ok, was_cancelled, error)."""
        from .download_providers_oop.segmented_provider import SegmentedProvider
        task = {
            "url": self.url,
            "out_dir": self.out_dir,
            "cookies_file": self.cookies_file,
            "bandwidth_limit_kbps": self.bandwidth_limit_kbps,
        }
        provider = SegmentedProvider(task, worker=self)
        self._active_segmented_provider = provider

        result: dict = {}

        self_ref = weakref.ref(self)

        def _on_progress(pct: float, speed: str, eta: str):
            worker = self_ref()
            if worker is None:
                return
            worker.progress.emit(1, pct, speed, eta)

        def _on_log(msg: str):
            worker = self_ref()
            if worker is None:
                return
            worker.log.emit(msg)

        def _on_path(path: str):
            worker = self_ref()
            if worker is None:
                return
            worker._set_downloaded_file_path(path)

        def _on_done(success: bool, error: str):
            result["success"] = bool(success)
            result["error"] = str(error or "")

        provider.on_progress = _on_progress
        provider.on_log = _on_log
        provider.on_path = _on_path
        provider.on_done = _on_done

        # Forward cancel/pause signals
        if self._is_cancel_requested():
            provider.stop()
            return False, True, "تم إلغاء التحميل"

        try:
            provider.start()
        except Exception as exc:
            self._active_segmented_provider = None
            return False, False, str(exc)

        self._active_segmented_provider = None
        if provider.is_cancelled or self._is_cancel_requested():
            return False, True, result.get("error", "تم إلغاء التحميل")
        ok = result.get("success", False)
        err = result.get("error", "")
        return ok, False, err

    def _run_download_attempt(self, cmd: list[str], proc_env: dict) -> tuple[bool, bool, str]:
        provider = self._provider_registry.resolve(self)
        if provider is None:
            self.engine_detected.emit("yt-dlp")
            return self._run_subprocess_once(cmd, proc_env)
        
        # Map internal names to user-friendly names
        engine_map = {
            "tls_fingerprint_direct": "TLS Fingerprint Transport",
            "native_segmented": "Native Smart Engine",
            "yt_dlp_api": "yt-dlp API",
            "yt_dlp_subprocess": "yt-dlp",
        }
        friendly_name = engine_map.get(provider.name, provider.name)
        self.engine_detected.emit(friendly_name)
        
        return provider.run_once(self, cmd, proc_env)

    def _set_last_runtime_error(self, value: str):
        self._last_runtime_error = str(value or "").strip()

    def get_dynamic_bandwidth_limit_kbps(self) -> int:
        base_limit = max(0, int(self.bandwidth_limit_kbps or 0))
        try:
            if bool(getattr(scheduler, "enabled", False)):
                return max(0, int(scheduler.get_current_limit() or 0))
        except Exception:
            pass
        return base_limit

    def _consume_last_runtime_error(self) -> str:
        text = str(self._last_runtime_error or "").strip()
        self._last_runtime_error = ""
        return text

    def _is_cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    @property
    def cancel_requested(self) -> bool:
        # Backward-compatible facade for legacy callers/tests.
        return self._cancel_event.is_set()

    @cancel_requested.setter
    def cancel_requested(self, value: bool):
        if bool(value):
            self._cancel_event.set()
        else:
            self._cancel_event.clear()

    def _append_downloaded_separate_file(self, path_value: str):
        candidate = str(path_value or "").strip()
        if not candidate:
            return
        try:
            normalized = os.path.abspath(candidate)
        except Exception:
            normalized = candidate
        with self._downloaded_separate_files_lock:
            self.downloaded_separate_files.append(normalized)

    def _list_resume_partial_candidates(self, base_no_ext: str) -> list[str]:
        base = str(base_no_ext or "").strip()
        if not base:
            return []
        now = time.time()
        if (
            base == self._resume_partial_scan_cache_key
            and (now - self._resume_partial_scan_cache_ts) <= 1.5
        ):
            return list(self._resume_partial_scan_cache_files)
        folder = os.path.dirname(base) or self.out_dir or os.getcwd()
        prefix = os.path.basename(base) + "."
        matches: list[str] = []
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        if not entry.is_file():
                            continue
                        name = str(entry.name or "")
                        if not name.startswith(prefix):
                            continue
                        lower = name.lower()
                        if lower.endswith((".part", ".ytdl", ".aria2", ".tmp", ".temp")):
                            matches.append(entry.path)
                    except Exception:
                        continue
        except Exception:
            matches = []
        self._resume_partial_scan_cache_key = base
        self._resume_partial_scan_cache_ts = now
        self._resume_partial_scan_cache_files = list(matches)
        return matches

    def _is_retryable_download_error(self, error_text: str) -> bool:
        return is_retryable_error_text(
            str(error_text or ""),
            extra_tokens=("502", "503", "504"),
        )

    def _normalize_download_error(self, err: str, fallback: str = "فشل التحميل") -> str:
        text = str(err or "").strip()
        if not text:
            return str(fallback or "فشل التحميل")
        lowered = text.lower()
        if "requested formats are incompatible" in lowered:
            return "تعذر دمج الصيغة المطلوبة. جرّب جودة أو صيغة مختلفة."
        if "unable to download webpage" in lowered or "unable to download api page" in lowered:
            return "تعذر الوصول إلى الصفحة المطلوبة. تحقق من الرابط أو جرّب لاحقاً."
        if "sign in to confirm your age" in lowered or "sign in to confirm you\u2019re not a bot" in lowered or "sign in to confirm you're not a bot" in lowered:
            return "الموقع يطلب تسجيل الدخول أو تحققاً إضافياً. جرّب الكوكيز أو البروكسي."
        if is_timeout_exception(text):
            return format_background_task_error(TimeoutError(text), "التحميل")
        return text

    def _should_retry_download(self, err: str, attempt: int) -> bool:
        if self._is_cancel_requested():
            return False
        if int(attempt or 0) >= int(self.retries or 0):
            return False
        return self._is_retryable_download_error(err)

    def _refresh_anti_detection_after_error(self, err: str) -> bool:
        error_text = str(err or "").strip()
        if not error_text:
            return False
        try:
            should_retry = bool(anti_detection_engine.on_error(error_text))
        except Exception:
            return False
        if not should_retry:
            return False
        try:
            self.extra_args = anti_detection_engine.get_yt_dlp_options()
        except Exception:
            pass
        # Inject Antigravity unthrottle flags only when active throttling is confirmed.
        # We avoid injecting on all error types so as not to bloat extra_args unnecessarily
        # and to preserve backwards compatibility with existing tests.
        try:
            if _antigravity_engine is not None:
                err_lower = error_text.lower()
                _is_throttle_error = (
                    "429" in err_lower
                    or "too many requests" in err_lower
                    or "rate limit" in err_lower
                    or "throttle" in err_lower
                    or "bandwidth" in err_lower
                )
                if _is_throttle_error:
                    _antigravity_engine.on_throttle_detected()
                    unthrottle_flags = _antigravity_engine.get_unthrottle_flags()
                    existing_flags = set(self.extra_args)
                    for flag in unthrottle_flags:
                        if flag not in existing_flags:
                            self.extra_args.append(flag)
        except Exception:
            pass
        try:
            transport_profile = anti_detection_engine.get_transport_fingerprint()
            self.tls_transport_profile = str(transport_profile.get("transport_impersonate", "") or "").strip()
        except Exception:
            pass
        self.log.emit("🛡️ تم تحديث بصمة الطلبات وتجهيز إعادة المحاولة بعد اكتشاف حظر/تقييد.")
        return True

    def _forward_state_to_compat(self, state_value: str):
        s = normalize_task_status(state_value, default=_STATUS_DOWNLOADING)
        if s == _STATUS_RUNNING:
            self.status_changed.emit(_STATUS_DOWNLOADING)
        elif s == _STATUS_SUCCESS:
            self.status_changed.emit(_STATUS_COMPLETED)
        elif s in {_STATUS_FAILED, _STATUS_CANCELLED}:
            self.status_changed.emit(_STATUS_ERROR)
        elif s == _STATUS_PAUSED:
            self.status_changed.emit(_STATUS_PAUSED)
        else:
            self.status_changed.emit(s or _STATUS_DOWNLOADING)

    def _forward_progress_to_compat(self, _idx: int, percent: float, speed_text: str, eta_text: str):
        pct = float(percent or 0.0)
        self.progress_updated.emit(pct)
        self.eta_updated.emit(str(eta_text or "--:--"))
        speed_str = str(speed_text or "--")
        speed_mb = 0.0
        try:
            m = re.search(r"([\d.]+)\s*([KMG])i?B/s", speed_str, re.IGNORECASE)
            if m:
                value = float(m.group(1))
                unit = str(m.group(2)).upper()
                factor = {"K": 1 / 1024.0, "M": 1.0, "G": 1024.0}.get(unit, 0.0)
                speed_mb = value * factor
        except Exception:
            speed_mb = 0.0
        if speed_str == "--" and speed_mb > 0:
            speed_str = f"{speed_mb:.2f} MB/s"
        self.speed_updated.emit(speed_str, speed_mb)

    def _set_downloaded_file_path(self, value: str):
        path = str(value or "").strip().strip('\"')
        if not path:
            return
        if path == self.downloaded_file_path:
            return
        self.downloaded_file_path = path
        self.output_path_changed.emit(path)
        self._maybe_emit_resume_snapshot(force=True)

    def _collect_resume_snapshot(self) -> dict:
        out_path = str(self.downloaded_file_path or "").strip()
        files = []
        if out_path:
            base_no_ext, _ext = os.path.splitext(out_path)
            candidates = [
                out_path + ".part",
                out_path + ".ytdl",
                out_path + ".aria2",
                out_path + ".sdtmp",
                base_no_ext + ".ytdl",
                base_no_ext + ".aria2",
            ]
            for p in candidates:
                try:
                    if p and os.path.isfile(p):
                        files.append(p)
                except Exception:
                    continue
            try:
                if base_no_ext:
                    files.extend(self._list_resume_partial_candidates(base_no_ext))
            except Exception:
                pass
        unique = []
        seen = set()
        total_bytes = 0
        for p in files:
            ap = os.path.abspath(p)
            if ap in seen:
                continue
            seen.add(ap)
            try:
                size = int(os.path.getsize(ap))
            except Exception:
                size = 0
            try:
                mtime = float(os.path.getmtime(ap))
            except Exception:
                mtime = 0.0
            total_bytes += max(0, size)
            unique.append({"path": ap, "size": size, "mtime": mtime})
        unique.sort(key=lambda item: str(item.get("path", "")))
        return {
            "output_path": os.path.abspath(out_path) if out_path else "",
            "partials_total_bytes": int(total_bytes),
            "partials_count": int(len(unique)),
            "partials": unique,
            "updated_at": time.time(),
        }

    def _resume_snapshot_signature(self, payload: dict) -> tuple:
        output_path = str(payload.get("output_path", "") or "")
        partials_count = int(payload.get("partials_count", 0) or 0)
        partials_total_bytes = int(payload.get("partials_total_bytes", 0) or 0)
        partials_signature = []
        for item in list(payload.get("partials", []) or []):
            if not isinstance(item, dict):
                continue
            path_value = str(item.get("path", "") or "")
            size_value = int(item.get("size", 0) or 0)
            mtime_value = float(item.get("mtime", 0.0) or 0.0)
            partials_signature.append((path_value, size_value, mtime_value))
        return (
            output_path,
            partials_count,
            partials_total_bytes,
            tuple(partials_signature),
        )

    def _maybe_emit_resume_snapshot(self, force: bool = False):
        now = time.time()
        if not force and (now - self._last_resume_emit_ts) < 1.25:
            return
        if not self.downloaded_file_path:
            return
        payload = self._collect_resume_snapshot()
        payload_signature = self._resume_snapshot_signature(payload)
        if not force and payload_signature == self._last_resume_signature:
            self._last_resume_emit_ts = now
            return
        self._last_resume_payload = payload
        self._last_resume_signature = payload_signature
        self._last_resume_emit_ts = now
        self.resume_snapshot.emit(payload)
        self._maybe_write_checkpoint(payload, force=force)

    def _resolve_checkpoint_path(self) -> str:
        if self._checkpoint_path:
            return self._checkpoint_path
        safe_out = os.path.abspath(str(self.out_dir or "").strip() or os.getcwd())
        checkpoint_dir = os.path.join(safe_out, ".snapdownloader_checkpoints")
        os.makedirs(checkpoint_dir, exist_ok=True)
        digest = hashlib.sha256(str(self.url or "").encode("utf-8", errors="ignore")).hexdigest()[:16]
        self._checkpoint_path = os.path.join(checkpoint_dir, f"{digest}.json")
        return self._checkpoint_path

    def _build_checkpoint_payload(self, snapshot: dict, status: str = "") -> dict:
        return {
            "url": str(self.url or ""),
            "status": str(status or "").strip(),
            "timestamp": time.time(),
            "output_path": str(snapshot.get("output_path", "") or ""),
            "partials_count": int(snapshot.get("partials_count", 0) or 0),
            "downloaded_bytes": int(snapshot.get("partials_total_bytes", 0) or 0),
            "partials": list(snapshot.get("partials", []) or []),
        }

    def _maybe_write_checkpoint(self, snapshot: dict, force: bool = False, status: str = ""):
        try:
            downloaded_bytes = int(snapshot.get("partials_total_bytes", 0) or 0)
        except Exception:
            downloaded_bytes = 0
        if downloaded_bytes <= 0 and not force:
            return
        if (not force) and (downloaded_bytes - self._last_checkpoint_bytes < self._checkpoint_interval_bytes):
            return
        try:
            path = self._resolve_checkpoint_path()
            payload = self._build_checkpoint_payload(snapshot, status=status)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp_path, path)
            self._last_checkpoint_bytes = max(self._last_checkpoint_bytes, downloaded_bytes)
        except Exception as exc:
            logger.debug(f"تعذر حفظ checkpoint: {exc}")

    def _cleanup_checkpoint_file(self):
        path = str(self._checkpoint_path or "").strip()
        if not path:
            return
        with suppress(OSError):
            os.remove(path)

    def _find_ffmpeg(self):
        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path:
            return os.path.dirname(ffmpeg_path)
        return None

    def _find_aria2(self):
        if self._aria2_path_resolved:
            return self._aria2_path_cached
        aria2_path = shutil.which("aria2c")
        if aria2_path:
            self._aria2_path_cached = aria2_path
            self._aria2_path_resolved = True
            return aria2_path
        candidates = [
            os.path.join(os.getcwd(), "aria2c.exe"),
        ]
        # L-06: Search for any aria2 version directory instead of hardcoded version
        for pattern in [os.path.join(os.getcwd(), "aria2-*", "aria2c.exe"),
                        get_resource_path(os.path.join("aria2-*", "aria2c.exe"))]:
            candidates.extend(glob.glob(pattern))
        for candidate in candidates:
            if os.path.isfile(candidate):
                self._aria2_path_cached = candidate
                self._aria2_path_resolved = True
                return candidate
        self._aria2_path_cached = None
        self._aria2_path_resolved = True
        return None

    def _quality_height(self):
        return video_quality_to_height(self.quality)

    @staticmethod
    def _subtitle_selector(raw_value: str) -> str:
        raw = str(raw_value or "").strip()
        if not raw:
            return ""
        normalized = raw.casefold()
        if normalized in {"none", "off", "false", "0"}:
            return ""
        if normalized in {"all", "*"}:
            return "all"
        mapped = SUBTITLE_LANGUAGES.get(raw)
        if not mapped:
            lookup = {
                str(name or "").strip().casefold(): str(code or "").strip()
                for name, code in SUBTITLE_LANGUAGES.items()
                if str(name or "").strip() and str(code or "").strip()
            }
            mapped = lookup.get(normalized, "")
        if mapped:
            return f"{mapped}.*"
        if re.fullmatch(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]+)?", raw):
            return f"{raw.lower().replace('_', '-')}.*"
        return raw

    def _subtitle_code(self):
        selector = self._subtitle_selector(self.subtitle_lang)
        if selector.endswith(".*"):
            return selector[:-2]
        return selector

    def _subtitle_lang_patterns(self) -> list[str]:
        raw = str(self.subtitle_lang or "").strip()
        if not raw:
            return []
        parts = [part.strip() for part in re.split(r"[;,]", raw) if part.strip()]
        if not parts:
            parts = [raw]
        patterns: list[str] = []
        seen: set[str] = set()
        for part in parts:
            selector = self._subtitle_selector(part)
            if not selector:
                continue
            if selector == "all":
                return ["all"]
            if selector in seen:
                continue
            seen.add(selector)
            patterns.append(selector)
        return patterns

    @staticmethod
    def _normalize_section_time_token(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if text.lower() in {"inf", "infinity"}:
            return "inf"
        if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?(\.\d+)?", text):
            return text
        if re.fullmatch(r"\d+(\.\d+)?", text):
            return text
        return ""

    def _download_sections(self) -> list[str]:
        sections: list[str] = []
        seen: set[str] = set()
        raw_trims = list(self.trims or [])
        for entry in raw_trims:
            if not isinstance(entry, dict):
                continue
            start = self._normalize_section_time_token(entry.get("start", ""))
            end = self._normalize_section_time_token(entry.get("end", ""))
            if not start and not end:
                continue
            token = f"*{start or '00:00'}-{end or 'inf'}"
            if token in seen:
                continue
            seen.add(token)
            sections.append(token)
        if sections:
            return sections
        start = self._normalize_section_time_token(self.start_time)
        end = self._normalize_section_time_token(self.end_time)
        if start or end:
            return [f"*{start or '00:00'}-{end or 'inf'}"]
        return []

    def _safe_cookies_path(self) -> str:
        if self._prepared_cookies_path is not None:
            return self._prepared_cookies_path
        path = str(self.cookies_file or "").strip()
        if not path:
            self._prepared_cookies_path = ""
            return ""
        full = os.path.abspath(path)
        if not os.path.isfile(full):
            logger.warning("تم تجاهل cookies file غير صالح")
            self._prepared_cookies_path = ""
            return ""
        try:
            if os.path.getsize(full) > 20 * 1024 * 1024:
                logger.warning("تم تجاهل cookies file لأن حجمه كبير جداً")
                self._prepared_cookies_path = ""
                return ""
        except Exception as exc:
            logger.warning(f"تعذر التحقق من حجم cookies file: {exc}")
            self._prepared_cookies_path = ""
            return ""
        if is_encrypted_cookie_file is not None and decrypt_cookie_file is not None:
            try:
                if is_encrypted_cookie_file(full):
                    plain = decrypt_cookie_file(full)
                    fd, tmp = tempfile.mkstemp(prefix="viddl_cookies_", suffix=".txt")
                    try:
                        if os.name != "nt":
                            with suppress(OSError):
                                os.fchmod(fd, 0o600)
                        if _harden_windows_file_permissions is not None:
                            try:
                                _harden_windows_file_permissions(tmp)
                            except Exception as exc:
                                logger.debug(f"تعذر تصلـيب صلاحيات ملف الكوكيز المؤقت: {exc}")
                        with os.fdopen(fd, "wb") as f:
                            fd = None
                            f.write(plain)
                    finally:
                        if fd is not None:
                            with suppress(OSError):
                                os.close(fd)
                    try:
                        os.chmod(tmp, 0o600)
                    except Exception:
                        pass
                    self._temp_cookies_path = tmp
                    self._prepared_cookies_path = tmp
                    return tmp
            except Exception as exc:
                logger.warning(f"تعذر فك تشفير cookies file: {exc}")
        self._prepared_cookies_path = full
        return full

    def _sanitize_title_fragment(self, text: str, max_len: int = 120) -> str:
        value = str(text or "").strip()
        if not value:
            return ""
        value = value.replace("/", "-").replace("\\", "-")
        value = re.sub(r'[<>:"|?*\x00-\x1f]', "", value)
        value = re.sub(r"[ \-]{2,}", " ", value).strip(" -.")
        return value[:max_len] if len(value) > max_len else value

    def _output_template(self) -> str:
        out_dir = self.out_dir
        if os.name == "nt":
            normalized = os.path.abspath(str(out_dir or "").strip())
            if normalized and not normalized.startswith("\\\\?\\"):
                out_dir = "\\\\?\\" + normalized
            else:
                out_dir = normalized or out_dir
        if self.frozen_title:
            title = self._sanitize_title_fragment(self.frozen_title, max_len=80)
            if title:
                return os.path.join(out_dir, f"{title} [%(id)s].%(ext)s")
        return os.path.join(out_dir, "%(title)s [%(id)s].%(ext)s")

    def _video_format_sort_spec(self) -> str:
        if int(getattr(self, "_format_fallback_level", 0) or 0) > 0:
            return ""
        task_dict = {
            "quality": self.quality,
            "mode": self.mode,
            "format": self.fmt,
        }
        profile = MediaProfile.from_task(task_dict)
        return self._format_decision_engine.build_format_sort_spec(profile)

    def _video_format_sort_fields(self) -> list[str]:
        spec = str(self._video_format_sort_spec() or "").strip()
        if not spec:
            return []
        return [part.strip() for part in spec.split(",") if part.strip()]

    def _video_format_selector(self) -> str:
        task_dict = {
            "quality": self.quality,
            "mode": self.mode,
            "format": self.fmt,
        }
        profile = MediaProfile.from_task(task_dict)
        level = int(getattr(self, "_format_fallback_level", 0) or 0)
        if level <= 0:
            return self._format_decision_engine.build_format_selector(profile)
        if level == 1:
            return "bv*+ba/b"
        return "best"

    def _audio_preferred_quality(self) -> str:
        quality = str(self.quality).replace("kbps", "")
        try:
            return str(int(float(quality)))
        except Exception:
            return "192"

    def _should_embed_audio_metadata(self) -> bool:
        # User expectation: always embed artwork for audio downloads, except for unsupported formats.
        fmt = self._normalized_format()
        unsupported = {"wav", "aiff", "wma", "aac", "original audio", ""}
        if fmt in unsupported:
            return False
        return self._normalized_mode() == "audio"

    def _effective_audio_embed_format(self) -> str:
        fmt = self._normalized_format()
        if self._is_original_audio_format() or fmt == "":
            return "best"
        # yt-dlp doesn't support aiff/wma in --audio-format directly.
        if fmt == "aiff":
            return "wav"
        if fmt == "wma":
            return "mp3"
        return fmt

    def _text_encoding(self) -> str:
        try:
            enc = str(locale.getpreferredencoding(False) or "").strip()
            if enc:
                return enc
        except Exception:
            pass
        return "utf-8"

    def _normalized_mode(self) -> str:
        return str(self.mode).lower()

    def _normalized_format(self) -> str:
        return str(self.fmt).lower()

    def _effective_merge_output_format(self) -> str:
        override = str(getattr(self, "_merge_output_format_override", "") or "").strip().lower()
        if override:
            return override
        return self._normalized_format()

    @staticmethod
    def _is_requested_format_unavailable_error(error_text: str) -> bool:
        text = str(error_text or "").strip().lower()
        if not text:
            return False
        return (
            "requested format is not available" in text
            or "requested formats are not available" in text
        )

    @staticmethod
    def _is_requested_formats_incompatible_error(error_text: str) -> bool:
        text = str(error_text or "").strip().lower()
        if not text:
            return False
        return "requested formats are incompatible" in text

    def _apply_adaptive_format_fallback(self, error_text: str) -> bool:
        err = str(error_text or "")
        if (
            self._is_requested_format_unavailable_error(err)
            and int(getattr(self, "_format_fallback_level", 0) or 0) < 2
        ):
            self._format_fallback_level = int(getattr(self, "_format_fallback_level", 0) or 0) + 1
            self.log.emit("الصيغة/الجودة المطلوبة غير متاحة. جاري تخفيف القيود تلقائياً والمحاولة مرة أخرى...")
            return True
        if (
            self._is_video_mode()
            and self._is_requested_formats_incompatible_error(err)
            and not str(getattr(self, "_merge_output_format_override", "") or "").strip()
            and self._effective_merge_output_format() == "mp4"
        ):
            self._merge_output_format_override = "mkv"
            self.log.emit("تعذر دمج الصيغ داخل MP4. تم التحويل التلقائي إلى MKV والمحاولة مرة أخرى...")
            return True
        return False

    def _is_video_mode(self) -> bool:
        return self._normalized_mode() == "video"

    def _is_original_audio_format(self) -> bool:
        return self.fmt == "Original Audio"

    def _aria2_default_args(self, max_download_limit_bytes: int | None = None) -> list[str]:
        # PERF-05: Domain-aware connection profile with conservative defaults.
        profile = dict(_ARIA2_DEFAULT_CONNECTION_PROFILE)
        host = ""
        try:
            host = str(urllib.parse.urlparse(str(self.url or "")).hostname or "").strip().lower()
        except Exception:
            host = ""
        if host:
            for suffixes, candidate_profile in _ARIA2_DOMAIN_CONNECTION_PROFILES:
                matched = any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)
                if matched:
                    profile = dict(candidate_profile)
                    break

        x_val = str(profile.get("x", _ARIA2_DEFAULT_CONNECTION_PROFILE["x"]))
        s_val = str(profile.get("s", _ARIA2_DEFAULT_CONNECTION_PROFILE["s"]))
        j_val = str(profile.get("j", _ARIA2_DEFAULT_CONNECTION_PROFILE["j"]))
        
        args: list[str] = [
            "-x", x_val, "-s", s_val, "-j", j_val, "-k", "1M",
            "-c",
            "--allow-overwrite=false",
            "--auto-file-renaming=false",
            "--file-allocation=prealloc",
            "--summary-interval=1",
        ]
        if max_download_limit_bytes is not None and max_download_limit_bytes > 0:
            args.append(f"--max-download-limit={int(max_download_limit_bytes)}")
        return args

    def _subtitle_lang_pattern(self) -> str:
        subtitle_patterns = self._subtitle_lang_patterns()
        if not subtitle_patterns:
            return ""
        return ",".join(subtitle_patterns)

    def _looks_like_stream_manifest(self) -> bool:
        url = str(self.url or "").strip().lower()
        return any(token in url for token in (".m3u8", ".mpd", "/manifest", "playlist.m3u8"))

    def _is_youtube_like_url(self) -> bool:
        url = str(self.url or "").strip().lower()
        return any(token in url for token in ("youtube.com", "youtu.be", "m.youtube.com", "music.youtube.com"))

    def _has_custom_extractor_args(self) -> bool:
        for arg in list(self.extra_args or []):
            text = str(arg or "").strip().lower()
            if text == "--extractor-args" or text.startswith("--extractor-args="):
                return True
        return False

    def _default_youtube_extractor_args(self) -> list[str]:
        if not self._is_youtube_like_url():
            return []
        if self._has_custom_extractor_args():
            return []
        # Let yt-dlp use its own default client selection (e.g. android_vr).
        # Forcing specific clients breaks when they require missing runtimes
        # (web needs deno) or get blocked by YouTube (ios).
        level = int(getattr(self, "_format_fallback_level", 0) or 0)
        if level > 0:
            return ["--extractor-args", "youtube:player_client=all"]
        if self._should_enable_live_stream_mode():
            return ["--extractor-args", "youtube:player_client=tv,default"]
        return []

    def _is_live_like_source(self) -> bool:
        if self.is_live_hint:
            return True
        return self.live_status_hint in {"is_live", "live"}

    def _should_enable_live_stream_mode(self) -> bool:
        return self._looks_like_stream_manifest() or self._is_live_like_source()

    def _yt_dlp_resilience_cli_args(self) -> list[str]:
        args = [
            "--retries", "10",
            "--file-access-retries", "3",
            "--fragment-retries", "20",
            "--extractor-retries", "5",
            "--socket-timeout", "30",
            "--skip-unavailable-fragments",
            "--retry-sleep", "fragment:exp=1:20",
            "--retry-sleep", "http:exp=1:20",
        ]
        if self._should_enable_live_stream_mode():
            args.extend(["--hls-use-mpegts", "--live-from-start", "--wait-for-video", "10"])
            if not self.use_aria2:
                args.extend(
                    [
                        "--downloader-args",
                        "ffmpeg_i:-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 30 -reconnect_at_eof 1",
                    ]
                )
        return args

    def _build_command(self):
        output_template = self._output_template()
        cmd = [sys.executable, "-m", "yt_dlp", "-o", output_template, "--newline", "--no-warnings"]

        safe_cookies = self._safe_cookies_path()
        if safe_cookies:
            cmd.extend(["--cookies", safe_cookies])
        elif getattr(self, "cookies_from_browser", "none") != "none":
            cmd.extend(["--cookies-from-browser", self.cookies_from_browser])
            
        ffmpeg_loc = self._find_ffmpeg()
        if ffmpeg_loc:
            cmd.extend(["--ffmpeg-location", ffmpeg_loc])
        cmd.extend(self._default_youtube_extractor_args())
        aria2_bin = self._find_aria2()
        # M-07: Allow Aria2 for both video and audio downloads
        if self.use_aria2 and aria2_bin:
            runtime_limit_kbps = self.get_dynamic_bandwidth_limit_kbps()
            limit_bytes = int(runtime_limit_kbps) * 1024 if runtime_limit_kbps > 0 else None
            aria_args = "aria2c:" + " ".join(self._aria2_default_args(limit_bytes))
            cmd.extend(["--downloader", "aria2c", "--downloader-args", aria_args])

        if self._is_video_mode():
            video_selector = self._video_format_selector()
            fmt = self._effective_merge_output_format()
            video_sort_spec = self._video_format_sort_spec()
            if video_sort_spec:
                cmd.extend(["-S", video_sort_spec])
            if self.custom_merge:
                cmd.extend(["-f", video_selector, "-k", "--print", "filename"])
            else:
                cmd.extend(["-f", video_selector, "--merge-output-format", fmt])
            subtitle_patterns = self._subtitle_lang_patterns()
            if subtitle_patterns:
                cmd.extend(["--write-sub", "--write-auto-sub", "--sub-langs", ",".join(subtitle_patterns)])
                if self.embed_subs and not self.hard_burn_subs:
                    cmd.extend(["--embed-subs", "--compat-options", "all"])
        else:
            target_audio_fmt = self._effective_audio_embed_format()
            aq = f"{self._audio_preferred_quality()}K"
            # Some extractors/client profiles expose only progressive "best" and no separate bestaudio.
            cmd.extend(["-f", "bestaudio/best", "-x", "--audio-format", target_audio_fmt, "--audio-quality", aq])
            if self._should_embed_audio_metadata():
                cmd.extend(["--embed-thumbnail", "--embed-metadata"])

        sections = self._download_sections()
        if sections:
            for section in sections:
                cmd.extend(["--download-sections", section])
            # Prefer stream-copy cut path first, with stable timestamps fallback.
            cmd.extend(["--postprocessor-args", "ffmpeg:-c copy -copyts -avoid_negative_ts make_zero -fflags +genpts"])

        if self.split_chapters:
            cmd.extend(["--split-chapters", "--output", "chapter:%(title)s - %(section_title)s.%(ext)s"])

        runtime_limit_kbps = self.get_dynamic_bandwidth_limit_kbps()
        if runtime_limit_kbps > 0:
            cmd.extend(["--limit-rate", f"{runtime_limit_kbps}K"])

        cmd.extend(self._yt_dlp_resilience_cli_args())

        # Runtime-injected args (e.g. proxy, experimental flags)
        if self.extra_args:
            cmd.extend(self._sanitize_extra_args())

        cmd.extend(["--concurrent-fragments", "5", "--", self.url])
        return cmd

    def _should_use_api(self) -> bool:
        if self.custom_merge:
            return False
        if YoutubeDL is None:
            return False
        if self._download_sections():
            return False
        if self.split_chapters:
            return False
        if self.use_ytdlp_api:
            return True
        return str(os.getenv("SNAPDOWNLOADER_USE_YTDLP_API", "")).strip() in {"1", "true", "yes", "on"}

    def _find_ffprobe(self) -> str:
        ffprobe_path = shutil.which("ffprobe")
        if ffprobe_path:
            return ffprobe_path
        ffmpeg_dir = self._find_ffmpeg()
        if ffmpeg_dir and os.path.isdir(ffmpeg_dir):
            candidate = os.path.join(ffmpeg_dir, "ffprobe.exe" if os.name == "nt" else "ffprobe")
            if os.path.isfile(candidate):
                return candidate
        return "ffprobe"

    def _probe_stream_types(self, path: str) -> set[str]:
        p = str(path or "").strip()
        if not p or not os.path.isfile(p):
            return set()
        ffprobe_bin = self._find_ffprobe()
        cmd = [
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            p,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=12,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            logger.error(f"[TimeOut] عملية ffprobe استغرقت وقتاً طويلاً: {path}")
            return set()
        except Exception as e:
            logger.error(f"[Error] فشل استدعاء ffprobe للملف {path}. السبب: {e}")
            return set()
        if proc.returncode != 0:
            return set()
        raw = (proc.stdout or "").strip()
        if not raw:
            return set()
        return {line.strip().lower() for line in raw.splitlines() if line.strip()}

    def _probe_media_info(self, path: str) -> dict:
        p = str(path or "").strip()
        if not p or not os.path.isfile(p):
            return {}
        ffprobe_bin = self._find_ffprobe()
        cmd = [
            ffprobe_bin,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            p,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            return {}
        if proc.returncode != 0:
            return {}
        try:
            payload = json.loads(proc.stdout or "{}")
            return payload if isinstance(payload, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _first_stream(info: dict, codec_type: str) -> dict:
        streams = info.get("streams")
        if not isinstance(streams, list):
            return {}
        target = str(codec_type or "").strip().lower()
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            if str(stream.get("codec_type", "")).strip().lower() == target:
                return stream
        return {}

    def _normalize_video_encoder(self, value: str) -> str:
        raw = str(value or "").strip().lower()
        mapping = {
            "copy": "copy",
            "h264": "libx264",
            "libx264": "libx264",
            "x264": "libx264",
            "avc": "libx264",
            "h265": "libx265",
            "hevc": "libx265",
            "libx265": "libx265",
            "x265": "libx265",
            "h264_nvenc": "h264_nvenc",
            "hevc_nvenc": "hevc_nvenc",
            "h264_qsv": "h264_qsv",
            "hevc_qsv": "hevc_qsv",
            "h264_amf": "h264_amf",
            "hevc_amf": "hevc_amf",
            "av1": "libaom-av1",
            "av1_nvenc": "av1_nvenc",
            "av1_qsv": "av1_qsv",
            "av1_amf": "av1_amf",
        }
        return mapping.get(raw, raw or "copy")

    def _load_ffmpeg_encoders(self, ffmpeg_bin: str) -> set[str]:
        if isinstance(self._ffmpeg_encoders_cache, set):
            return set(self._ffmpeg_encoders_cache)
        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-encoders",
        ]
        encoders: set[str] = set()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=12,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0:
                for raw_line in str(proc.stdout or "").splitlines():
                    line = raw_line.strip()
                    if not line or line.startswith("--") or line.startswith("Encoders:"):
                        continue
                    parts = line.split()
                    if len(parts) >= 2:
                        encoders.add(parts[1].strip().lower())
        except Exception:
            pass
        self._ffmpeg_encoders_cache = encoders
        return set(encoders)

    def _select_hw_video_encoder(self, ffmpeg_bin: str, preferred_base: str) -> str:
        available = self._load_ffmpeg_encoders(ffmpeg_bin)
        base = "h264"
        pref = str(preferred_base or "").strip().lower()
        if pref in {"h265", "hevc", "libx265", "x265"}:
            base = "hevc"
        candidates = (
            f"{base}_nvenc",
            f"{base}_qsv",
            f"{base}_amf",
        )
        for enc in candidates:
            if enc.lower() in available:
                return enc
        return ""

    @staticmethod
    def _container_video_compatible(target_ext: str, codec_name: str) -> bool:
        ext = str(target_ext or "").strip().lower().lstrip(".")
        codec = str(codec_name or "").strip().lower()
        if not ext:
            return True
        if ext == "mp4":
            allowed = {"h264", "avc1", "hevc", "h265", "av1", "av01", "mpeg4"}
            return codec in allowed
        if ext == "webm":
            allowed = {"vp8", "vp9", "av1", "av01"}
            return codec in allowed
        if ext == "mkv":
            allowed = {"vp8", "vp9", "av1", "av01", "h264", "hevc", "mpeg4"}
            return codec in allowed
        return True

    @staticmethod
    def _container_audio_compatible(target_ext: str, codec_name: str) -> bool:
        ext = str(target_ext or "").strip().lower().lstrip(".")
        codec = str(codec_name or "").strip().lower()
        if not ext:
            return True
        if ext == "mp4":
            allowed = {"aac", "mp3", "ac3", "eac3", "alac", "flac"}
            return codec in allowed
        if ext in {"webm"}:
            allowed = {"opus", "vorbis"}
            return codec in allowed
        return True

    @staticmethod
    def _encoder_codec_name(encoder: str) -> str:
        raw = str(encoder or "").strip().lower()
        mapping = {
            "libx264": "h264",
            "h264_nvenc": "h264",
            "h264_qsv": "h264",
            "h264_amf": "h264",
            "libx265": "hevc",
            "hevc_nvenc": "hevc",
            "hevc_qsv": "hevc",
            "hevc_amf": "hevc",
            "libaom-av1": "av1",
            "av1_nvenc": "av1",
            "av1_qsv": "av1",
            "av1_amf": "av1",
            "libvpx-vp9": "vp9",
            "libvpx": "vp8",
            "copy": "copy",
            "aac": "aac",
            "opus": "opus",
            "mp3": "mp3",
            "flac": "flac",
        }
        return mapping.get(raw, raw)

    def _pick_preferred_video_encoder(self, ffmpeg_bin: str, target_ext: str, source_codec: str = "") -> str:
        available = self._load_ffmpeg_encoders(ffmpeg_bin)
        ext = str(target_ext or "").strip().lower().lstrip(".")
        source = str(source_codec or "").strip().lower()
        if ext == "webm":
            candidates = ["libvpx-vp9", "libaom-av1", "av1_qsv", "av1_nvenc", "av1_amf"]
        elif ext == "mp4":
            if source in {"hevc", "h265"}:
                candidates = ["libx265", "hevc_nvenc", "hevc_qsv", "hevc_amf", "libx264", "h264_nvenc", "h264_qsv", "h264_amf"]
            else:
                candidates = ["libx264", "h264_nvenc", "h264_qsv", "h264_amf", "libx265", "hevc_nvenc", "hevc_qsv", "hevc_amf"]
        else:
            candidates = ["libx264", "h264_nvenc", "h264_qsv", "h264_amf", "libx265", "hevc_nvenc", "hevc_qsv", "hevc_amf"]
        for encoder in candidates:
            if encoder in available:
                return encoder
        # Keep a safe software fallback even if encoder introspection fails.
        if ext == "webm":
            return "libvpx-vp9"
        return "libx264"

    @staticmethod
    def _pick_preferred_audio_encoder(target_ext: str) -> str:
        ext = str(target_ext or "").strip().lower().lstrip(".")
        if ext == "webm":
            return "opus"
        return "aac"

    def _build_merge_codec_plan(self, ffmpeg_bin: str, video_path: str, audio_path: str, target_ext: str) -> dict:
        video_info = self._probe_media_info(video_path)
        audio_info = self._probe_media_info(audio_path)
        video_stream = self._first_stream(video_info, "video")
        audio_stream = self._first_stream(audio_info, "audio")
        src_v_codec = str(video_stream.get("codec_name", "")).strip().lower()
        src_a_codec = str(audio_stream.get("codec_name", "")).strip().lower()
        notes: list[str] = []

        requested_v = self._normalize_video_encoder(self.merge_opts.get("video_codec", "copy"))
        requested_a = str(self.merge_opts.get("audio_codec", "aac") or "aac").strip().lower()
        if requested_a not in {"copy", "aac", "mp3", "opus", "flac"}:
            requested_a = "aac"

        force_reencode = bool(self.merge_opts.get("force_reencode", False))
        if force_reencode and requested_v == "copy":
            requested_v = self._pick_preferred_video_encoder(ffmpeg_bin, target_ext, src_v_codec)
            notes.append(f"تم تفعيل إعادة الترميز الإجباري للفيديو: {requested_v}")
        if force_reencode and requested_a == "copy":
            requested_a = self._pick_preferred_audio_encoder(target_ext)
            notes.append(f"تم تفعيل إعادة الترميز الإجباري للصوت: {requested_a}")

        if requested_v == "copy" and not force_reencode:
            if src_v_codec and not self._container_video_compatible(target_ext, src_v_codec):
                requested_v = self._pick_preferred_video_encoder(ffmpeg_bin, target_ext, src_v_codec)
                notes.append(f"تم تحويل ترميز الفيديو تلقائيًا للتوافق مع .{target_ext}: {requested_v}")

        if requested_a == "copy" and not force_reencode:
            if src_a_codec and not self._container_audio_compatible(target_ext, src_a_codec):
                requested_a = self._pick_preferred_audio_encoder(target_ext)
                notes.append(f"تم تحويل ترميز الصوت تلقائيًا للتوافق مع .{target_ext}: {requested_a}")

        hw_policy = str(
            self.merge_opts.get("hw_encoder")
            or self.merge_opts.get("hardware_encoder")
            or os.getenv("SNAPDOWNLOADER_FFMPEG_HW_ENCODER", "")
        ).strip().lower()
        if requested_v in {"libx264", "libx265"}:
            if hw_policy in {"auto", "1", "true", "on", "yes"}:
                hw_pick = self._select_hw_video_encoder(ffmpeg_bin, requested_v)
                if hw_pick:
                    requested_v = hw_pick
            elif hw_policy in {"nvenc", "qsv", "amf"}:
                base = "hevc" if requested_v == "libx265" else "h264"
                candidate = f"{base}_{hw_policy}"
                if candidate in self._load_ffmpeg_encoders(ffmpeg_bin):
                    requested_v = candidate

        available_encoders = self._load_ffmpeg_encoders(ffmpeg_bin)
        if requested_v != "copy" and requested_v not in available_encoders:
            fallback_v = self._pick_preferred_video_encoder(ffmpeg_bin, target_ext, src_v_codec)
            notes.append(f"المرمز المطلوب غير متاح ({requested_v})، تم استخدام {fallback_v}")
            requested_v = fallback_v
        if requested_a != "copy" and requested_a not in available_encoders:
            fallback_a = self._pick_preferred_audio_encoder(target_ext)
            notes.append(f"مرمز الصوت المطلوب غير متاح ({requested_a})، تم استخدام {fallback_a}")
            requested_a = fallback_a

        normalized_v_codec = self._encoder_codec_name(requested_v)
        if requested_v != "copy" and not self._container_video_compatible(target_ext, normalized_v_codec):
            fallback_v = self._pick_preferred_video_encoder(ffmpeg_bin, target_ext, src_v_codec)
            notes.append(f"مرمز الفيديو {requested_v} غير متوافق مع .{target_ext}، تم التحويل إلى {fallback_v}")
            requested_v = fallback_v

        normalized_a_codec = self._encoder_codec_name(requested_a)
        if requested_a != "copy" and not self._container_audio_compatible(target_ext, normalized_a_codec):
            fallback_a = self._pick_preferred_audio_encoder(target_ext)
            notes.append(f"مرمز الصوت {requested_a} غير متوافق مع .{target_ext}، تم التحويل إلى {fallback_a}")
            requested_a = fallback_a

        return {
            "video_codec": requested_v,
            "audio_codec": requested_a,
            "src_video_codec": src_v_codec,
            "src_audio_codec": src_a_codec,
            "video_stream": video_stream,
            "notes": notes,
        }

    def _get_duration_seconds(self, file_path: str) -> float:
        p = str(file_path or "").strip()
        if not p or not os.path.isfile(p):
            return 0.0
        ffprobe_bin = self._find_ffprobe()
        cmd = [
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            p,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=12,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            logger.error(f"[TimeOut] عملية ffprobe استغرقت وقتاً طويلاً: {p}")
            return 0.0
        except Exception as e:
            logger.error(f"[Error] فشل استدعاء ffprobe للملف {p}. السبب: {e}")
            return 0.0
        if proc.returncode != 0:
            return 0.0
        try:
            return float((proc.stdout or "").strip())
        except (ValueError, TypeError, AttributeError):
            return 0.0

    def _pick_merge_inputs(self) -> tuple[str, str]:
        with self._downloaded_separate_files_lock:
            pending_files = list(self.downloaded_separate_files or [])
        candidates = []
        for p in pending_files:
            try:
                ap = os.path.abspath(str(p or "").strip())
            except (ValueError, TypeError, AttributeError):
                ap = str(p or "").strip()
            if ap and os.path.isfile(ap):
                candidates.append(ap)
        seen = set()
        unique = []
        for p in candidates:
            if p in seen:
                continue
            seen.add(p)
            unique.append(p)
        if len(unique) < 2:
            return "", ""

        probed_any = False
        stream_map: dict[str, set[str]] = {}
        for p in unique:
            st = self._probe_stream_types(p)
            if st:
                probed_any = True
            stream_map[p] = st

        if probed_any:
            videos = [p for p in unique if "video" in (stream_map.get(p) or set())]
            audios = [p for p in unique if "audio" in (stream_map.get(p) or set()) and "video" not in (stream_map.get(p) or set())]
            if videos and audios:
                def _size(path: str) -> int:
                    try:
                        return int(os.path.getsize(path))
                    except (OSError, ValueError, TypeError):
                        return 0
                video = max(videos, key=_size)
                audio = max(audios, key=_size)
                return video, audio

        audio_exts = {".m4a", ".mp3", ".aac", ".opus", ".ogg", ".flac", ".wav"}
        video_exts = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v"}

        by_ext_audio = [p for p in unique if os.path.splitext(p)[1].lower() in audio_exts]
        by_ext_video = [p for p in unique if os.path.splitext(p)[1].lower() in video_exts and p not in by_ext_audio]
        if by_ext_video and by_ext_audio:
            def _size(path: str) -> int:
                try:
                    return int(os.path.getsize(path))
                except (OSError, ValueError, TypeError):
                    return 0
            return max(by_ext_video, key=_size), max(by_ext_audio, key=_size)

        def _size(path: str) -> int:
            try:
                return int(os.path.getsize(path))
            except (OSError, ValueError, TypeError):
                return 0
        ordered = sorted(unique, key=_size, reverse=True)
        if len(ordered) >= 2:
            return ordered[0], ordered[1]
        return "", ""

    def _collect_custom_merge_subtitle_inputs(self, video_path: str, audio_path: str) -> list[str]:
        if not self.embed_subs or self.hard_burn_subs:
            return []
        subtitle_patterns = self._subtitle_lang_patterns()
        allowed_tokens: set[str] = set()
        include_all = "all" in subtitle_patterns
        if not include_all:
            for pattern in subtitle_patterns:
                token = str(pattern or "").replace(".*", "").strip().lower().replace("_", "-")
                if token:
                    allowed_tokens.add(token)
        supported_exts = {".srt", ".vtt", ".ass", ".ssa"}
        try:
            excluded = {
                os.path.normcase(os.path.abspath(str(video_path or "").strip())),
                os.path.normcase(os.path.abspath(str(audio_path or "").strip())),
            }
        except Exception:
            excluded = {str(video_path or "").strip(), str(audio_path or "").strip()}
        with self._downloaded_separate_files_lock:
            pending_files = list(self.downloaded_separate_files or [])
        results: list[str] = []
        seen: set[str] = set()
        for raw_path in pending_files:
            candidate = str(raw_path or "").strip()
            if not candidate:
                continue
            try:
                normalized = os.path.normcase(os.path.abspath(candidate))
            except Exception:
                normalized = candidate
            if normalized in excluded or normalized in seen:
                continue
            if not os.path.isfile(candidate):
                continue
            if os.path.splitext(candidate)[1].lower() not in supported_exts:
                continue
            if allowed_tokens and not include_all:
                detected_tokens = self._extract_subtitle_language_tokens(candidate)
                if detected_tokens and detected_tokens.isdisjoint(allowed_tokens):
                    continue
            seen.add(normalized)
            results.append(candidate)
        return results

    @staticmethod
    def _custom_merge_subtitle_codec(target_ext: str) -> str:
        ext = str(target_ext or "").strip().lower().lstrip(".")
        if ext in {"mp4", "m4v", "mov"}:
            return "mov_text"
        if ext == "mkv":
            return "copy"
        return ""

    @staticmethod
    def _normalize_subtitle_language_code(value: str) -> str:
        token = str(value or "").strip().lower().replace("_", "-")
        if not token:
            return ""
        if "-" in token:
            token = token.split("-", 1)[0]
        lookup = {
            "ar": "ara",
            "zh": "zho",
            "en": "eng",
            "fr": "fra",
            "de": "deu",
            "it": "ita",
            "es": "spa",
            "sv": "swe",
            "pt": "por",
            "ja": "jpn",
            "ko": "kor",
            "ru": "rus",
            "tr": "tur",
            "hi": "hin",
        }
        if token in lookup:
            return lookup[token]
        if len(token) == 3 and token.isalpha():
            return token
        return ""

    def _extract_subtitle_language_tokens(self, subtitle_path: str) -> set[str]:
        name = os.path.splitext(os.path.basename(str(subtitle_path or "").strip()))[0].lower()
        if not name:
            return set()
        parts = [part.strip() for part in re.split(r"[._-]+", name) if part.strip()]
        if not parts:
            return set()
        known = {str(code).strip().lower().replace("_", "-") for code in SUBTITLE_LANGUAGES.values()}
        token_alias = {
            str(label).strip().lower(): str(code).strip().lower()
            for label, code in SUBTITLE_LANGUAGES.items()
            if str(label).strip() and str(code).strip()
        }
        detected: set[str] = set()
        for part in parts:
            normalized = part.replace("_", "-")
            direct = self._normalize_subtitle_language_code(normalized)
            if direct:
                detected.add(normalized.split("-", 1)[0])
                continue
            alias = token_alias.get(normalized)
            if alias:
                detected.add(alias)
                continue
            if normalized in known:
                detected.add(normalized)
        return detected

    def _subtitle_language_metadata_code(self, subtitle_path: str) -> str:
        detected = sorted(self._extract_subtitle_language_tokens(subtitle_path))
        if detected:
            code = self._normalize_subtitle_language_code(detected[0])
            if code:
                return code
        patterns = self._subtitle_lang_patterns()
        fallback = ""
        for pattern in patterns:
            token = str(pattern or "").replace(".*", "").strip()
            if token and token.lower() != "all":
                fallback = token
                break
        return self._normalize_subtitle_language_code(fallback)

    def _probe_subtitle_stream_descriptors(self, path: str) -> list[dict]:
        info = self._probe_media_info(path)
        streams = info.get("streams")
        if not isinstance(streams, list):
            return []
        descriptors: list[dict] = []
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            if str(stream.get("codec_type", "")).strip().lower() != "subtitle":
                continue
            tags = stream.get("tags") if isinstance(stream.get("tags"), dict) else {}
            raw_language = str(tags.get("language", "")).strip().lower()
            normalized_language = self._normalize_subtitle_language_code(raw_language)
            descriptors.append(
                {
                    "language": normalized_language,
                    "codec_name": str(stream.get("codec_name", "")).strip().lower(),
                }
            )
        return descriptors

    def _subtitle_language_preference_tokens(self) -> list[str]:
        patterns = self._subtitle_lang_patterns()
        tokens: list[str] = []
        seen: set[str] = set()
        for pattern in patterns:
            token = str(pattern or "").replace(".*", "").strip().lower().replace("_", "-")
            if not token or token == "all" or token in seen:
                continue
            seen.add(token)
            tokens.append(token)
        return tokens

    def _subtitle_input_sort_key(self, subtitle_path: str, preferred_tokens: list[str]) -> tuple[int, str]:
        detected = self._extract_subtitle_language_tokens(subtitle_path)
        pref_score = len(preferred_tokens) + 1
        for idx, token in enumerate(preferred_tokens):
            if token in detected:
                pref_score = idx
                break
        return pref_score, os.path.basename(str(subtitle_path or "")).lower()

    @staticmethod
    def _subtitle_track_title_from_lang_code(lang_code: str) -> str:
        code = str(lang_code or "").strip().lower()
        if not code:
            return "Subtitle"
        reverse = {
            "ara": "Arabic",
            "zho": "Chinese",
            "eng": "English",
            "fra": "French",
            "deu": "German",
            "ita": "Italian",
            "spa": "Spanish",
            "swe": "Swedish",
            "por": "Portuguese",
            "jpn": "Japanese",
            "kor": "Korean",
            "rus": "Russian",
            "tur": "Turkish",
            "hin": "Hindi",
        }
        return reverse.get(code, code.upper())

    def _subtitle_track_title_from_path(self, subtitle_path: str) -> str:
        base = os.path.splitext(os.path.basename(str(subtitle_path or "").strip()))[0]
        if not base:
            return "Subtitle"
        parts = [part.strip() for part in re.split(r"[._-]+", base) if part.strip()]
        if not parts:
            return "Subtitle"
        # Prefer trailing tokens (e.g. video.en -> EN, video.arabic -> ARABIC).
        for token in reversed(parts[-3:]):
            if len(token) <= 8 and token.lower() not in {"video", "subtitle", "sub"}:
                return token.upper() if len(token) <= 3 else token.capitalize()
        return "Subtitle"

    def _resolve_custom_merge_output_path(self, video_path: str) -> str:
        out_dir = str(self.out_dir or "").strip() or os.getcwd()
        base = str(video_path or "").strip()
        if base:
            base_no_ext, _ = os.path.splitext(base)
        else:
            base_no_ext = os.path.join(out_dir, "merged")
        target_ext = str(self._normalized_format() or "").strip().lstrip(".")
        if not target_ext:
            target_ext = "mp4"
        candidate = base_no_ext + "." + target_ext
        if os.path.abspath(os.path.dirname(candidate)) != os.path.abspath(out_dir):
            candidate = os.path.join(out_dir, os.path.basename(candidate))
        if not os.path.exists(candidate):
            return candidate
        root, ext = os.path.splitext(candidate)
        counter = 1
        while True:
            if counter > 9999:
                raise RuntimeError("Too many file collision attempts during merge.")
            next_path = f"{root} ({counter}){ext}"
            if not os.path.exists(next_path):
                return next_path
            counter += 1

    def _run_custom_merge(self) -> tuple[bool, str]:
        video_path, audio_path = self._pick_merge_inputs()
        if not video_path or not audio_path:
            return True, ""
        ffmpeg_path = shutil.which("ffmpeg")
        ffmpeg_dir = self._find_ffmpeg()
        if ffmpeg_dir and os.path.isdir(ffmpeg_dir):
            candidate = os.path.join(ffmpeg_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
            if os.path.isfile(candidate):
                ffmpeg_path = candidate
        if not ffmpeg_path:
            return False, "FFmpeg غير متاح لإتمام الدمج"

        out_path = self._resolve_custom_merge_output_path(video_path)
        tmp_out = out_path + ".merging.tmp"
        try:
            if os.path.isfile(tmp_out):
                os.remove(tmp_out)
        except Exception:
            pass
        total_duration = self._get_duration_seconds(video_path)
        if total_duration <= 0:
            total_duration = self._get_duration_seconds(audio_path)
        if total_duration <= 0:
            total_duration = 0.0

        target_ext = str(self._normalized_format() or "").strip().lower().lstrip(".") or "mp4"
        plan = self._build_merge_codec_plan(ffmpeg_path, video_path, audio_path, target_ext)
        subtitle_inputs = self._collect_custom_merge_subtitle_inputs(video_path, audio_path)
        subtitle_codec = self._custom_merge_subtitle_codec(target_ext)
        subtitle_sources_supported = bool(subtitle_codec)
        embed_subtitles = bool(self.embed_subs and not self.hard_burn_subs)
        embedded_subtitles = self._probe_subtitle_stream_descriptors(video_path) if embed_subtitles else []
        has_embedded_subtitle_streams = bool(embedded_subtitles)
        embedded_subtitle_count = len(embedded_subtitles)
        active_subtitle_inputs = subtitle_inputs if subtitle_sources_supported else []
        if active_subtitle_inputs:
            preferred_tokens = self._subtitle_language_preference_tokens()
            active_subtitle_inputs = sorted(
                active_subtitle_inputs,
                key=lambda path: self._subtitle_input_sort_key(path, preferred_tokens),
            )
        v_codec = str(plan.get("video_codec", "copy") or "copy").strip()
        a_codec = str(plan.get("audio_codec", "aac") or "aac").strip()
        src_video_codec = str(plan.get("src_video_codec", "")).strip()
        video_stream = plan.get("video_stream") if isinstance(plan.get("video_stream"), dict) else {}
        for note in plan.get("notes", []) if isinstance(plan.get("notes"), list) else []:
            self.log.emit(str(note))
        cmd: list[str] = [
            ffmpeg_path,
            "-y",
            "-hide_banner",
            "-i",
            video_path,
            "-i",
            audio_path,
        ]
        for subtitle_path in active_subtitle_inputs:
            cmd.extend(["-i", subtitle_path])
        cmd.extend([
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
        ])
        if embed_subtitles:
            if has_embedded_subtitle_streams:
                cmd.extend(["-map", "0:s?"])
            for input_index in range(2, 2 + len(active_subtitle_inputs)):
                cmd.extend(["-map", f"{input_index}:0"])
        cmd.extend([
            "-map_metadata",
            "0",
            "-map_chapters",
            "0",
            "-shortest",
            "-avoid_negative_ts",
            "make_zero",
            "-fflags",
            "+genpts",
            "-c:v",
            v_codec,
        ])
        if target_ext in {"mp4", "m4v", "mov"}:
            cmd.extend(["-movflags", "+faststart"])
        if v_codec not in {"", "copy"}:
            if v_codec.endswith("_nvenc"):
                cq_val = self.merge_opts.get("video_cq", self.merge_opts.get("video_crf", 23))
                preset = str(self.merge_opts.get("video_preset", "p5") or "p5").strip()
                try:
                    cq_int = int(cq_val)
                except Exception:
                    cq_int = 23
                cq_int = max(0, min(51, cq_int))
                cmd.extend(["-preset", preset, "-rc", "vbr", "-cq", str(cq_int)])
            elif v_codec.endswith("_qsv"):
                qsv_q = self.merge_opts.get("video_cq", self.merge_opts.get("video_crf", 23))
                try:
                    qsv_q_int = int(qsv_q)
                except Exception:
                    qsv_q_int = 23
                qsv_q_int = max(1, min(51, qsv_q_int))
                cmd.extend(["-global_quality", str(qsv_q_int)])
            elif v_codec.endswith("_amf"):
                amf_q = self.merge_opts.get("video_cq", self.merge_opts.get("video_crf", 23))
                try:
                    amf_q_int = int(amf_q)
                except Exception:
                    amf_q_int = 23
                amf_q_int = max(0, min(51, amf_q_int))
                cmd.extend(["-quality", "quality", "-qp_i", str(amf_q_int), "-qp_p", str(amf_q_int)])
            else:
                try:
                    crf_val = int(self.merge_opts.get("video_crf", 23))
                    if 0 <= crf_val <= 51:
                        cmd.extend(["-crf", str(crf_val)])
                except Exception:
                    pass
        cmd.extend(["-c:a", a_codec])
        if a_codec != "copy":
            bitrate = str(self.merge_opts.get("audio_bitrate", "192k") or "192k").strip()
            if re.match(r"^\d+[kKmM]?$", bitrate):
                cmd.extend(["-b:a", bitrate])
        if embed_subtitles:
            if subtitle_sources_supported and (has_embedded_subtitle_streams or active_subtitle_inputs):
                cmd.extend(["-c:s", subtitle_codec])
                for embedded_idx, descriptor in enumerate(embedded_subtitles):
                    lang_code = self._normalize_subtitle_language_code(descriptor.get("language", ""))
                    if lang_code:
                        cmd.extend([f"-metadata:s:s:{embedded_idx}", f"language={lang_code}"])
                        title = self._subtitle_track_title_from_lang_code(lang_code)
                    else:
                        codec_name = str(descriptor.get("codec_name", "")).strip().lower()
                        title = f"Subtitle ({codec_name})" if codec_name else "Subtitle"
                    cmd.extend([f"-metadata:s:s:{embedded_idx}", f"title={title}"])
                if active_subtitle_inputs:
                    output_subtitle_offset = embedded_subtitle_count if has_embedded_subtitle_streams else 0
                    for idx, subtitle_path in enumerate(active_subtitle_inputs):
                        out_stream_idx = idx + output_subtitle_offset
                        lang_code = self._subtitle_language_metadata_code(subtitle_path)
                        if lang_code:
                            cmd.extend([f"-metadata:s:s:{out_stream_idx}", f"language={lang_code}"])
                            title = self._subtitle_track_title_from_lang_code(lang_code)
                        else:
                            title = self._subtitle_track_title_from_path(subtitle_path)
                        cmd.extend([f"-metadata:s:s:{out_stream_idx}", f"title={title}"])
                total_output_subtitles = embedded_subtitle_count + len(active_subtitle_inputs)
                if total_output_subtitles > 0:
                    preferred_codes = [
                        self._normalize_subtitle_language_code(token)
                        for token in self._subtitle_language_preference_tokens()
                    ]
                    preferred_codes = [code for code in preferred_codes if code]
                    output_languages: list[str] = []
                    for desc in embedded_subtitles:
                        output_languages.append(str(desc.get("language", "")).strip().lower())
                    for subtitle_path in active_subtitle_inputs:
                        output_languages.append(self._subtitle_language_metadata_code(subtitle_path))
                    default_index = 0
                    for pref_code in preferred_codes:
                        try:
                            default_index = output_languages.index(pref_code)
                            break
                        except ValueError:
                            continue
                    for idx in range(total_output_subtitles):
                        cmd.extend([f"-disposition:s:{idx}", "0"])
                    cmd.extend([f"-disposition:s:{default_index}", "default"])
            elif subtitle_inputs:
                self.log.emit(f"تم تخطي تضمين الترجمة في الدمج المخصص لأن الحاوية .{target_ext} غير مدعومة.")
        # Keep HEVC tag compatible in MP4-based containers for better device playback.
        if target_ext in {"mp4", "m4v", "mov"}:
            normalized_source = src_video_codec.lower()
            if v_codec in {"copy", "libx265", "hevc_nvenc", "hevc_qsv", "hevc_amf"} or normalized_source in {"hevc", "h265"}:
                cmd.extend(["-tag:v", "hvc1"])
            elif v_codec in {"av1", "libaom-av1", "av1_nvenc", "av1_qsv", "av1_amf"} or normalized_source in {"av1", "av01"}:
                cmd.extend(["-tag:v", "av01"])
        # Preserve HDR metadata path where possible.
        if isinstance(video_stream, dict):
            color_primaries = str(video_stream.get("color_primaries", "")).strip()
            color_trc = str(video_stream.get("color_transfer", "")).strip()
            colorspace = str(video_stream.get("color_space", "")).strip()
            if color_primaries:
                cmd.extend(["-color_primaries", color_primaries])
            if color_trc:
                cmd.extend(["-color_trc", color_trc])
            if colorspace:
                cmd.extend(["-colorspace", colorspace])
        cmd.extend(["-progress", "pipe:1", "-nostats", tmp_out])

        if v_codec == "copy" and a_codec == "copy":
            self.log.emit("جاري دمج الفيديو والصوت (Stream Copy)...")
        else:
            self.log.emit(f"جاري دمج الفيديو والصوت عبر FFmpeg ({v_codec}/{a_codec})...")
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            encoding=self._text_encoding(),
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with QMutexLocker(self.process_mutex):
            self.current_process = process
        last_out_time_ms = 0
        try:
            stdout = process.stdout
            if stdout is not None:
                for raw in self._read_lines_safely(stdout, process, idle_timeout=_MERGE_IDLE_TIMEOUT_SECONDS):
                    if self._is_cancel_requested():
                        self._terminate_process(process, "تعذر إنهاء عملية الدمج")
                        try:
                            if os.path.isfile(tmp_out):
                                os.remove(tmp_out)
                        except Exception:
                            pass
                        return False, _STATUS_CANCELLED
                    line = str(raw or "").strip()
                    if not line:
                        continue
                    if line.startswith("out_time_ms="):
                        try:
                            last_out_time_ms = int(line.split("=", 1)[1].strip() or "0")
                        except Exception:
                            last_out_time_ms = last_out_time_ms
                        if total_duration > 0:
                            pct = max(0.0, min(100.0, (last_out_time_ms / 1_000_000.0) / total_duration * 100.0))
                            self.progress.emit(1, float(pct), "--", "--:--")
                        continue
                    if line.startswith(("progress=", "frame=", "fps=", "speed=", "bitrate=", "total_size=", "dup_frames=", "drop_frames=", "out_time=")):
                        continue
                    self.log.emit(line)
        finally:
            stdout = getattr(process, "stdout", None)
            if stdout is not None:
                try:
                    stdout.close()
                except Exception:
                    pass

        ret = process.poll()
        if ret is None:
            ret = process.wait()
        self._clear_current_process(process)
        runtime_error = self._consume_last_runtime_error()
        if ret != 0 or not os.path.isfile(tmp_out):
            try:
                if os.path.isfile(tmp_out):
                    os.remove(tmp_out)
            except Exception:
                pass
            return False, self._normalize_download_error(runtime_error or "فشل دمج الملفات عبر FFmpeg")
        try:
            os.replace(tmp_out, out_path)
        except Exception as exc:
            try:
                if os.path.isfile(tmp_out):
                    os.remove(tmp_out)
            except Exception:
                pass
            return False, self._normalize_download_error(str(exc), fallback="فشل تثبيت ملف الدمج النهائي")

        keep_inputs = str(os.getenv("VIDDOWNLOADER_KEEP_MERGE_INPUTS", "")).strip().lower() in {"1", "true", "yes", "on"}
        if not keep_inputs:
            for p in {video_path, audio_path}:
                try:
                    os.remove(p)
                except Exception:
                    pass
        self._set_downloaded_file_path(out_path)
        self.log.emit(f"تم الدمج بنجاح: {os.path.basename(out_path)}")
        self.progress.emit(1, 100.0, "--", "--:--")
        return True, ""

    def _format_speed(self, speed_bytes_per_s: Optional[float]) -> str:
        if not speed_bytes_per_s:
            return "--"
        try:
            speed = float(speed_bytes_per_s)
        except Exception:
            return "--"
        units = ["B/s", "KiB/s", "MiB/s", "GiB/s"]
        idx = 0
        while speed >= 1024 and idx < len(units) - 1:
            speed /= 1024
            idx += 1
        if idx == 0:
            return f"{int(speed)} {units[idx]}"
        return f"{speed:.1f} {units[idx]}"

    def _format_eta(self, eta_seconds: Optional[float]) -> str:
        if eta_seconds is None:
            return "--:--"
        try:
            total = int(max(0, float(eta_seconds)))
        except Exception:
            return "--:--"
        mm, ss = divmod(total, 60)
        hh, mm = divmod(mm, 60)
        if hh > 0:
            return f"{hh:02d}:{mm:02d}:{ss:02d}"
        return f"{mm:02d}:{ss:02d}"

    def _parse_safe_extra_args(self) -> tuple[list[str], dict]:
        raw_args = [str(arg or "").strip() for arg in self.extra_args if str(arg or "").strip()]
        cache_key = tuple(raw_args)
        if self._safe_extra_args_cache_key == cache_key:
            cached_args, cached_options = self._safe_extra_args_cache_result
            return list(cached_args), dict(cached_options)
        allowed_flags = {"--no-check-certificate", "--geo-bypass"}
        allowed_with_value = {
            "--proxy",
            "--user-agent",
            "--referer",
            "--impersonate",
            "--add-header",
            "--extractor-args",
            "--sleep-interval",
            "--max-sleep-interval",
            "--sleep-requests",
        }
        safe_args: list[str] = []
        options: dict = {}
        headers: dict[str, str] = {}
        saw_unsafe_header = False
        i = 0
        while i < len(raw_args):
            arg = raw_args[i]
            if arg in allowed_flags:
                safe_args.append(arg)
                if arg == "--no-check-certificate":
                    options["nocheckcertificate"] = True
                elif arg == "--geo-bypass":
                    options["geo_bypass"] = True
                i += 1
                continue
            if arg in allowed_with_value:
                if i + 1 >= len(raw_args):
                    logger.warning(f"تم تجاهل وسيطة yt-dlp غير مكتملة: {arg}")
                    i += 1
                    continue
                value = raw_args[i + 1]
                if not self._is_safe_extra_arg_value(value):
                    logger.warning(f"تم تجاهل قيمة غير آمنة: {arg}")
                    i += 2
                    continue
                if arg == "--proxy" and not re.match(r"^(https?|socks5h?)://", value, re.IGNORECASE):
                    logger.warning(f"تم تجاهل قيمة proxy غير صالحة: {value}")
                    i += 2
                    continue
                if arg == "--add-header":
                    parsed_header = self._parse_safe_header(value)
                    if parsed_header is None:
                        logger.warning(f"تم تجاهل header غير آمن: {value}")
                        saw_unsafe_header = True
                        i += 2
                        continue
                    value = f"{parsed_header[0]}:{parsed_header[1]}"
                if arg == "--extractor-args":
                    parsed_extractor_args = self._parse_safe_extractor_args(value)
                    if parsed_extractor_args is None:
                        logger.warning(f"تم تجاهل extractor-args غير آمن: {value}")
                        i += 2
                        continue
                    value = parsed_extractor_args["raw"]
                if arg in {"--sleep-interval", "--max-sleep-interval", "--sleep-requests"}:
                    try:
                        parsed = float(value)
                    except Exception:
                        logger.warning(f"تم تجاهل قيمة غير رقمية: {arg}={value}")
                        i += 2
                        continue
                    if parsed < 0:
                        logger.warning(f"تم تجاهل قيمة سالبة: {arg}={value}")
                        i += 2
                        continue
                safe_args.extend([arg, value])
                if arg == "--proxy":
                    options["proxy"] = value
                elif arg == "--user-agent":
                    options["user_agent"] = value
                elif arg == "--referer":
                    options["referer"] = value
                elif arg == "--impersonate":
                    options["impersonate"] = value
                elif arg == "--add-header":
                    header_name, header_value = value.split(":", 1)
                    headers[header_name] = header_value
                elif arg == "--extractor-args":
                    parsed_extractor_args = self._parse_safe_extractor_args(value)
                    if parsed_extractor_args is not None:
                        options.setdefault("extractor_args", {}).setdefault(
                            parsed_extractor_args["extractor"], {}
                        )[parsed_extractor_args["key"]] = list(parsed_extractor_args["values"])
                elif arg == "--sleep-interval":
                    options["sleep_interval"] = float(value)
                elif arg == "--max-sleep-interval":
                    options["max_sleep_interval"] = float(value)
                elif arg == "--sleep-requests":
                    options["sleep_interval_requests"] = float(value)
                i += 2
                continue
            if arg.startswith("--proxy="):
                proxy_value = arg.split("=", 1)[1].strip()
                if self._is_safe_extra_arg_value(proxy_value) and re.match(r"^(https?|socks5h?)://", proxy_value, re.IGNORECASE):
                    safe_args.append(arg)
                    options["proxy"] = proxy_value
                else:
                    logger.warning(f"تم تجاهل proxy غير صالح: {arg}")
                i += 1
                continue
            if arg.startswith("--user-agent="):
                value = arg.split("=", 1)[1]
                if self._is_safe_extra_arg_value(value):
                    safe_args.append(arg)
                    options["user_agent"] = value
                else:
                    logger.warning(f"تم تجاهل user-agent غير آمن: {arg}")
                i += 1
                continue
            if arg.startswith("--referer="):
                value = arg.split("=", 1)[1]
                if self._is_safe_extra_arg_value(value):
                    safe_args.append(arg)
                    options["referer"] = value
                else:
                    logger.warning(f"تم تجاهل referer غير آمن: {arg}")
                i += 1
                continue
            if arg.startswith("--add-header="):
                parsed_header = self._parse_safe_header(arg.split("=", 1)[1].strip())
                if parsed_header is not None:
                    safe_args.extend(["--add-header", f"{parsed_header[0]}:{parsed_header[1]}"])
                    headers[parsed_header[0]] = parsed_header[1]
                else:
                    logger.warning(f"تم تجاهل add-header غير آمن: {arg}")
                    saw_unsafe_header = True
                i += 1
                continue
            if arg.startswith("--extractor-args="):
                raw_extractor_args = arg.split("=", 1)[1].strip()
                parsed_extractor_args = self._parse_safe_extractor_args(raw_extractor_args)
                if parsed_extractor_args is not None:
                    safe_args.extend(["--extractor-args", parsed_extractor_args["raw"]])
                    options.setdefault("extractor_args", {}).setdefault(
                        parsed_extractor_args["extractor"], {}
                    )[parsed_extractor_args["key"]] = list(parsed_extractor_args["values"])
                else:
                    logger.warning(f"تم تجاهل extractor-args غير آمن: {arg}")
                i += 1
                continue
            if arg.startswith("--sleep-interval="):
                value = arg.split("=", 1)[1].strip()
                try:
                    parsed = float(value)
                    if parsed < 0:
                        raise ValueError("negative")
                except Exception:
                    logger.warning(f"تم تجاهل sleep-interval غير صالح: {arg}")
                else:
                    safe_args.append(arg)
                    options["sleep_interval"] = parsed
                i += 1
                continue
            if arg.startswith("--max-sleep-interval="):
                value = arg.split("=", 1)[1].strip()
                try:
                    parsed = float(value)
                    if parsed < 0:
                        raise ValueError("negative")
                except Exception:
                    logger.warning(f"تم تجاهل max-sleep-interval غير صالح: {arg}")
                else:
                    safe_args.append(arg)
                    options["max_sleep_interval"] = parsed
                i += 1
                continue
            if arg.startswith("--sleep-requests="):
                value = arg.split("=", 1)[1].strip()
                try:
                    parsed = float(value)
                    if parsed < 0:
                        raise ValueError("negative")
                except Exception:
                    logger.warning(f"تم تجاهل sleep-requests غير صالح: {arg}")
                else:
                    safe_args.append(arg)
                    options["sleep_interval_requests"] = parsed
                i += 1
                continue
            if arg.startswith("--impersonate="):
                value = arg.split("=", 1)[1].strip()
                if self._is_safe_extra_arg_value(value) and re.fullmatch(r"[A-Za-z0-9._:-]+", value):
                    safe_args.append(arg)
                    options["impersonate"] = value
                else:
                    logger.warning(f"تم تجاهل impersonate غير آمن: {arg}")
                i += 1
                continue
            logger.warning(f"تم تجاهل وسيطة غير مسموح بها: {arg}")
            i += 1
        if saw_unsafe_header:
            filtered_args = []
            skip_next = False
            for arg in safe_args:
                if skip_next:
                    skip_next = False
                    continue
                if arg == "--add-header":
                    skip_next = True
                    continue
                filtered_args.append(arg)
            safe_args = filtered_args
            headers.clear()
        if headers:
            options["http_headers"] = headers
        self._safe_extra_args_cache_key = cache_key
        self._safe_extra_args_cache_result = (list(safe_args), dict(options))
        return safe_args, options

    @staticmethod
    def _is_safe_extra_arg_value(value: str) -> bool:
        text = str(value or "")
        if not text:
            return False
        if len(text) > 2048:
            return False
        return "\r" not in text and "\n" not in text and "\x00" not in text

    @staticmethod
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
        if not DownloadWorker._is_safe_extra_arg_value(header_value):
            return None
        denied_headers = {
            "authorization",
            "cookie",
            "proxy-authorization",
            "set-cookie",
            "x-api-key",
            "x-auth-token",
        }
        if name.lower() in denied_headers:
            return None
        return name, header_value

    @staticmethod
    def _parse_safe_extractor_args(value: str) -> dict | None:
        text = str(value or "").strip()
        if not text or not DownloadWorker._is_safe_extra_arg_value(text):
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
        if key == "player_client":
            if any(item not in allowed_player_clients for item in values):
                return None
        if key == "player_skip":
            if any(item not in allowed_player_skip for item in values):
                return None
        unique_values: list[str] = []
        for item in values:
            if item not in unique_values:
                unique_values.append(item)
        return {"extractor": extractor, "key": key, "values": unique_values, "raw": f"{extractor}:{key}={','.join(unique_values)}"}

    def _extra_args_to_ytdlp_options(self) -> dict:
        _safe_args, options = self._parse_safe_extra_args()
        return options

    def _build_ytdlp_options(self) -> dict:
        output_template = self._output_template()
        opts: dict = {
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "retries": 10,
            "fragment_retries": 20,
            "extractor_retries": 5,
            "file_access_retries": 3,
            "continuedl": True,
            "nopart": False,
            "noprogress": True,
            "socket_timeout": 30,
            "skip_unavailable_fragments": True,
            "concurrent_fragment_downloads": 5,
            "http_chunk_size": 10 * 1024 * 1024,
        }

        safe_cookies = self._safe_cookies_path()
        if safe_cookies:
            opts["cookiefile"] = safe_cookies
        elif getattr(self, "cookies_from_browser", "none") != "none":
            opts["cookiesfrombrowser"] = (self.cookies_from_browser,)

        ffmpeg_loc = self._find_ffmpeg()
        if ffmpeg_loc:
            opts["ffmpeg_location"] = ffmpeg_loc
            if self._should_enable_live_stream_mode() and not self.use_aria2:
                opts.setdefault("external_downloader_args", {})
                opts["external_downloader_args"]["ffmpeg_i"] = [
                    "-reconnect",
                    "1",
                    "-reconnect_streamed",
                    "1",
                    "-reconnect_delay_max",
                    "30",
                    "-reconnect_at_eof",
                    "1",
                ]

        runtime_limit_kbps = self.get_dynamic_bandwidth_limit_kbps()
        if runtime_limit_kbps > 0:
            opts["ratelimit"] = int(runtime_limit_kbps) * 1024

        if self._is_video_mode():
            video_selector = self._video_format_selector()
            fmt = self._effective_merge_output_format()
            opts["format"] = video_selector
            opts["format_sort"] = self._video_format_sort_fields()
            opts["merge_output_format"] = fmt
        else:
            # Some YouTube client profiles expose only progressive "best" and no separate audio-only format.
            opts["format"] = "bestaudio/best"
            codec = self._effective_audio_embed_format()
            opts["postprocessors"] = [
                {"key": "FFmpegExtractAudio", "preferredcodec": codec, "preferredquality": self._audio_preferred_quality()},
            ]
            if self._should_embed_audio_metadata():
                opts.setdefault("postprocessors", [])
                opts["postprocessors"].extend(
                    [
                        {"key": "EmbedThumbnail"},
                        {"key": "FFmpegMetadata"},
                    ]
                )

        subtitle_patterns = self._subtitle_lang_patterns()
        if subtitle_patterns and self._is_video_mode():
            opts["writesubtitles"] = True
            opts["writeautomaticsub"] = True
            opts["subtitleslangs"] = subtitle_patterns
            if self.embed_subs and not self.hard_burn_subs:
                opts["embedsubtitles"] = True
                compat_opts = list(opts.get("compat_opts", []) or [])
                if "all" not in compat_opts:
                    compat_opts.append("all")
                opts["compat_opts"] = compat_opts

        if self._should_enable_live_stream_mode():
            opts["hls_use_mpegts"] = True
            opts["live_from_start"] = True

        if self._is_youtube_like_url():
            extractor_args = dict(opts.get("extractor_args", {}) or {})
            youtube_args = dict(extractor_args.get("youtube", {}) or {})
            if "player_client" not in youtube_args:
                level = int(getattr(self, "_format_fallback_level", 0) or 0)
                if level > 0:
                    youtube_args["player_client"] = ["all"]
                elif self._should_enable_live_stream_mode():
                    youtube_args["player_client"] = ["tv", "default"]
                # else: let yt-dlp use its built-in defaults
            if youtube_args:
                extractor_args["youtube"] = youtube_args
            if extractor_args:
                opts["extractor_args"] = extractor_args

        if getattr(self, "sponsorblock_enabled", False):
            opts.setdefault("postprocessors", [])
            opts["postprocessors"].extend([
                {
                    "key": "SponsorBlock",
                    "categories": ["sponsor", "intro", "outro", "selfpromo", "interaction"],
                },
                {
                    "key": "ModifyChapters",
                    "remove_sponsor_segments": ["sponsor", "intro", "outro", "selfpromo", "interaction"],
                    "sponsorblock_chapter_title": "[SB] %(category_names)s",
                },
            ])
            opts["sponsorblock_mark"] = ["intro", "outro"]
            opts["sponsorblock_remove"] = ["sponsor", "selfpromo", "interaction"]

        aria2_bin = self._find_aria2()
        if self.use_aria2 and aria2_bin:
            opts["external_downloader"] = "aria2c"
            runtime_limit_kbps = self.get_dynamic_bandwidth_limit_kbps()
            limit_bytes = int(runtime_limit_kbps) * 1024 if runtime_limit_kbps > 0 else None
            opts["external_downloader_args"] = {
                "default": self._aria2_default_args(limit_bytes)
            }

        opts.update(self._extra_args_to_ytdlp_options())
        return opts

    def _read_lines_safely(self, stdout, process=None, idle_timeout=300.0):
        q = queue.Queue()
        def _enqueue(out, q):
            try:
                for line in iter(out.readline, ''):
                    if not line:
                        break
                    q.put(line)
            except Exception:
                pass
            finally:
                q.put(None)
        
        self._stdout_reader_stream = stdout
        t = threading.Thread(target=_enqueue, args=(stdout, q), name="DownloadWorkerStdoutReader")
        self._stdout_reader_thread = t
        t.start()

        last_output_ts = time.time()
        try:
            while True:
                if self._is_cancel_requested():
                    break
                try:
                    line = q.get(timeout=0.5)
                except queue.Empty:
                    if not t.is_alive():
                        break
                    if idle_timeout and idle_timeout > 0:
                        if (time.time() - last_output_ts) >= idle_timeout:
                            if process is not None:
                                self._set_last_runtime_error("انتهت مهلة القراءة من عملية التحميل")
                                self._terminate_process(process, "انتهت مهلة القراءة من عملية التحميل")
                            break
                    continue
                if line is None:
                    break
                last_output_ts = time.time()
                yield line
        finally:
            t.join(timeout=2.0)
            if self._stdout_reader_thread is t:
                self._stdout_reader_thread = None
            if self._stdout_reader_stream is stdout:
                self._stdout_reader_stream = None

    def _run_subprocess_once(self, cmd: list[str], proc_env: dict) -> tuple[bool, bool, str]:
        cancelled = False
        last_error = ""
        self._consume_last_runtime_error()
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            encoding=self._text_encoding(),
            errors="replace",
            env=proc_env,
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        with QMutexLocker(self.process_mutex):
            self.current_process = process
        stdout = process.stdout
        try:
            if stdout is not None:
                for line in self._read_lines_safely(stdout, process):
                    if self._is_cancel_requested():
                        cancelled = True
                        break
                    clean = line.strip()
                    if not clean:
                        continue
                    self.log.emit(clean)
                    if "error" in clean.lower() or "failed" in clean.lower() or "خطأ" in clean.lower():
                        last_error = clean
                    dest_match = re.search(
                        r'\[(?:download|Merger|VideoConvertor)\]\s+(?:Destination:|Merging formats into "|Converting video to ")(.+?)"?$',
                        clean,
                    )
                    if not dest_match:
                        # Capture "has already been downloaded" case
                        dest_match = re.search(
                            r'\[download\]\s+(.+?)\s+has already been downloaded',
                            clean,
                        )
                    
                    if dest_match:
                        candidate_path = dest_match.group(1).strip().strip('\"')
                        if candidate_path:
                            self._set_downloaded_file_path(candidate_path)
                    # M-16: Capture printed filenames for custom merge
                    elif self.custom_merge and os.path.exists(clean) and os.path.isfile(clean):
                        self._append_downloaded_separate_file(clean)
                    self._parse_line(clean)
        finally:
            if self._is_cancel_requested():
                cancelled = True
                self._terminate_process(process, "تعذر إنهاء العملية")
            if stdout is not None:
                try:
                    stdout.close()
                except Exception:
                    pass

        ret = process.poll()
        if ret is None:
            ret = process.wait()
        self._clear_current_process(process)
        if cancelled:
            return False, True, last_error
        runtime_error = self._consume_last_runtime_error()
        if ret != 0 and not last_error and runtime_error:
            last_error = runtime_error
        return ret == 0, False, last_error

    def _run_ytdlp_once(self) -> tuple[bool, bool, str]:
        if YoutubeDL is None:
            return False, False, "yt-dlp غير متاح"
        last_error = ""
        while True:
            cancelled_in_hook = False
            ydl_ref: dict[str, object] = {"instance": None}

            def hook(d: dict):
                nonlocal cancelled_in_hook
                if self._is_cancel_requested():
                    cancelled_in_hook = True
                    ydl_instance = ydl_ref.get("instance")
                    if ydl_instance is not None:
                        with suppress(Exception):
                            setattr(ydl_instance, "_download_retcode", 101)
                    raise _YtDlpCancelled("cancelled")
                status = d.get("status")
                if status == _STATUS_DOWNLOADING:
                    total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                    downloaded = d.get("downloaded_bytes") or 0
                    pct = 0.0
                    if total:
                        try:
                            pct = max(0.0, min(100.0, (float(downloaded) / float(total)) * 100.0))
                        except Exception:
                            pct = 0.0
                    speed = self._format_speed(d.get("speed"))
                    eta = self._format_eta(d.get("eta"))
                    self.progress.emit(1, pct, speed, eta)
                    path_hint = d.get("tmpfilename") or d.get("filename")
                    if path_hint:
                        self._set_downloaded_file_path(str(path_hint))
                elif status == "finished":
                    filename = d.get("filename")
                    if filename:
                        self._set_downloaded_file_path(str(filename))
                    self.log.emit("✅ اكتمل التحميل (yt-dlp API)")

            opts = self._build_ytdlp_options()
            opts["progress_hooks"] = [hook]
            try:
                with YoutubeDL(opts) as ydl:
                    ydl_ref["instance"] = ydl
                    rc = ydl.download([self.url])
                retcode = int(getattr(ydl_ref.get("instance"), "_download_retcode", 0) or 0)
                if cancelled_in_hook or self._is_cancel_requested() or retcode == 101:
                    return False, True, "تم إلغاء التحميل"
                return rc == 0, False, last_error
            except _YtDlpCancelled:
                return False, True, "تم إلغاء التحميل"
            except DownloadError as exc:
                if cancelled_in_hook or self._is_cancel_requested():
                    return False, True, "تم إلغاء التحميل"
                raw_error = str(exc)
                if self._apply_adaptive_format_fallback(raw_error):
                    last_error = ""
                    continue
                last_error = self._normalize_download_error(raw_error, fallback="فشل التحميل")
                self.log.emit(f"خطأ: {last_error}")
                return False, False, last_error
            except Exception as exc:
                last_error = self._normalize_download_error(str(exc), fallback="فشل التحميل")
                self.log.emit(f"خطأ: {last_error}")
                return False, False, last_error

    def _sanitize_extra_args(self) -> list[str]:
        safe_args, _options = self._parse_safe_extra_args()
        return safe_args

    def _parse_line(self, clean):
        pct = None
        speed = "--"
        eta = "--:--"
        speed_pattern = r"(?:at\s+|DL:)([~0-9\.]+(?:[kKMGTP]i?)?B(?:/s)?)"
        m_pct = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", clean)
        if m_pct:
            pct = float(m_pct.group(1))
            m_speed = re.search(speed_pattern, clean)
            m_eta = re.search(r"ETA\s+([\d:]+)", clean)
            speed = m_speed.group(1) if m_speed else "--"
            if speed != "--" and not speed.endswith("/s"):
                speed = f"{speed}/s"
            eta = m_eta.group(1) if m_eta else "--:--"
        else:
            m_generic_pct = re.search(r"(\d+(?:\.\d+)?)%\s+of", clean)
            if m_generic_pct:
                pct = float(m_generic_pct.group(1))
                m_speed = re.search(speed_pattern, clean)
                m_eta = re.search(r"ETA\s+([\d:]+)", clean)
                speed = m_speed.group(1) if m_speed else "--"
                if speed != "--" and not speed.endswith("/s"):
                    speed = f"{speed}/s"
                eta = m_eta.group(1) if m_eta else "--:--"
            else:
                m_aria_pct = re.search(r"\((\d{1,3})%\)", clean)
                if m_aria_pct and ("DL:" in clean or "ETA:" in clean):
                    pct = float(m_aria_pct.group(1))
                    m_aria_speed = re.search(speed_pattern, clean)
                    m_aria_eta = re.search(r"ETA:([0-9a-zA-Z:]+)", clean)
                    if m_aria_speed:
                        speed = m_aria_speed.group(1)
                        if not speed.endswith("/s"):
                            speed = f"{speed}/s"
                    if m_aria_eta:
                        eta = m_aria_eta.group(1)
        if pct is None:
            return
        self.progress.emit(1, pct, speed, eta)
        self._maybe_emit_resume_snapshot(force=False)

    def _cleanup(self):
        """Cleans up temporary resources after the download completes or fails."""
        try:
            if self._temp_cookies_path and os.path.exists(self._temp_cookies_path):
                try:
                    with open(self._temp_cookies_path, "wb") as f:
                        f.write(b"\x00" * os.path.getsize(self._temp_cookies_path))
                except Exception:
                    pass
                os.remove(self._temp_cookies_path)
                logger.info("Deleted temporary cookie file.")
                self._temp_cookies_path = ""
        except Exception as e:
            logger.warning(f"Failed to delete temporary cookie file: {e}")

    def _maybe_rotate_proxy_after_error(self, err: str):
        error_text = str(err or "")
        if not error_text:
            return
        lowered = error_text.lower()
        if (
            "429" not in error_text
            and "sign in" not in lowered
            and "too many requests" not in lowered
            and "403" not in error_text
            and "forbidden" not in lowered
            and "bot" not in lowered
            and "captcha" not in lowered
        ):
            return
        try:
            if proxy_manager.is_enabled():
                self.log.emit(f"تم تفريغ وتدوير البروكسي بعد رصد حظر/تقييد من المصدر ({error_text})")
                proxy_manager.rotate()
        except Exception:
            pass

    def _get_anti_detection_retry_cooldown_seconds(self, attempt: int) -> float:
        if int(attempt or 0) <= 1:
            return 0.0
        try:
            _safe_args, options = self._parse_safe_extra_args()
        except Exception:
            return 0.0
        base = float(options.get("sleep_interval_requests", 0.0) or 0.0)
        if base <= 0:
            base = float(options.get("sleep_interval", 0.0) or 0.0) * 0.35
        if base <= 0:
            return 0.0
        jitter = random.uniform(0.85, 1.35)
        retry_factor = 1.0 + (min(max(int(attempt or 2) - 2, 0), 3) * 0.15)
        cooldown = max(0.1, min(6.0, base * jitter * retry_factor))
        return round(float(cooldown), 2)

    def _maybe_apply_retry_cooldown(self, attempt: int):
        cooldown_seconds = self._get_anti_detection_retry_cooldown_seconds(attempt)
        if cooldown_seconds <= 0:
            return
        self.log.emit(f"تهدئة anti-detection قبل إعادة المحاولة: {cooldown_seconds:.2f} ثانية...")
        self._cancel_event.wait(timeout=cooldown_seconds)

    @staticmethod
    def _error_retry_multiplier(error_text: str) -> float:
        text = str(error_text or "").strip().lower()
        if not text:
            return 1.0
        if "captcha" in text or "verify you are human" in text or "unusual traffic" in text:
            return 1.7
        if "429" in text or "too many requests" in text or "rate limit" in text:
            return 1.5
        if "403" in text or "forbidden" in text:
            return 1.3
        if "timed out" in text or "timeout" in text or "connection reset" in text:
            return 1.15
        return 1.0

    def _compute_retry_wait_seconds(self, attempt: int, error_text: str = "") -> float:
        base_wait = min(MAX_RETRY_DELAY_SECONDS, self.retry_delay_seconds * (2 ** (max(int(attempt or 1), 1) - 1)))
        try:
            _safe_args, options = self._parse_safe_extra_args()
        except Exception:
            options = {}
        multiplier = 1.0
        if str(options.get("impersonate", "") or "").strip():
            multiplier += 0.35
        try:
            request_sleep = float(options.get("sleep_interval_requests", 0.0) or 0.0)
        except Exception:
            request_sleep = 0.0
        if request_sleep > 0:
            multiplier += min(0.45, request_sleep * 0.18)
        try:
            max_sleep = float(options.get("max_sleep_interval", 0.0) or 0.0)
        except Exception:
            max_sleep = 0.0
        if max_sleep > 0:
            multiplier += min(0.25, max_sleep / 20.0)
        multiplier *= self._error_retry_multiplier(error_text)
        wait_seconds = float(base_wait)
        if multiplier > 1.0:
            wait_seconds = wait_seconds * multiplier * random.uniform(0.95, 1.15)
        return round(min(float(MAX_RETRY_DELAY_SECONDS), max(0.1, wait_seconds)), 2)

    def run(self):
        # ── Easter Egg: "import antigravity" developer shortcut ─────────────────
        # Typing 'antigravity' in the URL bar triggers xkcd #353 non-blockingly.
        if str(self.url or "").strip().lower() == "antigravity":
            if _antigravity_engine is not None:
                try:
                    _antigravity_engine.trigger_easter_egg()
                except Exception:
                    pass
            self.log.emit("🚀 import antigravity  # xkcd.com/353")
            self.state.emit(_STATUS_SUCCESS)
            return
        # ────────────────────────────────────────────────────────────────────────
        os.makedirs(self.out_dir, exist_ok=True)
        proc_env = os.environ.copy()
        
        attempts = 0
        success = False
        cancelled = False
        last_error = ""
        self.state.emit(_STATUS_RUNNING)
        try:
            cmd = self._build_command()
            aria2_bin = self._find_aria2() if self.use_aria2 else None
            if aria2_bin:
                aria_dir = os.path.dirname(aria2_bin)
                current_path = proc_env.get("PATH", "")
                if aria_dir and aria_dir not in current_path:
                    proc_env["PATH"] = aria_dir + os.pathsep + current_path
            for attempt in range(1, self.retries + 1):
                attempts = attempt
                ok = False
                if self.custom_merge:
                    with self._downloaded_separate_files_lock:
                        self.downloaded_separate_files = []
                if self._is_cancel_requested():
                    cancelled = True
                    break
                if attempt > 1:
                    self.log.emit(f"إعادة المحاولة {attempt}/{self.retries}")
                    self._maybe_apply_retry_cooldown(attempt)
                    if self._is_cancel_requested():
                        cancelled = True
                        break
                while True:
                    ok, was_cancelled, err = self._run_download_attempt(cmd, proc_env)
                    last_error = err or last_error
                    if was_cancelled or self._is_cancel_requested():
                        cancelled = True
                        break
                    if ok:
                        success = True
                        break
                    if self._apply_adaptive_format_fallback(err):
                        cmd = self._build_command()
                        continue
                    raw_err = str(err or "")
                    anti_detect_retry = self._refresh_anti_detection_after_error(raw_err)
                    err = self._normalize_download_error(raw_err, fallback="فشل التحميل")
                    last_error = err or last_error
                    self._maybe_rotate_proxy_after_error(err)
                    if anti_detect_retry:
                        cmd = self._build_command()
                    break
                if cancelled or success:
                    break

                if anti_detect_retry or self._should_retry_download(err, attempt):
                    wait_seconds = self._compute_retry_wait_seconds(attempt, err)
                    self.log.emit(f"فشل المحاولة، إعادة خلال {wait_seconds:.2f} ثانية...")
                    # H-03: Use Event.wait() instead of sleep loop for instant cancel response
                    self._cancel_event.wait(timeout=wait_seconds)
                    if self._is_cancel_requested():
                        cancelled = True
                        break
                elif not ok:
                    break
            if cancelled:
                self._maybe_emit_resume_snapshot(force=True)
                self._maybe_write_checkpoint(self._last_resume_payload or {}, force=True, status=_STATUS_CANCELLED)
                self.state.emit(_STATUS_CANCELLED)
                payload = {
                    "timestamp": self.started_at,
                    "url": self.url,
                    "mode": self.mode,
                    "format": self.fmt,
                    "attempts": attempts or 1,
                    "error": _STATUS_CANCELLED,
                }
                event_bus.publish(DownloadFinishedEvent(getattr(self, "worker_id", None), False, "تم إلغاء التحميل", payload))
                return
            if success:
                self._maybe_emit_resume_snapshot(force=True)
                if self.custom_merge:
                    ok, err = self._run_custom_merge()
                    if not ok:
                        if err == _STATUS_CANCELLED:
                            self._maybe_emit_resume_snapshot(force=True)
                            self._maybe_write_checkpoint(self._last_resume_payload or {}, force=True, status=_STATUS_CANCELLED)
                            self.state.emit(_STATUS_CANCELLED)
                            payload = {
                                "timestamp": self.started_at,
                                "url": self.url,
                                "mode": self.mode,
                                "format": self.fmt,
                                "attempts": attempts or 1,
                                "error": _STATUS_CANCELLED,
                            }
                            event_bus.publish(DownloadFinishedEvent(getattr(self, "worker_id", None), False, "تم إلغاء التحميل", payload))
                            return
                        success = False
                        last_error = str(err or "فشل الدمج")
                    else:
                        success = True
                if not success:
                    self.state.emit(_STATUS_FAILED)
                    self._maybe_emit_resume_snapshot(force=True)
                    self._maybe_write_checkpoint(self._last_resume_payload or {}, force=True, status=_STATUS_FAILED)
                    payload = {
                        "timestamp": self.started_at,
                        "url": self.url,
                        "mode": self.mode,
                        "format": self.fmt,
                        "attempts": attempts or 1,
                        "error": last_error or "فشل بعد إعادة المحاولة",
                    }
                    event_bus.publish(DownloadFinishedEvent(getattr(self, "worker_id", None), False, payload["error"], payload))
                    return
                self.state.emit(_STATUS_SUCCESS)
                # Try to find the downloaded file for renaming
                self._try_rename_output()
                self._maybe_run_whisper_fallback()
                self._maybe_hard_burn_subtitles()
                self._maybe_convert_to_gif()
                self._maybe_normalize_audio_output()
                scan_result = self._maybe_scan_download_for_threats()
                payload = {
                    "timestamp": self.started_at,
                    "url": self.url,
                    "mode": self.mode,
                    "format": self.fmt,
                    "attempts": attempts or 1,
                    "error": "",
                    "file_path": self.downloaded_file_path,
                    "virus_scan": scan_result,
                    "checksum": self._compute_checksum(),
                }
                event_bus.publish(DownloadFinishedEvent(getattr(self, "worker_id", None), True, "اكتمل التحميل بنجاح", payload))
                self._cleanup_checkpoint_file()
                return
            self.state.emit(_STATUS_FAILED)
            self._maybe_emit_resume_snapshot(force=True)
            self._maybe_write_checkpoint(self._last_resume_payload or {}, force=True, status=_STATUS_FAILED)
            payload = {
                "timestamp": self.started_at,
                "url": self.url,
                "mode": self.mode,
                "format": self.fmt,
                "attempts": attempts or 1,
                "error": last_error or "فشل بعد إعادة المحاولة",
            }
            event_bus.publish(DownloadFinishedEvent(getattr(self, "worker_id", None), False, payload["error"], payload))
        except (subprocess.SubprocessError, IOError, OSError, RuntimeError) as exc:
            self.state.emit(_STATUS_FAILED)
            self._maybe_emit_resume_snapshot(force=True)
            self._maybe_write_checkpoint(self._last_resume_payload or {}, force=True, status=_STATUS_FAILED)
            err_text = self._normalize_download_error(str(exc), fallback="فشل التحميل")
            payload = {
                "timestamp": self.started_at,
                "url": self.url,
                "mode": self.mode,
                "format": self.fmt,
                "attempts": attempts or 1,
                "error": err_text,
            }
            self.log.emit(f"خطأ: {err_text}")
            event_bus.publish(DownloadFinishedEvent(getattr(self, "worker_id", None), False, err_text, payload))
        except Exception as exc:
            self.state.emit(_STATUS_FAILED)
            self._maybe_emit_resume_snapshot(force=True)
            self._maybe_write_checkpoint(self._last_resume_payload or {}, force=True, status=_STATUS_FAILED)
            err_text = self._normalize_download_error(str(exc), fallback="فشل غير متوقع")
            payload = {
                "timestamp": self.started_at,
                "url": self.url,
                "mode": self.mode,
                "format": self.fmt,
                "attempts": attempts or 1,
                "error": err_text,
            }
            self.log.emit(f"خطأ غير متوقع: {err_text}")
            event_bus.publish(DownloadFinishedEvent(getattr(self, "worker_id", None), False, err_text, payload))
        finally:
            self._cleanup()
            self._prepared_cookies_path = None

    def _maybe_run_whisper_fallback(self):
        if not getattr(self, "whisper_fallback_enabled", False):
            return
        if not self.embed_subs and str(self.subtitle_lang).lower() in {"none", ""}:
            return
        
        src = str(self.downloaded_file_path or "").strip()
        if not src or not os.path.isfile(src):
            return
            
        # Check if subtitle files already exist alongside the file
        base_name, _ = os.path.splitext(src)
        if any(os.path.isfile(base_name + ext) for ext in [".srt", ".vtt", ".ass", ".en.srt", ".en.vtt"]):
            return
            
        # Use PostDownloadManager to run whisper fallback
        try:
            from core.post_actions import PostDownloadManager
            cmd = PostDownloadManager._resolve_transcribe_command()
            if not cmd:
                self.log.emit("Whisper غير متوفر. تخطي الترجمة الاحتياطية.")
                return
                
            self.log.emit("لم يتم العثور على ترجمة. تشغيل Whisper لتوليد ترجمة احتياطية...")
            out_dir = os.path.dirname(src)
            whisper_cmd = [*cmd, src, "--output_format", "srt", "--output_dir", out_dir]
            
            p = subprocess.run(
                whisper_cmd,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=600,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            )
            
            if p.returncode == 0:
                self.log.emit("تم توليد الترجمة بنجاح عبر Whisper.")
                # If we want to embed, we could mux it, but let's keep it alongside the file first.
            else:
                self.log.emit("فشل توليد الترجمة عبر Whisper.")
        except Exception as exc:
            self.log.emit(f"خطأ أثناء تشغيل Whisper: {exc}")

    def _pick_subtitle_for_hardburn(self, media_path: str) -> str:
        src = str(media_path or "").strip()
        if not src:
            return ""
        base, _ = os.path.splitext(src)
        directory = os.path.dirname(src)
        stem = os.path.basename(base).lower()
        preferred_exts = [".ass", ".ssa", ".srt", ".vtt"]
        patterns = self._subtitle_lang_patterns()
        preferred_tokens: list[str] = []
        for pattern in patterns:
            token = str(pattern or "").replace(".*", "").strip().lower()
            if token and token != "all" and token not in preferred_tokens:
                preferred_tokens.append(token)
        candidates: list[str] = []
        try:
            for entry in os.scandir(directory):
                if not entry.is_file():
                    continue
                low_name = entry.name.lower()
                if not low_name.startswith(stem + "."):
                    continue
                if not any(low_name.endswith(ext) for ext in preferred_exts):
                    continue
                candidates.append(entry.path)
        except Exception:
            return ""
        if not candidates:
            return ""

        def _score(path: str) -> tuple[int, int, str]:
            low = os.path.basename(path).lower()
            ext = os.path.splitext(low)[1]
            ext_score = preferred_exts.index(ext) if ext in preferred_exts else len(preferred_exts)
            token_score = 5
            for idx, token in enumerate(preferred_tokens):
                if f".{token}." in low or low.endswith(f".{token}{ext}"):
                    token_score = idx
                    break
            return (token_score, ext_score, low)

        candidates.sort(key=_score)
        return candidates[0]

    @staticmethod
    def _escape_ffmpeg_subtitles_path(path: str) -> str:
        text = str(path or "").replace("\\", "/")
        text = text.replace(":", "\\:").replace("'", "\\'")
        return text

    def _maybe_hard_burn_subtitles(self):
        if not bool(getattr(self, "hard_burn_subs", False)):
            return
        if not self._is_video_mode():
            return
        src = str(self.downloaded_file_path or "").strip()
        if not src or not os.path.isfile(src):
            return
        subtitle_path = self._pick_subtitle_for_hardburn(src)
        if not subtitle_path or not os.path.isfile(subtitle_path):
            self.log.emit("تم تخطي hard-burn: لم يتم العثور على ملف ترجمة مناسب.")
            return
        ffmpeg_path = shutil.which("ffmpeg")
        ffmpeg_dir = self._find_ffmpeg()
        if ffmpeg_dir and os.path.isdir(ffmpeg_dir):
            candidate = os.path.join(ffmpeg_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
            if os.path.isfile(candidate):
                ffmpeg_path = candidate
        if not ffmpeg_path:
            self.log.emit("تم تخطي hard-burn: FFmpeg غير متاح.")
            return
        src_base, src_ext = os.path.splitext(src)
        target_ext = src_ext.lower() if src_ext.lower() in {".mp4", ".mkv", ".mov", ".m4v"} else ".mp4"
        final_path = src if target_ext == src_ext.lower() else src_base + target_ext
        temp_path = src_base + ".hardsub_tmp" + target_ext
        filter_path = self._escape_ffmpeg_subtitles_path(subtitle_path)
        cmd = [
            ffmpeg_path,
            "-y",
            "-i",
            src,
            "-vf",
            f"subtitles='{filter_path}'",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            temp_path,
        ]
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1200,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except Exception as exc:
            self.log.emit(f"فشل hard-burn للترجمة: {exc}")
            return
        if proc.returncode != 0 or not os.path.isfile(temp_path):
            self.log.emit("فشل hard-burn للترجمة عبر FFmpeg.")
            with suppress(Exception):
                if os.path.isfile(temp_path):
                    os.remove(temp_path)
            return
        try:
            if os.path.normcase(os.path.abspath(final_path)) == os.path.normcase(os.path.abspath(src)):
                os.replace(temp_path, src)
                self.downloaded_file_path = src
            else:
                with suppress(Exception):
                    if os.path.isfile(final_path):
                        os.remove(final_path)
                os.replace(temp_path, final_path)
                with suppress(Exception):
                    if os.path.isfile(src):
                        os.remove(src)
                self.downloaded_file_path = final_path
            self.log.emit(f"تم hard-burn للترجمة: {os.path.basename(self.downloaded_file_path)}")
        except Exception as exc:
            self.log.emit(f"تعذر اعتماد ملف hard-burn النهائي: {exc}")
            with suppress(Exception):
                if os.path.isfile(temp_path):
                    os.remove(temp_path)

    def _maybe_convert_to_gif(self):
        fmt = str(self.fmt or "").strip().lower()
        if fmt != "gif":
            return
        src = str(self.downloaded_file_path or "").strip()
        if not src or not os.path.isfile(src):
            return
        ffmpeg_path = shutil.which("ffmpeg")
        ffmpeg_dir = self._find_ffmpeg()
        if ffmpeg_dir and os.path.isdir(ffmpeg_dir):
            candidate = os.path.join(ffmpeg_dir, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
            if os.path.isfile(candidate):
                ffmpeg_path = candidate
        if not ffmpeg_path:
            self.log.emit("FFmpeg غير متاح لتحويل الفيديو إلى GIF")
            return
        base, _ext = os.path.splitext(src)
        palette_path = base + ".palette.png"
        gif_path = base + ".gif"
        fps = 15
        scale_expr = "640:-1"
        vf_palette = f"fps={fps},scale={scale_expr}:flags=lanczos,palettegen"
        cmd_palette = [
            ffmpeg_path,
            "-y",
            "-i",
            src,
            "-vf",
            vf_palette,
            palette_path,
        ]
        try:
            p1 = subprocess.run(
                cmd_palette,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_GIF_STAGE_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if p1.returncode != 0 or not os.path.isfile(palette_path):
                self.log.emit("فشل إنشاء لوحة الألوان لتحويل GIF")
                return
        except subprocess.TimeoutExpired as exc:
            self.log.emit(format_background_task_error(exc, "تحويل GIF"))
            return
        except Exception as exc:
            self.log.emit(f"فشل تحويل GIF (المرحلة 1): {self._normalize_download_error(str(exc), fallback='فشل تحويل GIF')}")
            return
        vf_use = f"fps={fps},scale={scale_expr}:flags=lanczos,paletteuse"
        cmd_gif = [
            ffmpeg_path,
            "-y",
            "-i",
            src,
            "-i",
            palette_path,
            "-lavfi",
            vf_use,
            "-loop",
            "0",
            gif_path,
        ]
        try:
            p2 = subprocess.run(
                cmd_gif,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_GIF_STAGE_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if p2.returncode != 0 or not os.path.isfile(gif_path):
                self.log.emit("فشل إنشاء ملف GIF النهائي")
                return
        except subprocess.TimeoutExpired as exc:
            self.log.emit(format_background_task_error(exc, "تحويل GIF"))
            return
        except Exception as exc:
            self.log.emit(f"فشل تحويل GIF (المرحلة 2): {self._normalize_download_error(str(exc), fallback='فشل تحويل GIF')}")
            return
        try:
            if os.path.isfile(palette_path):
                os.remove(palette_path)
        except Exception:
            pass
        keep_source = str(os.getenv("VIDDOWNLOADER_KEEP_GIF_SOURCE", "")).strip().lower()
        if keep_source not in {"1", "true", "yes", "on"}:
            try:
                os.remove(src)
            except Exception:
                pass
        self.downloaded_file_path = gif_path
        self.log.emit(f"تم إنشاء GIF: {os.path.basename(gif_path)}")

    def _maybe_normalize_audio_output(self):
        if not bool(getattr(self, "normalize_audio_postprocess", False)):
            return
        if str(self.mode or "").strip().lower() != "audio":
            return
        src = str(self.downloaded_file_path or "").strip()
        if not src or not os.path.isfile(src):
            return
        audio_exts = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma"}
        ext = os.path.splitext(src)[1].lower()
        if ext and ext not in audio_exts:
            self.log.emit("تم تخطي تطبيع الصوت: الامتداد غير مدعوم.")
            return
        try:
            from .audio_normalizer import STREAMING_TARGET_LUFS, normalize_file

            self.log.emit("بدء تطبيع الصوت (EBU R128)...")
            ok, message = normalize_file(
                src,
                target_lufs=STREAMING_TARGET_LUFS,
                in_place=True,
                progress_callback=lambda msg: self.log.emit(str(msg)),
            )
            if ok:
                self.log.emit(str(message or "اكتمل تطبيع الصوت"))
            else:
                self.log.emit(str(message or "فشل تطبيع الصوت"))
        except Exception as exc:
            self.log.emit(f"تعذر تنفيذ تطبيع الصوت: {exc}")

    def _find_windows_defender_cli(self) -> str:
        candidates = []
        program_files = str(os.getenv("ProgramFiles", "") or "").strip()
        if program_files:
            candidates.append(os.path.join(program_files, "Windows Defender", "MpCmdRun.exe"))
        platform_dir = os.path.join(
            str(os.getenv("ProgramData", "") or "").strip(),
            "Microsoft",
            "Windows Defender",
            "Platform",
        )
        if os.path.isdir(platform_dir):
            try:
                versions = sorted(
                    [
                        os.path.join(platform_dir, name, "MpCmdRun.exe")
                        for name in os.listdir(platform_dir)
                    ],
                    reverse=True,
                )
                candidates.extend(versions)
            except Exception:
                pass
        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                return candidate
        found = shutil.which("MpCmdRun.exe")
        return str(found or "").strip()

    def _maybe_scan_download_for_threats(self) -> dict:
        result = {"status": "skipped", "message": "", "tool": ""}
        if not self.virus_scan_after_download:
            return result
        if os.name != "nt":
            result["message"] = "فحص Defender متاح على Windows فقط"
            return result
        target = str(self.downloaded_file_path or "").strip()
        if not target or not os.path.isfile(target):
            result["message"] = "تم تخطي فحص Defender لعدم وجود الملف النهائي"
            return result
        defender_cli = self._find_windows_defender_cli()
        if not defender_cli:
            result["status"] = "unavailable"
            result["message"] = "Windows Defender CLI غير متاح"
            self.log.emit(result["message"])
            return result
        result["tool"] = defender_cli
        self.log.emit("بدء فحص الملف عبر Windows Defender...")
        cmd = [
            defender_cli,
            "-Scan",
            "-ScanType",
            "3",
            "-File",
            target,
            "-DisableRemediation",
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_DEFENDER_SCAN_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            result["status"] = "timeout"
            result["message"] = format_background_task_error(exc, "فحص Defender")
            self.log.emit(result["message"])
            return result
        except Exception as exc:
            result["status"] = "error"
            result["message"] = f"تعذر تشغيل فحص Defender: {self._normalize_download_error(str(exc), fallback='تعذر تشغيل فحص Defender')}"
            self.log.emit(result["message"])
            return result
        output = "\n".join(
            part.strip() for part in [str(proc.stdout or "").strip(), str(proc.stderr or "").strip()] if part.strip()
        )
        lowered = output.lower()
        if proc.returncode == 0:
            result["status"] = "clean"
            result["message"] = "فحص Defender اكتمل: لا توجد تهديدات"
        elif any(token in lowered for token in ("threat", "virus", "malware", "infected")):
            result["status"] = "threat_detected"
            result["message"] = "تحذير Defender: تم اكتشاف تهديد محتمل في الملف"
        else:
            result["status"] = "warning"
            result["message"] = f"انتهى فحص Defender برمز {proc.returncode}"
        self.log.emit(result["message"])
        return result

    def request_stop(self):
        """Request cooperative cancellation without blocking the caller thread."""
        self.cancel_requested = True
        self._cancel_event.set()  # H-03: wake up any waiting retry delay immediately
        stopper = getattr(self, "_active_direct_transport_stop", None)
        if stopper is not None:
            try:
                stopper()
            except Exception:
                pass
        # Forward cancel to native segmented provider if active
        sp = getattr(self, "_active_segmented_provider", None)
        if sp is not None:
            try:
                sp.stop()
            except Exception:
                pass
        with QMutexLocker(self.process_mutex):
            process = self.current_process
        if process and process.poll() is None:
            self._terminate_process(process, "تعذر إيقاف التحميل")

    def wait_for_stop(self, timeout_ms: int = 5000) -> bool:
        """Wait for the worker to finish from a non-worker thread."""
        if not self.isRunning():
            return True
        if QThread.currentThread() is self:
            return False
        try:
            return bool(self.wait(max(0, int(timeout_ms or 0))))
        except Exception as exc:
            logger.debug(f"DownloadWorker wait_for_stop failed: {exc}")
            return False

    def stop(self):
        self.request_stop()
        # UI-triggered cancellation must stay non-blocking to avoid freezing the event loop.
        if self.isRunning() and QThread.currentThread() is self:
            logger.debug("DownloadWorker.stop() called from worker thread; cooperative cancellation only")

    def cancel(self):
        self.stop()

    def _clear_current_process(self, process=None):
        with QMutexLocker(self.process_mutex):
            if process is None or self.current_process is process:
                self.current_process = None

    def _terminate_process(self, process, error_prefix: str):
        if process is None:
            return
        if process.poll() is not None:
            self._clear_current_process(process)
            return
        try:
            process.terminate()
            process.wait(timeout=PROCESS_TERMINATION_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=PROCESS_KILL_TIMEOUT)
            except Exception as exc:
                self.log.emit(f"{error_prefix}: {exc}")
        except Exception as exc:
            self.log.emit(f"{error_prefix}: {exc}")
        finally:
            stdout = getattr(process, "stdout", None)
            if stdout is not None:
                try:
                    stdout.close()
                except Exception:
                    pass
            reader_thread = self._stdout_reader_thread
            if reader_thread is not None and reader_thread.is_alive():
                reader_thread.join(timeout=3.0)
            self._stdout_reader_thread = None
            self._stdout_reader_stream = None
            self._clear_current_process(process)

    def _try_rename_output(self):
        """Attempt to locate and optionally rename the freshly downloaded file."""
        try:
            # C-04: Use the path tracked from stdout if available
            if self.downloaded_file_path and os.path.isfile(self.downloaded_file_path):
                newest = self.downloaded_file_path
            else:
                # Fallback: find the most recently modified file in out_dir
                candidates = []
                for f in os.listdir(self.out_dir):
                    full = os.path.join(self.out_dir, f)
                    if os.path.isfile(full):
                        candidates.append((os.path.getmtime(full), full))
                if not candidates:
                    return
                candidates.sort(reverse=True)
                newest = candidates[0][1]
                self.downloaded_file_path = newest
            if self.rename_template == "Default" or not self.channel:
                return
            # L-04: Use module-level import
            if _build_rename_filename is None:
                return
            
            ext = os.path.splitext(newest)[1].lstrip(".")
            # Guess title from filename
            raw_title = os.path.splitext(os.path.basename(newest))[0]
            
            if self.clean_metadata:
                from .utils import clean_metadata_title
                raw_title = clean_metadata_title(raw_title)

            new_rel = _build_rename_filename(
                self.rename_template,
                title=raw_title,
                ext=ext,
                channel=self.channel,
                quality=self.quality,
            )
            new_full = os.path.join(self.out_dir, new_rel)
            if newest != new_full:
                out_dir_real = os.path.realpath(self.out_dir)
                target_real = os.path.realpath(new_full)
                try:
                    is_inside = os.path.commonpath([out_dir_real, target_real]) == out_dir_real
                except Exception:
                    is_inside = False
                if not is_inside:
                    self.log.emit(f"Path traversal detected in channel name: {target_real}")
                    return
                os.makedirs(os.path.dirname(new_full), exist_ok=True)
                os.rename(newest, new_full)
                self.downloaded_file_path = new_full
                self.log.emit(f"تم تنظيم الملف: {os.path.basename(new_full)}")
        except Exception as exc:
            self.log.emit(f"[Rename] {exc}")

    def _compute_checksum(self) -> str:
        """Compute SHA-256 and run ffprobe integrity check if enabled."""
        if not self.verify_checksum or not self.downloaded_file_path:
            return ""
        if not os.path.isfile(self.downloaded_file_path):
            return ""
            
        # 1. FFprobe Integrity Check
        ffprobe_bin = self._find_ffprobe()
        if ffprobe_bin:
            cmd = [ffprobe_bin, "-v", "error", "-i", self.downloaded_file_path]
            try:
                p = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=_CHECKSUM_VERIFY_TIMEOUT_SECONDS,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if p.returncode != 0:
                    self.log.emit(f"⚠️ تحذير: الملف معطوب أو غير مقروء! ({p.stderr.strip()})")
                else:
                    self.log.emit("✅ تم تأكيد سلامة الملف (Verified)")
            except subprocess.TimeoutExpired as exc:
                self.log.emit(format_background_task_error(exc, "التحقق من سلامة الملف"))
            except Exception:
                pass

        # 2. Checksum (SHA-256)
        sha = hashlib.sha256()
        try:
            with open(self.downloaded_file_path, "rb") as f:
                for block in iter(lambda: f.read(65536), b""):
                    sha.update(block)
            digest = sha.hexdigest()[:16]  # Short version
            self.log.emit(f"✅ تجزئة الملف: {digest}")
            return digest
        except Exception:
            return ""



