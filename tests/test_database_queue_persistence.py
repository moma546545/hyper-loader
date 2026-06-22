import json
import os
import pytest
import sqlite3
from datetime import datetime, timedelta

from core import database as db


@pytest.fixture()
def isolated_db(monkeypatch, tmp_path):
    db.close_thread_connection()
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "queue-persistence.db"))
    monkeypatch.setattr(db, "_last_queue_save_signature", None)
    db.init_db()
    yield
    db.close_thread_connection()


def test_update_task_state_fast_targets_logical_queue_item_after_resave(isolated_db):
    items = [
        {"url": "https://example.com/a", "title": "First"},
        {"url": "https://example.com/b", "title": "Second"},
    ]

    db.save_queue_items(items)
    db.save_queue_items(items)

    db.update_task_state_fast(0, 37.5, "1.0 MiB/s", "00:10", "running")
    loaded = db.load_queue_items()

    assert loaded[0]["progress"] == 37.5
    assert loaded[0]["speed"] == "1.0 MiB/s"
    assert loaded[0]["eta"] == "00:10"
    assert loaded[0]["status"] == "running"
    assert loaded[1]["progress"] == 0.0
    assert loaded[1]["status"] == "pending"


def test_save_and_load_queue_items_preserve_retry_schedule_and_file_metadata(isolated_db):
    item = {
        "task_uuid": "task-1",
        "url": "https://example.com/watch?v=1",
        "title": "Track One",
        "out_dir": "D:/downloads/music",
        "mode": "video",
        "quality": "720p",
        "format": "mp4",
        "subtitle": "en",
        "start_time": "00:05",
        "end_time": "01:05",
        "retries": 8,
        "auto_retry_delay_seconds": 11,
        "queue_retry_limit": 5,
        "retry_count": 2,
        "next_retry_at": 123.45,
        "scheduled_at": 456.78,
        "duration_seconds": 360,
        "is_live": True,
        "was_live": False,
        "live_status": "is_live",
        "bandwidth_limit_kbps": 900,
        "use_aria2": False,
        "status": "queued",
        "thumbnail": "thumb.jpg",
        "category": "Music",
        "schedule_repeat": "daily",
        "channel": "Example Channel",
        "source": "browser_extension",
        "playlist_url": "https://example.com/playlist?id=42",
        "playlist_index": 7,
        "playlist_title": "My Playlist",
        "entry_id": "entry-007",
        "progress": 12.5,
        "speed": "500 KiB/s",
        "eta": "00:22",
        "error_msg": "retry later",
        "file_path": "D:/downloads/music/track-one.mp4",
        "last_output_path": "D:/downloads/music/final-track-one.mp4",
        "resume_json": "{\"parts\": 2}",
        "trims": [{"start": "00:05", "end": "00:20", "title": "clip"}],
        "size": "~12.0 MB",
        "size_bytes": 12_582_912,
        "estimated_size_bytes": 12_582_912,
        "size_is_estimate": True,
        "video_id": "vid-123",
        "file_hash": "abc123",
        "post_action": "open_folder",
        "post_download_script": "D:/scripts/post.bat",
        "embed_subs": False,
        "split_chapters": True,
        "whisper_fallback": True,
        "sponsorblock_enabled": True,
        "verify_checksum": True,
        "virus_scan_after_download": True,
        "use_ytdlp_api": True,
        "rename_template": "Archive",
        "use_native_engine": True,
        "cookies_from_browser": "edge",
        "merge_opts": {
            "enabled": True,
            "video_codec": "libx264",
            "video_crf": 21,
            "audio_codec": "aac",
            "audio_bitrate": "256k",
            "hw_encoder": "auto",
            "force_reencode": True,
            "video_preset": "p6",
        },
    }

    db.save_queue_items([item])
    loaded = db.load_queue_items()

    assert len(loaded) == 1
    restored = loaded[0]
    assert restored["task_uuid"] == "task-1"
    assert restored["auto_retry_delay_seconds"] == 11
    assert restored["queue_retry_limit"] == 5
    assert restored["duration_seconds"] == 360
    assert restored["is_live"] is True
    assert restored["was_live"] is False
    assert restored["live_status"] == "is_live"
    assert restored["category"] == "Music"
    assert restored["schedule_repeat"] == "daily"
    assert restored["channel"] == "Example Channel"
    assert restored["source"] == "browser_extension"
    assert restored["playlist_url"] == "https://example.com/playlist?id=42"
    assert restored["playlist_index"] == 7
    assert restored["playlist_title"] == "My Playlist"
    assert restored["entry_id"] == "entry-007"
    assert restored["file_path"] == "D:/downloads/music/track-one.mp4"
    assert restored["last_output_path"] == "D:/downloads/music/final-track-one.mp4"
    assert restored["resume_json"] == "{\"parts\": 2}"
    assert restored["resume"] == {"parts": 2}
    assert restored["trims"] == [{"start": "00:05", "end": "00:20", "title": "clip"}]
    assert restored["size"] == "~12.0 MB"
    assert restored["size_text"] == "~12.0 MB"
    assert restored["size_bytes"] == 12_582_912
    assert restored["estimated_size_bytes"] == 12_582_912
    assert restored["size_is_estimate"] is True
    assert restored["video_id"] == "vid-123"
    assert restored["file_hash"] == "abc123"
    assert restored["post_action"] == "open_folder"
    assert restored["post_download_script"] == ""
    assert restored["embed_subs"] is False
    assert restored["split_chapters"] is True
    assert restored["whisper_fallback"] is True
    assert restored["sponsorblock_enabled"] is True
    assert restored["verify_checksum"] is True
    assert restored["virus_scan_after_download"] is True
    assert restored["use_ytdlp_api"] is True
    assert restored["rename_template"] == "Archive"
    assert restored["use_native_engine"] is True
    assert restored["cookies_from_browser"] == "edge"
    assert restored["merge_opts"] == {
        "enabled": True,
        "video_codec": "libx264",
        "video_crf": 21,
        "audio_codec": "aac",
        "audio_bitrate": "256k",
        "hw_encoder": "auto",
        "force_reencode": True,
        "video_preset": "p6",
    }


