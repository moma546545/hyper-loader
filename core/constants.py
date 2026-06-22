
"""
core/constants.py — Centralized constants for SnapDownloader
Includes quality mappings, format lists, and magic numbers.
"""

# Video Quality Mappings
VIDEO_QUALITIES = ["8K", "4K", "1440p", "1080p", "720p", "480p", "360p", "240p", "144p"]
QUALITY_HEIGHT_ALIASES = {
    "8K": "4320",
    "4320": "4320",
    "4K": "2160",
    "2160": "2160",
}

# Format Lists
VIDEO_FORMATS = ["MP4", "WEBM", "MKV", "AVI", "FLV", "MOV"]
AUDIO_FORMATS = ["Original Audio", "MP3", "AAC", "M4A", "WAV", "AIFF", "WMA", "FLAC"]
AUDIO_QUALITIES = ["320kbps", "256kbps", "192kbps", "160kbps", "128kbps", "96kbps"]
SUBTITLE_OPTIONS = ["None", "All", "Arabic", "Chinese", "English", "French", "German", "Italian", "Spanish", "Swedish"]

DOWNLOAD_CATEGORIES = [
    "General",
    "Music",
    "Movies",
    "Education",
    "Podcast",
    "Other",
]

# Subtitle Mappings
SUBTITLE_LANGUAGES = {
    "Arabic": "ar",
    "Chinese": "zh",
    "English": "en",
    "French": "fr",
    "German": "de",
    "Italian": "it",
    "Spanish": "es",
    "Swedish": "sv",
}

# Magic Numbers for Retry Logic
MAX_RETRY_DELAY_SECONDS = 120
POLL_INTERVAL_SECONDS = 0.1
PROCESS_TERMINATION_TIMEOUT = 5
PROCESS_KILL_TIMEOUT = 2


DOWNLOAD_CATEGORIES = [
    "General",
    "Music",
    "Movies",
    "Education",
    "Podcast",
    "Other",
]

# Subtitle Mappings
SUBTITLE_LANGUAGES = {
    "Arabic": "ar",
    "Chinese": "zh",
    "English": "en",
    "French": "fr",
    "German": "de",
    "Italian": "it",
    "Spanish": "es",
    "Swedish": "sv",
}

# Magic Numbers for Retry Logic
MAX_RETRY_DELAY_SECONDS = 120
POLL_INTERVAL_SECONDS = 0.1
PROCESS_TERMINATION_TIMEOUT = 5
PROCESS_KILL_TIMEOUT = 2

# Storage Guard
MIN_FREE_SPACE_GB = 5

# Search History
SEARCH_HISTORY_LIMIT = 40

# UI/Performance Constants
THUMBNAIL_CACHE_MAX = 200
THUMBNAIL_FAILED_MAX = 2000
MAX_CONCURRENT_THUMBNAILS = 4
THUMBNAIL_CLEANUP_INTERVAL_MS = 30000
THUMBNAIL_WAITER_TTL_SECONDS = 30
LIQUID_TIMER_INTERVAL_MS = 120
THUMBNAIL_VISIBLE_BATCH_PER_TICK = 6
PROGRESS_BUS_DRAIN_MS = 80

APP_VERSION = "3.0.0"
