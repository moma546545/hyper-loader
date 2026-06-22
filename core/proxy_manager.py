
"""
core/proxy_manager.py — Smart Proxy / VPN Rotation Manager
Supports HTTP/HTTPS/SOCKS5 proxies with automatic rotation on failure.
Integrates directly with yt-dlp's --proxy flag.
"""
import base64
import hashlib
import json
import logging
import os
import random
import sys
import tempfile
import time
import threading
import re
from typing import Optional
from urllib.parse import urlsplit, urlunsplit, unquote, quote
from .secure_storage import get_windows_protector
from .utils import get_app_data_dir

try:
    import keyring
except ImportError:
    keyring = None

logger = logging.getLogger("SnapDownloader.Proxy")


CONFIG_PATH = os.path.join(get_app_data_dir(), "proxy_config.json")
SERVICE_NAME = "SnapDownloader.Proxy"

DEFAULT_CONFIG = {
    "enabled": False,
    "mode": "single",          # "single" | "rotate" | "none"
    "proxies": [],             # list of proxy strings
    "current_index": 0,
    "test_url": "https://httpbin.org/ip",
    "timeout_seconds": 8,
    "auto_rotate_on_fail": True,
    "rotate_interval_minutes": 0,   # 0 = only on failure
    "failure_threshold": 2,
    "cooldown_seconds": 180,
    "kill_switch_enabled": True,
}