def test_save_queue_items_serializes_resume_payload_when_resume_json_is_missing(isolated_db):
    item = {
        "task_uuid": "task-resume",
        "url": "https://example.com/watch?v=resume",
        "title": "Resume Test",
        "resume": {
            "partials_count": 3,
            "partials_total_bytes": 2048,
            "output_path": "D:/downloads/resume.mp4",
        },
    }

    db.save_queue_items([item])
    loaded = db.load_queue_items()

    assert len(loaded) == 1
    restored = loaded[0]
    assert restored["resume"] == {
        "partials_count": 3,
        "partials_total_bytes": 2048,
        "output_path": "D:/downloads/resume.mp4",
    }
    assert restored["resume_json"]


def test_save_queue_items_reuses_rows_by_queue_position_instead_of_reinserting(isolated_db):
    items = [
        {"url": "https://example.com/a", "title": "First"},
        {"url": "https://example.com/b", "title": "Second"},
    ]

    db.save_queue_items(items)
    with sqlite3.connect(db.DB_PATH) as conn:
        first_snapshot = conn.execute(
            "SELECT id, queue_position, title FROM queue_items ORDER BY queue_position ASC"
        ).fetchall()

    db.save_queue_items(
        [
            {"url": "https://example.com/a", "title": "First Updated"},
            {"url": "https://example.com/b", "title": "Second"},
        ]
    )
    with sqlite3.connect(db.DB_PATH) as conn:
        second_snapshot = conn.execute(
            "SELECT id, queue_position, title FROM queue_items ORDER BY queue_position ASC"
        ).fetchall()

    assert [row[0] for row in first_snapshot] == [row[0] for row in second_snapshot]
    assert [row[1] for row in second_snapshot] == [0, 1]
    assert [row[2] for row in second_snapshot] == ["First Updated", "Second"]


