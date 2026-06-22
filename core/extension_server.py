
"""
core/extension_server.py — Local WebSocket Server for Browser Extension
Allows the SnapDownloader browser extension to send links directly to the app.

M-06 FIX: Added proper stop() mechanism using asyncio.Event to cleanly
shut down the WebSocket server instead of relying on daemon thread death.
"""
import asyncio
import base64
import hmac
import ipaddress
import json
import logging
import os
import secrets
import ssl
import sys
import tempfile
import threading
import urllib.parse
from .event_bus import event_bus, ShowNotificationEvent, ExtensionLinkReceivedEvent
from .network_safety import is_basic_hostname, is_safe_host, resolve_tcp_host_ips
from .post_actions import PostDownloadManager
from .secure_storage import get_windows_protector
from .utils import get_app_data_dir, redact_url

logger = logging.getLogger("SnapDownloader.ExtensionServer")

try:
    import keyring
except Exception:
    keyring = None

_TOKEN_SERVICE_NAME = "VidDownloader.Extension"
_TOKEN_ACCOUNT_NAME = "auth_token"


def _atomic_write_json(path: str, payload: dict) -> None:
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="exttoken-", suffix=".json", dir=folder or None)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = None
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp_path, path)
    finally:
        try:
            if fd is not None:
                os.close(fd)
        except Exception:
            pass
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _load_or_create_token() -> str:
    env_token = str(os.getenv("SNAPDOWNLOADER_EXTENSION_TOKEN", "")).strip()
    if env_token:
        return env_token
    token_file = os.path.join(get_app_data_dir(), "extension_token.json")
    protector = get_windows_protector() if os.name == "nt" else None
    if keyring is not None:
        try:
            token = str(keyring.get_password(_TOKEN_SERVICE_NAME, _TOKEN_ACCOUNT_NAME) or "").strip()
            if token:
                try:
                    _atomic_write_json(token_file, {"token_keyring": True})
                    if os.name != "nt":
                        try:
                            os.chmod(token_file, 0o600)
                        except Exception:
                            pass
                except Exception:
                    pass
                return token
        except Exception:
            pass
    if os.path.isfile(token_file):
        try:
            with open(token_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            token_enc = str(data.get("token_encrypted", "")).strip()
            if token_enc and protector is not None:
                try:
                    raw = base64.b64decode(token_enc.encode("utf-8"), validate=True)
                    token = protector.unprotect(raw).decode("utf-8", errors="replace").strip()
                    if token:
                        return token
                except Exception:
                    pass
            token_plain = str(data.get("token", "")).strip()
            if token_plain:
                if protector is not None:
                    try:
                        raw = protector.protect(token_plain.encode("utf-8"))
                        payload = {"token_encrypted": base64.b64encode(raw).decode("utf-8")}
                        _atomic_write_json(token_file, payload)
                    except Exception:
                        pass
                elif keyring is not None:
                    try:
                        keyring.set_password(_TOKEN_SERVICE_NAME, _TOKEN_ACCOUNT_NAME, token_plain)
                        _atomic_write_json(token_file, {"token_keyring": True})
                        if os.name != "nt":
                            try:
                                os.chmod(token_file, 0o600)
                            except Exception:
                                pass
                        return token_plain
                    except Exception:
                        pass
                
                # M-06 FIX: Never use plaintext fallback. If secure storage is unavailable,
                # remove the plaintext file and use an in-memory token.
                logger.warning(
                    "Secure storage is unavailable; deleting plaintext extension token from disk. "
                    "Install 'keyring' or set SNAPDOWNLOADER_EXTENSION_TOKEN for persistence."
                )
                try:
                    os.remove(token_file)
                except Exception:
                    pass
        except Exception:
            pass
    token = secrets.token_urlsafe(32)
    try:
        if protector is not None:
            raw = protector.protect(token.encode("utf-8"))
            payload = {"token_encrypted": base64.b64encode(raw).decode("utf-8")}
            _atomic_write_json(token_file, payload)
        elif keyring is not None:
            try:
                keyring.set_password(_TOKEN_SERVICE_NAME, _TOKEN_ACCOUNT_NAME, token)
                _atomic_write_json(token_file, {"token_keyring": True})
                if os.name != "nt":
                    try:
                        os.chmod(token_file, 0o600)
                    except Exception:
                        pass
            except Exception:
                logger.warning(
                    "Secure token persistence via keyring failed; using an in-memory extension token for this session only."
                )
        else:
            logger.warning(
                "Secure token storage is unavailable; using an in-memory extension token for this session only."
            )
    except Exception as exc:
        logger.warning(f"Could not persist extension token: {exc}")
    return token

class ExtensionServer:
    def __init__(self, host="127.0.0.1", port=8765):
        self.host = host
        self.port = port
        self.server = None
        self._loop = None
        self._thread = None
        self._stop_event = None  # M-06: asyncio.Event for clean shutdown
        self.max_message_size = 65536
        self.auth_token = _load_or_create_token()
        ids_sources = [
            str(os.getenv("SNAPDOWNLOADER_ALLOWED_EXT_IDS", "")).strip(),
            str(os.getenv("SNAPDOWNLOADER_EXTENSION_ID", "")).strip(),
        ]
        ids_raw = ",".join([value for value in ids_sources if value])
        self.allowed_extension_ids = {x.strip().lower() for x in ids_raw.split(",") if x.strip()}
        self.allow_any_extension_origin = str(
            os.getenv("SNAPDOWNLOADER_ALLOW_ANY_EXTENSION_ORIGIN", "")
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.allow_empty_origin = str(
            os.getenv("SNAPDOWNLOADER_ALLOW_EMPTY_EXTENSION_ORIGIN", "")
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.allow_private_targets = str(os.getenv("SNAPDOWNLOADER_ALLOW_PRIVATE_TARGETS", "")).strip().lower() in {"1", "true", "yes", "on"}
        self.rate_limit_window_seconds = 2.0
        self.rate_limit_max_messages = 20
        self.rate_limit_max_messages_per_ip = max(
            self.rate_limit_max_messages,
            int(os.getenv("SNAPDOWNLOADER_IP_RATE_LIMIT_MAX_MESSAGES", "60") or 60),
        )
        self.max_links_per_connection = 100
        self._ip_rate_limit_lock = threading.Lock()
        self._ip_rate_limit_windows: dict[str, list[float]] = {}
        self.enable_tls = str(os.getenv("SNAPDOWNLOADER_EXTENSION_TLS", "")).strip().lower() in {"1", "true", "yes", "on"}
        self.tls_cert_path = str(os.getenv("SNAPDOWNLOADER_EXTENSION_TLS_CERT", "")).strip()
        self.tls_key_path = str(os.getenv("SNAPDOWNLOADER_EXTENSION_TLS_KEY", "")).strip()
        self.allow_nonlocal_server = str(os.getenv("SNAPDOWNLOADER_EXTENSION_ALLOW_NONLOCAL_SERVER", "")).strip().lower() in {"1", "true", "yes", "on"}
        self._enforce_local_bind()

    def _enforce_local_bind(self) -> None:
        host = str(self.host or "").strip()
        if not host:
            self.host = "127.0.0.1"
            return
        if self.allow_nonlocal_server:
            return
        lowered = host.lower()
        if lowered in {"localhost", "127.0.0.1", "::1"}:
            self.host = host
            return
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_loopback:
                self.host = host
                return
        except ValueError:
            pass
        logger.warning(f"Non-local extension server host rejected: {host}. Forcing 127.0.0.1.")
        self.host = "127.0.0.1"

    def _build_ssl_context(self):
        if not self.enable_tls:
            return None
        if not self.tls_cert_path or not self.tls_key_path:
            logger.warning("Extension TLS enabled but cert/key paths are missing. Falling back to ws://")
            return None
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=self.tls_cert_path, keyfile=self.tls_key_path)
            return ctx
        except Exception as exc:
            logger.warning(f"Failed to initialize extension TLS context. Falling back to ws:// ({exc})")
            return None

    def _is_safe_target_host(self, host: str) -> bool:
        return is_safe_host(
            host,
            allow_private=self.allow_private_targets,
            resolver=resolve_tcp_host_ips,
            host_validator=is_basic_hostname,
        )

    def _is_allowed_url(self, url: str) -> bool:
        try:
            parsed = urllib.parse.urlparse(str(url or "").strip())
        except Exception:
            return False
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        return self._is_safe_target_host(parsed.hostname or "")

    def _is_allowed_origin(self, origin: str) -> bool:
        origin = str(origin or "").strip().lower()
        if not origin:
            return False
        prefixes = ("chrome-extension://", "moz-extension://", "edge-extension://")
        for prefix in prefixes:
            if origin.startswith(prefix):
                ext_id = origin[len(prefix):].split("/")[0].strip().lower()
                if not ext_id:
                    return False
                if self.allowed_extension_ids:
                    return ext_id in self.allowed_extension_ids
                return self.allow_any_extension_origin
        return False

    def _get_client_ip(self, websocket) -> str:
        try:
            remote = getattr(websocket, "remote_address", None)
        except Exception:
            remote = None
        if isinstance(remote, (tuple, list)) and remote:
            return str(remote[0] or "").strip()
        if isinstance(remote, str):
            return remote.strip()
        return ""

    def _consume_ip_rate_limit(self, client_ip: str, now: float) -> bool:
        ip = str(client_ip or "").strip()
        if not ip:
            return True
        cutoff = now - float(self.rate_limit_window_seconds)
        with self._ip_rate_limit_lock:
            window = [
                ts for ts in self._ip_rate_limit_windows.get(ip, [])
                if float(ts) >= cutoff
            ]
            if len(window) >= int(self.rate_limit_max_messages_per_ip):
                self._ip_rate_limit_windows[ip] = window
                return False
            window.append(float(now))
            self._ip_rate_limit_windows[ip] = window
            stale_cutoff = now - max(float(self.rate_limit_window_seconds), 1.0)
            stale_ips = [
                key for key, timestamps in self._ip_rate_limit_windows.items()
                if not timestamps or max(float(ts) for ts in timestamps) < stale_cutoff
            ]
            for key in stale_ips:
                self._ip_rate_limit_windows.pop(key, None)
        return True

    async def _handler(self, websocket):
        try:
            origin = ""
            try:
                origin = str(websocket.request_headers.get("Origin", "")).strip()
            except Exception:
                origin = ""
            if not self._is_allowed_origin(origin):
                logger.warning(f"Rejected extension websocket from untrusted origin: {origin}")
                try:
                    await websocket.close(code=1008, reason="untrusted origin")
                except Exception:
                    pass
                return
            loop = asyncio.get_running_loop()
            window_start = loop.time()
            window_messages = 0
            links_sent = 0
            invalid_payloads = 0
            client_ip = self._get_client_ip(websocket)
            async for message in websocket:
                if not isinstance(message, str):
                    continue
                now = loop.time()
                if not self._consume_ip_rate_limit(client_ip, now):
                    logger.warning(f"Rejected extension message due to per-IP rate limit: {client_ip or 'unknown'}")
                    try:
                        await websocket.close(code=1008, reason="ip rate limit")
                    except Exception:
                        pass
                    return
                if now - window_start >= self.rate_limit_window_seconds:
                    window_start = now
                    window_messages = 0
                window_messages += 1
                if window_messages > self.rate_limit_max_messages:
                    logger.warning("Rejected extension message due to rate limit.")
                    continue
                if len(message.encode("utf-8", errors="ignore")) > self.max_message_size:
                    logger.warning("Rejected oversized extension message.")
                    continue
                try:
                    data = json.loads(message)
                except Exception:
                    invalid_payloads += 1
                    logger.warning("Rejected extension message with invalid JSON.")
                    if invalid_payloads > 10:
                        try:
                            await websocket.close(code=1008, reason="invalid payload")
                        except Exception:
                            pass
                        return
                    continue
                if not isinstance(data, dict):
                    invalid_payloads += 1
                    logger.warning("Rejected extension message with invalid payload type.")
                    if invalid_payloads > 10:
                        try:
                            await websocket.close(code=1008, reason="invalid payload")
                        except Exception:
                            pass
                        return
                    continue
                if self.auth_token:
                    token = str(data.get("token", "")).strip()
                    if not hmac.compare_digest(token, self.auth_token):
                        logger.warning("Rejected extension message with invalid token.")
                        continue
                url = data.get("url")
                if url and self._is_allowed_url(url):
                    redacted_url = redact_url(url)
                    links_sent += 1
                    if links_sent > self.max_links_per_connection:
                        logger.warning("Closing extension websocket due to excessive link submissions.")
                        try:
                            await websocket.close(code=1008, reason="too many links")
                        except Exception:
                            pass
                        return
                    logger.info(f"Received link from extension: {redacted_url}")
                    post_action_raw = str(data.get("post_action", "none")).strip().lower() or "none"
                    post_action = PostDownloadManager.normalize_action(post_action_raw, extension_safe=True)
                    if post_action != post_action_raw:
                        logger.warning(
                            f"[Extension] Rejected unsafe post_action '{post_action_raw}' from extension payload."
                        )
                    payload = {
                        "url": str(url).strip(),
                        "title": str(data.get("title", "")).strip(),
                        "thumbnail": str(data.get("thumbnail", "")).strip(),
                        "format": str(data.get("format", "MP4")).strip() or "MP4",
                        "quality": str(data.get("quality", "1080p")).strip() or "1080p",
                        "subtitle": str(data.get("subtitle", "None")).strip() or "None",
                        "auto_download": bool(data.get("auto_download", True)),
                        "bandwidth_limit_kbps": data.get("bandwidth_limit_kbps", 0),
                        "post_action": post_action,
                        "schedule_repeat": str(data.get("schedule_repeat", "none")).strip() or "none",
                        "source": "browser_extension",
                    }
                    event_bus.publish(ExtensionLinkReceivedEvent(payload))
                    event_bus.publish(ShowNotificationEvent(f"Link received from extension: {redacted_url}"))
        except Exception as exc:
            logger.error(f"Extension server handler error: {exc}")

    def _run_server(self):
        try:
            import websockets
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._stop_event = asyncio.Event()

            async def main():
                ssl_ctx = self._build_ssl_context()
                scheme = "wss" if ssl_ctx is not None else "ws"
                async with websockets.serve(
                    self._handler,
                    self.host,
                    self.port,
                    max_size=self.max_message_size,
                    ssl=ssl_ctx,
                ) as server:
                    self.server = server
                    logger.info(f"Extension WebSocket server listening on {scheme}://{self.host}:{self.port}")
                    await self._stop_event.wait()  # M-06: Wait until stop() is called
                    logger.info("Extension server shutting down cleanly.")

            self._loop.run_until_complete(main())
        except ImportError:
            logger.error("websockets library not installed. Extension server disabled.")
        except Exception as exc:
            logger.error(f"Failed to start extension server: {exc}")
        finally:
            if self._loop and not self._loop.is_closed():
                self._loop.close()
            self._loop = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_server, daemon=True, name="ExtensionServer")
        self._thread.start()
        scheme = "wss" if self.enable_tls and self.tls_cert_path and self.tls_key_path else "ws"
        logger.info(f"Extension server started on {scheme}://{self.host}:{self.port}")

    def stop(self):
        """M-06: Cleanly stop the WebSocket server."""
        thread = self._thread
        if self._loop and self._stop_event:
            try:
                self._loop.call_soon_threadsafe(self._stop_event.set)
                logger.info("Extension server stop signal sent.")
            except Exception as exc:
                logger.debug(f"Failed to send extension server stop signal: {exc}")
        else:
            logger.info("Extension server stopping (daemon)")
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=2.5)
            if thread.is_alive():
                logger.warning("Extension server thread did not stop within timeout")
            else:
                self._thread = None

# Singleton
extension_server = ExtensionServer()