class ProxyManager:
    _default_instance = None
    _default_instance_lock = threading.Lock()

    def __new__(cls, config_path: Optional[str] = None, protector=None):
        # Reuse the shared default manager so accidental `ProxyManager()` calls do
        # not fork proxy state away from the live singleton used by the app.
        if config_path is None and protector is None:
            with cls._default_instance_lock:
                if cls._default_instance is None:
                    cls._default_instance = super().__new__(cls)
                    cls._default_instance._initialized = False
                return cls._default_instance
        instance = super().__new__(cls)
        instance._initialized = False
        return instance

    def __init__(self, config_path: Optional[str] = None, protector=None):
        if getattr(self, "_initialized", False):
            return
        self.config_path = config_path or CONFIG_PATH
        self._protector = protector if protector is not None else self._build_default_protector()
        self._lock = threading.RLock()
        self.config = dict(DEFAULT_CONFIG)
        self._fail_counts: dict[str, int] = {}
        self._last_failure_ts: dict[str, float] = {}
        self._disabled_until: dict[str, float] = {}
        self._last_rotate_time = 0.0
        self.load()
        self._initialized = True

    def _build_default_protector(self):
        if os.name != "nt":
            return None
        try:
            return get_windows_protector()
        except Exception as exc:
            logger.warning(f"[Proxy] Secure storage fallback unavailable: {exc}")
            return None

    # ── Persistence ──────────────────────────────────────────────────────────

    def load(self):
        with self._lock:
            if os.path.exists(self.config_path):
                try:
                    with open(self.config_path, "r", encoding="utf-8") as f:
                        data = json.load(f)

                    self.config = dict(DEFAULT_CONFIG)
                    encrypted_proxies = data.pop("proxies_encrypted", None)
                    raw_proxies = data.pop("proxies", None)
                    self.config.update(data)
                    if self._protector is not None and encrypted_proxies:
                        self.config["proxies"] = self._decrypt_proxy_list(encrypted_proxies)
                    else:
                        proxies = self._normalize_proxy_list(raw_proxies)
                        self.config["proxies"] = self._restore_passwords(proxies)
                    self._sanitize_runtime_state_locked()
                    if not self.config["proxies"]:
                        self.config["current_index"] = 0
                    else:
                        self.config["current_index"] = int(self.config.get("current_index", 0)) % len(self.config["proxies"])
                    return
                except Exception as exc:
                    logger.warning(f"[Proxy] Failed to load config, using defaults: {exc}")
            self.config = dict(DEFAULT_CONFIG)

    def save(self):
        with self._lock:
            try:
                config_dir = os.path.dirname(self.config_path)
                if config_dir:
                    os.makedirs(config_dir, exist_ok=True)
                payload = dict(self.config)
                proxies = self._normalize_proxy_list(payload.get("proxies", []))
                if self._protector is not None:
                    payload["proxies"] = []
                    payload["proxies_encrypted"] = self._encrypt_proxy_list(proxies) if proxies else []
                else:
                    payload["proxies"] = self._store_passwords(proxies)
                    payload.pop("proxies_encrypted", None)
                self._atomic_write_config(payload)
            except Exception as exc:
                logger.warning(f"[Proxy] Failed to save config: {exc}")

    def _atomic_write_config(self, payload: dict) -> None:
        config_dir = os.path.dirname(self.config_path) or os.getcwd()
        fd = -1
        tmp_path = ""
        try:
            fd, tmp_path = tempfile.mkstemp(prefix=".proxy_config_", suffix=".tmp", dir=config_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fd = -1
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            with _suppress_exception():
                os.chmod(tmp_path, 0o600)
            if os.name == "nt":
                from .cookie_importer import _harden_windows_file_permissions
                with _suppress_exception():
                    _harden_windows_file_permissions(tmp_path)
            os.replace(tmp_path, self.config_path)
            with _suppress_exception():
                os.chmod(self.config_path, 0o600)
            if os.name == "nt":
                from .cookie_importer import _harden_windows_file_permissions
                with _suppress_exception():
                    _harden_windows_file_permissions(self.config_path)
        finally:
            if fd != -1:
                with _suppress_exception():
                    os.close(fd)
            if tmp_path and os.path.exists(tmp_path):
                with _suppress_exception():
                    os.remove(tmp_path)

    def _store_passwords(self, proxies: list[str]) -> list[str]:
        if not keyring:
            # M-05: Fail closed. Do not save passwords in plain text.
            # The presence of a password without keyring indicates a setup error.
            for proxy in proxies:
                if urlsplit(proxy).password:
                    raise RuntimeError(
                        "keyring is not installed, cannot save proxy with password securely. "
                        "Please install with: pip install keyring"
                    )
            return proxies  # Return unmodified if no passwords are present

        redacted_proxies = []
        for i, proxy in enumerate(proxies):
            try:
                parts = urlsplit(proxy)
                if parts.password:
                    key = self._proxy_secret_key(parts)
                    keyring.set_password(SERVICE_NAME, key, parts.password)
                    username = parts.username or ""
                    host = parts.hostname or ""
                    new_netloc = f"{username}:***@{host}"
                    if parts.port:
                        new_netloc += f":{parts.port}"
                    redacted = urlunsplit((parts.scheme, new_netloc, parts.path, parts.query, parts.fragment))
                    redacted_proxies.append(redacted)
                else:
                    redacted_proxies.append(proxy)
            except Exception as exc:
                logger.warning(f"[Proxy] Failed to store password for {self._redact_proxy(proxy)}: {exc}")
                redacted_proxies.append(proxy)
        return redacted_proxies

    def _restore_passwords(self, redacted_proxies: list[str]) -> list[str]:
        if not keyring:
            for proxy in redacted_proxies:
                if "***" in urlsplit(proxy).netloc:
                    raise RuntimeError(
                        "keyring is not installed, cannot restore proxy password securely. "
                        "Please install with: pip install keyring"
                    )
            return redacted_proxies
        restored_proxies: list[str] = []
        for i, proxy in enumerate(redacted_proxies):
            try:
                parts = urlsplit(proxy)
                if "***" in parts.netloc:
                    key = self._proxy_secret_key(parts)
                    password = keyring.get_password(SERVICE_NAME, key)
                    if not password:
                        password = keyring.get_password(SERVICE_NAME, self._legacy_proxy_secret_key(i, parts))
                    if password:
                        username = parts.username or ""
                        host = parts.hostname or ""
                        new_netloc = f"{username}:{password}@{host}"
                        if parts.port:
                            new_netloc += f":{parts.port}"
                        restored = urlunsplit((parts.scheme, new_netloc, parts.path, parts.query, parts.fragment))
                        restored_proxies.append(restored)
                    else:
                        restored_proxies.append(proxy)
                else:
                    restored_proxies.append(proxy)
            except Exception as exc:
                logger.warning(f"[Proxy] Failed to restore password for {self._redact_proxy(proxy)}: {exc}")
                restored_proxies.append(proxy)
        return restored_proxies

    def _encrypt_proxy_list(self, proxies: list[str]) -> list[str]:
        encrypted: list[str] = []
        for proxy in proxies:
            protected = self._protector.protect(proxy.encode("utf-8"))
            encrypted.append(base64.b64encode(protected).decode("ascii"))
        return encrypted

    def _decrypt_proxy_list(self, encrypted_proxies: list[str]) -> list[str]:
        decrypted: list[str] = []
        for encoded in encrypted_proxies or []:
            raw = base64.b64decode(encoded)
            value = self._protector.unprotect(raw).decode("utf-8")
            if value:
                decrypted.append(value)
        return decrypted

    def _normalize_proxy_list(self, proxies) -> list[str]:
        return [str(proxy or "").strip() for proxy in proxies or [] if str(proxy or "").strip()]

    def _proxy_secret_key(self, parts) -> str:
        scheme = str(parts.scheme or "http").lower()
        username = str(parts.username or "")
        host = str(parts.hostname or "").lower()
        port = str(parts.port or "")
        identity = "\n".join([scheme, username, host, port])
        digest = hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()[:24]
        return f"proxy_{digest}"

    def _legacy_proxy_secret_key(self, index: int, parts) -> str:
        return f"proxy_{index}_{parts.hostname}"

    def _sanitize_runtime_state_locked(self) -> None:
        proxies = set(self._normalize_proxy_list(self.config.get("proxies", [])))
        self._fail_counts = {proxy: int(count) for proxy, count in self._fail_counts.items() if proxy in proxies}
        self._last_failure_ts = {proxy: float(ts) for proxy, ts in self._last_failure_ts.items() if proxy in proxies}
        self._disabled_until = {proxy: float(ts) for proxy, ts in self._disabled_until.items() if proxy in proxies}

    def _failure_threshold_locked(self) -> int:
        return max(1, int(self.config.get("failure_threshold", 2) or 2))

    def _cooldown_seconds_locked(self) -> float:
        return max(0.0, float(self.config.get("cooldown_seconds", 180) or 0))

    def _kill_switch_enabled_locked(self) -> bool:
        return bool(self.config.get("kill_switch_enabled", True))

    def _rotation_interval_seconds_locked(self) -> float:
        minutes = float(self.config.get("rotate_interval_minutes", 0) or 0)
        return max(0.0, minutes * 60.0)

    def _set_current_proxy_locked(self, proxy: str) -> None:
        proxies = list(self.config.get("proxies", []))
        if proxy in proxies:
            self.config["current_index"] = proxies.index(proxy)

    def _available_rotation_candidates_locked(self, exclude: set[str] | None = None) -> list[str]:
        proxies = list(self.config.get("proxies", []))
        exclude_set = {str(proxy or "").strip() for proxy in (exclude or set()) if str(proxy or "").strip()}
        if not proxies:
            return []
        now = time.time()
        ready = [
            proxy
            for proxy in proxies
            if proxy not in exclude_set and float(self._disabled_until.get(proxy, 0.0) or 0.0) <= now
        ]
        if ready:
            return ready
        return [proxy for proxy in proxies if proxy not in exclude_set]

    def _pick_next_proxy_locked(
        self,
        candidates: list[str],
        *,
        current_proxy: str = "",
        randomize: bool = False,
    ) -> Optional[str]:
        normalized = [str(proxy or "").strip() for proxy in candidates if str(proxy or "").strip()]
        if not normalized:
            return None
        proxies = list(self.config.get("proxies", []))
        if not randomize:
            if current_proxy in proxies:
                current_index = proxies.index(current_proxy)
                ordered = proxies[current_index + 1 :] + proxies[: current_index + 1]
            else:
                ordered = proxies
            for proxy in ordered:
                if proxy in normalized:
                    return proxy
            return normalized[0]
        scored: list[tuple[tuple[int, float], str]] = []
        for proxy in normalized:
            scored.append(
                (
                    (
                        int(self._fail_counts.get(proxy, 0) or 0),
                        float(self._last_failure_ts.get(proxy, 0.0) or 0.0),
                    ),
                    proxy,
                )
            )
        best_score = min(score for score, _proxy in scored)
        best = sorted(proxy for score, proxy in scored if score == best_score)
        movable = [proxy for proxy in best if proxy != current_proxy]
        pool = movable or best
        return random.choice(pool)

    # ── Public API ────────────────────────────────────────────────────────────

    def is_enabled(self) -> bool:
        with self._lock:
            return bool(self.config.get("enabled")) and bool(self.config.get("proxies"))

    def can_rotate(self) -> bool:
        with self._lock:
            if not self.config.get("enabled"):
                return False
            proxies = list(self.config.get("proxies", []))
            if len(proxies) <= 1:
                return False
            self._sanitize_runtime_state_locked()
            current = proxies[int(self.config.get("current_index", 0)) % len(proxies)]
            now = time.time()
            ready_alternatives = [
                proxy
                for proxy in proxies
                if proxy != current and float(self._disabled_until.get(proxy, 0.0) or 0.0) <= now
            ]
            return bool(ready_alternatives)

    def get_current_proxy(self) -> Optional[str]:
        changed = False
        with self._lock:
            if not self.config.get("enabled"):
                return None
            proxies = list(self.config.get("proxies", []))
            if not proxies:
                return None
            self._sanitize_runtime_state_locked()
            idx = int(self.config.get("current_index", 0)) % len(proxies)
            proxy = proxies[idx]
            disabled_until = float(self._disabled_until.get(proxy, 0.0) or 0.0)
            if disabled_until > time.time():
                replacement = self._pick_next_proxy_locked(
                    self._available_rotation_candidates_locked(exclude={proxy}),
                    current_proxy=proxy,
                    randomize=True,
                )
                if replacement:
                    self._set_current_proxy_locked(replacement)
                    proxy = replacement
                    changed = True
            interval_seconds = self._rotation_interval_seconds_locked()
            should_rotate_interval = (
                interval_seconds > 0.0
                and len(proxies) > 1
                and (time.time() - float(self._last_rotate_time or 0.0)) >= interval_seconds
            )
            if should_rotate_interval:
                next_proxy = self._pick_next_proxy_locked(
                    self._available_rotation_candidates_locked(exclude={proxy}),
                    current_proxy=proxy,
                    randomize=True,
                )
                if next_proxy:
                    changed = changed or (next_proxy != proxy)
                    proxy = next_proxy
                    self._set_current_proxy_locked(proxy)
                    self._last_rotate_time = time.time()
        if changed:
            self.save()
        return proxy

    def rotate(self, randomize: bool = False) -> Optional[str]:
        """Move to the next proxy in the list."""
        changed = False
        with self._lock:
            proxies = list(self.config.get("proxies", []))
            if not proxies:
                return None
            self._sanitize_runtime_state_locked()
            current_proxy = proxies[int(self.config.get("current_index", 0)) % len(proxies)]
            candidates = self._available_rotation_candidates_locked(exclude={current_proxy} if len(proxies) > 1 else set())
            if not candidates:
                candidates = self._available_rotation_candidates_locked()
            proxy = self._pick_next_proxy_locked(candidates, current_proxy=current_proxy, randomize=randomize)
            if proxy is None:
                return None
            changed = proxy != current_proxy
            self._set_current_proxy_locked(proxy)
            self._last_rotate_time = time.time()
        if changed:
            self.save()
            logger.info(f"[Proxy] Rotated to: {self._redact_proxy(proxy)}")
        return proxy

    def on_failure(self, proxy: str):
        """Called when a download fails — optionally rotate proxy."""
        rotate_after_failure = False
        trigger_kill_switch = False
        normalized_proxy = str(proxy or "").strip()
        with self._lock:
            self._sanitize_runtime_state_locked()
            if not normalized_proxy:
                normalized_proxy = str(self.get_current_proxy() or "").strip()
            if not normalized_proxy:
                return
            self._fail_counts[normalized_proxy] = self._fail_counts.get(normalized_proxy, 0) + 1
            self._last_failure_ts[normalized_proxy] = time.time()
            failures = self._fail_counts[normalized_proxy]
            threshold = self._failure_threshold_locked()
            if failures >= threshold:
                cooldown_seconds = self._cooldown_seconds_locked()
                if cooldown_seconds > 0:
                    self._disabled_until[normalized_proxy] = time.time() + cooldown_seconds
            available_alternatives = self._available_rotation_candidates_locked(exclude={normalized_proxy})
            rotate_after_failure = (
                bool(self.config.get("auto_rotate_on_fail"))
                and len(self.config.get("proxies", [])) > 1
                and bool(available_alternatives)
            )
            trigger_kill_switch = (
                failures >= threshold
                and self._kill_switch_enabled_locked()
                and not available_alternatives
            )
            if trigger_kill_switch:
                self.config["enabled"] = False
        logger.warning(f"[Proxy] Failure #{failures} for {self._redact_proxy(normalized_proxy)}")
        if trigger_kill_switch:
            self.save()
            logger.error("[Proxy] Kill-switch engaged after exhausting healthy proxies.")
            return
        if rotate_after_failure:
            self.rotate(randomize=True)

    def add_proxy(self, proxy_url: str):
        """Add a proxy (e.g. 'http://1.2.3.4:8080' or 'socks5://user:pass@host:port')."""
        should_save = False
        with self._lock:
            proxies = self.config.setdefault("proxies", [])
            proxy_url = proxy_url.strip()
            if proxy_url and proxy_url not in proxies:
                parsed = urlsplit(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
                if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h", "socks4", "socks4a"}:
                    logger.warning(f"[Proxy] Invalid scheme rejected: {proxy_url}")
                    return
                proxies.append(proxy_url)
                self._sanitize_runtime_state_locked()
                should_save = True
        if should_save:
            self.save()

    def remove_proxy(self, proxy_url: str):
        should_save = False
        with self._lock:
            proxies = self.config.get("proxies", [])
            if proxy_url in proxies:
                proxies.remove(proxy_url)
                self._sanitize_runtime_state_locked()
                self.config["current_index"] = 0
                should_save = True
        if should_save:
            self.save()

    def test_proxy(self, proxy_url: str) -> tuple[bool, str]:
        """Test if a proxy is reachable. Returns (ok, detected_ip_or_error)."""
        test_url = self.config.get("test_url", "https://httpbin.org/ip")
        try:
            from .network_safety import is_safe_host, resolve_tcp_host_ips
            parsed_test = urlsplit(test_url)
            test_host = parsed_test.hostname or ""
            if not is_safe_host(test_host, resolver=resolve_tcp_host_ips, allow_private=False):
                return False, "Unsafe test URL host rejected"
        except Exception as exc:
            return False, f"Failed to validate test URL: {exc}"
        timeout = int(self.config.get("timeout_seconds", 8))
        try:
            proxies_env = {"http": proxy_url, "https": proxy_url}
            import urllib.request
            proxy_handler = urllib.request.ProxyHandler(proxies_env)
            opener = urllib.request.build_opener(proxy_handler)
            resp = opener.open(test_url, timeout=timeout)
            body = resp.read().decode("utf-8", errors="replace")
            return True, body[:200]
        except Exception as exc:
            error_msg = str(exc)
            redacted = self.redact_proxy(proxy_url)
            proxy_candidates = [
                str(proxy_url or ""),
                unquote(str(proxy_url or "")),
                quote(str(proxy_url or ""), safe=":/?#[]@!$&'()*+,;=%"),
            ]
            for candidate in proxy_candidates:
                candidate_text = str(candidate or "").strip()
                if not candidate_text:
                    continue
                error_msg = re.sub(
                    re.escape(candidate_text),
                    redacted,
                    error_msg,
                    flags=re.IGNORECASE,
                )
            return False, error_msg

    def set_enabled(self, enabled: bool):
        with self._lock:
            self.config["enabled"] = enabled
            if enabled:
                self._disabled_until.clear()
        self.save()

    def get_yt_dlp_flag(self) -> list[str]:
        """Return yt-dlp command-line flags for the current proxy."""
        proxy = self.get_current_proxy()
        if proxy:
            return ["--proxy", proxy]
        return []

    def format_status(self) -> str:
        with self._lock:
            if not self.config.get("enabled") or not self.config.get("proxies"):
                return "Proxy: Disabled"
            proxy = self.get_current_proxy() or "None"
            count = len(self.config.get("proxies", []))
            idx = self.config.get("current_index", 0) + 1
            ready_count = len(self._available_rotation_candidates_locked())
        return f"Proxy: [{idx}/{count}] {self.redact_proxy(proxy)[:40]} | ready {ready_count}/{count}"

    def redact_proxy(self, proxy_url: str) -> str:
        return self._redact_proxy(proxy_url)

    def _redact_proxy(self, proxy_url: str) -> str:
        proxy_text = str(proxy_url or "").strip()
        if not proxy_text:
            return "None"
        try:
            parts = urlsplit(proxy_text if "://" in proxy_text else f"http://{proxy_text}")
            scheme = parts.scheme or "http"
            host = parts.hostname or ""
            if not host:
                return "***"
            # Avoid leaking proxy path/query fragments in logs.
            if not parts.username and not parts.password:
                netloc = host
                if parts.port:
                    netloc += f":{parts.port}"
                return urlunsplit((scheme, netloc, "", "", ""))
            host = parts.hostname or ""
            if parts.username and parts.password:
                netloc = f"***:***@{host}"
            else:
                netloc = f"***@{host}"
            if parts.port:
                netloc += f":{parts.port}"
            return urlunsplit((scheme, netloc, "", "", ""))
        except Exception:
            return "***"


# Singleton
proxy_manager = ProxyManager()


class _suppress_exception:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return True