def test_save_queue_items_preserves_row_identity_across_reorder_when_task_uuid_is_stable(isolated_db):
    items = [
        {"task_uuid": "task-a", "url": "https://example.com/a", "title": "First"},
        {"task_uuid": "task-b", "url": "https://example.com/b", "title": "Second"},
    ]

    db.save_queue_items(items)
    with sqlite3.connect(db.DB_PATH) as conn:
        first_snapshot = {
            row[1]: {"id": row[0], "queue_position": row[2], "title": row[3]}
            for row in conn.execute(
                "SELECT id, task_uuid, queue_position, title FROM queue_items ORDER BY queue_position ASC"
            ).fetchall()
        }

    db.save_queue_items([items[1], items[0]])
    with sqlite3.connect(db.DB_PATH) as conn:
        second_snapshot = {
            row[1]: {"id": row[0], "queue_position": row[2], "title": row[3]}
            for row in conn.execute(
                "SELECT id, task_uuid, queue_position, title FROM queue_items ORDER BY queue_position ASC"
            ).fetchall()
        }

    assert second_snapshot["task-a"]["id"] == first_snapshot["task-a"]["id"]
    assert second_snapshot["task-b"]["id"] == first_snapshot["task-b"]["id"]
    assert second_snapshot["task-a"]["queue_position"] == 1
    assert second_snapshot["task-b"]["queue_position"] == 0
    assert second_snapshot["task-a"]["title"] == "First"
    assert second_snapshot["task-b"]["title"] == "Second"


def test_save_queue_items_preserves_created_at_across_reorder(isolated_db):
    items = [
        {"task_uuid": "task-a", "url": "https://example.com/a", "title": "First"},
        {"task_uuid": "task-b", "url": "https://example.com/b", "title": "Second"},
    ]

    db.save_queue_items(items)
    with sqlite3.connect(db.DB_PATH) as conn:
        first_created_at = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT task_uuid, created_at FROM queue_items ORDER BY queue_position ASC"
            ).fetchall()
        }

    db.save_queue_items([items[1], items[0]])
    with sqlite3.connect(db.DB_PATH) as conn:
        second_created_at = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT task_uuid, created_at FROM queue_items ORDER BY queue_position ASC"
            ).fetchall()
        }

    assert second_created_at == first_created_at


def test_save_queue_items_noops_when_payload_is_identical(isolated_db):
    items = [
        {"task_uuid": "task-a", "url": "https://example.com/a", "title": "First"},
        {"task_uuid": "task-b", "url": "https://example.com/b", "title": "Second"},
    ]

    db.save_queue_items(items)
    with db._get_conn() as conn:
        before = conn.total_changes

    db.save_queue_items(items)
    with db._get_conn() as conn:
        after = conn.total_changes

    assert after - before == 0


def test_save_queue_items_updates_changed_rows_in_place_when_order_is_stable(isolated_db):
    items = [
        {"task_uuid": "task-a", "url": "https://example.com/a", "title": "First"},
        {"task_uuid": "task-b", "url": "https://example.com/b", "title": "Second"},
    ]

    db.save_queue_items(items)
    with db._get_conn() as conn:
        before = conn.total_changes

    db.save_queue_items(
        [
            {"task_uuid": "task-a", "url": "https://example.com/a", "title": "First Updated"},
            {"task_uuid": "task-b", "url": "https://example.com/b", "title": "Second"},
        ]
    )
    with db._get_conn() as conn:
        after = conn.total_changes

    assert after - before == 1


