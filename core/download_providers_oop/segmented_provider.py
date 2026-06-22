from __future__ import annotations
import hashlib
import http.cookiejar
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Optional

from .base import AbstractDownloadProvider
from .ytdlp_provider import _url_is_direct
from core.network_safety import (
    extract_response_peer_ip,
    is_basic_hostname,
    resolve_safe_host_snapshot,
    resolve_tcp_host_ips,
)

logger = logging.getLogger("SnapDownloader.SegmentedProvider")

# ── Constants ────────────────────────────────────────────────────────────────
_DEFAULT_CONNECTIONS = 8
_MIN_CONNECTIONS = 1
_MAX_CONNECTIONS = 16
_MIN_CHUNK_BYTES = 512 * 1024          # 512 KB min chunk
_DEFAULT_CHUNK_BYTES = 2 * 1024 * 1024 # 2 MB default
_MAX_CHUNK_BYTES = 8 * 1024 * 1024     # 8 MB cap
_CONNECT_TIMEOUT = 10                  # seconds
_READ_TIMEOUT = 30                     # seconds
_STALL_TIMEOUT = 45                    # seconds without data
_PROGRESS_INTERVAL = 0.5               # seconds
_RESUME_META_FLUSH_INTERVAL_SECONDS = max(
    0.05,
    min(3.0, float(os.getenv("SNAPDOWNLOADER_RESUME_META_FLUSH_INTERVAL_SECONDS", "0.35") or 0.35)),
)
_RESUME_META_FLUSH_EVERY_CHUNKS = max(
    1,
    min(256, int(os.getenv("SNAPDOWNLOADER_RESUME_META_FLUSH_EVERY_CHUNKS", "8") or 8)),
)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class _SegmentedRangeError(RuntimeError):
    """Raised when the server claims range support but does not honor it correctly."""


def _make_request(url: str, headers: dict | None = None) -> urllib.request.Request:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", _USER_AGENT)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    return req


def _cookie_header_for_url(url: str, cookies_path: str = "") -> str:
    if not cookies_path:
        return ""
    try:
        full = os.path.abspath(str(cookies_path or "").strip())
        if not os.path.isfile(full):
            return ""
        jar = http.cookiejar.MozillaCookieJar()
        jar.load(full, ignore_discard=True, ignore_expires=True)
        parsed = urllib.parse.urlparse(url)
        host = str(parsed.hostname or "").strip().lower()
        path = parsed.path or "/"
        is_secure = parsed.scheme.lower() == "https"
        parts: list[str] = []
        for cookie in jar:
            if cookie.is_expired():
                continue
            domain = str(cookie.domain or "").lstrip(".").lower()
            if domain and host != domain and not host.endswith("." + domain):
                continue
            cookie_path = str(cookie.path or "/")
            normalized_cookie_path = cookie_path.rstrip("/") or "/"
            normalized_request_path = path.rstrip("/") or "/"
            if not normalized_request_path.startswith(normalized_cookie_path):
                continue
            if cookie.secure and not is_secure:
                continue
            if not cookie.name:
                continue
            parts.append(f"{cookie.name}={cookie.value}")
        return "; ".join(parts)
    except Exception as exc:
        logger.debug(f"[Cookies] failed to load cookies for {url}: {exc}")
        return ""


def _head_info(url: str, cookies_path: str = "", allow_private_hosts: bool = False) -> dict:
    """Probe the URL via HEAD request for file size and range support."""
    result = {
        "size": -1,
        "accept_ranges": False,
        "final_url": url,
        "filename": "",
        "head_error": "",
        "head_error_code": "",
        "status_code": 0,
    }
    try:
        headers = {}
        cookie_header = _cookie_header_for_url(url, cookies_path)
        if cookie_header:
            headers["Cookie"] = cookie_header
        req = _make_request(url, headers)
        req.get_method = lambda: "HEAD"  # type: ignore[method-assign]
        with urllib.request.urlopen(req, timeout=_CONNECT_TIMEOUT) as resp:
            result["final_url"] = resp.url or url
            parsed_final = urllib.parse.urlparse(result["final_url"])
            final_host = str(parsed_final.hostname or "").strip()
            snapshot = resolve_safe_host_snapshot(
                final_host,
                allow_private=bool(allow_private_hosts),
                resolver=resolve_tcp_host_ips,
                host_validator=is_basic_hostname,
            )
            if snapshot is None:
                raise ValueError("Unsafe final host in HEAD response")
            peer_ip = extract_response_peer_ip(resp)
            if peer_ip and peer_ip not in set(snapshot.allowed_ips):
                raise ValueError("HEAD response peer IP does not match resolved host snapshot")
            result["accept_ranges"] = (
                resp.headers.get("Accept-Ranges", "").lower() == "bytes"
            )
            cl = resp.headers.get("Content-Length", "")
            if cl and cl.isdigit():
                result["size"] = int(cl)
            cd = resp.headers.get("Content-Disposition", "")
            if cd and "filename=" in cd:
                fname = cd.split("filename=", 1)[1].strip().strip('"').strip("'")
                result["filename"] = fname
    except urllib.error.HTTPError as exc:
        result["status_code"] = int(getattr(exc, "code", 0) or 0)
        result["head_error"] = str(exc)
        if result["status_code"] == 403:
            result["head_error_code"] = "auth_forbidden"
        else:
            result["head_error_code"] = "head_http_error"
        logger.debug(f"[HEAD] HTTP error for {url}: {exc}")
    except Exception as exc:
        result["head_error"] = str(exc)
        result["head_error_code"] = "head_unreachable"
        logger.debug(f"[HEAD] failed for {url}: {exc}")
    return result


