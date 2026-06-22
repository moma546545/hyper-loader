
"""
core/anti_detection.py - Unified Anti-Detection Engine

This engine coordinates various strategies to avoid being blocked by web services.
It reacts to download errors by adjusting request patterns, rotating proxies,
and changing browser identity.
"""
import random
import logging
import subprocess
import sys
import os
import threading
from .proxy_manager import proxy_manager

logger = logging.getLogger("SnapDownloader.AntiDetect")

BROWSER_PROFILES = (
    {
        "name": "chrome_win",
        "impersonate": "chrome",
        "transport_impersonate": "chrome124",
        "ja3": "771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-17513-65037,29-23-24,0",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "headers": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-CH-UA": '"Chromium";v="147", "Google Chrome";v="147", "Not_A Brand";v="24"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
        },
    },
    {
        "name": "chrome_mac",
        "impersonate": "chrome",
        "transport_impersonate": "chrome124",
        "ja3": "771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-17513-65037,29-23-24,0",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "headers": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
            "Sec-CH-UA": '"Chromium";v="147", "Google Chrome";v="147", "Not_A Brand";v="24"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"macOS"',
        },
    },
    {
        "name": "edge_win",
        "impersonate": "edge",
        "transport_impersonate": "edge101",
        "ja3": "771,4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-18-51-45-43-27-17513,29-23-24,0",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0",
        "headers": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-CH-UA": '"Chromium";v="146", "Microsoft Edge";v="146", "Not_A Brand";v="24"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
        },
    },
    {
        "name": "safari_mac",
        "impersonate": "safari",
        "transport_impersonate": "safari17_0",
        "ja3": "771,4865-4866-4867-49196-49195-52393-52392-49188-49192-49187-49191-159-158-107-103-57-51-157-156-61-60-53-47,0-23-65281-10-11-35-16-5-13-18-45-43-51,29-23-24,0",
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.4 Safari/605.1.15",
        "headers": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    },
    {
        "name": "firefox_win",
        "impersonate": "chrome",
        "transport_impersonate": "firefox133",
        "ja3": "771,4865-4867-4866-49195-49199-49196-49200-52393-52392-49171-49172-156-157-47-53,0-23-65281-10-11-35-16-5-13-45-43-51,29-23-24,0",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:149.0) Gecko/20100101 Firefox/149.0",
        "headers": {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.7",
        },
    },
)
USER_AGENTS = [profile["user_agent"] for profile in BROWSER_PROFILES]

_NORMAL_SLEEP_RANGE = (0.2, 0.8)
_NORMAL_MAX_SLEEP_RANGE = (1.0, 2.0)
_NORMAL_REQUEST_SLEEP_RANGE = (0.05, 0.35)
_CAUTIOUS_SLEEP_RANGE = (1.2, 2.8)
_CAUTIOUS_MAX_SLEEP_RANGE = (3.0, 5.5)
_CAUTIOUS_REQUEST_SLEEP_RANGE = (0.8, 1.8)
_STRICT_SLEEP_RANGE = (2.2, 4.0)
_STRICT_MAX_SLEEP_RANGE = (5.0, 8.0)
_STRICT_REQUEST_SLEEP_RANGE = (1.4, 2.6)
_CAUTIOUS_IMPERSONATION_PROFILES = ("chrome", "edge", "safari")
_MAX_ERROR_STREAK = 10
_STRICT_STREAK_THRESHOLD = 3
_COOLDOWN_ERRORS_FOR_NORMAL = 0
_LANGUAGE_ROTATION = (
    "en-US,en;q=0.9",
    "en-GB,en;q=0.8",
    "ar-SA,ar;q=0.9,en-US;q=0.6,en;q=0.5",
    "fr-FR,fr;q=0.9,en;q=0.6",
)

# ── Header obfuscation: optional low-risk headers rotated to look organic ─────
_OPTIONAL_VIEWPORT_WIDTHS = ("1280", "1366", "1440", "1920")
_OPTIONAL_CACHE_CONTROLS = ("max-age=0", "no-cache", "")
_OPTIONAL_FETCH_SITES = ("none", "same-origin", "cross-site")
_OPTIONAL_PRIORITIES = ("u=0, i", "u=1", "")
_SEC_CH_UA_ARCH_VALUES = ('"x86"', '"arm"', "")