def test_save_queue_items_uses_in_memory_signature_fast_path_for_identical_payload(monkeypatch, isolated_db):
    items = [
        {"task_uuid": "task-a", "url": "https://example.com/a", "title": "First"},
        {"task_uuid": "task-b", "url": "https://example.com/b", "title": "Second"},
    ]
    db.save_queue_items(items)
    copied = [dict(item) for item in items]

    def _unexpected_db_access():
        raise AssertionError("save_queue_items should skip DB access for identical signed payload")

    monkeypatch.setattr(db, "_get_conn", _unexpected_db_access)
    db.save_queue_items(copied)


def test_update_task_state_fast_can_target_stable_task_uuid_after_reorder(isolated_db):
    items = [
        {"task_uuid": "task-a", "url": "https://example.com/a", "title": "First"},
        {"task_uuid": "task-b", "url": "https://example.com/b", "title": "Second"},
    ]

    db.save_queue_items(items)
    db.save_queue_items([items[1], items[0]])

    db.update_task_state_fast(0, 88.0, "2.0 MiB/s", "00:03", "running", task_uuid="task-a")
    loaded = db.load_queue_items()

    assert loaded[0]["task_uuid"] == "task-b"
    assert loaded[0]["progress"] == 0.0
    assert loaded[1]["task_uuid"] == "task-a"
    assert loaded[1]["progress"] == 88.0
    assert loaded[1]["speed"] == "2.0 MiB/s"
    assert loaded[1]["eta"] == "00:03"
    assert loaded[1]["status"] == "running"


def test_update_task_states_fast_batch_updates_multiple_rows_in_single_pass(isolated_db):
    items = [
        {"task_uuid": "task-a", "url": "https://example.com/a", "title": "First"},
        {"task_uuid": "task-b", "url": "https://example.com/b", "title": "Second"},
    ]

    db.save_queue_items(items)
    db.update_task_states_fast_batch(
        [
            {
                "queue_index": 0,
                "task_uuid": "task-a",
                "progress": 15.5,
                "speed": "500 KiB/s",
                "eta": "00:20",
                "status": "running",
            },
            {
                "queue_index": 1,
                "task_uuid": "task-b",
                "progress": 95.0,
                "speed": "1.5 MiB/s",
                "eta": "00:01",
                "status": "processing",
            },
        ]
    )

    loaded = db.load_queue_items()

    assert loaded[0]["progress"] == 15.5
    assert loaded[0]["speed"] == "500 KiB/s"
    assert loaded[0]["eta"] == "00:20"
    assert loaded[0]["status"] == "running"
    assert loaded[1]["progress"] == 95.0
    assert loaded[1]["speed"] == "1.5 MiB/s"
    assert loaded[1]["eta"] == "00:01"
    assert loaded[1]["status"] == "processing"


def test_update_task_states_fast_batch_uses_last_update_for_same_task_uuid(isolated_db):
    items = [
        {"task_uuid": "task-a", "url": "https://example.com/a", "title": "First"},
    ]

    db.save_queue_items(items)
    db.update_task_states_fast_batch(
        [
            {
                "queue_index": 0,
                "task_uuid": "task-a",
                "progress": 10.0,
                "speed": "100 KiB/s",
                "eta": "00:10",
                "status": "running",
            },
            {
                "queue_index": 0,
                "task_uuid": "task-a",
                "progress": 99.0,
                "speed": "900 KiB/s",
                "eta": "00:01",
                "status": "processing",
            },
        ]
    )

    loaded = db.load_queue_items()

    assert loaded[0]["progress"] == 99.0
    assert loaded[0]["speed"] == "900 KiB/s"
    assert loaded[0]["eta"] == "00:01"
    assert loaded[0]["status"] == "processing"


