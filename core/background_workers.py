
import subprocess
import sys
import json
import os
import hashlib
import logging
import threading
import time
import urllib.request
import urllib.parse
import re
import base64
from contextlib import suppress

from .error_handler import format_background_task_error, is_timeout_exception
from .network_safety import (
    extract_response_peer_ip,
    is_basic_hostname,
    resolve_safe_host_snapshot,
    resolve_tcp_host_ips,
)
from .retry_utils import is_retryable_error_text, is_retryable_exception, run_with_retries

try:
    from PySide6.QtCore import QThread, Signal
except ImportError:
    from PyQt6.QtCore import QThread, pyqtSignal as Signal


logger = logging.getLogger("SnapDownloader.BackgroundWorkers")
_SUBPROCESS_TIMEOUT_SECONDS = 180
_NETWORK_REQUEST_TIMEOUT_SECONDS = 8
_DOWNLOAD_REQUEST_TIMEOUT_SECONDS = 20
_RETRY_BACKOFF_SECONDS = (1.0, 2.0, 4.0)
_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
_UPDATE_OVERRIDE_ACK_ENV = "VIDDOWNLOADER_ENABLE_UPDATE_SECURITY_OVERRIDES"
_warned_update_override_flags = set()
_warned_ignored_update_override_flags = set()
_warned_production_override_blocks = set()


def _env_flag_enabled(name: str) -> bool:
    return str(os.getenv(name, "")).strip().lower() in _TRUTHY_ENV_VALUES


def _safe_host_snapshot_for_https_url(raw_url: str):
    parsed = urllib.parse.urlparse(str(raw_url or "").strip())
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return None
    return resolve_safe_host_snapshot(
        parsed.hostname,
        allow_private=False,
        resolver=resolve_tcp_host_ips,
        host_validator=is_basic_hostname,
    )


class _InterruptibleBackgroundThread(QThread):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        self._stop_event.set()
        self.requestInterruption()

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

    def _is_stop_requested(self) -> bool:
        return self._stop_event.is_set() or self.isInterruptionRequested()


def _warn_update_override_enabled(name: str, detail: str) -> None:
    if not _env_flag_enabled(name) or name in _warned_update_override_flags:
        return
    _warned_update_override_flags.add(name)
    logger.warning("[%s] update security override enabled: %s", name, detail)


def _warn_update_override_requires_ack(name: str) -> None:
    if not _env_flag_enabled(name) or name in _warned_ignored_update_override_flags:
        return
    _warned_ignored_update_override_flags.add(name)
    logger.warning(
        "[%s] update security override ignored until %s=1 is also set",
        name,
        _UPDATE_OVERRIDE_ACK_ENV,
    )


def _update_override_enabled(name: str, detail: str) -> bool:
    if not _env_flag_enabled(name):
        return False
    if not _env_flag_enabled(_UPDATE_OVERRIDE_ACK_ENV):
        _warn_update_override_requires_ack(name)
        return False
    _warn_update_override_enabled(name, detail)
    return True


def _is_production_environment() -> bool:
    explicit_env = str(os.getenv("VIDDOWNLOADER_ENV", "") or "").strip().lower()
    if explicit_env in {"prod", "production"}:
        return True
    if _env_flag_enabled("VIDDOWNLOADER_SIGNED_BUILD"):
        return True
    if getattr(sys, "frozen", False) and not _env_flag_enabled("VIDDOWNLOADER_DEV_MODE"):
        return True
    return False


def check_production_safety() -> dict[str, bool | str]:
    """
    Enforce update-override policy. Production or signed builds always disable
    insecure/unsigned update bypass flags even if the environment variables are set.
    """
    production = _is_production_environment()
    allow_insecure_requested = _env_flag_enabled("VIDDOWNLOADER_ALLOW_INSECURE_UPDATES")
    allow_unsigned_requested = _env_flag_enabled("VIDDOWNLOADER_ALLOW_UNSIGNED_UPDATES")
    result = {
        "is_production": production,
        "allow_insecure": False,
        "allow_unsigned": False,
        "reason": "",
    }
    if production:
        blocked_flags = []
        if allow_insecure_requested:
            blocked_flags.append("VIDDOWNLOADER_ALLOW_INSECURE_UPDATES")
            os.environ["VIDDOWNLOADER_ALLOW_INSECURE_UPDATES"] = "0"
        if allow_unsigned_requested:
            blocked_flags.append("VIDDOWNLOADER_ALLOW_UNSIGNED_UPDATES")
            os.environ["VIDDOWNLOADER_ALLOW_UNSIGNED_UPDATES"] = "0"
        if blocked_flags:
            cache_key = tuple(blocked_flags)
            if cache_key not in _warned_production_override_blocks:
                _warned_production_override_blocks.add(cache_key)
                logger.warning(
                    "Update security overrides were force-disabled for production mode: %s",
                    ", ".join(blocked_flags),
                )
        result["reason"] = "production"
        return result
    result["allow_insecure"] = _update_override_enabled(
        "VIDDOWNLOADER_ALLOW_INSECURE_UPDATES",
        "HTTPS enforcement for update manifest/download URLs is disabled.",
    )
    result["allow_unsigned"] = _update_override_enabled(
        "VIDDOWNLOADER_ALLOW_UNSIGNED_UPDATES",
        "unsigned manifests are allowed unless a SHA256 pin is configured.",
    )
    return result


