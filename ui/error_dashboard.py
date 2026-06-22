
"""
ui/error_dashboard.py — Smart Error Reporting Dashboard
Displays a premium error analysis panel with actionable solutions.
Categorizes errors by type and suggests specific fixes.
"""
try:
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import (
        QWidget, QFrame, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QListWidget, QListWidgetItem,
        QTextEdit, QScrollArea, QApplication,
    )
except ImportError:
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtWidgets import (
        QWidget, QFrame, QVBoxLayout, QHBoxLayout,
        QLabel, QPushButton, QListWidget, QListWidgetItem,
        QTextEdit, QScrollArea, QApplication,
    )

import re
import json
import os
from datetime import datetime
from core.utils import redact_url


# ── Error Pattern Database ────────────────────────────────────────────────────

ERROR_PATTERNS = [
    {
        "id": "geo_block",
        "pattern": r"(not available in your country|geo.?block|region)",
        "category": "🌍 Geo-Block",
        "severity": "high",
        "title": "Content blocked in your region",
        "solutions": [
            "Enable a VPN or Proxy in Settings → Proxy Manager",
            "Try a different server location (US/EU)",
            "Use Cookie Import from a browser where content is accessible",
        ],
    },
    {
        "id": "auth_required",
        "pattern": r"(sign in|login required|private video|members only|age.?restricted)",
        "category": "🔐 Authentication",
        "severity": "high",
        "title": "Login or authentication required",
        "solutions": [
            "Use Settings → Auto-Import Cookies from your browser (Chrome/Edge)",
            "Log in to the website in your browser first, then import cookies",
            "Manually download a cookies.txt file using browser extension",
        ],
    },
    {
        "id": "network_error",
        "pattern": r"(connection reset|connection refused|network|timeout|socket|unreachable)",
        "category": "🌐 Network",
        "severity": "medium",
        "title": "Network connectivity issue",
        "solutions": [
            "Check your internet connection",
            "Check if the website is up (try opening URL in browser)",
            "Try enabling Proxy in Settings",
            "Increase retry count in Settings → Download Logic",
        ],
    },
    {
        "id": "rate_limited",
        "pattern": r"(too many requests|rate.?limit|429|403.*youtube|slow down)",
        "category": "⏱ Rate Limited",
        "severity": "medium",
        "title": "Too many requests — you've been rate-limited",
        "solutions": [
            "Wait 10-30 minutes before retrying",
            "Enable Proxy rotation to distribute requests",
            "Reduce Max Concurrent Downloads in Settings (set to 1-2)",
            "Enable bandwidth limiter to appear as a normal user",
        ],
    },
    {
        "id": "format_unavailable",
        "pattern": r"(format.*not available|requested format|no video formats found|bestvideo)",
        "category": "🎞 Format/Quality",
        "severity": "low",
        "title": "Requested quality or format not available",
        "solutions": [
            "Try a lower quality (720p instead of 1080p)",
            "Choose a different format (MP4 instead of WEBM)",
            "Tick 'Allow lower quality fallback' in settings",
        ],
    },
    {
        "id": "ffmpeg_missing",
        "pattern": r"(ffmpeg|ffprobe|merge output|postprocessing|converter)",
        "category": "🔧 FFMPEG",
        "severity": "medium",
        "title": "FFMPEG is missing or not in PATH",
        "solutions": [
            "Download FFMPEG from https://ffmpeg.org/download.html",
            "Add FFMPEG to your system PATH environment variable",
            "Place ffmpeg.exe in the same folder as SnapDownloader",
        ],
    },
    {
        "id": "url_invalid",
        "pattern": r"(unsupported url|invalid url|unable to extract|extractor|no suitable)",
        "category": "🔗 URL/Site",
        "severity": "low",
        "title": "URL is unsupported or invalid",
        "solutions": [
            "Check the URL is correct and the page still exists",
            "Update yt-dlp core: Settings → Check for yt-dlp Updates",
            "This site might not be supported — check yt-dlp supported sites list",
        ],
    },
    {
        "id": "disk_full",
        "pattern": r"(no space left|disk.*full|not enough space|errno 28)",
        "category": "💾 Disk Space",
        "severity": "high",
        "title": "Not enough disk space",
        "solutions": [
            "Free up disk space (delete old files)",
            "Change download folder to a drive with more space (Settings → Download Path)",
            "Clear completed downloads from the Downloads history",
        ],
    },
    {
        "id": "cancelled",
        "pattern": r"(cancel|interrupt|stopped by user)",
        "category": "⏹ Cancelled",
        "severity": "info",
        "title": "Download was cancelled by user",
        "solutions": ["Use Retry button to restart this download"],
    },
]

SEVERITY_COLORS = {
    "high":   "#FF4D4D",
    "medium": "#FFA040",
    "low":    "#FFD700",
    "info":   "#00F0FF",
}