def test_update_task_states_fast_batch_falls_back_to_queue_position_without_task_uuid(isolated_db):
    items = [
        {"url": "https://example.com/a", "title": "First"},
        {"url": "https://example.com/b", "title": "Second"},
    ]

    db.save_queue_items(items)
    loaded_before = db.load_queue_items()
    assert loaded_before[0]["task_uuid"]
    assert loaded_before[1]["task_uuid"]

    db.update_task_states_fast_batch(
        [
            {
                "queue_index": 1,
                "progress": 64.0,
                "speed": "900 KiB/s",
                "eta": "00:04",
                "status": "running",
            }
        ]
    )

    loaded = db.load_queue_items()

    assert loaded[0]["progress"] == 0.0
    assert loaded[0]["status"] == "pending"
    assert loaded[1]["progress"] == 64.0
    assert loaded[1]["speed"] == "900 KiB/s"
    assert loaded[1]["eta"] == "00:04"
    assert loaded[1]["status"] == "running"


def test_save_queue_items_reorder_with_insert_and_delete_keeps_existing_identity(isolated_db):
    initial = [
        {"task_uuid": "task-a", "url": "https://example.com/a", "title": "First"},
        {"task_uuid": "task-b", "url": "https://example.com/b", "title": "Second"},
        {"task_uuid": "task-c", "url": "https://example.com/c", "title": "Third"},
    ]
    db.save_queue_items(initial)

    with sqlite3.connect(db.DB_PATH) as conn:
        before = {
            row[1]: {"id": row[0], "created_at": row[2]}
            for row in conn.execute(
                "SELECT id, task_uuid, created_at FROM queue_items ORDER BY queue_position ASC"
            ).fetchall()
        }

    changed = [
        {"task_uuid": "task-c", "url": "https://example.com/c", "title": "Third"},
        {"task_uuid": "task-a", "url": "https://example.com/a", "title": "First"},
        {"task_uuid": "task-d", "url": "https://example.com/d", "title": "Fourth"},
    ]
    db.save_queue_items(changed)

    with sqlite3.connect(db.DB_PATH) as conn:
        after_rows = conn.execute(
            "SELECT id, task_uuid, queue_position, created_at FROM queue_items ORDER BY queue_position ASC"
        ).fetchall()
    after = {row[1]: {"id": row[0], "queue_position": row[2], "created_at": row[3]} for row in after_rows}

    assert "task-b" not in after
    assert after["task-a"]["id"] == before["task-a"]["id"]
    assert after["task-c"]["id"] == before["task-c"]["id"]
    assert after["task-a"]["created_at"] == before["task-a"]["created_at"]
    assert after["task-c"]["created_at"] == before["task-c"]["created_at"]
    assert after["task-c"]["queue_position"] == 0
    assert after["task-a"]["queue_position"] == 1
    assert after["task-d"]["queue_position"] == 2


def test_fetch_queue_entries_page_from_db_filters_views_and_paginates(isolated_db):
    db.save_queue_items(
        [
            {"task_uuid": "t1", "url": "https://example.com/a", "title": "A", "status": "running"},
            {"task_uuid": "t2", "url": "https://example.com/b", "title": "B", "status": "pending"},
            {"task_uuid": "t3", "url": "https://example.com/c", "title": "C", "status": "pending", "scheduled_at": 9999999999},
            {"task_uuid": "t4", "url": "https://example.com/d", "title": "D", "status": "queued"},
        ]
    )

    active = db.fetch_queue_entries_page_from_db(view="active", now_ts=0, page=1, page_size=10)
    queued = db.fetch_queue_entries_page_from_db(view="queued", now_ts=0, page=1, page_size=10)
    scheduled = db.fetch_queue_entries_page_from_db(view="scheduled", now_ts=0, page=1, page_size=10)
    queued_page_1 = db.fetch_queue_entries_page_from_db(view="queued", now_ts=0, page=1, page_size=1)
    queued_page_2 = db.fetch_queue_entries_page_from_db(view="queued", now_ts=0, page=2, page_size=1)

    assert [entry["task_uuid"] for entry in active["entries"]] == ["t1"]
    assert [entry["task_uuid"] for entry in queued["entries"]] == ["t2", "t4"]
    assert [entry["task_uuid"] for entry in scheduled["entries"]] == ["t3"]
    assert scheduled["entries"][0]["status"] == "scheduled"
    assert queued_page_1["total_pages"] == 2
    assert queued_page_1["entries"][0]["task_uuid"] == "t2"
    assert queued_page_2["entries"][0]["task_uuid"] == "t4"


