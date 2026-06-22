import os
import re
import sys
from urllib.parse import urlsplit, urlunsplit

def get_app_data_dir() -> str:
    """
    Return the path to the application's data directory.
    Uses platform-specific locations (AppData on Windows, Application Support on macOS, XDG_DATA_HOME on Linux).
    Creates the directory if it does not exist.
    """
    app_name = "VidDownloader"
    if sys.platform == "win32":
        base = os.getenv("APPDATA") or os.path.expanduser("~")
        path = os.path.join(base, app_name)
    elif sys.platform == "darwin":
        path = os.path.join(os.path.expanduser("~"), "Library", "Application Support", app_name)
    else:
        base = os.getenv("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
        path = os.path.join(base, app_name)
        
    os.makedirs(path, exist_ok=True)
    return path

def get_resource_path(relative_path: str) -> str:
    """
    Get the absolute path to a resource, works for dev and for PyInstaller.
    """
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except Exception:
        # Not running in a PyInstaller bundle
        base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    return os.path.join(base_path, relative_path)


def clean_metadata_title(title: str) -> str:
    """
    Cleans video titles by removing common suffixes and noise.
    Example: "Video Title [Official HD] (2024)" -> "Video Title"
    """
    if not title:
        return ""
    
    # Common noise patterns
    patterns = [
        r"\[.*?\]",           # Anything in square brackets
        r"\(.*?\)",           # Anything in parentheses
        r"\{.*?\}",           # Anything in curly braces
        r"official (video|audio|lyrics|music video|hd|4k|mv)",
        r"full (episode|movie|hd|album)",
        r"\d{3,4}p",          # Resolution like 1080p, 720p
        r"\|.*",              # Anything after a pipe
        r"-.*Official.*",     # Anything after a dash containing Official
        r"\b(hd|4k|uhd|hq|sd)\b",
        r"lyrics video",
        r"premiered on.*",
    ]
    
    cleaned = title
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    
    # Remove extra spaces and leading/trailing punctuation
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.strip("-_|/\\ ")
    
    return cleaned or title


def redact_url(url: str) -> str:
    """
    Return a privacy-safe representation of a URL for logs and notifications.
    Removes query params and fragments and redacts embedded credentials.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except Exception:
        return text.split("?", 1)[0].split("#", 1)[0]
    if not parts.scheme or not parts.netloc:
        return text.split("?", 1)[0].split("#", 1)[0]
    hostname = parts.hostname or ""
    netloc = hostname
    if parts.username:
        netloc = f"{parts.username}:***@{hostname}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def redact_urls_in_text(text: str) -> str:
    value = str(text or "")
    if not value:
        return ""
    return re.sub(r"https?://[^\s\"'<>]+", lambda m: redact_url(m.group(0)), value)


def sanitize_queue_items_for_safe_export(items: list[dict]) -> list[dict]:
    safe_items = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        safe_item = {
            "title": str(raw.get("title", "")).strip() or redact_url(raw.get("url", "")),
            "url": redact_url(raw.get("url", "")),
            "mode": str(raw.get("mode", "") or ""),
            "quality": str(raw.get("quality", "") or ""),
            "format": str(raw.get("format", "") or ""),
            "subtitle": str(raw.get("subtitle", "") or ""),
            "duration_seconds": int(raw.get("duration_seconds") or 0),
            "status": str(raw.get("status", "") or ""),
            "category": str(raw.get("category", "") or ""),
            "channel": str(raw.get("channel", "") or ""),
            "source": str(raw.get("source", "") or ""),
            "playlist_title": str(raw.get("playlist_title", "") or ""),
            "playlist_index": int(raw.get("playlist_index") or 0),
        }
        error_msg = redact_urls_in_text(raw.get("error_msg", ""))
        if error_msg:
            safe_item["error_msg"] = error_msg
        safe_items.append(safe_item)
    return safe_items