def analyze_error(error_text: str) -> dict:
    """
    Match an error string against known patterns and return analysis dict.
    Returns a dict with category, severity, title, solutions.
    """
    if not error_text:
        return {
            "id": "unknown",
            "category": "❓ Unknown",
            "severity": "low",
            "title": "Unknown error",
            "solutions": [
                "Check app.log for detailed output",
                "Update yt-dlp to the latest version",
                "Try the download again",
            ],
            "raw": error_text,
        }

    lower = error_text.lower()
    for pattern in ERROR_PATTERNS:
        if re.search(pattern["pattern"], lower):
            result = dict(pattern)
            result["raw"] = error_text
            return result

    return {
        "id": "unknown",
        "category": "❓ Unknown",
        "severity": "low",
        "title": "Unrecognized error",
        "solutions": [
            "Check the log panel for full output",
            "Google the exact error message",
            "Update yt-dlp: Settings → Check for yt-dlp Updates",
            "Open a GitHub issue with the log attached",
        ],
        "raw": error_text,
    }


class ErrorDashboard(QWidget):
    """
    Premium error reporting panel showing failed downloads
    with AI-style root cause analysis and solution cards.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._errors: list[dict] = []
        self._build_ui()

    # ── UI Construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        # Header
        hdr = QHBoxLayout()
        title = QLabel("⚠️ Error Analysis Center")
        title.setObjectName("single_title")
        clear_btn = QPushButton("🗑 Clear All")
        clear_btn.setObjectName("action_schedule")
        clear_btn.setFixedWidth(110)
        clear_btn.clicked.connect(self._clear_errors)
        hdr.addWidget(title)
        hdr.addStretch(1)
        hdr.addWidget(clear_btn)
        layout.addLayout(hdr)

        sub_lbl = QLabel("Smart root-cause analysis with step-by-step solutions")
        sub_lbl.setObjectName("single_sub")
        layout.addWidget(sub_lbl)

        # Error list
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._list_container = QWidget()
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(10)
        self._list_layout.addStretch(1)
        scroll.setWidget(self._list_container)
        layout.addWidget(scroll, 1)

        self._empty_lbl = QLabel("✅ No errors recorded — all downloads are healthy!")
        self._empty_lbl.setObjectName("empty_title")
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._empty_lbl)

    # ── Public API ────────────────────────────────────────────────────────────

    def report_error(self, url: str, error_text: str, title: str = ""):
        """Add a new error report to the dashboard."""
        analysis = analyze_error(error_text)
        analysis["url"] = url
        analysis["title_hint"] = title
        analysis["timestamp"] = datetime.now().strftime("%H:%M:%S")
        self._errors.insert(0, analysis)
        self._rebuild_list()

    def _clear_errors(self):
        self._errors.clear()
        self._rebuild_list()

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _rebuild_list(self):
        # Clear existing widgets except the stretch
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self._empty_lbl.setVisible(len(self._errors) == 0)

        for err in self._errors:
            card = self._make_error_card(err)
            self._list_layout.insertWidget(self._list_layout.count() - 1, card)

    def _make_error_card(self, err: dict) -> QFrame:
        severity = err.get("severity", "low")
        color = SEVERITY_COLORS.get(severity, "#FFD700")

        card = QFrame()
        card.setObjectName("playlist_row")
        card.setStyleSheet(f"QFrame#playlist_row {{ border-left: 4px solid {color}; }}")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(6)

        # Top row
        top = QHBoxLayout()
        cat_lbl = QLabel(err.get("category", "Unknown"))
        cat_lbl.setStyleSheet(f"color: {color}; font-weight: 900; font-size: 11px;")
        time_lbl = QLabel(err.get("timestamp", ""))
        time_lbl.setObjectName("single_sub")
        copy_btn = QPushButton("📋")
        copy_btn.setObjectName("icon_btn")
        copy_btn.setFixedSize(26, 26)
        copy_btn.setToolTip("Copy Error")
        raw = err.get("raw", "")
        copy_btn.clicked.connect(lambda _, r=raw: QApplication.clipboard().setText(r))
        top.addWidget(cat_lbl)
        top.addStretch(1)
        top.addWidget(time_lbl)
        top.addWidget(copy_btn)
        layout.addLayout(top)

        # Title
        title_lbl = QLabel(err.get("title", "Error"))
        title_lbl.setObjectName("playlist_title")
        layout.addWidget(title_lbl)

        # URL hint
        url = err.get("url", "")
        if url:
            safe_url = redact_url(url)
            url_lbl = QLabel(f"🔗 {safe_url[:70]}{'…' if len(safe_url) > 70 else ''}")
            url_lbl.setObjectName("playlist_url")
            layout.addWidget(url_lbl)

        # Solutions
        for i, sol in enumerate(err.get("solutions", []), 1):
            sol_lbl = QLabel(f"  {i}. {sol}")
            sol_lbl.setObjectName("single_sub")
            sol_lbl.setWordWrap(True)
            layout.addWidget(sol_lbl)

        return card



