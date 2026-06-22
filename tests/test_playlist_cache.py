"""
tests/test_playlist_cache.py — Regression tests for the SQLite-backed
playlist_cache differential sync functions:
  - upsert_playlist_entries
  - get_playlist_known_ids
  - diff_playlist_entries
  - purge_playlist_cache
"""
import os
import sys
import time
import sqlite3
import tempfile
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Redirect DB_PATH to a fresh temp file for each test."""
    import core.database as db_module
    db_path = str(tmp_path / "test_snap.db")
    monkeypatch.setattr(db_module, "DB_PATH", db_path)
    # Reset thread-local connection so the new path is picked up
    if hasattr(db_module._local, "conn") and db_module._local.conn is not None:
        try:
            db_module._local.conn.close()
        except Exception:
            pass
        db_module._local.conn = None
    db_module.init_db()
    yield db_path
    # Cleanup
    if hasattr(db_module._local, "conn") and db_module._local.conn is not None:
        try:
            db_module._local.conn.close()
        except Exception:
            pass
        db_module._local.conn = None


# ── Tests: upsert + get_playlist_known_ids ────────────────────────────────────

def test_upsert_empty_entries_is_noop():
    from core.database import upsert_playlist_entries, get_playlist_known_ids
    upsert_playlist_entries("https://playlist.test/1", [])
    assert get_playlist_known_ids("https://playlist.test/1") == set()


def test_upsert_and_retrieve():
    from core.database import upsert_playlist_entries, get_playlist_known_ids
    url = "https://youtube.com/playlist?list=PLtest"
    entries = [
        {"id": "vid001", "title": "Video 1", "duration_seconds": 120, "playlist_index": 0},
        {"id": "vid002", "title": "Video 2", "duration_seconds": 240, "playlist_index": 1},
    ]
    upsert_playlist_entries(url, entries)
    known = get_playlist_known_ids(url)
    assert known == {"vid001", "vid002"}


def test_upsert_idempotent():
    from core.database import upsert_playlist_entries, get_playlist_known_ids
    url = "https://youtube.com/playlist?list=PLidem"
    entries = [{"id": "abc", "title": "X"}]
    upsert_playlist_entries(url, entries)
    upsert_playlist_entries(url, entries)  # second insert should upsert not duplicate
    known = get_playlist_known_ids(url)
    assert known == {"abc"}


def test_upsert_uses_entry_id_fallback():
    """Entries with 'entry_id' key instead of 'id' must still work."""
    from core.database import upsert_playlist_entries, get_playlist_known_ids
    url = "https://youtube.com/playlist?list=PLfallback"
    entries = [{"entry_id": "e001", "title": "Fallback Entry"}]
    upsert_playlist_entries(url, entries)
    known = get_playlist_known_ids(url)
    assert "e001" in known


def test_upsert_entries_without_id_are_skipped():
    from core.database import upsert_playlist_entries, get_playlist_known_ids
    url = "https://youtube.com/playlist?list=PLno_id"
    entries = [{"title": "No ID here"}]
    upsert_playlist_entries(url, entries)
    assert get_playlist_known_ids(url) == set()


def test_separate_playlists_dont_mix():
    from core.database import upsert_playlist_entries, get_playlist_known_ids
    url1 = "https://youtube.com/playlist?list=PL1"
    url2 = "https://youtube.com/playlist?list=PL2"
    upsert_playlist_entries(url1, [{"id": "shared_id", "title": "PL1 video"}])
    upsert_playlist_entries(url2, [{"id": "only_in_pl2", "title": "PL2 video"}])
    known1 = get_playlist_known_ids(url1)
    known2 = get_playlist_known_ids(url2)
    assert known1 == {"shared_id"}
    assert known2 == {"only_in_pl2"}


# ── Tests: diff_playlist_entries ──────────────────────────────────────────────

def test_diff_first_fetch_all_new():
    """If cache is empty every entry is reported as 'new'."""
    from core.database import diff_playlist_entries
    url = "https://youtube.com/playlist?list=PLnew"
    result = diff_playlist_entries(url, ["v1", "v2", "v3"])
    assert result["is_first_fetch"] is True
    assert result["new_ids"] == {"v1", "v2", "v3"}
    assert result["removed_ids"] == set()
    assert result["known_ids"] == set()


def test_diff_second_fetch_no_change():
    from core.database import upsert_playlist_entries, diff_playlist_entries
    url = "https://youtube.com/playlist?list=PLnochange"
    ids = ["v1", "v2", "v3"]
    entries = [{"id": v} for v in ids]
    upsert_playlist_entries(url, entries)
    result = diff_playlist_entries(url, ids)
    assert result["is_first_fetch"] is False
    assert result["new_ids"] == set()
    assert result["removed_ids"] == set()
    assert result["known_ids"] == {"v1", "v2", "v3"}


def test_diff_detects_new_entry():
    from core.database import upsert_playlist_entries, diff_playlist_entries
    url = "https://youtube.com/playlist?list=PLgrowth"
    original = ["v1", "v2"]
    upsert_playlist_entries(url, [{"id": v} for v in original])
    result = diff_playlist_entries(url, ["v1", "v2", "v3_new"])
    assert result["new_ids"] == {"v3_new"}
    assert result["removed_ids"] == set()
    assert result["known_ids"] == {"v1", "v2"}


def test_diff_detects_removed_entry():
    from core.database import upsert_playlist_entries, diff_playlist_entries
    url = "https://youtube.com/playlist?list=PLshrink"
    original = ["v1", "v2", "v3"]
    upsert_playlist_entries(url, [{"id": v} for v in original])
    result = diff_playlist_entries(url, ["v1", "v2"])  # v3 removed
    assert result["removed_ids"] == {"v3"}
    assert result["new_ids"] == set()


def test_diff_mixed_changes():
    from core.database import upsert_playlist_entries, diff_playlist_entries
    url = "https://youtube.com/playlist?list=PLmixed"
    upsert_playlist_entries(url, [{"id": v} for v in ["old1", "old2", "keep"]])
    result = diff_playlist_entries(url, ["keep", "brand_new"])
    assert result["new_ids"] == {"brand_new"}
    assert result["removed_ids"] == {"old1", "old2"}
    assert result["known_ids"] == {"keep"}


# ── Tests: sync_playlist_snapshot ─────────────────────────────────────────────

def test_sync_playlist_snapshot_removes_missing_entries():
    from core.database import upsert_playlist_entries, get_playlist_known_ids, sync_playlist_snapshot
    url = "https://youtube.com/playlist?list=PLsyncA"
    upsert_playlist_entries(url, [{"id": "v1"}, {"id": "v2"}, {"id": "v3"}])

    deleted = sync_playlist_snapshot(url, ["v1", "v3"])

    assert deleted == 1
    assert get_playlist_known_ids(url) == {"v1", "v3"}


def test_sync_playlist_snapshot_empty_current_deletes_all():
    from core.database import upsert_playlist_entries, get_playlist_known_ids, sync_playlist_snapshot
    url = "https://youtube.com/playlist?list=PLsyncEmpty"
    upsert_playlist_entries(url, [{"id": "v1"}, {"id": "v2"}])

    deleted = sync_playlist_snapshot(url, [])

    assert deleted == 2
    assert get_playlist_known_ids(url) == set()


def test_sync_playlist_snapshot_preserves_sync_metadata():
    from core.database import get_playlist_sync_state, upsert_playlist_entries, sync_playlist_snapshot

    url = "https://youtube.com/playlist?list=PLmeta"
    upsert_playlist_entries(
        url,
        [{"id": "v1"}, {"id": "v2"}],
        etag="etag-1",
        last_modified="Mon, 01 Jan 2024 00:00:00 GMT",
        snapshot_id="snap-123",
        sync_status="syncing",
        channel_id="channel-42",
        playlist_title="Playlist Title",
        sync_cursor="cursor-1",
    )

    sync_playlist_snapshot(
        url,
        ["v1", "v2"],
        etag="etag-1",
        last_modified="Mon, 01 Jan 2024 00:00:00 GMT",
        snapshot_id="snap-123",
        channel_id="channel-42",
        playlist_title="Playlist Title",
        sync_cursor="cursor-1",
    )

    state = get_playlist_sync_state(url)
    assert state["sync_status"] == "synced"
    assert state["etag"] == "etag-1"
    assert state["last_modified"] == "Mon, 01 Jan 2024 00:00:00 GMT"
    assert state["snapshot_id"] == "snap-123"
    assert state["sync_cursor"] == "cursor-1"


def test_playlist_sync_service_marks_failure_state():
    from core.playlist_sync_service import PlaylistSyncService

    service = PlaylistSyncService()
    url = "https://youtube.com/playlist?list=PLfailed"

    service.mark_sync_started(url, payload={"url": url, "title": "Broken Playlist", "snapshot_id": "snap-fail"})
    service.mark_sync_failed(url, "network timeout", payload={"url": url, "title": "Broken Playlist", "snapshot_id": "snap-fail"})

    state = service.get_sync_state(url)
    assert state["sync_status"] == "failed"
    assert state["snapshot_id"] == "snap-fail"
    assert state["last_error"] == "network timeout"
    assert state["consecutive_failures"] >= 1
    assert state["next_sync_after"] >= state["last_sync_at"]


def test_playlist_sync_service_backoff_increases_with_repeated_failures():
    from core.playlist_sync_service import PlaylistSyncService

    service = PlaylistSyncService()
    url = "https://youtube.com/playlist?list=PLfailed-twice"

    service.mark_sync_failed(url, "first timeout", payload={"url": url})
    first = service.get_sync_state(url)
    service.mark_sync_failed(url, "second timeout", payload={"url": url})
    second = service.get_sync_state(url)

    assert int(first.get("consecutive_failures", 0)) == 1
    assert int(second.get("consecutive_failures", 0)) == 2
    assert int(second.get("next_sync_after", 0)) >= int(first.get("next_sync_after", 0))


def test_playlist_sync_service_success_resets_backoff_counters():
    from core.playlist_sync_service import PlaylistSyncService

    service = PlaylistSyncService()
    url = "https://youtube.com/playlist?list=PLrecover"
    payload = {"url": url, "etag": "etag-ok", "snapshot_id": "snap-ok"}

    service.mark_sync_failed(url, "rate-limited", payload=payload)
    service.upsert_entries(url, [{"id": "v1"}], payload=payload, sync_status="syncing")
    service.sync_snapshot(url, ["v1"], payload=payload)
    state = service.get_sync_state(url)

    assert state["sync_status"] == "synced"
    assert int(state.get("consecutive_failures", 0)) == 0
    assert int(state.get("next_sync_after", 0)) == 0
    assert int(state.get("last_success_at", 0)) >= int(state.get("last_sync_at", 0))


def test_playlist_sync_service_should_defer_sync_requires_active_backoff_and_cache():
    from core.playlist_sync_service import PlaylistSyncService

    service = PlaylistSyncService()
    url = "https://youtube.com/playlist?list=PLdefer"
    service.mark_sync_failed(url, "temporary failure", payload={"url": url})

    defer = service.should_defer_sync(url, has_cached_ids=True, force=False)
    no_cache = service.should_defer_sync(url, has_cached_ids=False, force=False)
    forced = service.should_defer_sync(url, has_cached_ids=True, force=True)

    assert defer["should_defer"] is True
    assert no_cache["should_defer"] is False
    assert forced["should_defer"] is False


def test_get_playlists_due_for_sync_respects_backoff_and_staleness():
    from core.database import update_playlist_sync_state
    from core.playlist_sync_service import PlaylistSyncService

    service = PlaylistSyncService()
    now_ts = int(time.time())

    ready_url = "https://youtube.com/playlist?list=PLready"
    blocked_url = "https://youtube.com/playlist?list=PLblocked"
    syncing_url = "https://youtube.com/playlist?list=PLsyncing"

    update_playlist_sync_state(ready_url, sync_status="failed", last_error="e1")
    update_playlist_sync_state(ready_url, sync_status="failed", last_error="e1-repeat")

    update_playlist_sync_state(blocked_url, sync_status="failed", last_error="e2")
    update_playlist_sync_state(syncing_url, sync_status="syncing", last_error="")

    due_early = service.get_due_playlists(now_ts=now_ts, min_age_seconds=0, limit=50)
    due_late = service.get_due_playlists(now_ts=now_ts + 7200, min_age_seconds=0, limit=50)
    early_urls = {str(item.get("playlist_url", "")) for item in due_early}
    due_late_urls = {str(item.get("playlist_url", "")) for item in due_late}

    assert ready_url not in early_urls
    assert blocked_url not in early_urls
    assert ready_url in due_late_urls
    assert blocked_url in due_late_urls
    assert syncing_url not in due_late_urls


def test_playlist_sync_service_acquire_release_due_playlist_flow(monkeypatch):
    from core.playlist_sync_service import PlaylistSyncService

    service = PlaylistSyncService()
    due_rows = [
        {"playlist_url": "https://youtube.com/playlist?list=PL1"},
        {"playlist_url": "https://youtube.com/playlist?list=PL2"},
    ]
    monkeypatch.setattr(service, "get_due_playlists", lambda **_kwargs: list(due_rows))

    first = service.acquire_due_playlist_for_sync(min_age_seconds=0, limit=10)
    second = service.acquire_due_playlist_for_sync(min_age_seconds=0, limit=10)
    third = service.acquire_due_playlist_for_sync(min_age_seconds=0, limit=10)

    assert first.endswith("PL1")
    assert second.endswith("PL2")
    assert third == ""
    assert service.has_inflight_syncs() is True

    service.release_inflight_sync(first)
    again = service.acquire_due_playlist_for_sync(min_age_seconds=0, limit=10)
    assert again.endswith("PL1")


# ── Tests: purge_playlist_cache ───────────────────────────────────────────────

def test_purge_empty_is_noop():
    from core.database import purge_playlist_cache
    deleted = purge_playlist_cache("https://no-entries.test/", keep_days=30)
    assert deleted == 0


def test_purge_old_entries(isolated_db):
    """Manually backdate last_seen_at to simulate old entries."""
    from core.database import upsert_playlist_entries, get_playlist_known_ids, purge_playlist_cache
    url = "https://youtube.com/playlist?list=PLold"
    entries = [{"id": "old1"}, {"id": "old2"}]
    upsert_playlist_entries(url, entries)

    # Manually set last_seen_at to 100 days ago
    cutoff_ts = int(time.time()) - 100 * 86400
    with sqlite3.connect(isolated_db) as conn:
        conn.execute(
            "UPDATE playlist_cache SET last_seen_at = ? WHERE playlist_url = ?",
            (cutoff_ts, url),
        )
        conn.commit()

    deleted = purge_playlist_cache(url, keep_days=30)
    assert deleted == 2
    assert get_playlist_known_ids(url) == set()


def test_purge_keeps_recent_entries(isolated_db):
    from core.database import upsert_playlist_entries, get_playlist_known_ids, purge_playlist_cache
    url = "https://youtube.com/playlist?list=PLrecent"
    entries = [{"id": "recent1"}, {"id": "recent2"}]
    upsert_playlist_entries(url, entries)

    # 5 days old → should survive keep_days=30
    cutoff_ts = int(time.time()) - 5 * 86400
    with sqlite3.connect(isolated_db) as conn:
        conn.execute(
            "UPDATE playlist_cache SET last_seen_at = ? WHERE playlist_url = ?",
            (cutoff_ts, url),
        )
        conn.commit()

    deleted = purge_playlist_cache(url, keep_days=30)
    assert deleted == 0
    assert get_playlist_known_ids(url) == {"recent1", "recent2"}


def test_diff_and_sync_scale_5k_entries():
    from core.database import upsert_playlist_entries, diff_playlist_entries, sync_playlist_snapshot, get_playlist_known_ids

    url = "https://youtube.com/playlist?list=PLscale5k"
    cached_ids = [f"vid{idx:05d}" for idx in range(5000)]
    upsert_playlist_entries(url, [{"id": entry_id} for entry_id in cached_ids])

    # Simulate partial churn: remove 250, keep 4750, add 250.
    current_ids = [f"vid{idx:05d}" for idx in range(250, 5000)] + [f"new{idx:05d}" for idx in range(250)]
    started_at = time.perf_counter()
    result = diff_playlist_entries(url, current_ids)
    upsert_playlist_entries(url, [{"id": entry_id} for entry_id in current_ids])
    deleted = sync_playlist_snapshot(url, current_ids)
    elapsed = time.perf_counter() - started_at

    assert len(result["new_ids"]) == 250
    assert len(result["removed_ids"]) == 250
    assert deleted == 250
    assert len(get_playlist_known_ids(url)) == 5000
    # Guardrail only: this should comfortably finish on normal dev machines.
    assert elapsed < 5.0