def test_fetch_queue_entries_page_from_db_supports_state_filter_and_query(isolated_db):
    db.save_queue_items(
        [
            {"task_uuid": "q1", "url": "https://example.com/a", "title": "Alpha", "status": "pending"},
            {"task_uuid": "q2", "url": "https://example.com/b", "title": "Beta", "status": "paused"},
            {"task_uuid": "q3", "url": "https://example.com/c", "title": "Gamma", "status": "failed"},
            {"task_uuid": "q4", "url": "https://example.com/d", "title": "Alpha Match", "status": "pending"},
        ]
    )

    pending_only = db.fetch_queue_entries_page_from_db(
        view="queued",
        now_ts=0,
        queue_state_filter="pending",
        query="",
        page=1,
        page_size=10,
    )
    query_alpha = db.fetch_queue_entries_page_from_db(
        view="queued",
        now_ts=0,
        queue_state_filter="all",
        query="alpha",
        page=1,
        page_size=10,
    )

    assert [entry["task_uuid"] for entry in pending_only["entries"]] == ["q1", "q4"]
    assert [entry["task_uuid"] for entry in query_alpha["entries"]] == ["q1", "q4"]


def test_fetch_queue_entries_page_from_db_supports_media_filter_and_counts(isolated_db):
    db.save_queue_items(
        [
            {"task_uuid": "m1", "url": "https://example.com/video", "title": "Video", "status": "pending", "mode": "video"},
            {"task_uuid": "m2", "url": "https://example.com/audio", "title": "Audio", "status": "paused", "mode": "audio"},
            {"task_uuid": "m3", "url": "https://example.com/video-2", "title": "Video 2", "status": "queued", "mode": "video"},
        ]
    )

    audio_only = db.fetch_queue_entries_page_from_db(
        view="queued",
        now_ts=0,
        queue_state_filter="all",
        media_filter="audio",
        query="",
        page=1,
        page_size=10,
    )

    assert [entry["task_uuid"] for entry in audio_only["entries"]] == ["m2"]
    assert audio_only["media_counts"] == {"all": 3, "video": 2, "audio": 1}


def test_fetch_history_escapes_like_wildcards(isolated_db):
    db.insert_history(
        {
            "timestamp": "2026-01-01T00:00:00",
            "title": "100% Complete",
            "url": "https://example.com/percent",
            "mode": "video",
            "format": "mp4",
            "quality": "1080p",
            "size_text": "--",
            "size_bytes": 1,
            "status": "success",
            "message": "",
            "attempts": 1,
            "error": "",
            "file_path": "",
            "thumbnail": "",
            "channel": "",
        }
    )
    db.insert_history(
        {
            "timestamp": "2026-01-02T00:00:00",
            "title": "100 Percent Complete",
            "url": "https://example.com/plain",
            "mode": "video",
            "format": "mp4",
            "quality": "1080p",
            "size_text": "--",
            "size_bytes": 1,
            "status": "success",
            "message": "",
            "attempts": 1,
            "error": "",
            "file_path": "",
            "thumbnail": "",
            "channel": "",
        }
    )

    results = db.fetch_history(search="100%")

    assert [item["title"] for item in results] == ["100% Complete"]