def _unsigned_updates_blocked_message(expected_manifest_sha256: str) -> str:
    if str(expected_manifest_sha256 or "").strip():
        return "Unsigned updates are blocked"
    if _env_flag_enabled("VIDDOWNLOADER_ALLOW_UNSIGNED_UPDATES") and not _env_flag_enabled(_UPDATE_OVERRIDE_ACK_ENV):
        return (
            "Unsigned updates are blocked "
            f"({ _UPDATE_OVERRIDE_ACK_ENV }=1 is required to honor "
            "VIDDOWNLOADER_ALLOW_UNSIGNED_UPDATES=1, or pin "
            "VIDDOWNLOADER_UPDATE_MANIFEST_SHA256)"
        )
    return (
        "Unsigned updates are blocked "
        "(set VIDDOWNLOADER_ALLOW_UNSIGNED_UPDATES=1 together with "
        f"{_UPDATE_OVERRIDE_ACK_ENV}=1, or pin VIDDOWNLOADER_UPDATE_MANIFEST_SHA256)"
    )


def _is_retryable_process_result(returncode: int, stdout: str, stderr: str) -> bool:
    if int(returncode or 0) == 0:
        return False
    text = f"{stderr or ''}\n{stdout or ''}".strip().lower()
    if not text:
        return False
    return is_retryable_error_text(text)


def _terminate_subprocess(process) -> None:
    if process is None:
        return
    with suppress(Exception):
        process.terminate()
        process.wait(timeout=2)
        return
    with suppress(Exception):
        process.kill()


def _run_subprocess_with_retries(
    operation_name: str,
    command: list[str],
    *,
    timeout_seconds: float = _SUBPROCESS_TIMEOUT_SECONDS,
    retry_delays=_RETRY_BACKOFF_SECONDS,
    should_abort=None,
    abort_error_factory=None,
):
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def _attempt(_attempt_number: int, _total_attempts: int):
        process = None
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=creationflags,
            )
            deadline = time.monotonic() + max(1.0, float(timeout_seconds or 1.0))
            while True:
                if callable(should_abort) and should_abort():
                    _terminate_subprocess(process)
                    if callable(abort_error_factory):
                        raise abort_error_factory()
                    raise InterruptedError(f"{operation_name} cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _terminate_subprocess(process)
                    raise TimeoutError(f"{operation_name} timed out")
                try:
                    stdout, stderr = process.communicate(timeout=min(0.5, remaining))
                    result = subprocess.CompletedProcess(
                        command,
                        process.returncode,
                        stdout,
                        stderr,
                    )
                    break
                except subprocess.TimeoutExpired:
                    # Tiny sleep prevents CPU starvation on tight polling intervals
                    time.sleep(0.05)
                    continue
        except subprocess.TimeoutExpired as exc:
            _terminate_subprocess(process)
            raise TimeoutError(f"{operation_name} timed out") from exc
        if _is_retryable_process_result(result.returncode, result.stdout or "", result.stderr or ""):
            raise RuntimeError((result.stderr or result.stdout or f"{operation_name} failed").strip())
        return result

    return run_with_retries(
        operation_name,
        _attempt,
        retry_delays,
        should_retry_exception=lambda exc: is_retryable_exception(
            exc,
            timeout_checker=is_timeout_exception,
        ),
        logger=logger,
        sleep_func=time.sleep,
        should_abort=should_abort,
        abort_error_factory=abort_error_factory,
    )


def _run_simple_subprocess_with_retries(
    operation_name: str,
    command: list[str],
    *,
    timeout_seconds: float = _SUBPROCESS_TIMEOUT_SECONDS,
    retry_delays=_RETRY_BACKOFF_SECONDS,
    should_abort=None,
    abort_error_factory=None,
):
    """
    Simpler subprocess runner that uses subprocess.run (not Popen polling).
    This is the call path used by YtDlpPipUpdateWorker and YtDlpCoreUpdateWorker.
    Tests monkeypatch ``core.background_workers.subprocess.run`` — keeping these
    workers on subprocess.run preserves the mock contract and prevents test hangs.
    """
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def _attempt(_attempt_number: int, _total_attempts: int):
        if callable(should_abort) and should_abort():
            if callable(abort_error_factory):
                raise abort_error_factory()
            raise InterruptedError(f"{operation_name} cancelled")
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(1.0, float(timeout_seconds or 1.0)),
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"{operation_name} timed out") from exc
        if _is_retryable_process_result(result.returncode, result.stdout or "", result.stderr or ""):
            raise RuntimeError((result.stderr or result.stdout or f"{operation_name} failed").strip())
        return result

    return run_with_retries(
        operation_name,
        _attempt,
        retry_delays,
        should_retry_exception=lambda exc: is_retryable_exception(
            exc,
            timeout_checker=is_timeout_exception,
        ),
        logger=logger,
        sleep_func=time.sleep,
        should_abort=should_abort,
        abort_error_factory=abort_error_factory,
    )


