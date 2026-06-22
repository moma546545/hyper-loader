import hashlib
import json
import threading
import time
import urllib.parse
from typing import Any

from .database import (
    diff_playlist_entries,
    get_playlists_due_for_sync,
    get_playlist_known_ids,
    get_playlist_sync_state,
    purge_playlist_cache,
    sync_playlist_snapshot,
    update_playlist_sync_state,
    upsert_playlist_entries,
)


def _is_safe_playlist_url(url: str) -> bool:
    target = str(url or "").strip()
    if not target:
        return False
    try:
        parsed = urllib.parse.urlparse(target)
        return parsed.scheme.lower() in {"http", "https", "ftp", "ftps"}
    except Exception:
        return False


class PlaylistSyncService:
    def __init__(self):
        self._inflight_lock = threading.RLock()
        self._inflight_urls: set[str] = set()

    @staticmethod
    def _metadata_json(
        playlist_url: str,
        *,
        entry_count: int = 0,
        payload: dict | None = None,
        last_error: str = "",
    ) -> str:
        info = payload if isinstance(payload, dict) else {}
        compact = {
            "playlist_url": str(playlist_url or "").strip(),
            "entry_count": max(0, int(entry_count or 0)),
            "kind": str(info.get("kind", "") or "").strip(),
            "title": str(info.get("playlist_title") or info.get("title", "") or "").strip(),
            "channel_id": str(info.get("channel_id") or info.get("channel", "") or "").strip(),
            "last_error": str(last_error or "").strip(),
        }
        return json.dumps(compact, ensure_ascii=False)

    @staticmethod
    def build_snapshot_id(playlist_url: str, entry_count: int = 0) -> str:
        base = f"{str(playlist_url or '').strip()}|{int(entry_count or 0)}|{int(time.time())}"
        return hashlib.sha1(base.encode("utf-8", errors="ignore")).hexdigest()[:20]

    @staticmethod
    def extract_playlist_url(payload: dict | None, fallback_url: str = "") -> str:
        return str(
            (payload or {}).get("url")
            or (payload or {}).get("webpage_url")
            or fallback_url
            or ""
        ).strip()

    @staticmethod
    def extract_sync_metadata(payload: dict | None, *, entry_count: int = 0) -> dict[str, str]:
        info = payload if isinstance(payload, dict) else {}
        playlist_url = str(info.get("url") or info.get("webpage_url") or "").strip()
        snapshot_id = str(info.get("snapshot_id", "") or "").strip()
        if not snapshot_id:
            snapshot_id = PlaylistSyncService.build_snapshot_id(playlist_url, entry_count=entry_count)
        return {
            "etag": str(info.get("etag", "") or "").strip(),
            "last_modified": str(info.get("last_modified", "") or "").strip(),
            "snapshot_id": snapshot_id,
            "sync_status": str(info.get("sync_status", "syncing") or "syncing").strip(),
            "channel_id": str(info.get("channel_id") or info.get("channel", "") or "").strip(),
            "playlist_title": str(info.get("playlist_title") or info.get("title", "") or "").strip(),
            "sync_cursor": str(info.get("sync_cursor", "") or "").strip(),
        }

    def get_known_ids(self, playlist_url: str) -> set[str]:
        if not _is_safe_playlist_url(playlist_url):
            raise ValueError(f"Unsafe playlist URL rejected: {playlist_url}")
        return set(get_playlist_known_ids(playlist_url))

    def get_sync_state(self, playlist_url: str) -> dict[str, Any]:
        if not _is_safe_playlist_url(playlist_url):
            raise ValueError(f"Unsafe playlist URL rejected: {playlist_url}")
        return dict(get_playlist_sync_state(playlist_url) or {})

    def get_due_playlists(
        self,
        *,
        now_ts: int | None = None,
        min_age_seconds: int = 1800,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        rows = get_playlists_due_for_sync(
            now_ts=now_ts,
            min_age_seconds=min_age_seconds,
            limit=limit,
        )
        return [dict(item or {}) for item in rows or []]

    def has_inflight_syncs(self) -> bool:
        with self._inflight_lock:
            return bool(self._inflight_urls)

    def has_inflight_sync(self, playlist_url: str) -> bool:
        target = str(playlist_url or "").strip()
        if not target:
            return False
        if not _is_safe_playlist_url(target):
            raise ValueError(f"Unsafe playlist URL rejected: {playlist_url}")
        with self._inflight_lock:
            return target in self._inflight_urls

    def acquire_due_playlist_for_sync(
        self,
        *,
        now_ts: int | None = None,
        min_age_seconds: int = 1800,
        limit: int = 20,
    ) -> str:
        due_rows = self.get_due_playlists(
            now_ts=now_ts,
            min_age_seconds=min_age_seconds,
            limit=max(1, int(limit or 1)),
        )
        for row in due_rows:
            playlist_url = str((row or {}).get("playlist_url", "") or "").strip()
            if not playlist_url:
                continue
            if not _is_safe_playlist_url(playlist_url):
                continue
            with self._inflight_lock:
                if playlist_url in self._inflight_urls:
                    continue
                self._inflight_urls.add(playlist_url)
                return playlist_url
        return ""

    def release_inflight_sync(self, playlist_url: str) -> None:
        target = str(playlist_url or "").strip()
        if not target:
            return
        if not _is_safe_playlist_url(target):
            raise ValueError(f"Unsafe playlist URL rejected: {playlist_url}")
        with self._inflight_lock:
            self._inflight_urls.discard(target)

    def get_backoff_info(self, playlist_url: str, *, now_ts: int | None = None) -> dict[str, int | bool]:
        state = self.get_sync_state(playlist_url)
        now_value = int(time.time()) if now_ts is None else int(now_ts or 0)
        next_sync_after = int(state.get("next_sync_after", 0) or 0)
        remaining = max(0, next_sync_after - now_value)
        return {
            "is_active": bool(remaining > 0),
            "remaining_seconds": int(remaining),
            "next_sync_after": int(next_sync_after),
            "consecutive_failures": int(state.get("consecutive_failures", 0) or 0),
        }

    def should_defer_sync(
        self,
        playlist_url: str,
        *,
        has_cached_ids: bool = False,
        force: bool = False,
        now_ts: int | None = None,
    ) -> dict[str, int | bool]:
        info = self.get_backoff_info(playlist_url, now_ts=now_ts)
        should_defer = bool(info.get("is_active")) and bool(has_cached_ids) and not bool(force)
        return {
            "should_defer": should_defer,
            "is_active": bool(info.get("is_active")),
            "remaining_seconds": int(info.get("remaining_seconds", 0) or 0),
            "next_sync_after": int(info.get("next_sync_after", 0) or 0),
            "consecutive_failures": int(info.get("consecutive_failures", 0) or 0),
        }

    def mark_sync_started(self, playlist_url: str, *, payload: dict | None = None) -> None:
        url = str(playlist_url or self.extract_playlist_url(payload) or "").strip()
        if not url:
            return
        if not _is_safe_playlist_url(url):
            raise ValueError(f"Unsafe playlist URL rejected: {url}")
        metadata = self.extract_sync_metadata(payload, entry_count=0)
        update_playlist_sync_state(
            url,
            playlist_title=metadata.get("playlist_title", ""),
            channel_id=metadata.get("channel_id", ""),
            etag=metadata.get("etag", ""),
            last_modified=metadata.get("last_modified", ""),
            snapshot_id=metadata.get("snapshot_id", ""),
            sync_status="syncing",
            sync_cursor=metadata.get("sync_cursor", ""),
            metadata_json=self._metadata_json(url, payload=payload),
            last_error="",
        )

    def upsert_entries(
        self,
        playlist_url: str,
        entries: list[dict],
        *,
        payload: dict | None = None,
        sync_status: str = "syncing",
    ) -> None:
        if not _is_safe_playlist_url(playlist_url):
            raise ValueError(f"Unsafe playlist URL rejected: {playlist_url}")
        metadata = self.extract_sync_metadata(payload, entry_count=len(entries or []))
        metadata["sync_status"] = str(sync_status or metadata.get("sync_status") or "syncing")
        upsert_playlist_entries(
            playlist_url,
            entries,
            etag=metadata.get("etag", ""),
            last_modified=metadata.get("last_modified", ""),
            snapshot_id=metadata.get("snapshot_id", ""),
            sync_status=metadata.get("sync_status", "syncing"),
            channel_id=metadata.get("channel_id", ""),
            playlist_title=metadata.get("playlist_title", ""),
            sync_cursor=metadata.get("sync_cursor", ""),
        )

    def diff_entries(self, playlist_url: str, current_entry_ids: list[str]) -> dict:
        if not _is_safe_playlist_url(playlist_url):
            raise ValueError(f"Unsafe playlist URL rejected: {playlist_url}")
        return dict(diff_playlist_entries(playlist_url, current_entry_ids))

    def mark_sync_failed(
        self,
        playlist_url: str,
        error_message: str,
        *,
        payload: dict | None = None,
    ) -> None:
        url = str(playlist_url or self.extract_playlist_url(payload) or "").strip()
        if not url:
            return
        if not _is_safe_playlist_url(url):
            raise ValueError(f"Unsafe playlist URL rejected: {url}")
        metadata = self.extract_sync_metadata(payload, entry_count=0)
        last_error = str(error_message or "").strip()
        update_playlist_sync_state(
            url,
            playlist_title=metadata.get("playlist_title", ""),
            channel_id=metadata.get("channel_id", ""),
            etag=metadata.get("etag", ""),
            last_modified=metadata.get("last_modified", ""),
            snapshot_id=metadata.get("snapshot_id", ""),
            sync_status="failed",
            sync_cursor=metadata.get("sync_cursor", ""),
            metadata_json=self._metadata_json(url, payload=payload, last_error=last_error),
            last_error=last_error,
        )

    def sync_snapshot(
        self,
        playlist_url: str,
        current_entry_ids: list[str],
        *,
        payload: dict | None = None,
    ) -> int:
        if not _is_safe_playlist_url(playlist_url):
            raise ValueError(f"Unsafe playlist URL rejected: {playlist_url}")
        metadata = self.extract_sync_metadata(payload, entry_count=len(current_entry_ids or []))
        current_state = self.get_sync_state(playlist_url)
        if not metadata.get("etag"):
            metadata["etag"] = str(current_state.get("etag", "") or "")
        if not metadata.get("last_modified"):
            metadata["last_modified"] = str(current_state.get("last_modified", "") or "")
        if not metadata.get("snapshot_id"):
            metadata["snapshot_id"] = str(current_state.get("snapshot_id", "") or "")
        if not metadata.get("sync_cursor"):
            metadata["sync_cursor"] = str(current_state.get("sync_cursor", "") or "")
        return int(
            sync_playlist_snapshot(
                playlist_url,
                current_entry_ids,
                etag=metadata.get("etag", ""),
                last_modified=metadata.get("last_modified", ""),
                snapshot_id=metadata.get("snapshot_id", ""),
                channel_id=metadata.get("channel_id", ""),
                playlist_title=metadata.get("playlist_title", ""),
                sync_cursor=metadata.get("sync_cursor", ""),
                last_error="",
            )
            or 0
        )

    def purge_stale(self, playlist_url: str, *, keep_days: int = 90) -> int:
        if not _is_safe_playlist_url(playlist_url):
            raise ValueError(f"Unsafe playlist URL rejected: {playlist_url}")
        return int(purge_playlist_cache(playlist_url, keep_days=keep_days) or 0)