def test_count_history_statuses_counts_multiple_status_values(isolated_db):
    db.insert_history(
        {
            "timestamp": datetime.now().isoformat(),
            "title": "S1",
            "url": "https://example.com/s1",
            "mode": "video",
            "format": "mp4",
            "quality": "1080p",
            "size_text": "--",
            "size_bytes": 1,
            "status": "success",
            "message": "",
            "attempts": 1,
            "error": "",
            "file_path": "",
            "thumbnail": "",
            "channel": "",
        }
    )
    db.insert_history(
        {
            "timestamp": datetime.now().isoformat(),
            "title": "C1",
            "url": "https://example.com/c1",
            "mode": "audio",
            "format": "mp3",
            "quality": "320kbps",
            "size_text": "--",
            "size_bytes": 2,
            "status": "completed",
            "message": "",
            "attempts": 1,
            "error": "",
            "file_path": "",
            "thumbnail": "",
            "channel": "",
        }
    )
    db.insert_history(
        {
            "timestamp": datetime.now().isoformat(),
            "title": "F1",
            "url": "https://example.com/f1",
            "mode": "video",
            "format": "mkv",
            "quality": "720p",
            "size_text": "--",
            "size_bytes": 3,
            "status": "failed",
            "message": "",
            "attempts": 1,
            "error": "",
            "file_path": "",
            "thumbnail": "",
            "channel": "",
        }
    )

    assert db.count_history_statuses(["success", "completed"]) == 2
    assert db.count_history_statuses(["failed"]) == 1
    assert db.count_history_statuses([]) == 0


def test_fetch_completed_history_page_from_db_filters_counts_and_pagination(isolated_db):
    now_iso = datetime.now().isoformat()
    old_iso = (datetime.now() - timedelta(days=10)).isoformat()
    rows = [
        {
            "timestamp": now_iso,
            "title": "Alpha Video",
            "url": "https://example.com/a",
            "mode": "video",
            "format": "mp4",
            "quality": "1080p",
            "size_text": "100 MB",
            "size_bytes": 100,
            "status": "success",
            "message": "",
            "attempts": 1,
            "error": "",
            "file_path": "",
            "thumbnail": "",
            "channel": "",
        },
        {
            "timestamp": now_iso,
            "title": "Alpha Audio",
            "url": "https://example.com/b",
            "mode": "audio",
            "format": "mp3",
            "quality": "320kbps",
            "size_text": "10 MB",
            "size_bytes": 10,
            "status": "completed",
            "message": "",
            "attempts": 1,
            "error": "",
            "file_path": "",
            "thumbnail": "",
            "channel": "",
        },
        {
            "timestamp": old_iso,
            "title": "Old Video",
            "url": "https://example.com/c",
            "mode": "video",
            "format": "mkv",
            "quality": "720p",
            "size_text": "50 MB",
            "size_bytes": 50,
            "status": "success",
            "message": "",
            "attempts": 1,
            "error": "",
            "file_path": "",
            "thumbnail": "",
            "channel": "",
        },
        {
            "timestamp": now_iso,
            "title": "Failed Item",
            "url": "https://example.com/d",
            "mode": "video",
            "format": "mp4",
            "quality": "720p",
            "size_text": "2 MB",
            "size_bytes": 2,
            "status": "failed",
            "message": "",
            "attempts": 1,
            "error": "",
            "file_path": "",
            "thumbnail": "",
            "channel": "",
        },
    ]
    for row in rows:
        db.insert_history(row)

    payload = db.fetch_completed_history_page_from_db(
        media_filter="all",
        format_filter="all",
        date_filter="7d",
        query="alpha",
        sort="size_bytes DESC",
        page=1,
        page_size=1,
    )

    assert payload["total_matches"] == 2
    assert payload["total_pages"] == 2
    assert payload["page"] == 1
    assert payload["media_counts"] == {"all": 2, "video": 1, "audio": 1}
    assert len(payload["entries"]) == 1
    assert payload["entries"][0]["title"] == "Alpha Video"

    page_two = db.fetch_completed_history_page_from_db(
        media_filter="all",
        format_filter="all",
        date_filter="7d",
        query="alpha",
        sort="size_bytes DESC",
        page=2,
        page_size=1,
    )
    assert page_two["page"] == 2
    assert len(page_two["entries"]) == 1
    assert page_two["entries"][0]["title"] == "Alpha Audio"


