
from .constants import (
    QUALITY_HEIGHT_ALIASES,
    MAX_RETRY_DELAY_SECONDS,
    POLL_INTERVAL_SECONDS,
    PROCESS_TERMINATION_TIMEOUT,
    PROCESS_KILL_TIMEOUT,
)
import os

DEFAULT_SETTINGS = {
    "retries": 3,
    "auto_retry_delay_seconds": 4,
    "queue_auto_retry_limit": 2,
    "queue_priority": "fifo",
    "max_concurrent": 3,
    "storage_guard_enabled": True,
    "storage_min_free_gb": 5,
    "trial_total_days": 14,
    "search_history_limit": 40,
    "search_history_ttl_days": 30,
    "thumbnail_cache_max": 200,
    "virus_scan_after_download": False,
    "use_ytdlp_api": False,
    "use_native_engine": True,
    "sponsorblock_enabled": False,
    "hard_burn_subs": False,
    "normalize_audio_postprocess": False,
    "auto_categorize_downloads": False,
    "auto_categorize_mode": "off",
    "cookies_from_browser": "none",
    "custom_merge_hw_encoder": "off",
    "custom_merge_force_reencode": False,
    "custom_merge_video_preset": "p5",
    "clean_metadata": True,
}

THEME_MODE_MAP = {
    "dark": "Modern Dark",
    "light": "Elegant Light",
}


def normalize_video_quality_label(value: str, default: str = "1080p") -> str:
    text = str(value or "").replace("p", "").split("(")[0].strip().upper()
    if not text:
        return default
    if text in {"8K", "4320"}:
        return "8K"
    if text in {"4K", "2160"}:
        return "4K"
    digits = "".join(ch for ch in text if ch.isdigit())
    return f"{digits}p" if digits else default


def video_quality_to_height(value: str, default: str = "1080") -> str:
    text = str(value or "").replace("p", "").split("(")[0].strip().upper()
    if not text:
        return default
    if text in QUALITY_HEIGHT_ALIASES:
        return QUALITY_HEIGHT_ALIASES[text]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits or default


def default_download_dir() -> str:
    home = os.path.expanduser("~")
    for candidate in [os.path.join(home, "Downloads"), os.path.join(home, "downloads")]:
        if os.path.isdir(candidate):
            return candidate
    return os.path.join(os.getcwd(), "downloads")


def estimate_file_size_bytes(duration_seconds: int, mode: str, quality: str) -> int:
    try:
        seconds = max(0, int(duration_seconds or 0))
    except (TypeError, ValueError, OverflowError):
        return 0
    if seconds <= 0:
        return 0

    mode_text = str(mode or "").strip().lower()
    quality_text = str(quality or "").strip().lower()

    if mode_text in {"audio", "صوت"}:
        digits = "".join(ch for ch in quality_text if ch.isdigit())
        try:
            kbps = int(digits) if digits else 192
        except (TypeError, ValueError, OverflowError):
            kbps = 192
        return int((seconds * kbps * 1000) / 8)

    per_minute_mb = 7
    if "8k" in quality_text or "4320" in quality_text:
        per_minute_mb = 450
    elif "4k" in quality_text or "2160" in quality_text:
        per_minute_mb = 220
    elif "1440" in quality_text:
        per_minute_mb = 140
    elif "1080" in quality_text:
        per_minute_mb = 90
    elif "720" in quality_text:
        per_minute_mb = 50
    elif "480" in quality_text:
        per_minute_mb = 28
    elif "360" in quality_text:
        per_minute_mb = 17
    elif "240" in quality_text:
        per_minute_mb = 10

    mb = (seconds / 60.0) * per_minute_mb
    return int(mb * 1024 * 1024)