class YtDlpPipUpdateWorker(_InterruptibleBackgroundThread):
    finished = Signal(bool, str)

    def run(self):
        try:
            # NOTE: Uses _run_simple_subprocess_with_retries (subprocess.run path)
            # so that test monkeypatches on subprocess.run are correctly intercepted.
            result = _run_simple_subprocess_with_retries(
                "yt-dlp pip update",
                [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
                should_abort=self._is_stop_requested,
                abort_error_factory=lambda: InterruptedError("yt-dlp pip update cancelled"),
            )
            if result.returncode == 0:
                self.finished.emit(True, result.stdout or "")
            else:
                self.finished.emit(False, (result.stderr or result.stdout or "").strip())
        except InterruptedError as exc:
            self.finished.emit(False, str(exc))
        except Exception as exc:
            self.finished.emit(False, format_background_task_error(exc, "yt-dlp pip update"))


class YtDlpCoreUpdateWorker(_InterruptibleBackgroundThread):
    finished = Signal(int, str, str)

    def run(self):
        try:
            # NOTE: Uses _run_simple_subprocess_with_retries (subprocess.run path)
            # so that test monkeypatches on subprocess.run are correctly intercepted.
            result = _run_simple_subprocess_with_retries(
                "yt-dlp core update",
                [sys.executable, "-m", "yt_dlp", "-U"],
                should_abort=self._is_stop_requested,
                abort_error_factory=lambda: InterruptedError("yt-dlp core update cancelled"),
            )
            self.finished.emit(result.returncode, result.stdout or "", result.stderr or "")
        except InterruptedError as exc:
            self.finished.emit(-1, "", str(exc))
        except Exception as exc:
            self.finished.emit(-1, "", format_background_task_error(exc, "yt-dlp core update"))


def _parse_version(value: str) -> tuple:
    raw = str(value or "").strip()
    parts = []
    for chunk in raw.replace("-", ".").replace("+", ".").split("."):
        if not chunk:
            continue
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts or [0])


def is_newer_version(current: str, candidate: str) -> bool:
    return _parse_version(candidate) > _parse_version(current)


def _canonical_manifest_payload(manifest: dict) -> bytes:
    payload = {k: v for k, v in (manifest or {}).items() if k != "signature"}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _verify_manifest_signature(manifest: dict, public_key_pem: str) -> tuple[bool, str]:
    pub = str(public_key_pem or "").strip()
    if not pub:
        return False, "Missing update public key"
    sig_b64 = str((manifest or {}).get("signature", "") or "").strip()
    if not sig_b64:
        return False, "Update manifest is missing signature"
    try:
        signature = base64.b64decode(sig_b64, validate=True)
    except Exception:
        return False, "Update manifest signature is invalid"
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except Exception:
        return False, "cryptography is required to verify signed updates"
    try:
        key = serialization.load_pem_public_key(pub.encode("utf-8"))
        key.verify(signature, _canonical_manifest_payload(manifest), padding.PKCS1v15(), hashes.SHA256())
        return True, ""
    except Exception:
        return False, "Update manifest signature verification failed"