def _derive_filename(url: str, content_disp: str = "") -> str:
    """Best-effort filename extraction from URL or Content-Disposition."""
    if content_disp:
        for part in content_disp.split(";"):
            if "filename=" in part.lower():
                return part.split("=", 1)[1].strip().strip('"').strip("'")
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.rstrip("/")
    name = os.path.basename(urllib.parse.unquote(path))
    if name and "." in name:
        return name
    digest = hashlib.sha1(url.encode()).hexdigest()[:8]
    return f"download_{digest}.bin"


class _Chunk:
    """Represents a byte range segment to be downloaded."""
    __slots__ = ("index", "start", "end", "done", "error")

    def __init__(self, index: int, start: int, end: int):
        self.index = index
        self.start = start
        self.end = end            # inclusive
        self.done = False
        self.error: str = ""


class SegmentedProvider(AbstractDownloadProvider):
    """
    Pure-Python multi-connection segmented downloader.

    Features:
    - Byte-range parallel downloads using urllib (no extra deps).
    - Dynamic connection scaling based on measured throughput.
    - Robust pause / resume via threading.Event.
    - Completion detection and atomic file assembly.
    - Falls back to single-connection if server doesn't support ranges.
    """

    # ── Progress callbacks (set by DownloadWorker before calling start) ──────
    on_progress: Optional[Callable[[float, str, str], None]] = None   # (pct, speed, eta)
    on_log: Optional[Callable[[str], None]] = None
    on_path: Optional[Callable[[str], None]] = None
    on_done: Optional[Callable[[bool, str], None]] = None             # (success, error_msg)

    def __init__(self, task: dict, worker: Any):
        super().__init__(task, worker)
        self._pause_event = threading.Event()
        self._pause_event.set()          # un-paused by default
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._downloaded_bytes = 0
        self._total_bytes = 0
        self._start_ts = 0.0
        self._last_progress_ts = 0.0
        self._last_progress_bytes = 0
        self._speed_samples: list[float] = []
        self._cookie_header_cache: dict[str, str] = {}
        self._throttle_lock = threading.Lock()
        self._throttle_next_ts = 0.0
        self._resume_meta_last_flush_ts = 0.0
        self._resume_meta_last_saved_count = 0
        self._active_response_lock = threading.Lock()
        self._active_responses: set[object] = set()

    # ── AbstractDownloadProvider interface ───────────────────────────────────

    @classmethod
    def can_handle(cls, url: str, is_direct: bool = False) -> bool:
        return is_direct or _url_is_direct(url)

    def start(self) -> None:
        success = False
        error = ""
        out_path = ""
        try:
            url = str(self.task.get("url", "")).strip()
            out_dir = str(self.task.get("out_dir", "") or os.getcwd()).strip()
            cookies_path = str(self.task.get("cookies_file", "") or "").strip()
            bandwidth_limit_kbps = int(self.task.get("bandwidth_limit_kbps", 0) or 0)
            allow_private_hosts = bool(self.task.get("allow_private_hosts", False))
            if not url:
                raise ValueError("رابط التحميل غير صالح")
            os.makedirs(out_dir, exist_ok=True)

            self._emit_log(f"🔍 جاري فحص الرابط: {url}")
            info = _head_info(url, cookies_path, allow_private_hosts=allow_private_hosts)
            final_url = info["final_url"]
            total_bytes = info["size"]
            accept_ranges = info["accept_ranges"]
            self._total_bytes = max(0, total_bytes)

            filename = info.get("filename") or _derive_filename(final_url)
            out_path = self._reserve_output_path(out_dir, filename)

            if self.on_path:
                self.on_path(out_path)

            if accept_ranges and total_bytes > 0:
                self._emit_log(
                    f"⚡ الخادم يدعم التقسيم | الحجم: {total_bytes / 1024 / 1024:.1f} MB"
                )
                success, error = self._download_segmented(
                    final_url, out_path, total_bytes,
                    cookies_path=cookies_path,
                    bandwidth_limit_kbps=bandwidth_limit_kbps,
                    allow_private_hosts=allow_private_hosts,
                )
                if (not success) and (not self.is_cancelled) and (not self._cancel_event.is_set()):
                    self._emit_log(
                        f"↩️ فشل التحميل المجزأ ({error or 'unknown reason'})، جاري التحويل إلى التحميل المباشر..."
                    )
                    self._prepare_simple_fallback(out_path)
                    success, error = self._download_simple(
                        final_url,
                        out_path,
                        cookies_path=cookies_path,
                        bandwidth_limit_kbps=bandwidth_limit_kbps,
                        allow_private_hosts=allow_private_hosts,
                    )
            else:
                head_error = str(info.get("head_error", "") or "").strip()
                head_error_code = str(info.get("head_error_code", "") or "").strip()
                status_code = int(info.get("status_code", 0) or 0)
                if head_error:
                    if head_error_code == "auth_forbidden" or status_code == 403:
                        self._emit_log("🔒 فحص HEAD أعاد 403 (يبدو أن الرابط يتطلب صلاحية/كوكيز). سيتم تجربة التحميل المباشر.")
                    else:
                        self._emit_log(f"ℹ️ تعذر فحص HEAD بدقة ({head_error})، سيتم التحميل المباشر.")
                else:
                    self._emit_log("📥 تحميل مباشر (الخادم لا يدعم التقسيم)")
                success, error = self._download_simple(
                    final_url, out_path,
                    cookies_path=cookies_path,
                    bandwidth_limit_kbps=bandwidth_limit_kbps,
                    allow_private_hosts=allow_private_hosts,
                )
        except Exception as exc:
            success = False
            error = str(exc)
            if out_path and os.path.isfile(out_path):
                with suppress_exc():
                    os.remove(out_path)
        if (not success) and out_path and os.path.isfile(out_path):
            with suppress_exc():
                os.remove(out_path)
        if self.on_done:
            self.on_done(success, error)

    def pause(self) -> None:
        self.is_paused = True
        self._pause_event.clear()
        self._emit_log("⏸ التحميل في وضع الإيقاف المؤقت")

    def resume(self) -> None:
        self.is_paused = False
        self._pause_event.set()
        self._emit_log("▶️ استئناف التحميل")

    def stop(self) -> None:
        self.is_cancelled = True
        self._cancel_event.set()
        self._pause_event.set()   # unblock any paused threads
        with self._active_response_lock:
            responses = list(self._active_responses)
            self._active_responses.clear()
        for response in responses:
            with suppress_exc():
                response.close()
        self._emit_log("🛑 تم إلغاء التحميل")

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _emit_log(self, msg: str):
        if self.on_log:
            self.on_log(msg)
        else:
            logger.info(msg)

    def _register_response(self, response) -> None:
        if response is None:
            return
        with self._active_response_lock:
            self._active_responses.add(response)

    def _unregister_response(self, response) -> None:
        if response is None:
            return
        with self._active_response_lock:
            self._active_responses.discard(response)

    def _emit_progress(self, downloaded: int, total: int):
        now = time.monotonic()
        if now - self._last_progress_ts < _PROGRESS_INTERVAL:
            return
        pct = (downloaded / total * 100.0) if total > 0 else 0.0
        elapsed = now - self._start_ts
        speed_bps = downloaded / elapsed if elapsed > 0 else 0
        speed_str = self._fmt_speed(speed_bps)
        eta_str = self._fmt_eta((total - downloaded) / speed_bps) if speed_bps > 0 and total > 0 else "--:--"
        if self.on_progress:
            self.on_progress(pct, speed_str, eta_str)
        self._last_progress_ts = now
        self._last_progress_bytes = downloaded

    def _reserve_output_path(self, out_dir: str, filename: str) -> str:
        base_name = os.path.basename(str(filename or "").strip()) or "download.bin"
        base_path = os.path.join(out_dir, base_name)
        stem, ext = os.path.splitext(base_path)
        candidate = base_path
        suffix = 1
        while True:
            if self._resume_artifacts_exist(candidate) and self._can_reuse_resume_target(candidate):
                return candidate
            try:
                fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return candidate
            except FileExistsError:
                candidate = f"{stem}_{suffix}{ext}"
                suffix += 1

    def _prepare_simple_fallback(self, out_path: str) -> None:
        # Segmented mode may leave a preallocated temp file and chunk metadata.
        # Clear them so the simple stream starts from a clean state.
        self._cleanup_resume_artifacts(out_path, remove_tmp=True)
        with self._lock:
            self._downloaded_bytes = 0

    @staticmethod
    def _resume_artifacts_exist(out_path: str) -> bool:
        tmp_path = out_path + ".sdtmp"
        meta_path = tmp_path + ".meta.json"
        return os.path.isfile(tmp_path) or os.path.isfile(meta_path)

    @staticmethod
    def _can_reuse_resume_target(out_path: str) -> bool:
        if not os.path.exists(out_path):
            return True
        try:
            return os.path.isfile(out_path) and int(os.path.getsize(out_path) or 0) == 0
        except OSError:
            return False

    @staticmethod
    def _cleanup_resume_artifacts(out_path: str, *, remove_tmp: bool = False) -> None:
        tmp_path = out_path + ".sdtmp"
        meta_path = tmp_path + ".meta.json"
        if remove_tmp and os.path.exists(tmp_path):
            with suppress_exc():
                os.remove(tmp_path)
        if os.path.isfile(meta_path):
            with suppress_exc():
                os.remove(meta_path)

    @staticmethod
    def _load_simple_resume_state(
        *,
        out_path: str,
        url: str,
    ) -> tuple[int, int]:
        tmp_path = out_path + ".sdtmp"
        meta_path = tmp_path + ".meta.json"
        if not os.path.isfile(tmp_path):
            return 0, 0
        try:
            tmp_size = max(0, int(os.path.getsize(tmp_path) or 0))
        except OSError:
            return 0, 0
        if tmp_size <= 0:
            return 0, 0
        try:
            with open(meta_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                return 0, 0
            if str(payload.get("mode", "")).strip().lower() != "simple":
                return 0, 0
            if str(payload.get("url", "")).strip() != str(url or "").strip():
                return 0, 0
            total_hint = int(payload.get("total_bytes", 0) or 0)
            done_hint = int(payload.get("downloaded_bytes", 0) or 0)
            done = max(0, min(tmp_size, done_hint if done_hint > 0 else tmp_size))
            return done, max(0, total_hint)
        except Exception:
            return tmp_size, 0

    @staticmethod
    def _save_simple_resume_state(
        *,
        out_path: str,
        url: str,
        downloaded_bytes: int,
        total_bytes: int,
    ) -> None:
        tmp_path = out_path + ".sdtmp"
        meta_path = tmp_path + ".meta.json"
        payload = {
            "mode": "simple",
            "url": str(url or ""),
            "downloaded_bytes": max(0, int(downloaded_bytes or 0)),
            "total_bytes": max(0, int(total_bytes or 0)),
            "updated_at": int(time.time()),
        }
        tmp_meta_path = meta_path + ".tmp"
        with open(tmp_meta_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_meta_path, meta_path)

    def _resolve_bandwidth_limit_kbps(self, fallback_limit_kbps: int) -> int:
        worker_limit_fn = getattr(self.worker, "get_dynamic_bandwidth_limit_kbps", None)
        if callable(worker_limit_fn):
            try:
                return max(0, int(worker_limit_fn()))
            except Exception:
                return max(0, int(fallback_limit_kbps or 0))
        return max(0, int(fallback_limit_kbps or 0))

    def _apply_bandwidth_throttle(self, size_bytes: int, fallback_limit_kbps: int) -> None:
        limit_kbps = self._resolve_bandwidth_limit_kbps(fallback_limit_kbps)
        if limit_kbps <= 0 or size_bytes <= 0:
            return
        rate_bps = float(limit_kbps) * 1024.0
        required_window = float(size_bytes) / max(1.0, rate_bps)
        with self._throttle_lock:
            now = time.monotonic()
            start_ts = max(now, self._throttle_next_ts)
            self._throttle_next_ts = start_ts + required_window
            sleep_for = start_ts - now
        while sleep_for > 0 and not self._cancel_event.is_set():
            self._cancel_event.wait(timeout=min(0.25, sleep_for))
            sleep_for -= 0.25

    def _request_headers(self, url: str, headers: dict | None = None, cookies_path: str = "") -> dict:
        merged = dict(headers or {})
        if cookies_path and "Cookie" not in merged:
            cookie_header = self._cookie_header_cache.get(url)
            if cookie_header is None:
                cookie_header = _cookie_header_for_url(url, cookies_path)
                self._cookie_header_cache[url] = cookie_header
            if cookie_header:
                merged["Cookie"] = cookie_header
        return merged

    @staticmethod
    def _fmt_speed(bps: float) -> str:
        if bps <= 0:
            return "--"
        for unit in ("B/s", "KiB/s", "MiB/s", "GiB/s"):
            if bps < 1024:
                return f"{bps:.1f} {unit}"
            bps /= 1024
        return f"{bps:.1f} GiB/s"

    @staticmethod
    def _fmt_eta(sec: float) -> str:
        try:
            t = int(max(0, sec))
        except Exception:
            return "--:--"
        h, rem = divmod(t, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def _compute_connections(self, total_bytes: int) -> int:
        """Smart connection scaling based on file size."""
        if total_bytes <= 0:
            return 1
        mb = total_bytes / (1024 * 1024)
        if mb < 5:
            return 2
        if mb < 20:
            return 4
        if mb < 100:
            return 8
        return min(_MAX_CONNECTIONS, _DEFAULT_CONNECTIONS)

    def _compute_chunk_size(self, total_bytes: int, n_connections: int) -> int:
        """Compute balanced chunk size."""
        if total_bytes <= 0 or n_connections <= 1:
            return _DEFAULT_CHUNK_BYTES
        ideal = total_bytes // (n_connections * 4)  # ~4 passes per connection
        return max(_MIN_CHUNK_BYTES, min(_MAX_CHUNK_BYTES, ideal))

    def _download_chunk(
        self,
        chunk: _Chunk,
        url: str,
        tmp_path: str,
        cookies_path: str,
        bandwidth_limit_kbps: int,
        allow_private_hosts: bool = False,
    ) -> bool:
        """Download a single byte-range chunk. Returns True on success."""
        attempt = 0
        max_attempts = 3
        while attempt < max_attempts:
            if self._cancel_event.is_set():
                chunk.error = "cancelled"
                return False
            self._pause_event.wait()    # blocks while paused
            if self._cancel_event.is_set():
                chunk.error = "cancelled"
                return False
            resp = None
            try:
                headers = self._request_headers(
                    url,
                    headers={"Range": f"bytes={chunk.start}-{chunk.end}"},
                    cookies_path=cookies_path,
                )
                req = _make_request(url, headers)
                with urllib.request.urlopen(req, timeout=_READ_TIMEOUT) as resp:
                    self._register_response(resp)
                    status_code = int(getattr(resp, "status", 0) or resp.getcode() or 0)
                    self._validate_chunk_response(
                        chunk=chunk,
                        response_url=str(getattr(resp, "url", "") or url),
                        status_code=status_code,
                        headers=resp.headers,
                    )
                    final_url = str(getattr(resp, "url", "") or url)
                    parsed_final = urllib.parse.urlparse(final_url)
                    final_host = str(parsed_final.hostname or "").strip()
                    snapshot = resolve_safe_host_snapshot(
                        final_host,
                        allow_private=bool(allow_private_hosts),
                        resolver=resolve_tcp_host_ips,
                        host_validator=is_basic_hostname,
                    )
                    if snapshot is None:
                        raise ValueError("Unsafe final host in segmented response")
                    peer_ip = extract_response_peer_ip(resp)
                    if peer_ip and peer_ip not in set(snapshot.allowed_ips):
                        raise ValueError("Segment peer IP does not match resolved host snapshot")
                    with open(tmp_path, "r+b") as handle:
                        handle.seek(chunk.start)
                        while True:
                            if self._cancel_event.is_set():
                                chunk.error = "cancelled"
                                return False
                            self._pause_event.wait()
                            block = resp.read(65536)  # 64 KB read blocks
                            if not block:
                                break
                            handle.write(block)
                            with self._lock:
                                self._downloaded_bytes += len(block)
                                downloaded_now = self._downloaded_bytes
                            self._emit_progress(downloaded_now, self._total_bytes)
                            self._apply_bandwidth_throttle(len(block), bandwidth_limit_kbps)
                chunk.done = True
                return True
            except _SegmentedRangeError as exc:
                chunk.error = str(exc)
                logger.warning(
                    f"[Segment {chunk.index}] non-retryable range failure: {exc}"
                )
                return False
            except Exception as exc:
                attempt += 1
                chunk.error = str(exc)
                logger.warning(
                    f"[Segment {chunk.index}] attempt {attempt}/{max_attempts} failed: {exc}"
                )
                if self._cancel_event.is_set():
                    return False
                time.sleep(min(2 ** attempt, 8))
            finally:
                self._unregister_response(resp)
        return False

    def _validate_chunk_response(
        self,
        *,
        chunk: _Chunk,
        response_url: str,
        status_code: int,
        headers,
    ) -> None:
        if status_code != 206:
            raise _SegmentedRangeError(
                f"الخادم تجاهل طلب Range للجزء {chunk.index} وأعاد الحالة {status_code or 'unknown'}"
            )
        content_range = str(headers.get("Content-Range", "") or "").strip()
        match = re.match(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", content_range, flags=re.IGNORECASE)
        if not match:
            raise _SegmentedRangeError(
                f"استجابة Range غير صالحة للجزء {chunk.index} من {response_url}"
            )
        start = int(match.group(1))
        end = int(match.group(2))
        if start != int(chunk.start) or end != int(chunk.end):
            raise _SegmentedRangeError(
                f"استجابة Range لا تطابق حدود الجزء {chunk.index}: {start}-{end} != {chunk.start}-{chunk.end}"
            )

    def _download_segmented(
        self,
        url: str,
        out_path: str,
        total_bytes: int,
        cookies_path: str = "",
        bandwidth_limit_kbps: int = 0,
        allow_private_hosts: bool = False,
    ) -> tuple[bool, str]:
        n_connections = self._compute_connections(total_bytes)
        chunk_size = self._compute_chunk_size(total_bytes, n_connections)
        self._emit_log(
            f"⚙️ {n_connections} اتصالات متوازية | حجم القطعة: {chunk_size // 1024} KB"
        )
        tmp_path = out_path + ".sdtmp"
        meta_path = tmp_path + ".meta.json"

        # Build chunk plan
        chunks: list[_Chunk] = []
        pos = 0
        idx = 0
        while pos < total_bytes:
            end = min(pos + chunk_size - 1, total_bytes - 1)
            chunks.append(_Chunk(idx, pos, end))
            pos = end + 1
            idx += 1

        done_indices: set[int] = set()
        chunk_count = len(chunks)
        try:
            if os.path.isfile(tmp_path) and int(os.path.getsize(tmp_path) or 0) == int(total_bytes):
                done_indices = self._load_resume_chunk_indices(
                    meta_path=meta_path,
                    url=url,
                    total_bytes=total_bytes,
                    chunk_size=chunk_size,
                    chunk_count=chunk_count,
                )
            else:
                with open(tmp_path, "wb") as handle:
                    handle.truncate(total_bytes)
                with suppress_exc():
                    if os.path.isfile(meta_path):
                        os.remove(meta_path)
        except Exception as exc:
            return False, f"تعذر تجهيز الملف المؤقت: {exc}"

        resumed_bytes = 0
        for chunk in chunks:
            if chunk.index in done_indices:
                chunk.done = True
                resumed_bytes += (chunk.end - chunk.start + 1)

        with self._lock:
            self._downloaded_bytes = int(resumed_bytes)
        self._start_ts = time.monotonic()
        self._resume_meta_last_saved_count = len(done_indices)
        self._resume_meta_last_flush_ts = float(time.monotonic())
        if resumed_bytes > 0:
            self._emit_log(
                f"↪️ استئناف التحميل من الأجزاء المحفوظة: {len(done_indices)} جزء ({resumed_bytes / 1024 / 1024:.1f} MB)"
            )
            self._emit_progress(self._downloaded_bytes, self._total_bytes)

        failed_chunk_error = ""
        pending_chunks = [chunk for chunk in chunks if not chunk.done]

        if not pending_chunks:
            try:
                os.replace(tmp_path, out_path)
                with suppress_exc():
                    if os.path.isfile(meta_path):
                        os.remove(meta_path)
                self._emit_progress(total_bytes, total_bytes)
                self._emit_log(f"✅ تم التحميل: {os.path.basename(out_path)}")
                return True, ""
            except Exception as exc:
                return False, f"تعذر تجميع الملف النهائي: {exc}"

        with ThreadPoolExecutor(max_workers=n_connections) as pool:
            futures = {
                pool.submit(
                    self._download_chunk,
                    chunk,
                    url,
                    tmp_path,
                    cookies_path,
                    bandwidth_limit_kbps,
                    allow_private_hosts,
                ): chunk
                for chunk in pending_chunks
            }
            for future in as_completed(futures):
                if self._cancel_event.is_set():
                    break
                chunk = futures[future]
                try:
                    ok = future.result()
                    if not ok:
                        failed_chunk_error = chunk.error or "فشل تحميل جزء من الملف"
                        self._cancel_event.set()
                        break
                    if chunk.done:
                        done_indices.add(chunk.index)
                        self._maybe_save_resume_chunk_indices(
                            meta_path=meta_path,
                            url=url,
                            total_bytes=total_bytes,
                            chunk_size=chunk_size,
                            done_indices=done_indices,
                        )
                except Exception as exc:
                    failed_chunk_error = str(exc)
                    self._cancel_event.set()
                    break

        if done_indices and len(done_indices) > int(self._resume_meta_last_saved_count or 0):
            with suppress_exc():
                self._maybe_save_resume_chunk_indices(
                    meta_path=meta_path,
                    url=url,
                    total_bytes=total_bytes,
                    chunk_size=chunk_size,
                    done_indices=done_indices,
                    force=True,
                )

        if self._cancel_event.is_set() and not self.is_cancelled:
            # Internal chunk failure: keep temp + metadata for true resume on next run.
            self._cancel_event.clear()
            return False, failed_chunk_error or "تعذر تحميل بعض أجزاء الملف"
        if self.is_cancelled:
            # User cancel: keep temp + metadata so next retry can resume.
            return False, "تم إلغاء التحميل"

        try:
            os.replace(tmp_path, out_path)
            with suppress_exc():
                if os.path.isfile(meta_path):
                    os.remove(meta_path)
        except Exception as exc:
            return False, f"تعذر تجميع الملف النهائي: {exc}"

        self._emit_progress(total_bytes, total_bytes)
        self._emit_log(f"✅ تم التحميل: {os.path.basename(out_path)}")
        return True, ""

    @staticmethod
    def _load_resume_chunk_indices(
        *,
        meta_path: str,
        url: str,
        total_bytes: int,
        chunk_size: int,
        chunk_count: int,
    ) -> set[int]:
        try:
            with open(meta_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return set()
        if not isinstance(payload, dict):
            return set()
        if str(payload.get("url", "")) != str(url):
            return set()
        if int(payload.get("total_bytes", -1) or -1) != int(total_bytes):
            return set()
        if int(payload.get("chunk_size", -1) or -1) != int(chunk_size):
            return set()
        raw_done = payload.get("done_chunks", [])
        if not isinstance(raw_done, list):
            return set()
        out: set[int] = set()
        for value in raw_done:
            try:
                index = int(value)
            except Exception:
                continue
            if 0 <= index < int(chunk_count):
                out.add(index)
        return out

    @staticmethod
    def _save_resume_chunk_indices(
        *,
        meta_path: str,
        url: str,
        total_bytes: int,
        chunk_size: int,
        done_indices: set[int],
    ) -> None:
        payload = {
            "url": str(url),
            "total_bytes": int(total_bytes),
            "chunk_size": int(chunk_size),
            "done_chunks": sorted(int(i) for i in done_indices),
            "updated_at": int(time.time()),
        }
        tmp_meta_path = meta_path + ".tmp"
        with open(tmp_meta_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_meta_path, meta_path)

    def _maybe_save_resume_chunk_indices(
        self,
        *,
        meta_path: str,
        url: str,
        total_bytes: int,
        chunk_size: int,
        done_indices: set[int],
        force: bool = False,
    ) -> None:
        done_count = len(done_indices or set())
        if done_count <= 0:
            return
        if not force:
            if done_count <= int(self._resume_meta_last_saved_count or 0):
                return
            delta_chunks = done_count - int(self._resume_meta_last_saved_count or 0)
            now = float(time.monotonic())
            elapsed = now - float(self._resume_meta_last_flush_ts or 0.0)
            if delta_chunks < int(_RESUME_META_FLUSH_EVERY_CHUNKS) and elapsed < float(_RESUME_META_FLUSH_INTERVAL_SECONDS):
                return
        self._save_resume_chunk_indices(
            meta_path=meta_path,
            url=url,
            total_bytes=total_bytes,
            chunk_size=chunk_size,
            done_indices=done_indices,
        )
        self._resume_meta_last_saved_count = done_count
        self._resume_meta_last_flush_ts = float(time.monotonic())

    def _download_simple(
        self,
        url: str,
        out_path: str,
        cookies_path: str = "",
        bandwidth_limit_kbps: int = 0,
        allow_private_hosts: bool = False,
    ) -> tuple[bool, str]:
        """Single-connection stream download with progress reporting."""
        self._start_ts = time.monotonic()
        tmp_path = out_path + ".sdtmp"
        resume_offset = 0
        total_hint = 0
        last_checkpoint_ts = 0.0
        response = None
        try:
            resume_offset, total_hint = self._load_simple_resume_state(out_path=out_path, url=url)
            headers = self._request_headers(url, cookies_path=cookies_path)
            if resume_offset > 0:
                headers["Range"] = f"bytes={resume_offset}-"
                self._emit_log(
                    f"↪️ محاولة استئناف التحميل المباشر من {resume_offset / 1024 / 1024:.1f} MB"
                )
            req = _make_request(url, headers)
            response = urllib.request.urlopen(req, timeout=_READ_TIMEOUT)
            self._register_response(response)
            status_code = int(getattr(response, "status", 0) or response.getcode() or 0)
            final_url = str(getattr(response, "url", "") or url)
            parsed_final = urllib.parse.urlparse(final_url)
            final_host = str(parsed_final.hostname or "").strip()
            snapshot = resolve_safe_host_snapshot(
                final_host,
                allow_private=bool(allow_private_hosts),
                resolver=resolve_tcp_host_ips,
                host_validator=is_basic_hostname,
            )
            if snapshot is None:
                raise ValueError("Unsafe final host in simple response")
            peer_ip = extract_response_peer_ip(response)
            if peer_ip and peer_ip not in set(snapshot.allowed_ips):
                raise ValueError("Simple peer IP does not match resolved host snapshot")
            if resume_offset > 0 and status_code != 206:
                # Range ignored by server; restart simple stream from scratch.
                with suppress_exc():
                    response.close()
                response = None
                resume_offset = 0
                headers = self._request_headers(url, cookies_path=cookies_path)
                req = _make_request(url, headers)
                response = urllib.request.urlopen(req, timeout=_READ_TIMEOUT)
                self._register_response(response)
                status_code = int(getattr(response, "status", 0) or response.getcode() or 0)
                final_url = str(getattr(response, "url", "") or url)
                parsed_final = urllib.parse.urlparse(final_url)
                final_host = str(parsed_final.hostname or "").strip()
                snapshot = resolve_safe_host_snapshot(
                    final_host,
                    allow_private=bool(allow_private_hosts),
                    resolver=resolve_tcp_host_ips,
                    host_validator=is_basic_hostname,
                )
                if snapshot is None:
                    raise ValueError("Unsafe final host in restarted simple response")
                peer_ip = extract_response_peer_ip(response)
                if peer_ip and peer_ip not in set(snapshot.allowed_ips):
                    raise ValueError("Restarted simple peer IP does not match resolved host snapshot")
                with suppress_exc():
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                self._emit_log("ℹ️ الخادم لا يدعم استئناف التحميل المباشر لهذا الرابط، سيتم البدء من الصفر.")

            cl = str(response.headers.get("Content-Length", "") or "").strip()
            total = 0
            if status_code == 206 and resume_offset > 0:
                content_range = str(response.headers.get("Content-Range", "") or "").strip()
                match = re.search(r"/(\d+)$", content_range)
                if match:
                    total = int(match.group(1))
                elif cl.isdigit():
                    total = resume_offset + int(cl)
                else:
                    total = max(0, int(total_hint or 0))
            else:
                total = int(cl) if cl.isdigit() else max(0, int(total_hint or 0))
            self._total_bytes = max(0, int(total or 0))
            downloaded = int(resume_offset or 0)
            with self._lock:
                self._downloaded_bytes = downloaded
            open_mode = "r+b" if downloaded > 0 and os.path.isfile(tmp_path) else "wb"
            with open(tmp_path, open_mode) as f:
                if downloaded > 0:
                    f.seek(downloaded)
                while True:
                    if self._cancel_event.is_set():
                        break
                    self._pause_event.wait()
                    block = response.read(65536)
                    if not block:
                        break
                    f.write(block)
                    downloaded += len(block)
                    with self._lock:
                        self._downloaded_bytes = downloaded
                    self._emit_progress(downloaded, self._total_bytes)
                    self._apply_bandwidth_throttle(len(block), bandwidth_limit_kbps)
                    now = time.monotonic()
                    if now - last_checkpoint_ts >= 0.75:
                        self._save_simple_resume_state(
                            out_path=out_path,
                            url=url,
                            downloaded_bytes=downloaded,
                            total_bytes=self._total_bytes,
                        )
                        last_checkpoint_ts = now
            self._save_simple_resume_state(
                out_path=out_path,
                url=url,
                downloaded_bytes=downloaded,
                total_bytes=self._total_bytes,
            )
        except Exception as exc:
            with suppress_exc():
                self._save_simple_resume_state(
                    out_path=out_path,
                    url=url,
                    downloaded_bytes=self._downloaded_bytes,
                    total_bytes=self._total_bytes,
                )
            return False, f"تعذر التحميل: {exc}"
        finally:
            if response is not None:
                self._unregister_response(response)
                with suppress_exc():
                    response.close()

        if self._cancel_event.is_set():
            with suppress_exc():
                self._save_simple_resume_state(
                    out_path=out_path,
                    url=url,
                    downloaded_bytes=self._downloaded_bytes,
                    total_bytes=self._total_bytes,
                )
            return False, "تم إلغاء التحميل"

        try:
            os.replace(tmp_path, out_path)
        except Exception as exc:
            return False, f"تعذر حفظ الملف: {exc}"
        self._cleanup_resume_artifacts(out_path)

        self._emit_log(f"✅ تم التحميل: {os.path.basename(out_path)}")
        return True, ""


class suppress_exc:
    """Minimal contextlib.suppress equivalent to avoid import."""
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return True
