"""
core/bandwidth_scheduler.py — Time-Based Bandwidth Scheduler
Automatically adjusts download speed based on time of day.
Example: Full speed at night, limited during work hours.
"""
import json
import os
import logging
from datetime import datetime, time
from typing import Optional
from .utils import get_app_data_dir

logger = logging.getLogger("SnapDownloader.BandwidthScheduler")

CONFIG_PATH = os.path.join(get_app_data_dir(), "bandwidth_schedule.json")
_LEGACY_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bandwidth_schedule.json")

DEFAULT_SCHEDULE = [
    # (start_hour, end_hour, limit_kbps)  — 0 means unlimited
    {"start": "00:00", "end": "08:00", "limit_kbps": 0,      "label": "Night (Unlimited)"},
    {"start": "08:00", "end": "12:00", "limit_kbps": 2048,   "label": "Morning (2 MB/s)"},
    {"start": "12:00", "end": "18:00", "limit_kbps": 1024,   "label": "Work Hours (1 MB/s)"},
    {"start": "18:00", "end": "22:00", "limit_kbps": 0,      "label": "Evening (Unlimited)"},
    {"start": "22:00", "end": "24:00", "limit_kbps": 0,      "label": "Late Night (Unlimited)"},
]


class BandwidthScheduler:
    def __init__(self):
        self.schedule = []
        self.enabled = False
        self._migrate_legacy_config()
        self.load()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _migrate_legacy_config(self):
        try:
            if os.path.exists(CONFIG_PATH):
                return
            if not os.path.exists(_LEGACY_CONFIG_PATH):
                return
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            os.replace(_LEGACY_CONFIG_PATH, CONFIG_PATH)
        except Exception:
            pass

    def load(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.enabled = bool(data.get("enabled", False))
                self.schedule = self.normalize_schedule(data.get("schedule", DEFAULT_SCHEDULE))
                return
            except Exception as exc:
                logger.warning(f"[Scheduler] Failed to load config, using defaults: {exc}")
        self.schedule = self.normalize_schedule(DEFAULT_SCHEDULE)
        self.enabled = False

    def save(self):
        try:
            payload_schedule = self.normalize_schedule(self.schedule)
            self.schedule = payload_schedule
            
            config_dir = os.path.dirname(CONFIG_PATH) or os.getcwd()
            if config_dir:
                os.makedirs(config_dir, exist_ok=True)
                
            import tempfile
            fd = -1
            tmp_path = ""
            try:
                fd, tmp_path = tempfile.mkstemp(prefix=".bandwidth_schedule_", suffix=".tmp", dir=config_dir)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    fd = -1
                    json.dump({"enabled": self.enabled, "schedule": payload_schedule}, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                try:
                    os.chmod(tmp_path, 0o600)
                except Exception:
                    pass
                if os.name == "nt":
                    try:
                        from .cookie_importer import _harden_windows_file_permissions
                        _harden_windows_file_permissions(tmp_path)
                    except Exception:
                        pass
                os.replace(tmp_path, CONFIG_PATH)
                try:
                    os.chmod(CONFIG_PATH, 0o600)
                except Exception:
                    pass
                if os.name == "nt":
                    try:
                        from .cookie_importer import _harden_windows_file_permissions
                        _harden_windows_file_permissions(CONFIG_PATH)
                    except Exception:
                        pass
            finally:
                if fd != -1:
                    try:
                        os.close(fd)
                    except Exception:
                        pass
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
        except Exception as exc:
            logger.warning(f"[Scheduler] Failed to save: {exc}")

    # ── Logic ─────────────────────────────────────────────────────────────────

    def get_current_limit(self) -> int:
        """
        Return the current bandwidth limit in KB/s.
        Returns 0 if scheduler is disabled or no matching rule.
        """
        if not self.enabled or not self.schedule:
            return 0

        now = datetime.now().time()
        for rule in self.schedule:
            try:
                start = _parse_time(rule["start"])
                end   = _parse_time(rule["end"])
                if _time_in_range(start, end, now):
                    return int(rule.get("limit_kbps", 0))
            except Exception as exc:
                logger.debug(f"[Scheduler] Invalid rule skipped in get_current_limit: {exc}")
                continue
        return 0

    def get_active_rule(self) -> Optional[dict]:
        """Return the currently active rule dict or None."""
        if not self.enabled:
            return None
        now = datetime.now().time()
        for rule in self.schedule:
            try:
                if _time_in_range(_parse_time(rule["start"]), _parse_time(rule["end"]), now):
                    return rule
            except Exception as exc:
                logger.debug(f"[Scheduler] Invalid rule skipped in get_active_rule: {exc}")
                continue
        return None

    def set_enabled(self, enabled: bool):
        self.enabled = enabled
        self.save()

    def set_schedule(self, schedule):
        self.schedule = self.normalize_schedule(schedule)
        self.save()

    def update_rule(self, index: int, start: str, end: str, limit_kbps: int, label: str = ""):
        if 0 <= index < len(self.schedule):
            self.schedule[index] = self.normalize_schedule(
                [{"start": start, "end": end, "limit_kbps": limit_kbps, "label": label}]
            )[0]
            self.save()

    def add_rule(self, start: str, end: str, limit_kbps: int, label: str = ""):
        self.schedule.extend(
            self.normalize_schedule(
                [{"start": start, "end": end, "limit_kbps": limit_kbps, "label": label}]
            )
        )
        self.save()

    def remove_rule(self, index: int):
        if 0 <= index < len(self.schedule):
            self.schedule.pop(index)
            self.save()

    def reset_to_defaults(self):
        self.schedule = self.normalize_schedule(DEFAULT_SCHEDULE)
        self.save()

    def normalize_schedule(self, schedule) -> list[dict]:
        if not isinstance(schedule, list):
            raise ValueError("schedule must be a list")
        normalized = []
        for item in schedule:
            if not isinstance(item, dict):
                raise ValueError("each schedule rule must be an object")
            start = _normalize_time_text(item.get("start", "00:00"))
            end = _normalize_time_text(item.get("end", "00:00"))
            _parse_time(start)
            _parse_time(end)
            limit_kbps = max(0, int(item.get("limit_kbps", 0) or 0))
            label = str(item.get("label", "")).strip() or f"{start} → {end}"
            normalized.append(
                {
                    "start": start,
                    "end": end,
                    "limit_kbps": limit_kbps,
                    "label": label,
                }
            )
        return normalized

    def format_schedule_summary(self) -> str:
        if not self.enabled:
            return "Scheduler: Disabled"
        rule = self.get_active_rule()
        if not rule:
            return "Scheduler: No matching rule"
        limit = rule.get("limit_kbps", 0)
        label = rule.get("label", "")
        speed_str = f"{limit} KB/s" if limit > 0 else "Unlimited"
        return f"Scheduler: {label} → {speed_str}"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_time(t: str) -> time:
    text = _normalize_time_text(t)
    h_text, m_text = text.split(":")
    hour = int(h_text)
    minute = int(m_text)
    # M-13: Use 23:59:59 as sentinel for end-of-day instead of 0:0
    # This avoids confusion in _time_in_range when start < end
    if hour == 24 and minute == 0:
        return time(23, 59, 59)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid time: {t}")
    return time(hour, minute)


def _normalize_time_text(value: str) -> str:
    parts = str(value or "").strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid time format: {value}")
    hour = int(parts[0])
    minute = int(parts[1])
    if hour == 24 and minute == 0:
        return "24:00"
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"invalid time: {value}")
    return f"{hour:02d}:{minute:02d}"


def _time_in_range(start: time, end: time, now: time) -> bool:
    if start <= end:
        if end == time(23, 59, 59):
            return start <= now <= end
        return start <= now < end
    return now >= start or now < end  # handles midnight wrap with end-exclusive boundary


# Singleton instance
scheduler = BandwidthScheduler()