class AppUpdateCheckWorker(_InterruptibleBackgroundThread):
    finished = Signal(bool, dict, str)

    def __init__(self, current_version: str, manifest_url: str):
        super().__init__()
        self.current_version = str(current_version or "").strip()
        self.manifest_url = str(manifest_url or "").strip()

    def run(self):
        if not self.manifest_url:
            self.finished.emit(True, {"available": False}, "")
            return
        try:
            policy = check_production_safety()
            allow_insecure = bool(policy.get("allow_insecure"))
            allow_unsigned = bool(policy.get("allow_unsigned"))
            parsed = urllib.parse.urlparse(self.manifest_url)
            if not allow_insecure and parsed.scheme.lower() != "https":
                self.finished.emit(False, {}, "Update manifest must use HTTPS")
                return
            if not allow_insecure:
                manifest_snapshot = _safe_host_snapshot_for_https_url(self.manifest_url)
                if manifest_snapshot is None:
                    self.finished.emit(False, {}, "Update manifest host is not allowed")
                    return
            req = urllib.request.Request(
                self.manifest_url,
                headers={"User-Agent": "VidDownloader", "Accept": "application/json"},
            )
            def _fetch_manifest(_attempt: int, _total_attempts: int):
                with urllib.request.urlopen(req, timeout=_NETWORK_REQUEST_TIMEOUT_SECONDS) as resp:
                    final_url = str(getattr(resp, "geturl", lambda: self.manifest_url)() or self.manifest_url)
                    if not allow_insecure:
                        final_snapshot = _safe_host_snapshot_for_https_url(final_url)
                        if final_snapshot is None:
                            raise ValueError("Update manifest redirected to an unsafe host")
                        peer_ip = extract_response_peer_ip(resp)
                        if peer_ip and peer_ip not in set(final_snapshot.allowed_ips):
                            raise ValueError("Update manifest response peer IP mismatch")
                    raw_bytes = resp.read()
                return final_url, raw_bytes

            final_url, raw_bytes = run_with_retries(
                "app update check",
                _fetch_manifest,
                _RETRY_BACKOFF_SECONDS,
                should_retry_exception=lambda exc: is_retryable_exception(
                    exc,
                    timeout_checker=is_timeout_exception,
                ),
                logger=logger,
                sleep_func=time.sleep,
                should_abort=self._is_stop_requested,
                abort_error_factory=lambda: InterruptedError("App update check cancelled"),
            )
            if not allow_insecure and urllib.parse.urlparse(final_url).scheme.lower() != "https":
                self.finished.emit(False, {}, "Update manifest redirected to non-HTTPS URL")
                return
            raw = raw_bytes.decode("utf-8", errors="replace")
            expected_manifest_sha256 = str(os.getenv("VIDDOWNLOADER_UPDATE_MANIFEST_SHA256", "") or "").strip().lower()
            if expected_manifest_sha256:
                if not re.fullmatch(r"[a-f0-9]{64}", expected_manifest_sha256):
                    self.finished.emit(False, {}, "VIDDOWNLOADER_UPDATE_MANIFEST_SHA256 is invalid")
                    return
                actual = hashlib.sha256(raw_bytes).hexdigest().lower()
                if actual != expected_manifest_sha256:
                    self.finished.emit(False, {}, "Update manifest SHA256 mismatch")
                    return
            manifest = json.loads(raw or "{}")
            if not isinstance(manifest, dict):
                self.finished.emit(False, {}, "Invalid update manifest")
                return
            public_key_pem = str(os.getenv("VIDDOWNLOADER_UPDATE_PUBLIC_KEY_PEM", "") or "").strip()
            require_signed = _env_flag_enabled("VIDDOWNLOADER_REQUIRE_SIGNED_UPDATES")
            signature_present = bool(str(manifest.get("signature", "") or "").strip())
            if public_key_pem:
                ok, err = _verify_manifest_signature(manifest, public_key_pem)
                if not ok:
                    self.finished.emit(False, {}, err or "Update manifest signature verification failed")
                    return
            elif require_signed or signature_present:
                self.finished.emit(False, {}, "Signed updates require VIDDOWNLOADER_UPDATE_PUBLIC_KEY_PEM")
                return
            elif not allow_unsigned and not expected_manifest_sha256:
                self.finished.emit(False, {}, _unsigned_updates_blocked_message(expected_manifest_sha256))
                return
            latest = str(manifest.get("version", "")).strip()
            if not latest:
                self.finished.emit(False, {}, "Manifest missing version")
                return
            available = is_newer_version(self.current_version, latest)
            payload = dict(manifest)
            payload["available"] = available
            payload["current_version"] = self.current_version
            self.finished.emit(True, payload, "")
        except InterruptedError as exc:
            self.finished.emit(False, {}, str(exc))
        except Exception as exc:
            self.finished.emit(False, {}, format_background_task_error(exc, "App update check"))


