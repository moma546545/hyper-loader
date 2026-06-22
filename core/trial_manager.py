from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
import tempfile

from .secure_storage import protect_text, unprotect_text
from .utils import get_app_data_dir


logger = logging.getLogger("SnapDownloader.TrialManager")
_TRIAL_STATE_VERSION = 1


@dataclass
class TrialState:
    started_at: str
    total_days: int
    days_remaining: int


class TrialManager:
    def __init__(self, enabled: bool, total_days: int, load_settings=None):
        self.enabled = bool(enabled)
        self.total_days = max(1, int(total_days or 1))
        self._load_settings = load_settings
        self._state_path = os.path.join(get_app_data_dir(), "trial_state.json")

    def _now(self) -> datetime:
        return datetime.now()

    def _parse_dt(self, value: str) -> datetime | None:
        try:
            return datetime.fromisoformat(str(value))
        except Exception:
            return None

    def _canonical_dt(self, value: datetime) -> str:
        return value.replace(microsecond=0).isoformat()

    def _coerce_total_days(self, value: int | None) -> int:
        try:
            candidate = int(value or self.total_days)
        except Exception:
            candidate = self.total_days
        # Never trust persisted/session state to extend the configured trial window.
        return max(1, min(candidate, self.total_days))

    def _load_settings_snapshot(self) -> dict:
        if not callable(self._load_settings):
            return {}
        try:
            settings = self._load_settings() or {}
        except Exception:
            return {}
        return settings if isinstance(settings, dict) else {}

    def _extract_started_at(self, settings: dict) -> str:
        return (
            str(settings.get("trial_started_at") or "")
            or str(settings.get("trial_start") or "")
            or str(settings.get("trial_started") or "")
        ).strip()

    def _read_shadow_state(self) -> dict | None:
        try:
            if not os.path.isfile(self._state_path):
                return None
            with open(self._state_path, "r", encoding="utf-8") as handle:
                raw = handle.read().strip()
            if not raw:
                return None
            container = json.loads(raw)
            if not isinstance(container, dict):
                return None
            protected_payload = str(container.get("protected_state") or "").strip()
            payload_text = unprotect_text(protected_payload) if protected_payload else ""
            if not payload_text:
                payload_text = protected_payload
            if not payload_text:
                return None
            payload = json.loads(payload_text)
            if not isinstance(payload, dict):
                return None
            version = int(payload.get("version") or 0)
            if version != _TRIAL_STATE_VERSION:
                return None
            started_at = str(payload.get("started_at") or "").strip()
            last_seen_at = str(payload.get("last_seen_at") or "").strip()
            start_dt = self._parse_dt(started_at)
            last_seen_dt = self._parse_dt(last_seen_at)
            if start_dt is None or last_seen_dt is None:
                return None
            if last_seen_dt < start_dt:
                last_seen_dt = start_dt
            return {
                "started_at": self._canonical_dt(start_dt),
                "last_seen_at": self._canonical_dt(last_seen_dt),
            }
        except Exception:
            logger.debug("Failed to read trial shadow state", exc_info=True)
            return None

    def _write_shadow_state(self, started_at: str, last_seen_at: str) -> None:
        start_dt = self._parse_dt(started_at)
        last_seen_dt = self._parse_dt(last_seen_at)
        if start_dt is None or last_seen_dt is None:
            return
        if last_seen_dt < start_dt:
            last_seen_dt = start_dt
        payload = json.dumps(
            {
                "version": _TRIAL_STATE_VERSION,
                "started_at": self._canonical_dt(start_dt),
                "last_seen_at": self._canonical_dt(last_seen_dt),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        wrapped = json.dumps(
            {"protected_state": protect_text(payload) or payload},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        directory = os.path.dirname(self._state_path) or get_app_data_dir()
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="trial_state_", suffix=".tmp", dir=directory, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(wrapped)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._state_path)
        except Exception:
            logger.debug("Failed to persist trial shadow state", exc_info=True)
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def _resolve_state(self, started_at: str, total_days: int) -> TrialState:
        td = self._coerce_total_days(total_days)
        if not self.enabled:
            return TrialState("", td, td)

        shadow = self._read_shadow_state()
        now_dt = self._now()
        candidate_starts: list[datetime] = []
        provided_start = self._parse_dt(started_at)
        if provided_start is not None:
            candidate_starts.append(provided_start)
        if shadow is not None:
            shadow_start = self._parse_dt(shadow["started_at"])
            if shadow_start is not None:
                candidate_starts.append(shadow_start)

        start_dt = min(candidate_starts) if candidate_starts else now_dt

        shadow_last_seen = self._parse_dt(shadow["last_seen_at"]) if shadow is not None else None
        effective_now = now_dt
        if shadow_last_seen is not None and shadow_last_seen > effective_now:
            effective_now = shadow_last_seen
        if start_dt > effective_now:
            start_dt = effective_now

        elapsed_days = max(0, (effective_now.date() - start_dt.date()).days)
        remaining = max(0, td - elapsed_days)

        canonical_started_at = self._canonical_dt(start_dt)
        canonical_last_seen = self._canonical_dt(max(effective_now, start_dt))
        self._write_shadow_state(canonical_started_at, canonical_last_seen)
        return TrialState(canonical_started_at, td, remaining)

    def load_state(self) -> TrialState:
        if not self.enabled:
            return TrialState("", self.total_days, self.total_days)
        settings = self._load_settings_snapshot()
        started_at = self._extract_started_at(settings)
        return self._resolve_state(started_at=started_at, total_days=self.total_days)

    def recompute(self, started_at: str, total_days: int) -> TrialState:
        return self._resolve_state(started_at=started_at, total_days=total_days)

    def banner_text(self, days_remaining: int) -> str:
        if not self.enabled:
            return ""
        days = max(0, int(days_remaining))
        if days == 0:
            return "Trial expired"
        if days == 1:
            return "1 day remaining in trial"
        return f"{days} days remaining in trial"