def test_fetch_completed_history_page_from_db_applies_media_and_format_filters(isolated_db):
    db.insert_history(
        {
            "timestamp": datetime.now().isoformat(),
            "title": "V1",
            "url": "https://example.com/v1",
            "mode": "video",
            "format": "MP4",
            "quality": "1080p",
            "size_text": "--",
            "size_bytes": 11,
            "status": "success",
            "message": "",
            "attempts": 1,
            "error": "",
            "file_path": "",
            "thumbnail": "",
            "channel": "",
        }
    )
    db.insert_history(
        {
            "timestamp": datetime.now().isoformat(),
            "title": "A1",
            "url": "https://example.com/a1",
            "mode": "audio",
            "format": "MP3",
            "quality": "320kbps",
            "size_text": "--",
            "size_bytes": 7,
            "status": "success",
            "message": "",
            "attempts": 1,
            "error": "",
            "file_path": "",
            "thumbnail": "",
            "channel": "",
        }
    )

    payload = db.fetch_completed_history_page_from_db(
        media_filter="audio",
        format_filter="MP3",
        date_filter="all",
        query="",
        sort="timestamp DESC",
        page=1,
        page_size=20,
    )

    assert payload["media_counts"] == {"all": 1, "video": 0, "audio": 1}
    assert payload["total_matches"] == 1
    assert [item["title"] for item in payload["entries"]] == ["A1"]


def test_migrate_from_json_renames_source_only_after_successful_commit(isolated_db, tmp_path):
    source = tmp_path / "legacy-stats.json"
    source.write_text(
        json.dumps(
            {
                "download_history": [
                    {
                        "timestamp": "2026-01-03T00:00:00",
                        "title": "Legacy Item",
                        "url": "https://example.com/legacy",
                        "mode": "video",
                        "format": "mp4",
                        "quality": "720p",
                        "size": "12 MB",
                        "status": "success",
                        "message": "done",
                        "attempts": 2,
                        "error": "",
                        "file_path": "D:/legacy.mp4",
                        "thumbnail": "thumb.jpg",
                    }
                ],
                "total_videos": 7,
                "total_audios": 3,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    migrated = db.migrate_from_json(str(source))

    assert migrated == 1
    assert source.exists() is False
    assert (tmp_path / "legacy-stats.json.migrated").exists() is True
    assert db.get_stat("total_videos") == "7"
    assert db.get_stat("total_audios") == "3"

    history = db.fetch_history()
    assert len(history) == 1
    assert history[0]["title"] == "Legacy Item"
    assert history[0]["url"] == "https://example.com/legacy"


def test_save_queue_items_sanitizes_script_and_browser_fields(monkeypatch, isolated_db, tmp_path):
    safe_script = tmp_path / "trusted.py"
    safe_script.write_text("print('ok')", encoding="utf-8")
    resolved_safe = str(safe_script.resolve())

    monkeypatch.setattr(db.PostDownloadManager, "_resolve_script_path", lambda path: str(tmp_path / str(path)))
    monkeypatch.setattr(
        db.PostDownloadManager,
        "_is_safe_script_path",
        lambda path: os.path.abspath(path) == os.path.abspath(resolved_safe),
    )

    db.save_queue_items(
        [
            {
                "task_uuid": "task-safe",
                "url": "https://example.com/safe",
                "post_download_script": "trusted.py",
                "cookies_from_browser": "edge",
            },
            {
                "task_uuid": "task-unsafe",
                "url": "https://example.com/unsafe",
                "post_download_script": r"C:\temp\evil.bat",
                "cookies_from_browser": "evilbrowser",
            },
        ]
    )

    loaded = db.load_queue_items()

    assert loaded[0]["post_download_script"] == resolved_safe
    assert loaded[0]["cookies_from_browser"] == "edge"
    assert loaded[1]["post_download_script"] == ""
    assert loaded[1]["cookies_from_browser"] == "none"