class AppUpdateDownloadWorker(_InterruptibleBackgroundThread):
    progress = Signal(int, int)
    finished = Signal(bool, str, str)

    def __init__(self, url: str, expected_sha256: str, out_path: str):
        super().__init__()
        self.url = str(url or "").strip()
        self.expected_sha256 = str(expected_sha256 or "").strip().lower()
        self.out_path = str(out_path or "").strip()

    def run(self):
        if not self.url or not self.out_path:
            self.finished.emit(False, "", "Missing download URL or output path")
            return
        class _UpdateDownloadCancelled(Exception):
            pass
        try:
            policy = check_production_safety()
            allow_insecure = bool(policy.get("allow_insecure"))
            parsed = urllib.parse.urlparse(self.url)
            if not allow_insecure and parsed.scheme.lower() != "https":
                self.finished.emit(False, "", "Update download URL must use HTTPS")
                return
            if not allow_insecure:
                package_snapshot = _safe_host_snapshot_for_https_url(self.url)
                if package_snapshot is None:
                    self.finished.emit(False, "", "Update download host is not allowed")
                    return
            os.makedirs(os.path.dirname(self.out_path), exist_ok=True)
            req = urllib.request.Request(self.url, headers={"User-Agent": "VidDownloader"})

            def _download_once(_attempt: int, _total_attempts: int):
                if self._is_stop_requested():
                    raise _UpdateDownloadCancelled("App update download cancelled")
                with urllib.request.urlopen(req, timeout=_DOWNLOAD_REQUEST_TIMEOUT_SECONDS) as resp:
                    final_url = str(getattr(resp, "geturl", lambda: self.url)() or self.url)
                    if not allow_insecure and urllib.parse.urlparse(final_url).scheme.lower() != "https":
                        raise ValueError("Update download redirected to non-HTTPS URL")
                    if not allow_insecure:
                        final_snapshot = _safe_host_snapshot_for_https_url(final_url)
                        if final_snapshot is None:
                            raise ValueError("Update download redirected to an unsafe host")
                        peer_ip = extract_response_peer_ip(resp)
                        if peer_ip and peer_ip not in set(final_snapshot.allowed_ips):
                            raise ValueError("Update download response peer IP mismatch")
                    total = int(resp.headers.get("Content-Length") or 0)
                    sha = hashlib.sha256()
                    downloaded = 0
                    with open(self.out_path, "wb") as f:
                        while True:
                            if self._is_stop_requested():
                                raise _UpdateDownloadCancelled("App update download cancelled")
                            chunk = resp.read(1024 * 256)
                            if not chunk:
                                break
                            if self._is_stop_requested():
                                raise _UpdateDownloadCancelled("App update download cancelled")
                            f.write(chunk)
                            sha.update(chunk)
                            downloaded += len(chunk)
                            self.progress.emit(downloaded, total)
                return sha.hexdigest().lower()

            digest = run_with_retries(
                "app update download",
                _download_once,
                _RETRY_BACKOFF_SECONDS,
                should_retry_exception=lambda exc: is_retryable_exception(
                    exc,
                    timeout_checker=is_timeout_exception,
                ),
                logger=logger,
                sleep_func=time.sleep,
                should_abort=self._is_stop_requested,
                abort_error_factory=lambda: _UpdateDownloadCancelled("App update download cancelled"),
            )
            if self.expected_sha256 and digest != self.expected_sha256:
                try:
                    os.remove(self.out_path)
                except Exception:
                    pass
                self.finished.emit(False, "", "SHA256 mismatch")
                return
            self.finished.emit(True, self.out_path, "")
        except _UpdateDownloadCancelled as exc:
            with suppress(Exception):
                if os.path.exists(self.out_path):
                    os.remove(self.out_path)
            self.finished.emit(False, "", str(exc))
        except Exception as exc:
            with suppress(Exception):
                if os.path.exists(self.out_path):
                    os.remove(self.out_path)
            self.finished.emit(False, "", format_background_task_error(exc, "App update download"))