class AntiDetectionEngine:
    def __init__(self):
        self._lock = threading.RLock()
        self._current_profile = random.choice(BROWSER_PROFILES)
        self.current_user_agent = self._current_profile["user_agent"]
        self.strategy = "normal" # "normal" | "cautious"
        self._last_impersonation_profile = ""
        self._last_transport_profile = str(self._current_profile.get("name", "") or "")
        self._error_streak = 0
        self._clean_streak = 0
        self._impersonation_support_cache: dict[str, bool] = {}
        self._allow_impersonation = str(os.getenv("SNAPDOWNLOADER_ENABLE_IMPERSONATE", "0")).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _jittered_sleep_profile(strategy: str) -> dict[str, float]:
        strategy_text = str(strategy or "").strip().lower()
        if strategy_text == "strict":
            sleep_low, sleep_high = _STRICT_SLEEP_RANGE
            max_low, max_high = _STRICT_MAX_SLEEP_RANGE
            request_low, request_high = _STRICT_REQUEST_SLEEP_RANGE
        elif strategy_text == "cautious":
            sleep_low, sleep_high = _CAUTIOUS_SLEEP_RANGE
            max_low, max_high = _CAUTIOUS_MAX_SLEEP_RANGE
            request_low, request_high = _CAUTIOUS_REQUEST_SLEEP_RANGE
        else:
            sleep_low, sleep_high = _NORMAL_SLEEP_RANGE
            max_low, max_high = _NORMAL_MAX_SLEEP_RANGE
            request_low, request_high = _NORMAL_REQUEST_SLEEP_RANGE
        sleep_interval = round(random.uniform(sleep_low, sleep_high), 2)
        max_sleep_interval = round(max(random.uniform(max_low, max_high), sleep_interval + 0.2), 2)
        sleep_requests = round(random.uniform(request_low, request_high), 2)
        return {
            "sleep_interval": sleep_interval,
            "max_sleep_interval": max_sleep_interval,
            "sleep_requests": sleep_requests,
        }

    def get_next_user_agent(self) -> str:
        """Selects a new, random User-Agent different from the current one."""
        with self._lock:
            current_ua = self.current_user_agent
            next_profile = random.choice(BROWSER_PROFILES)
            while next_profile["user_agent"] == current_ua and len(BROWSER_PROFILES) > 1:
                next_profile = random.choice(BROWSER_PROFILES)
            self._current_profile = next_profile
            self.current_user_agent = next_profile["user_agent"]
            self._last_transport_profile = str(next_profile.get("name", "") or "")
        logger.info(f"[Anti-Detect] Switched browser profile to: {self._last_transport_profile}")
        return self.current_user_agent

    def _effective_strategy(self) -> str:
        with self._lock:
            if self.strategy != "cautious":
                return "normal"
            if self._error_streak >= _STRICT_STREAK_THRESHOLD:
                return "strict"
            return "cautious"

    def _choose_impersonation_profile(self) -> str:
        with self._lock:
            preferred = str((self._current_profile or {}).get("impersonate", "") or "").strip().lower()
            if not preferred:
                ua = str(self.current_user_agent or "").lower()
                if "edg/" in ua:
                    preferred = "edge"
                elif "safari/" in ua and "chrome/" not in ua:
                    preferred = "safari"
                else:
                    preferred = "chrome"
            if preferred == self._last_impersonation_profile and len(_CAUTIOUS_IMPERSONATION_PROFILES) > 1:
                pool = [item for item in _CAUTIOUS_IMPERSONATION_PROFILES if item != preferred]
                return random.choice(pool)
            return preferred

    def _current_headers(self) -> dict[str, str]:
        with self._lock:
            headers = dict((self._current_profile or {}).get("headers", {}) or {})
            # Rotate Accept-Language on 65 % of requests.
            if random.random() < 0.65:
                headers["Accept-Language"] = random.choice(_LANGUAGE_ROTATION)
            # Core security-fetch hints — always present
            headers.setdefault("Sec-Fetch-Dest", "document")
            headers.setdefault("Sec-Fetch-Mode", "navigate")
            # Vary Sec-Fetch-Site to mimic different navigation sources.
            headers["Sec-Fetch-Site"] = random.choice(_OPTIONAL_FETCH_SITES)
            headers.setdefault("Sec-Fetch-User", "?1")
            headers.setdefault("Accept-Encoding", "gzip, deflate, br")
            # DNT: present on ~80 % of requests (opt-out minorities are real).
            if random.random() < 0.80:
                headers["DNT"] = "1"
            elif "DNT" in headers:
                del headers["DNT"]
            # Upgrade-Insecure-Requests: browsers send this on navigations.
            headers.setdefault("Upgrade-Insecure-Requests", "1")
            # Optional viewport hint (Chrome 90+ adds this occasionally).
            if random.random() < 0.45:
                headers["Viewport-Width"] = random.choice(_OPTIONAL_VIEWPORT_WIDTHS)
            # Cache-Control variation (real browser navigations vary).
            cc = random.choice(_OPTIONAL_CACHE_CONTROLS)
            if cc:
                headers["Cache-Control"] = cc
            # Priority header (HTTP/3 / fetch priority — present on ~30 % of requests).
            priority = random.choice(_OPTIONAL_PRIORITIES)
            if priority:
                headers["Priority"] = priority
            return headers

    def get_obfuscated_headers(
        self,
        base_headers: dict[str, str] | None = None,
        *,
        shuffle_order: bool = True,
    ) -> dict[str, str]:
        """
        Return a fully obfuscated header dict that looks like a premium organic
        browser session.  Optionally shuffles insertion order so header-order
        fingerprinting is defeated (curl_cffi / HTTPX preserve dict order).

        Args:
            base_headers: Optional caller-supplied headers merged *after* the
                          profile defaults (caller values take priority).
            shuffle_order: If True, randomises the order of all optional headers
                           so that no two requests share the same ordering.

        Returns:
            A plain dict of header name → value strings.
        """
        merged = self._current_headers()
        if base_headers:
            merged.update(base_headers)

        if not shuffle_order or len(merged) <= 2:
            return merged

        # Separate "stable" headers (Accept, User-Agent) from shuffleable ones.
        stable_keys = {"accept", "accept-encoding", "user-agent"}
        stable: dict[str, str] = {}
        shuffleable: list[tuple[str, str]] = []
        for k, v in merged.items():
            if k.lower() in stable_keys:
                stable[k] = v
            else:
                shuffleable.append((k, v))
        random.shuffle(shuffleable)
        result: dict[str, str] = {**stable}
        result.update(shuffleable)
        return result


    def get_transport_fingerprint(self) -> dict[str, str]:
        with self._lock:
            profile = dict(self._current_profile or {})
            return {
                "profile_name": str(profile.get("name", "") or ""),
                "impersonate": str(profile.get("impersonate", "") or "chrome"),
                "transport_impersonate": str(profile.get("transport_impersonate", "") or "chrome124"),
                "ja3": str(profile.get("ja3", "") or ""),
            }

    def _impersonation_supported(self, profile_name: str) -> bool:
        target = str(profile_name or "").strip().lower()
        if not target:
            return False
        with self._lock:
            cached = self._impersonation_support_cache.get(target)
        if cached is not None:
            return bool(cached)
        command = [sys.executable, "-m", "yt_dlp", "--list-impersonate-targets"]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                creationflags=creationflags,
            )
        except Exception as exc:
            logger.warning(f"[Anti-Detect] Could not probe yt-dlp impersonation support: {exc}")
            with self._lock:
                self._impersonation_support_cache[target] = False
            return False
        output = f"{proc.stdout}\n{proc.stderr}".lower()
        supported = proc.returncode == 0 and target in output
        with self._lock:
            self._impersonation_support_cache[target] = bool(supported)
        if not supported:
            logger.warning(f"[Anti-Detect] yt-dlp impersonation target '{target}' is unavailable. Falling back to user-agent.")
        return bool(supported)

    def get_yt_dlp_options(self) -> list[str]:
        """
        Returns a list of command-line arguments for yt-dlp based on the
        current anti-detection strategy.
        """
        effective_strategy = self._effective_strategy()
        profile = self._jittered_sleep_profile(effective_strategy)
        opts = []

        with self._lock:
            allow_impersonation = bool(self._allow_impersonation)
            current_user_agent = str(self.current_user_agent or "")

        # Impersonation is opt-in because some yt-dlp environments advertise targets
        # but still fail at runtime due missing optional dependencies.
        impersonation = self._choose_impersonation_profile()
        if allow_impersonation and impersonation and self._impersonation_supported(impersonation):
            with self._lock:
                self._last_impersonation_profile = impersonation
            opts.extend(["--impersonate", impersonation])
        else:
            with self._lock:
                self._last_impersonation_profile = ""
            impersonation = ""
            opts.extend(["--user-agent", current_user_agent])

        # NOTE: YouTube player_client selection is managed by the downloader's
        # _default_youtube_extractor_args() which has adaptive format fallback logic.
        # Do NOT inject --extractor-args here — it would override the fallback mechanism.

        for header_name, header_value in self._current_headers().items():
            opts.extend(["--add-header", f"{header_name}:{header_value}"])
        opts.extend(["--sleep-interval", f"{profile['sleep_interval']:.2f}"])
        opts.extend(["--max-sleep-interval", f"{profile['max_sleep_interval']:.2f}"])
        opts.extend(["--sleep-requests", f"{profile['sleep_requests']:.2f}"])
        
        # Add proxy if enabled
        opts.extend(proxy_manager.get_yt_dlp_flag())

        return opts

    def get_yt_dlp_analysis_options(self) -> list[str]:
        """
        Conservative args for metadata analysis/probing.
        Keep this path minimal and stable to avoid parser/extractor edge cases.
        """
        effective_strategy = self._effective_strategy()
        with self._lock:
            user_agent = str(self.current_user_agent or "")
            allow_impersonation = bool(self._allow_impersonation)
        opts = []
        impersonation = self._choose_impersonation_profile()
        if (
            effective_strategy in {"cautious", "strict"}
            and allow_impersonation
            and impersonation
            and self._impersonation_supported(impersonation)
        ):
            with self._lock:
                self._last_impersonation_profile = impersonation
            opts.extend(["--impersonate", impersonation])
        else:
            with self._lock:
                self._last_impersonation_profile = ""
            opts.extend(["--user-agent", user_agent])
        for header_name, header_value in self._current_headers().items():
            opts.extend(["--add-header", f"{header_name}:{header_value}"])
        opts.extend(proxy_manager.get_yt_dlp_flag())
        return opts

    def on_error(self, error_text: str):
        """
        Analyzes a download error and adjusts the strategy accordingly.
        Returns True if a retry is recommended.
        """
        error_lower = error_text.lower()
        is_rate_limit = "too many requests" in error_lower or "429" in error_lower or "rate limit" in error_lower
        is_bot_challenge = any(
            marker in error_lower for marker in ["captcha", "forbidden", "403", "unusual traffic", "verify you are human"]
        )
        
        if is_rate_limit or is_bot_challenge:
            should_rotate_proxy = False
            with self._lock:
                self._error_streak = min(_MAX_ERROR_STREAK, self._error_streak + 1)
                self._clean_streak = 0
                self.strategy = "cautious"
                rotate_guard = getattr(proxy_manager, "can_rotate", None)
                if callable(rotate_guard):
                    should_rotate_proxy = bool(rotate_guard())
                else:
                    should_rotate_proxy = proxy_manager.is_enabled() and len(proxy_manager.config.get("proxies", [])) > 1
            logger.warning("[Anti-Detect] Rate limit detected. Switching strategy.")
            if should_rotate_proxy:
                proxy_manager.rotate(randomize=True)
                logger.info("[Anti-Detect] Rotated proxy.")
            self.get_next_user_agent()
            return True # Recommend a retry
        # Gradually cool down instead of instant reset to avoid oscillation.
        with self._lock:
            self._clean_streak += 1
            if self._clean_streak >= 2 and self._error_streak > 0:
                self._error_streak -= 1
                self._clean_streak = 0
            if self._error_streak <= _COOLDOWN_ERRORS_FOR_NORMAL:
                self._error_streak = 0
                self.strategy = "normal"
                self._last_impersonation_profile = ""
        return False

# Singleton instance
anti_detection_engine = AntiDetectionEngine()
