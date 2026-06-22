from __future__ import annotations
import re
import urllib.parse
from .base import AbstractDownloadProvider

# Known extractor-required domains: these need yt-dlp's logic, not direct HTTP
_EXTRACTOR_DOMAINS = {
    "youtube.com", "youtu.be",
    "vimeo.com",
    "twitter.com", "x.com",
    "instagram.com",
    "facebook.com", "fb.watch",
    "tiktok.com",
    "dailymotion.com",
    "twitch.tv",
    "reddit.com",
    "bilibili.com",
    "soundcloud.com",
    "spotify.com",
    "mixcloud.com",
    "ok.ru",
    "rutube.ru",
}

# Extensions considered "direct" (raw file links)
_DIRECT_EXTENSIONS = {
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv", ".m4v",
    ".mp3", ".m4a", ".aac", ".flac", ".opus", ".ogg", ".wav",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".pdf", ".iso",
}


def _url_is_direct(url: str) -> bool:
    """Heuristic: does the URL point directly at a file by extension?"""
    try:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path.lower().rstrip("/")
        ext = path[path.rfind("."):] if "." in path else ""
        if ext in _DIRECT_EXTENSIONS:
            return True
        # CDN-style direct links often have these query params
        if "Content-Disposition" in parsed.query or "response-content-disposition" in parsed.query:
            return True
    except Exception:
        pass
    return False


def _url_needs_extractor(url: str) -> bool:
    """Returns True if the URL requires yt-dlp's extractor logic."""
    try:
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower().strip()
        # strip www.
        host = host[4:] if host.startswith("www.") else host
        if host in _EXTRACTOR_DOMAINS:
            return True
        # Check if it's a direct file — extractors not needed
        if _url_is_direct(url):
            return False
        # Unknown domain — let yt-dlp handle it (it has 1800+ extractors)
        return True
    except Exception:
        return True


class YtDlpProvider(AbstractDownloadProvider):
    """
    Download provider that wraps the existing yt-dlp subprocess / API logic
    from DownloadWorker. Acts as the universal fallback for complex video URLs.
    """

    @classmethod
    def can_handle(cls, url: str, is_direct: bool = False) -> bool:
        # yt-dlp handles: known extractor domains, or anything not a direct file.
        if is_direct:
            return False
        return _url_needs_extractor(url)

    def start(self) -> None:
        runner = getattr(self.worker, "_run_ytdlp_once", None)
        if not callable(runner):
            raise RuntimeError("YtDlpProvider requires worker._run_ytdlp_once()")
        ok, was_cancelled, error = runner()
        if was_cancelled:
            self.is_cancelled = True
            return
        if not ok:
            raise RuntimeError(str(error or "yt-dlp provider failed"))

    def pause(self) -> None:
        self.is_paused = True

    def resume(self) -> None:
        self.is_paused = False

    def stop(self) -> None:
        self.is_cancelled = True
