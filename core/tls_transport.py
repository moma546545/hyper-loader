from __future__ import annotations

import os
import re
import tempfile
import time
import urllib.parse
from contextlib import suppress
from typing import Callable

from .network_safety import is_basic_hostname, resolve_safe_host_snapshot, resolve_tcp_host_ips

try:
    from .antigravity_engine import antigravity_engine as _antigravity_engine
except Exception:
    _antigravity_engine = None  # type: ignore[assignment]


_TRUTHY_VALUES = {"1", "true", "yes", "on"}
_DEFAULT_CHUNK_SIZE = 256 * 1024
_DEFAULT_CONNECT_TIMEOUT = 12
_DEFAULT_READ_TIMEOUT = 45


class TransportCancelled(Exception):
    pass


def is_tls_transport_enabled() -> bool:
    return str(os.getenv("VIDDOWNLOADER_ENABLE_TLS_FINGERPRINT_PROVIDER", "")).strip().lower() in _TRUTHY_VALUES


def is_tls_transport_available() -> bool:
    try:
        from curl_cffi import requests as _requests  # noqa: F401
        return True
    except Exception:
        return False


def normalize_impersonation(value: str) -> str:
    token = str(value or "").strip().lower()
    if token in {"chrome", "chrome120", "chrome124"}:
        return "chrome124"
    if token in {"edge", "edge101"}:
        return "edge101"
    if token in {"safari", "safari17", "safari17_0"}:
        return "safari17_0"
    if token in {"firefox", "firefox133"}:
        return "firefox133"
    return "chrome124"


def _derive_filename(url: str, content_disposition: str = "") -> str:
    raw_cd = str(content_disposition or "").strip()
    if raw_cd:
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', raw_cd, flags=re.IGNORECASE)
        if match:
            candidate = urllib.parse.unquote(match.group(1).strip())
            if candidate:
                return candidate
    parsed = urllib.parse.urlparse(str(url or ""))
    name = os.path.basename(urllib.parse.unquote(parsed.path or ""))
    if name and "." in name:
        return name
    return "download.bin"


def _reserve_output_path(out_dir: str, filename: str) -> str:
    candidate = os.path.join(out_dir, filename)
    base, ext = os.path.splitext(candidate)
    suffix = 1
    while os.path.exists(candidate):
        candidate = f"{base}_{suffix}{ext}"
        suffix += 1
    return candidate


def _build_temp_output_path(final_path: str) -> str:
    temp_dir = os.path.dirname(final_path) or os.getcwd()
    prefix = f".{os.path.basename(final_path)}."
    handle, temp_path = tempfile.mkstemp(
        dir=temp_dir,
        prefix=prefix,
        suffix=".tmp",
    )
    os.close(handle)
    return temp_path


def _flush_and_sync(out_file) -> None:
    out_file.flush()
    with suppress(OSError, AttributeError, ValueError):
        os.fsync(out_file.fileno())


def _extract_curl_response_ip(response) -> str:
    if response is None:
        return ""
    candidates = [
        getattr(response, "primary_ip", ""),
        getattr(response, "remote_ip", ""),
        getattr(response, "ip", ""),
    ]
    info_sources = [
        getattr(response, "infos", None),
        getattr(response, "info", None),
    ]
    for info in info_sources:
        if isinstance(info, dict):
            for key in ("primary_ip", "remote_ip", "ip"):
                value = str(info.get(key, "") or "").strip()
                if value:
                    candidates.append(value)
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def download_direct_file(
    *,
    url: str,
    out_dir: str,
    headers: dict[str, str] | None,
    proxy: str,
    impersonate: str,
    cancel_check: Callable[[], bool],
    on_progress: Callable[[int, int, float], None],
) -> tuple[str, int]:
    from curl_cffi import requests as curl_requests

    request_headers = dict(headers or {})
    proxy_text = str(proxy or "").strip()
    proxies = {"http": proxy_text, "https": proxy_text} if proxy_text else None
    timeout = (_DEFAULT_CONNECT_TIMEOUT, _DEFAULT_READ_TIMEOUT)
    session = curl_requests.Session()
    response = session.get(
        str(url or "").strip(),
        headers=request_headers,
        proxies=proxies,
        impersonate=normalize_impersonation(impersonate),
        stream=True,
        timeout=timeout,
        allow_redirects=True,
    )
    try:
        response.raise_for_status()
        final_url = str(getattr(response, "url", url) or url)
        parsed_final = urllib.parse.urlparse(final_url)
        final_host = str(parsed_final.hostname or "").strip()
        snapshot = resolve_safe_host_snapshot(
            final_host,
            allow_private=False,
            resolver=resolve_tcp_host_ips,
            host_validator=is_basic_hostname,
        )
        if snapshot is None:
            raise ValueError("Unsafe final host in TLS transport response")
        peer_ip = _extract_curl_response_ip(response)
        # curl_cffi may not expose peer-ip metadata in all environments.
        # Enforce pinning when the IP is available, otherwise keep behavior compatible.
        if peer_ip and peer_ip not in set(snapshot.allowed_ips):
            raise ValueError("TLS transport peer IP does not match resolved host snapshot")
        total_bytes = int(response.headers.get("Content-Length") or 0)
        filename = _derive_filename(final_url, response.headers.get("Content-Disposition", ""))
        os.makedirs(out_dir, exist_ok=True)
        out_path = _reserve_output_path(out_dir, filename)
        temp_path = _build_temp_output_path(out_path)
        downloaded = 0
        started_at = time.monotonic()
        # Reset the engine for this download task so telemetry is fresh.
        if _antigravity_engine is not None:
            try:
                _antigravity_engine.reset()
            except Exception:
                pass
        with open(temp_path, "wb") as out_file:
            for chunk in response.iter_content(chunk_size=_DEFAULT_CHUNK_SIZE):
                if cancel_check():
                    raise TransportCancelled("\u062a\u0645 \u0625\u0644\u063a\u0627\u0621 \u0627\u0644\u062a\u062d\u0645\u064a\u0644")
                if not chunk:
                    continue
                out_file.write(chunk)
                downloaded += len(chunk)
                elapsed = max(0.001, time.monotonic() - started_at)
                speed_bps = float(downloaded) / elapsed
                # Feed telemetry into the Antigravity engine
                if _antigravity_engine is not None:
                    try:
                        _antigravity_engine.record_speed(speed_bps)
                        if not _antigravity_engine.detect_throttle(speed_bps):
                            _antigravity_engine.on_speed_healthy()
                    except Exception:
                        pass
                on_progress(downloaded, total_bytes, speed_bps)
            _flush_and_sync(out_file)
        out_path = _reserve_output_path(out_dir, filename)
        os.replace(temp_path, out_path)
        temp_path = ""
        return out_path, downloaded
    except Exception:
        with suppress(Exception):
            if "temp_path" in locals() and temp_path and os.path.isfile(temp_path):
                os.remove(temp_path)
        raise
    finally:
        with suppress(Exception):
            response.close()
        with suppress(Exception):
            session.close()
