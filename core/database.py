
"""
core/database.py — SQLite Database Engine for SnapDownloader
Replaces slow JSON files with a high-performance embedded database.
Supports: download history, queue persistence, stats, and duplicate detection.
"""
import os
import sqlite3
import json
import hashlib
import threading
import logging
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

import sys
from .cookie_importer import SUPPORTED_BROWSERS
from .post_actions import PostDownloadManager
from .task_types import TaskStatus
from .utils import get_app_data_dir

_BASE_DIR = get_app_data_dir()
os.makedirs(_BASE_DIR, exist_ok=True)
DB_PATH = os.path.join(_BASE_DIR, "snapdownloader.db")
_local = threading.local()
_write_lock = threading.RLock()  # C-03: Global write lock for thread-safe DB writes
logger = logging.getLogger("SnapDownloader.DB")
_STATUS_PENDING = TaskStatus.PENDING.value
_STATUS_SUCCESS = TaskStatus.SUCCESS.value
_last_queue_save_signature: str | None = None
_QUEUE_STAGE_CHUNK_SIZE = 250
_ALLOWED_COOKIES_FROM_BROWSER = {"none", *[str(name).strip().lower() for name in SUPPORTED_BROWSERS], "chromium"}
_QUEUE_ITEM_COLUMNS = (
    "task_uuid",
    "queue_position",
    "url", "title", "out_dir", "mode", "quality", "format", "subtitle",
    "start_time", "end_time", "retries", "auto_retry_delay_seconds", "queue_retry_limit",
    "retry_count", "next_retry_at", "scheduled_at", "duration_seconds", "is_live", "was_live", "live_status",
    "bandwidth_limit_kbps", "use_aria2", "status", "thumbnail",
    "category", "schedule_repeat", "channel", "source", "playlist_url", "playlist_index",
    "playlist_title", "entry_id", "progress", "speed", "eta",
    "error_msg", "file_path", "last_output_path", "resume_json", "trims_json",
    "size", "size_bytes", "estimated_size_bytes", "size_is_estimate",
    "video_id", "file_hash", "post_action", "post_download_script",
    "embed_subs", "split_chapters", "whisper_fallback", "sponsorblock_enabled",
    "verify_checksum", "virus_scan_after_download", "use_ytdlp_api", "rename_template", "use_native_engine",
    "cookies_from_browser", "merge_opts_json",
)


def _escape_like_pattern(value: str) -> str:
    text = str(value or "")
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _contains_like_pattern(value: str) -> str:
    return f"%{_escape_like_pattern(value)}%"


def _normalize_cookies_from_browser(value: str) -> str:
    token = str(value or "none").strip().lower() or "none"
    if token in _ALLOWED_COOKIES_FROM_BROWSER:
        return token
    logger.warning(f"[DB] Ignored unsupported cookies_from_browser value: {token}")
    return "none"


def _normalize_post_download_script(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    resolved = PostDownloadManager._resolve_script_path(raw)
    if not PostDownloadManager._is_safe_script_path(resolved):
        logger.warning(f"[DB] Ignored unsafe post_download_script value: {raw}")
        return ""
    return resolved


@contextmanager
def _get_conn():
    """Thread-safe connection context manager."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=15.0)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
        _local.conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield _local.conn
    except sqlite3.Error:
        try:
            _local.conn.rollback()
        except Exception:
            pass
        close_thread_connection()
        raise
    except Exception:
        _local.conn.rollback()
        raise

def close_thread_connection():
    conn = getattr(_local, "conn", None)
    if conn is None:
        return
    try:
        conn.close()
    except Exception as exc:
        logger.debug(f"Failed to close thread-local DB connection: {exc}")
    finally:
        _local.conn = None


def init_db():
    """Initialize all database tables. Safe to call multiple times."""
    with _write_lock:
        with _get_conn() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.executescript("""
            CREATE TABLE IF NOT EXISTS download_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                title       TEXT    NOT NULL DEFAULT '',
                url         TEXT    NOT NULL DEFAULT '',
                mode        TEXT    NOT NULL DEFAULT 'video',
                format      TEXT    NOT NULL DEFAULT 'mp4',
                quality     TEXT    NOT NULL DEFAULT '',
                size_text   TEXT    NOT NULL DEFAULT '--',
                size_bytes  INTEGER NOT NULL DEFAULT 0,
                status      TEXT    NOT NULL DEFAULT 'success',
                message     TEXT    NOT NULL DEFAULT '',
                attempts    INTEGER NOT NULL DEFAULT 1,
                error       TEXT    NOT NULL DEFAULT '',
                file_path   TEXT    NOT NULL DEFAULT '',
                thumbnail   TEXT    NOT NULL DEFAULT '',
                channel     TEXT    NOT NULL DEFAULT '',
                created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_history_status    ON download_history(status);
            CREATE INDEX IF NOT EXISTS idx_history_url       ON download_history(url);
            CREATE INDEX IF NOT EXISTS idx_history_timestamp ON download_history(timestamp DESC);

            CREATE TABLE IF NOT EXISTS queue_items (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                task_uuid               TEXT    NOT NULL DEFAULT '',
                queue_position          INTEGER NOT NULL DEFAULT 0,
                url                     TEXT    NOT NULL,
                title                   TEXT    NOT NULL DEFAULT '',
                out_dir                 TEXT    NOT NULL DEFAULT '',
                mode                    TEXT    NOT NULL DEFAULT 'video',
                quality                 TEXT    NOT NULL DEFAULT '1080p',
                format                  TEXT    NOT NULL DEFAULT 'mp4',
                subtitle                TEXT    NOT NULL DEFAULT 'None',
                start_time              TEXT    NOT NULL DEFAULT '',
                end_time                TEXT    NOT NULL DEFAULT '',
                retries                 INTEGER NOT NULL DEFAULT 3,
                auto_retry_delay_seconds INTEGER NOT NULL DEFAULT 4,
                queue_retry_limit       INTEGER NOT NULL DEFAULT 2,
                retry_count             INTEGER NOT NULL DEFAULT 0,
                next_retry_at           REAL    NOT NULL DEFAULT 0,
                scheduled_at            REAL    NOT NULL DEFAULT 0,
                duration_seconds        INTEGER NOT NULL DEFAULT 0,
                is_live                 INTEGER NOT NULL DEFAULT 0,
                was_live                INTEGER NOT NULL DEFAULT 0,
                live_status             TEXT    NOT NULL DEFAULT '',
                bandwidth_limit_kbps    INTEGER NOT NULL DEFAULT 0,
                use_aria2               INTEGER NOT NULL DEFAULT 1,
                status                  TEXT    NOT NULL DEFAULT 'pending',
                thumbnail               TEXT    NOT NULL DEFAULT '',
                category                TEXT    NOT NULL DEFAULT '',
                schedule_repeat         TEXT    NOT NULL DEFAULT 'none',
                channel                 TEXT    NOT NULL DEFAULT '',
                source                  TEXT    NOT NULL DEFAULT '',
                playlist_url            TEXT    NOT NULL DEFAULT '',
                playlist_index          INTEGER NOT NULL DEFAULT 0,
                playlist_title          TEXT    NOT NULL DEFAULT '',
                entry_id                TEXT    NOT NULL DEFAULT '',
                progress                REAL    NOT NULL DEFAULT 0,
                speed                   TEXT    NOT NULL DEFAULT '--',
                eta                     TEXT    NOT NULL DEFAULT '--:--',
                error_msg               TEXT    NOT NULL DEFAULT '',
                file_path               TEXT    NOT NULL DEFAULT '',
                last_output_path        TEXT    NOT NULL DEFAULT '',
                resume_json             TEXT    NOT NULL DEFAULT '',
                trims_json              TEXT    NOT NULL DEFAULT '',
                size                    TEXT    NOT NULL DEFAULT '--',
                size_bytes              INTEGER NOT NULL DEFAULT 0,
                estimated_size_bytes    INTEGER NOT NULL DEFAULT 0,
                size_is_estimate        INTEGER NOT NULL DEFAULT 1,
                embed_subs              INTEGER NOT NULL DEFAULT 1,
                split_chapters          INTEGER NOT NULL DEFAULT 0,
                whisper_fallback        INTEGER NOT NULL DEFAULT 0,
                sponsorblock_enabled    INTEGER NOT NULL DEFAULT 0,
                verify_checksum         INTEGER NOT NULL DEFAULT 0,
                virus_scan_after_download INTEGER NOT NULL DEFAULT 0,
                use_ytdlp_api           INTEGER NOT NULL DEFAULT 0,
                rename_template         TEXT    NOT NULL DEFAULT 'Default',
                use_native_engine       INTEGER NOT NULL DEFAULT 0,
                cookies_from_browser    TEXT    NOT NULL DEFAULT 'none',
                merge_opts_json         TEXT    NOT NULL DEFAULT '',
                created_at              TEXT    NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_queue_status        ON queue_items(status);
            CREATE INDEX IF NOT EXISTS idx_queue_scheduled_at  ON queue_items(scheduled_at);
            CREATE INDEX IF NOT EXISTS idx_queue_retry_next    ON queue_items(next_retry_at);

            CREATE TABLE IF NOT EXISTS stats (
                key     TEXT PRIMARY KEY,
                value   TEXT NOT NULL DEFAULT '0'
            );

            INSERT OR IGNORE INTO stats(key, value) VALUES ('total_videos', '0');
            INSERT OR IGNORE INTO stats(key, value) VALUES ('total_audios', '0');
            INSERT OR IGNORE INTO stats(key, value) VALUES ('total_bytes',  '0');
            INSERT OR IGNORE INTO stats(key, value) VALUES ('peak_speed_kbps', '0');
            INSERT OR IGNORE INTO stats(key, value) VALUES ('app_version', '3.0.0');

            CREATE TABLE IF NOT EXISTS playlist_cache (
                playlist_url     TEXT NOT NULL,
                entry_id         TEXT NOT NULL,
                title            TEXT NOT NULL DEFAULT '',
                duration_seconds INTEGER NOT NULL DEFAULT 0,
                thumbnail        TEXT NOT NULL DEFAULT '',
                playlist_index   INTEGER NOT NULL DEFAULT 0,
                etag             TEXT NOT NULL DEFAULT '',
                last_modified    TEXT NOT NULL DEFAULT '',
                snapshot_id      TEXT NOT NULL DEFAULT '',
                sync_status      TEXT NOT NULL DEFAULT 'idle',
                channel_id       TEXT NOT NULL DEFAULT '',
                last_sync_at     INTEGER NOT NULL DEFAULT 0,
                sync_cursor      TEXT NOT NULL DEFAULT '',
                removed_at       INTEGER NOT NULL DEFAULT 0,
                first_seen_at    INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                last_seen_at     INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                PRIMARY KEY (playlist_url, entry_id)
            );
            CREATE INDEX IF NOT EXISTS idx_playlist_cache_url
                ON playlist_cache (playlist_url, last_seen_at DESC);
                """)
                _ensure_queue_items_columns(conn)
                _ensure_advanced_columns(conn)
                _ensure_playlist_sync_schema(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise

def _ensure_queue_items_columns(conn: sqlite3.Connection):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(queue_items)").fetchall()}
    additions = {
        "task_uuid": "ALTER TABLE queue_items ADD COLUMN task_uuid TEXT NOT NULL DEFAULT ''",
        "queue_position": "ALTER TABLE queue_items ADD COLUMN queue_position INTEGER NOT NULL DEFAULT 0",
        "last_output_path": "ALTER TABLE queue_items ADD COLUMN last_output_path TEXT NOT NULL DEFAULT ''",
        "resume_json": "ALTER TABLE queue_items ADD COLUMN resume_json TEXT NOT NULL DEFAULT ''",
        "auto_retry_delay_seconds": "ALTER TABLE queue_items ADD COLUMN auto_retry_delay_seconds INTEGER NOT NULL DEFAULT 4",
        "queue_retry_limit": "ALTER TABLE queue_items ADD COLUMN queue_retry_limit INTEGER NOT NULL DEFAULT 2",
        "duration_seconds": "ALTER TABLE queue_items ADD COLUMN duration_seconds INTEGER NOT NULL DEFAULT 0",
        "is_live": "ALTER TABLE queue_items ADD COLUMN is_live INTEGER NOT NULL DEFAULT 0",
        "was_live": "ALTER TABLE queue_items ADD COLUMN was_live INTEGER NOT NULL DEFAULT 0",
        "live_status": "ALTER TABLE queue_items ADD COLUMN live_status TEXT NOT NULL DEFAULT ''",
        "category": "ALTER TABLE queue_items ADD COLUMN category TEXT NOT NULL DEFAULT ''",
        "schedule_repeat": "ALTER TABLE queue_items ADD COLUMN schedule_repeat TEXT NOT NULL DEFAULT 'none'",
        "channel": "ALTER TABLE queue_items ADD COLUMN channel TEXT NOT NULL DEFAULT ''",
        "source": "ALTER TABLE queue_items ADD COLUMN source TEXT NOT NULL DEFAULT ''",
        "playlist_url": "ALTER TABLE queue_items ADD COLUMN playlist_url TEXT NOT NULL DEFAULT ''",
        "playlist_index": "ALTER TABLE queue_items ADD COLUMN playlist_index INTEGER NOT NULL DEFAULT 0",
        "playlist_title": "ALTER TABLE queue_items ADD COLUMN playlist_title TEXT NOT NULL DEFAULT ''",
        "entry_id": "ALTER TABLE queue_items ADD COLUMN entry_id TEXT NOT NULL DEFAULT ''",
        "file_path": "ALTER TABLE queue_items ADD COLUMN file_path TEXT NOT NULL DEFAULT ''",
        "trims_json": "ALTER TABLE queue_items ADD COLUMN trims_json TEXT NOT NULL DEFAULT ''",
        "size": "ALTER TABLE queue_items ADD COLUMN size TEXT NOT NULL DEFAULT '--'",
        "size_bytes": "ALTER TABLE queue_items ADD COLUMN size_bytes INTEGER NOT NULL DEFAULT 0",
        "estimated_size_bytes": "ALTER TABLE queue_items ADD COLUMN estimated_size_bytes INTEGER NOT NULL DEFAULT 0",
        "size_is_estimate": "ALTER TABLE queue_items ADD COLUMN size_is_estimate INTEGER NOT NULL DEFAULT 1",
        "embed_subs": "ALTER TABLE queue_items ADD COLUMN embed_subs INTEGER NOT NULL DEFAULT 1",
        "split_chapters": "ALTER TABLE queue_items ADD COLUMN split_chapters INTEGER NOT NULL DEFAULT 0",
        "whisper_fallback": "ALTER TABLE queue_items ADD COLUMN whisper_fallback INTEGER NOT NULL DEFAULT 0",
        "sponsorblock_enabled": "ALTER TABLE queue_items ADD COLUMN sponsorblock_enabled INTEGER NOT NULL DEFAULT 0",
        "verify_checksum": "ALTER TABLE queue_items ADD COLUMN verify_checksum INTEGER NOT NULL DEFAULT 0",
        "virus_scan_after_download": "ALTER TABLE queue_items ADD COLUMN virus_scan_after_download INTEGER NOT NULL DEFAULT 0",
        "use_ytdlp_api": "ALTER TABLE queue_items ADD COLUMN use_ytdlp_api INTEGER NOT NULL DEFAULT 0",
        "rename_template": "ALTER TABLE queue_items ADD COLUMN rename_template TEXT NOT NULL DEFAULT 'Default'",
        "use_native_engine": "ALTER TABLE queue_items ADD COLUMN use_native_engine INTEGER NOT NULL DEFAULT 0",
        "cookies_from_browser": "ALTER TABLE queue_items ADD COLUMN cookies_from_browser TEXT NOT NULL DEFAULT 'none'",
        "merge_opts_json": "ALTER TABLE queue_items ADD COLUMN merge_opts_json TEXT NOT NULL DEFAULT ''",
    }
    for name, sql in additions.items():
        if name not in cols:
            conn.execute(sql)
            cols.add(name)
    _ensure_queue_task_uuids(conn)
    _resequence_queue_positions(conn)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_task_uuid_unique ON queue_items(task_uuid)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_position_unique ON queue_items(queue_position)")


def _resequence_queue_positions(conn: sqlite3.Connection):
    rows = conn.execute("SELECT id, queue_position FROM queue_items ORDER BY id ASC").fetchall()
    if not rows:
        return
    positions = [int(row[1] or 0) for row in rows]
    expected = list(range(len(rows)))
    if positions == expected:
        return
    for queue_position, row in enumerate(rows):
        conn.execute("UPDATE queue_items SET queue_position = ? WHERE id = ?", (queue_position, int(row[0])))


def _ensure_queue_task_uuids(conn: sqlite3.Connection):
    rows = conn.execute("SELECT id, task_uuid FROM queue_items ORDER BY id ASC").fetchall()
    seen = set()
    for row in rows:
        task_uuid = str(row[1] or "").strip()
        if task_uuid and task_uuid not in seen:
            seen.add(task_uuid)
            continue
        task_uuid = str(uuid4())
        seen.add(task_uuid)
        conn.execute("UPDATE queue_items SET task_uuid = ? WHERE id = ?", (task_uuid, int(row[0])))


def _ensure_advanced_columns(conn: sqlite3.Connection):
    """
    تحديث هيكل قاعدة البيانات تلقائياً لدعم الميزات المتقدمة
    بدون التأثير على التحميلات السابقة.
    """
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(queue_items)")
    columns = {row[1] for row in cursor.fetchall()}
    additions = {
        "video_id": "ALTER TABLE queue_items ADD COLUMN video_id TEXT",
        "file_hash": "ALTER TABLE queue_items ADD COLUMN file_hash TEXT",
        "post_action": "ALTER TABLE queue_items ADD COLUMN post_action TEXT DEFAULT 'none'",
        "post_download_script": "ALTER TABLE queue_items ADD COLUMN post_download_script TEXT DEFAULT ''",
    }
    for name, sql in additions.items():
        if name in columns:
            continue
        cursor.execute(sql)
        logger.info(f"[DB Migration] Added '{name}' column.")
        columns.add(name)


def _ensure_playlist_sync_schema(conn: sqlite3.Connection):
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(playlist_cache)")
    playlist_cache_cols = {row[1] for row in cursor.fetchall()}
    cache_additions = {
        "etag": "ALTER TABLE playlist_cache ADD COLUMN etag TEXT NOT NULL DEFAULT ''",
        "last_modified": "ALTER TABLE playlist_cache ADD COLUMN last_modified TEXT NOT NULL DEFAULT ''",
        "snapshot_id": "ALTER TABLE playlist_cache ADD COLUMN snapshot_id TEXT NOT NULL DEFAULT ''",
        "sync_status": "ALTER TABLE playlist_cache ADD COLUMN sync_status TEXT NOT NULL DEFAULT 'idle'",
        "channel_id": "ALTER TABLE playlist_cache ADD COLUMN channel_id TEXT NOT NULL DEFAULT ''",
        "last_sync_at": "ALTER TABLE playlist_cache ADD COLUMN last_sync_at INTEGER NOT NULL DEFAULT 0",
        "sync_cursor": "ALTER TABLE playlist_cache ADD COLUMN sync_cursor TEXT NOT NULL DEFAULT ''",
        "removed_at": "ALTER TABLE playlist_cache ADD COLUMN removed_at INTEGER NOT NULL DEFAULT 0",
    }
    for name, sql in cache_additions.items():
        if name in playlist_cache_cols:
            continue
        cursor.execute(sql)

    cursor.execute("PRAGMA table_info(playlists)")
    playlists_cols = {row[1] for row in cursor.fetchall()}
    playlists_additions = {
        "last_success_at": "ALTER TABLE playlists ADD COLUMN last_success_at INTEGER NOT NULL DEFAULT 0",
        "consecutive_failures": "ALTER TABLE playlists ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0",
        "next_sync_after": "ALTER TABLE playlists ADD COLUMN next_sync_after INTEGER NOT NULL DEFAULT 0",
    }
    if playlists_cols:
        for name, sql in playlists_additions.items():
            if name in playlists_cols:
                continue
            cursor.execute(sql)

    cursor.execute("PRAGMA table_info(playlist_sync_state)")
    sync_state_cols = {row[1] for row in cursor.fetchall()}
    sync_state_additions = {
        "last_success_at": "ALTER TABLE playlist_sync_state ADD COLUMN last_success_at INTEGER NOT NULL DEFAULT 0",
        "consecutive_failures": "ALTER TABLE playlist_sync_state ADD COLUMN consecutive_failures INTEGER NOT NULL DEFAULT 0",
        "next_sync_after": "ALTER TABLE playlist_sync_state ADD COLUMN next_sync_after INTEGER NOT NULL DEFAULT 0",
    }
    if sync_state_cols:
        for name, sql in sync_state_additions.items():
            if name in sync_state_cols:
                continue
            cursor.execute(sql)

    cursor.executescript(
        """
        CREATE TABLE IF NOT EXISTS playlists (
            playlist_url   TEXT PRIMARY KEY,
            title          TEXT NOT NULL DEFAULT '',
            channel_id     TEXT NOT NULL DEFAULT '',
            etag           TEXT NOT NULL DEFAULT '',
            last_modified  TEXT NOT NULL DEFAULT '',
            snapshot_id    TEXT NOT NULL DEFAULT '',
            sync_status    TEXT NOT NULL DEFAULT 'idle',
            sync_cursor    TEXT NOT NULL DEFAULT '',
            last_sync_at   INTEGER NOT NULL DEFAULT 0,
            last_success_at INTEGER NOT NULL DEFAULT 0,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            next_sync_after INTEGER NOT NULL DEFAULT 0,
            metadata_json  TEXT NOT NULL DEFAULT '',
            created_at     INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            updated_at     INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_playlists_sync_status ON playlists(sync_status, last_sync_at DESC);

        CREATE TABLE IF NOT EXISTS playlist_entries (
            playlist_url     TEXT NOT NULL,
            entry_id         TEXT NOT NULL,
            title            TEXT NOT NULL DEFAULT '',
            duration_seconds INTEGER NOT NULL DEFAULT 0,
            thumbnail        TEXT NOT NULL DEFAULT '',
            playlist_index   INTEGER NOT NULL DEFAULT 0,
            snapshot_id      TEXT NOT NULL DEFAULT '',
            first_seen_at    INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            last_seen_at     INTEGER NOT NULL DEFAULT (strftime('%s','now')),
            removed_at       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (playlist_url, entry_id)
        );
        CREATE INDEX IF NOT EXISTS idx_playlist_entries_url_last_seen
            ON playlist_entries(playlist_url, last_seen_at DESC);
        CREATE INDEX IF NOT EXISTS idx_playlist_entries_url_removed
            ON playlist_entries(playlist_url, removed_at);

        CREATE TABLE IF NOT EXISTS playlist_sync_state (
            playlist_url    TEXT PRIMARY KEY,
            last_sync_at    INTEGER NOT NULL DEFAULT 0,
            last_success_at INTEGER NOT NULL DEFAULT 0,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            next_sync_after INTEGER NOT NULL DEFAULT 0,
            sync_status     TEXT NOT NULL DEFAULT 'idle',
            sync_cursor     TEXT NOT NULL DEFAULT '',
            etag            TEXT NOT NULL DEFAULT '',
            last_modified   TEXT NOT NULL DEFAULT '',
            snapshot_id     TEXT NOT NULL DEFAULT '',
            last_error      TEXT NOT NULL DEFAULT '',
            updated_at      INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_playlist_sync_state_status
            ON playlist_sync_state(sync_status, last_sync_at DESC);
        """
    )


# ─────────────────────────── Download History ───────────────────────────────

def insert_history(entry: dict) -> int:
    """Insert a completed download record. Returns new row id."""
    cols = [
        "timestamp", "title", "url", "mode", "format", "quality",
        "size_text", "size_bytes", "status", "message", "attempts",
        "error", "file_path", "thumbnail", "channel",
    ]
    values = [entry.get(c) or entry.get(
        {
            "size_text": "size",
            "size_bytes": "size_bytes",
            "message": "message",
            "error": "error",
        }.get(c, c), ""
    ) for c in cols]

    placeholders = ", ".join("?" * len(cols))
    sql = f"INSERT INTO download_history ({', '.join(cols)}) VALUES ({placeholders})"
    with _write_lock:
        with _get_conn() as conn:
            cur = conn.execute(sql, values)
            conn.commit()
            return cur.lastrowid


def fetch_history(status: str = None, limit: int = 250, offset: int = 0,
                  search: str = None, sort: str = "timestamp DESC") -> list:
    """Fetch download history with optional filtering."""
    where_clauses = []
    params = []

    if status and status != "all":
        where_clauses.append("status = ?")
        params.append(status)
    if search:
        pattern = _contains_like_pattern(search)
        where_clauses.append("(title LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\')")
        params.extend([pattern, pattern])

    where = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    valid_sorts = {
        "timestamp DESC", "timestamp ASC", "title ASC", "title DESC",
        "size_bytes DESC", "size_bytes ASC",
    }
    safe_sort = sort if sort in valid_sorts else "timestamp DESC"
    sql = f"SELECT * FROM download_history {where} ORDER BY {safe_sort} LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with _get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def fetch_completed_history_page_from_db(
    *,
    media_filter: str = "all",
    format_filter: str = "all",
    date_filter: str = "all",
    query: str = "",
    sort: str = "timestamp DESC",
    page: int = 1,
    page_size: int = 200,
) -> dict:
    """Fetch paginated completed history with filters and media counts."""
    media_key = str(media_filter or "all").strip().lower()
    format_key = str(format_filter or "all").strip()
    date_key = str(date_filter or "all").strip().lower()
    query_text = str(query or "").strip().lower()
    try:
        page_num = max(1, int(page or 1))
    except (TypeError, ValueError, OverflowError):
        page_num = 1
    try:
        per_page = max(1, int(page_size or 200))
    except (TypeError, ValueError, OverflowError):
        per_page = 200

    # "Completed" view in UI should include historical success/completed records only.
    base_where_clauses = ["LOWER(COALESCE(status, '')) IN (?, ?)"]
    base_params: list = [_STATUS_SUCCESS, "completed"]

    if query_text:
        query_pattern = _contains_like_pattern(query_text)
        base_where_clauses.append(
            "(LOWER(COALESCE(title, '')) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(url, '')) LIKE ? ESCAPE '\\')"
        )
        base_params.extend([query_pattern, query_pattern])

    if format_key and str(format_key).lower() != "all":
        base_where_clauses.append("UPPER(COALESCE(format, '')) = ?")
        base_params.append(str(format_key).upper())

    if date_key in {"24h", "7d", "30d"}:
        if date_key == "24h":
            cutoff = datetime.now().astimezone() - timedelta(days=1)
        elif date_key == "7d":
            cutoff = datetime.now().astimezone() - timedelta(days=7)
        else:
            cutoff = datetime.now().astimezone() - timedelta(days=30)
        base_where_clauses.append("COALESCE(timestamp, '') >= ?")
        base_params.append(cutoff.isoformat())

    summary_where_sql = f"WHERE {' AND '.join(base_where_clauses)}" if base_where_clauses else ""
    summary_params = list(base_params)

    where_clauses = list(base_where_clauses)
    params = list(base_params)
    if media_key in {"video", "audio"}:
        where_clauses.append("LOWER(COALESCE(mode, 'video')) = ?")
        params.append(media_key)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    valid_sorts = {
        "timestamp DESC",
        "timestamp ASC",
        "title ASC",
        "title DESC",
        "size_bytes DESC",
        "size_bytes ASC",
    }
    safe_sort = sort if sort in valid_sorts else "timestamp DESC"

    with _get_conn() as conn:
        media_rows = conn.execute(
            f"""
            SELECT
                CASE
                    WHEN LOWER(COALESCE(mode, 'video')) = 'audio' THEN 'audio'
                    ELSE 'video'
                END AS media_mode,
                COUNT(*) AS media_count
            FROM download_history
            {summary_where_sql}
            GROUP BY media_mode
            """,
            summary_params,
        ).fetchall()
        media_counts = {"all": 0, "video": 0, "audio": 0}
        for row in media_rows:
            payload = dict(row)
            mode_key = str(payload.get("media_mode", "video") or "video").strip().lower()
            count_value = int(payload.get("media_count", 0) or 0)
            media_counts["all"] += count_value
            if mode_key == "audio":
                media_counts["audio"] += count_value
            else:
                media_counts["video"] += count_value

        total_matches = int(
            conn.execute(
                f"SELECT COUNT(*) FROM download_history {where_sql}",
                params,
            ).fetchone()[0]
            or 0
        )
        total_pages = max(1, (total_matches + per_page - 1) // per_page)
        page_num = min(page_num, total_pages)
        offset = (page_num - 1) * per_page
        rows = conn.execute(
            f"""
            SELECT *
            FROM download_history
            {where_sql}
            ORDER BY {safe_sort}
            LIMIT ? OFFSET ?
            """,
            [*params, per_page, offset],
        ).fetchall()

    return {
        "entries": [dict(row) for row in rows],
        "total_matches": total_matches,
        "total_pages": total_pages,
        "page": page_num,
        "page_size": per_page,
        "media_counts": media_counts,
    }


def count_history(status: str = None) -> int:
    where = "WHERE status = ?" if status and status != "all" else ""
    params = [status] if where else []
    with _get_conn() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM download_history {where}", params).fetchone()[0]


def count_history_statuses(statuses: list[str] | tuple[str, ...]) -> int:
    normalized = [
        str(status or "").strip().lower()
        for status in list(statuses or [])
        if str(status or "").strip()
    ]
    if not normalized:
        return 0
    placeholders = ", ".join("?" for _ in normalized)
    with _get_conn() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM download_history WHERE LOWER(COALESCE(status, '')) IN ({placeholders})",
            normalized,
        ).fetchone()
    return int(row[0] or 0) if row else 0


def delete_history(status: str = _STATUS_SUCCESS):
    with _write_lock:
        with _get_conn() as conn:
            conn.execute("DELETE FROM download_history WHERE status = ?", (status,))
            conn.commit()


def url_exists_in_history(url: str) -> dict | None:
    """Return existing record if this URL was previously downloaded successfully."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM download_history WHERE url = ? AND status = ? ORDER BY timestamp DESC LIMIT 1",
            (url, _STATUS_SUCCESS),
        ).fetchone()
    return dict(row) if row else None


def get_existing_history_urls(urls: list[str], *, chunk_size: int = 500) -> set[str]:
    """Return the subset of URLs already downloaded successfully."""
    normalized = []
    seen = set()
    for raw_url in urls or []:
        url = str(raw_url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        normalized.append(url)
    if not normalized:
        return set()

    batch_size = max(1, min(int(chunk_size or 500), 900))
    existing = set()
    with _get_conn() as conn:
        for start in range(0, len(normalized), batch_size):
            batch = normalized[start:start + batch_size]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT DISTINCT url FROM download_history "
                f"WHERE status = ? AND url IN ({placeholders})",
                [_STATUS_SUCCESS, *batch],
            ).fetchall()
            existing.update(str(row[0] or "").strip() for row in rows if row and row[0])
    return existing


# ─────────────────────────── Statistics ─────────────────────────────────────

def get_stat(key: str, default="0") -> str:
    with _get_conn() as conn:
        row = conn.execute("SELECT value FROM stats WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_stat(key: str, value):
    with _write_lock:
        with _get_conn() as conn:
            conn.execute(
                "INSERT INTO stats(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value))
            )
            conn.commit()


def increment_stat(key: str, amount: int = 1):
    with _write_lock:
        with _get_conn() as conn:
            row = conn.execute("SELECT value FROM stats WHERE key = ?", (key,)).fetchone()
            current = int(row[0]) if row else 0
            conn.execute(
                "INSERT INTO stats(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(current + amount))
            )
            conn.commit()


def get_all_stats() -> dict:
    with _get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM stats").fetchall()
    return {r[0]: r[1] for r in rows}

def get_history_stats_snapshot(days: int = 7) -> dict:
    days = max(1, int(days or 7))
    with _get_conn() as conn:
        total_count = int(conn.execute("SELECT COUNT(*) FROM download_history").fetchone()[0] or 0)
        success_count = int(
            conn.execute("SELECT COUNT(*) FROM download_history WHERE status = ?", (_STATUS_SUCCESS,)).fetchone()[0] or 0
        )
        total_bytes = int(
            conn.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM download_history WHERE status = ?",
                (_STATUS_SUCCESS,),
            ).fetchone()[0]
            or 0
        )
        rows = conn.execute(
            """
            SELECT substr(timestamp, 1, 10) AS day, COUNT(*) AS c
            FROM download_history
            WHERE status = ?
            GROUP BY day
            ORDER BY day DESC
            LIMIT ?
            """,
            (_STATUS_SUCCESS, days),
        ).fetchall()
    day_counts = [(str(r[0] or ""), int(r[1] or 0)) for r in rows if str(r[0] or "").strip()]
    day_counts.reverse()
    counts = [c for _d, c in day_counts]
    max_count = max(counts) if counts else 0
    chart_data = [int((c / float(max_count or 1)) * 100.0) for c in counts] if counts else []
    return {
        "total_count": total_count,
        "success_count": success_count,
        "total_bytes": total_bytes,
        "day_counts": day_counts,
        "chart_data": chart_data,
    }


def record_peak_speed(kbps: float):
    with _write_lock:
        with _get_conn() as conn:
            row = conn.execute("SELECT value FROM stats WHERE key = ?", ("peak_speed_kbps",)).fetchone()
            current = float(row[0]) if row else 0.0
            if kbps > current:
                conn.execute(
                    "INSERT INTO stats(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("peak_speed_kbps", str(kbps))
                )
                conn.commit()


def save_queue_items(items: list[dict]):
    # Persist queue items by stable task UUID so DB rows stay attached to the same
    # logical task even after reorder/save cycles.
    global _last_queue_save_signature
    source_items = list(items or [])
    prepared_source_items = [dict(item) if isinstance(item, dict) else {} for item in source_items]
    source_signature = _build_queue_source_signature(prepared_source_items)

    def _resolve_saved_signature(rows_snapshot) -> str:
        if source_signature is not None:
            return str(source_signature)
        return _queue_rows_signature(rows_snapshot)

    with _write_lock:
        if source_signature is not None and source_signature == _last_queue_save_signature:
            for item in source_items:
                if isinstance(item, dict):
                    item["task_uuid"] = str(item.get("task_uuid", "") or "").strip()
            return
        with _get_conn() as conn:
            if not source_items:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    existing_any = conn.execute("SELECT 1 FROM queue_items LIMIT 1").fetchone()
                    if existing_any:
                        conn.execute("DELETE FROM queue_items")
                    conn.execute("COMMIT")
                    _last_queue_save_signature = "[]"
                    return
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            previous_snapshot = _fetch_queue_snapshot(conn)
            previous_identity_rows = previous_snapshot["identity_rows"]
            prepared_state = _prepare_queue_save_state(prepared_source_items, previous_identity_rows)
            rows = prepared_state["rows"]
            previous_order = previous_snapshot["order"]
            desired_order = prepared_state["desired_order"]
            preflight_changed_rows = None
            if len(previous_identity_rows) == len(rows) and previous_order == desired_order:
                preflight_changed_rows = _collect_changed_queue_rows_from_snapshot(previous_snapshot, rows)
                if not preflight_changed_rows:
                    _apply_task_uuids_to_source_items(source_items, rows)
                    _last_queue_save_signature = _resolve_saved_signature(rows)
                    return
            stage_prepared = False
            if not (len(previous_identity_rows) == len(rows) and previous_order == desired_order):
                # Stage rows before acquiring BEGIN IMMEDIATE so large snapshots are
                # prepared outside the write transaction window when reorder/insert/delete is likely.
                _stage_queue_rows(conn, rows, chunk_size=_QUEUE_STAGE_CHUNK_SIZE)
                conn.commit()
                stage_prepared = True
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing_snapshot = _fetch_queue_snapshot(conn)
                existing_identity_rows = existing_snapshot["identity_rows"]
                if existing_identity_rows != previous_identity_rows:
                    prepared_state = _prepare_queue_save_state(prepared_source_items, existing_identity_rows)
                    rows = prepared_state["rows"]
                    stage_prepared = False
                    preflight_changed_rows = None
                existing_position_by_uuid = existing_snapshot["position_by_uuid"]
                existing_order = existing_snapshot["order"]
                desired_order = prepared_state["desired_order"]
                _apply_task_uuids_to_source_items(source_items, rows)
                if len(existing_identity_rows) == len(rows) and existing_order == desired_order:
                    mutable_columns = _QUEUE_ITEM_COLUMNS[1:]
                    changed_rows = preflight_changed_rows
                    if changed_rows is None:
                        changed_rows = _collect_changed_queue_rows_from_snapshot(existing_snapshot, rows)
                    if changed_rows:
                        update_assignments = ", ".join(f"{column} = ?" for column in mutable_columns)
                        _executemany_in_chunks(
                            conn,
                            f"""
                            UPDATE queue_items
                            SET {update_assignments}
                            WHERE task_uuid = ?
                            """,
                            [(*row[1:], row[0]) for row in changed_rows],
                            chunk_size=_QUEUE_STAGE_CHUNK_SIZE,
                        )
                    conn.execute("COMMIT")
                    _last_queue_save_signature = _resolve_saved_signature(rows)
                    return
                if not stage_prepared:
                    _stage_queue_rows(conn, rows, chunk_size=_QUEUE_STAGE_CHUNK_SIZE)
                    stage_prepared = True
                stale_uuids = _select_stale_queue_uuids(conn)
                if stale_uuids:
                    delete_placeholders = ", ".join("?" for _ in stale_uuids)
                    conn.execute(f"DELETE FROM queue_items WHERE task_uuid IN ({delete_placeholders})", stale_uuids)
                moved_existing_uuids = _select_moved_queue_uuids(conn)
                if moved_existing_uuids:
                    # Move rows out of the unique queue_position range first so the
                    # staged update can rewrite the final order without collisions.
                    stage_offset = max(1000, len(rows) + len(existing_identity_rows) + 7)
                    in_placeholders = ", ".join("?" for _ in moved_existing_uuids)
                    conn.execute(
                        f"""
                        UPDATE queue_items
                        SET queue_position = queue_position + ?
                        WHERE task_uuid IN ({in_placeholders})
                        """,
                        [stage_offset, *moved_existing_uuids],
                    )
                _apply_staged_queue_updates(conn)
                _insert_staged_queue_rows(conn)
                conn.execute("COMMIT")
                _last_queue_save_signature = _resolve_saved_signature(rows)
            except Exception:
                conn.execute("ROLLBACK")
                raise


def _build_queue_source_signature(source_items: list[dict]) -> str | None:
    if not source_items:
        return "[]"
    digest = hashlib.sha256()
    for index, item in enumerate(source_items):
        task_uuid = str((item or {}).get("task_uuid", "") or "").strip()
        if not task_uuid:
            return None
        try:
            payload = json.dumps(item or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        except Exception:
            payload = repr(sorted((item or {}).items()))
        digest.update(str(index).encode("utf-8", errors="ignore"))
        digest.update(b"|")
        digest.update(task_uuid.encode("utf-8", errors="ignore"))
        digest.update(b"|")
        digest.update(payload.encode("utf-8", errors="ignore"))
        digest.update(b"\n")
    return digest.hexdigest()


def _queue_rows_signature(rows) -> str:
    seq = list(rows or [])
    if not seq:
        return "[]"
    digest = hashlib.sha256()
    for row in seq:
        payload = json.dumps(list(row or ()), ensure_ascii=False, separators=(",", ":"))
        digest.update(payload.encode("utf-8", errors="ignore"))
        digest.update(b"\n")
    return digest.hexdigest()


def _fetch_queue_snapshot(conn) -> dict:
    existing_rows = conn.execute(
        f"SELECT {', '.join(_QUEUE_ITEM_COLUMNS)} FROM queue_items ORDER BY queue_position ASC, id ASC"
    ).fetchall()
    identity_rows = []
    order = []
    position_by_uuid = {}
    tuple_by_uuid = {}
    for row in existing_rows:
        task_uuid = str(row["task_uuid"] or "").strip()
        queue_position = int(row["queue_position"] or 0)
        identity_rows.append((task_uuid, queue_position))
        order.append(task_uuid)
        if task_uuid:
            position_by_uuid[task_uuid] = queue_position
            tuple_by_uuid[task_uuid] = tuple(row[column] for column in _QUEUE_ITEM_COLUMNS)
    return {
        "identity_rows": identity_rows,
        "order": order,
        "position_by_uuid": position_by_uuid,
        "tuple_by_uuid": tuple_by_uuid,
    }


def _prepare_queue_save_state(source_items: list[dict], existing_identity_rows) -> dict:
    needs_position_fallback = any(not str((item or {}).get("task_uuid", "") or "").strip() for item in source_items)
    existing_by_position = {}
    if needs_position_fallback:
        existing_by_position = {
            int(queue_position or 0): task_uuid
            for task_uuid, queue_position in existing_identity_rows
            if task_uuid
        }
    existing_position_by_uuid = {
        task_uuid: int(queue_position or 0)
        for task_uuid, queue_position in existing_identity_rows
        if task_uuid
    }
    rows = []
    for index, item_dict in enumerate(source_items):
        post_download_script = _normalize_post_download_script(item_dict.get("post_download_script", ""))
        cookies_from_browser = _normalize_cookies_from_browser(item_dict.get("cookies_from_browser", "none"))
        task_uuid = str(
            (item_dict.get("task_uuid") or existing_by_position.get(index) or uuid4())
        ).strip()
        rows.append(
            (
                task_uuid,
                index,
                str(item_dict.get("url", "")),
                str(item_dict.get("title", "")),
                str(item_dict.get("out_dir", "")),
                str(item_dict.get("mode", "video")),
                str(item_dict.get("quality", "1080p")),
                str(item_dict.get("format", "mp4")),
                str(item_dict.get("subtitle", "None")),
                str(item_dict.get("start_time", "")),
                str(item_dict.get("end_time", "")),
                int(item_dict.get("retries", 3) or 3),
                int(item_dict.get("auto_retry_delay_seconds", 4) or 4),
                int(item_dict.get("queue_retry_limit", 2) or 2),
                int(item_dict.get("retry_count", 0) or 0),
                float(item_dict.get("next_retry_at", 0) or 0),
                float(item_dict.get("scheduled_at", 0) or 0),
                int(item_dict.get("duration_seconds", 0) or 0),
                1 if bool(item_dict.get("is_live", False)) else 0,
                1 if bool(item_dict.get("was_live", False)) else 0,
                str(item_dict.get("live_status", "")),
                int(item_dict.get("bandwidth_limit_kbps", 0) or 0),
                1 if bool(item_dict.get("use_aria2", True)) else 0,
                str(item_dict.get("status", _STATUS_PENDING)),
                str(item_dict.get("thumbnail", "")),
                str(item_dict.get("category", "")),
                str(item_dict.get("schedule_repeat", "none") or "none"),
                str(item_dict.get("channel", "")),
                str(item_dict.get("source", "")),
                str(item_dict.get("playlist_url", "")),
                int(item_dict.get("playlist_index", 0) or 0),
                str(item_dict.get("playlist_title", "")),
                str(item_dict.get("entry_id", "")),
                float(item_dict.get("progress", 0) or 0),
                str(item_dict.get("speed", "--")),
                str(item_dict.get("eta", "--:--")),
                str(item_dict.get("error_msg", "")),
                str(item_dict.get("file_path", "")),
                str(item_dict.get("last_output_path", "")),
                _serialize_resume_snapshot(item_dict),
                json.dumps(item_dict.get("trims", []) or [], ensure_ascii=False),
                str(item_dict.get("size", item_dict.get("size_text", "--")) or "--"),
                int(item_dict.get("size_bytes", item_dict.get("estimated_size_bytes", 0)) or 0),
                int(item_dict.get("estimated_size_bytes", item_dict.get("size_bytes", 0)) or 0),
                1 if bool(item_dict.get("size_is_estimate", True)) else 0,
                str(item_dict.get("video_id", "")),
                str(item_dict.get("file_hash", "")),
                str(item_dict.get("post_action", "none")),
                post_download_script,
                1 if bool(item_dict.get("embed_subs", True)) else 0,
                1 if bool(item_dict.get("split_chapters", False)) else 0,
                1 if bool(item_dict.get("whisper_fallback", False)) else 0,
                1 if bool(item_dict.get("sponsorblock_enabled", False)) else 0,
                1 if bool(item_dict.get("verify_checksum", False)) else 0,
                1 if bool(item_dict.get("virus_scan_after_download", False)) else 0,
                1 if bool(item_dict.get("use_ytdlp_api", False)) else 0,
                str(item_dict.get("rename_template", "Default") or "Default"),
                1 if bool(item_dict.get("use_native_engine", False)) else 0,
                cookies_from_browser,
                json.dumps(item_dict.get("merge_opts", {}) or {}, ensure_ascii=False, sort_keys=True),
            )
        )
    return {
        "rows": rows,
        "existing_position_by_uuid": existing_position_by_uuid,
        "existing_order": [task_uuid for task_uuid, _queue_position in existing_identity_rows],
        "desired_order": [str(row[0] or "").strip() for row in rows],
    }


def _collect_changed_queue_rows_from_snapshot(snapshot: dict, rows) -> list[tuple]:
    if not rows:
        return []
    existing_tuple_by_uuid = dict(snapshot.get("tuple_by_uuid", {}))
    return [
        row
        for row in rows
        if existing_tuple_by_uuid.get(str(row[0] or "").strip()) != row
    ]


def _stage_queue_rows(conn, rows, *, chunk_size: int = _QUEUE_STAGE_CHUNK_SIZE) -> None:
    conn.execute(
        f"""
        CREATE TEMP TABLE IF NOT EXISTS queue_items_stage (
            {', '.join(f"{column} TEXT" for column in _QUEUE_ITEM_COLUMNS)}
        )
        """
    )
    conn.execute("DELETE FROM queue_items_stage")
    if not rows:
        return
    placeholders = ", ".join("?" for _ in _QUEUE_ITEM_COLUMNS)
    _executemany_in_chunks(
        conn,
        f"""
        INSERT INTO queue_items_stage ({', '.join(_QUEUE_ITEM_COLUMNS)})
        VALUES ({placeholders})
        """,
        rows,
        chunk_size=chunk_size,
    )


def _executemany_in_chunks(conn, sql: str, rows, *, chunk_size: int = _QUEUE_STAGE_CHUNK_SIZE) -> None:
    batch = max(1, int(chunk_size or 1))
    if isinstance(rows, list):
        seq = rows
    elif isinstance(rows, tuple):
        seq = list(rows)
    else:
        seq = list(rows or [])
    if not seq:
        return
    for start in range(0, len(seq), batch):
        conn.executemany(sql, seq[start : start + batch])


def _select_stale_queue_uuids(conn) -> list[str]:
    rows = conn.execute(
        """
        SELECT q.task_uuid
        FROM queue_items AS q
        WHERE NOT EXISTS (
            SELECT 1
            FROM queue_items_stage AS s
            WHERE s.task_uuid = q.task_uuid
        )
        """
    ).fetchall()
    return [str(row[0] or "").strip() for row in rows if str(row[0] or "").strip()]


def _select_moved_queue_uuids(conn) -> list[str]:
    rows = conn.execute(
        """
        SELECT q.task_uuid
        FROM queue_items AS q
        INNER JOIN queue_items_stage AS s
            ON s.task_uuid = q.task_uuid
        WHERE CAST(COALESCE(q.queue_position, 0) AS INTEGER) != CAST(COALESCE(s.queue_position, 0) AS INTEGER)
        """
    ).fetchall()
    return [str(row[0] or "").strip() for row in rows if str(row[0] or "").strip()]


def _apply_staged_queue_updates(conn) -> None:
    assignments = []
    for column in _QUEUE_ITEM_COLUMNS[1:]:
        assignments.append(
            f"""
            {column} = (
                SELECT s.{column}
                FROM queue_items_stage AS s
                WHERE s.task_uuid = queue_items.task_uuid
            )
            """.strip()
        )
    conn.execute(
        f"""
        UPDATE queue_items
        SET {', '.join(assignments)}
        WHERE EXISTS (
            SELECT 1
            FROM queue_items_stage AS s
            WHERE s.task_uuid = queue_items.task_uuid
        )
        """
    )


def _insert_staged_queue_rows(conn) -> None:
    conn.execute(
        f"""
        INSERT INTO queue_items ({', '.join(_QUEUE_ITEM_COLUMNS)})
        SELECT {', '.join(f's.{column}' for column in _QUEUE_ITEM_COLUMNS)}
        FROM queue_items_stage AS s
        WHERE NOT EXISTS (
            SELECT 1
            FROM queue_items AS q
            WHERE q.task_uuid = s.task_uuid
        )
        """
    )


def _apply_task_uuids_to_source_items(source_items: list, rows) -> None:
    for index, item in enumerate(source_items):
        if isinstance(item, dict) and index < len(rows):
            item["task_uuid"] = str(rows[index][0] or "").strip()


def _decode_queue_row(row: dict) -> dict:
    r = dict(row or {})
    merge_opts = {}
    trims = []
    resume = {}
    raw_merge_opts = str(r.get("merge_opts_json", "") or "").strip()
    if raw_merge_opts:
        try:
            parsed_merge_opts = json.loads(raw_merge_opts)
            if isinstance(parsed_merge_opts, dict):
                merge_opts = parsed_merge_opts
        except json.JSONDecodeError:
            merge_opts = {}
    raw_trims = str(r.get("trims_json", "") or "").strip()
    if raw_trims:
        try:
            parsed_trims = json.loads(raw_trims)
            if isinstance(parsed_trims, list):
                trims = parsed_trims
        except json.JSONDecodeError:
            trims = []
    raw_resume = str(r.get("resume_json", "") or "").strip()
    if raw_resume:
        try:
            parsed_resume = json.loads(raw_resume)
            if isinstance(parsed_resume, dict):
                resume = parsed_resume
        except json.JSONDecodeError:
            resume = {}
    return {
        "task_uuid": r.get("task_uuid", ""),
        "url": r.get("url", ""),
        "title": r.get("title", ""),
        "out_dir": r.get("out_dir", ""),
        "mode": r.get("mode", "video"),
        "quality": r.get("quality", "1080p"),
        "format": r.get("format", "mp4"),
        "subtitle": r.get("subtitle", "None"),
        "start_time": r.get("start_time", ""),
        "end_time": r.get("end_time", ""),
        "retries": int(r.get("retries", 3) or 3),
        "auto_retry_delay_seconds": int(r.get("auto_retry_delay_seconds", 4) or 4),
        "queue_retry_limit": int(r.get("queue_retry_limit", 2) or 2),
        "retry_count": int(r.get("retry_count", 0) or 0),
        "next_retry_at": float(r.get("next_retry_at", 0) or 0),
        "scheduled_at": float(r.get("scheduled_at", 0) or 0),
        "duration_seconds": int(r.get("duration_seconds", 0) or 0),
        "is_live": bool(int(r.get("is_live", 0) or 0)),
        "was_live": bool(int(r.get("was_live", 0) or 0)),
        "live_status": str(r.get("live_status", "") or ""),
        "bandwidth_limit_kbps": int(r.get("bandwidth_limit_kbps", 0) or 0),
        "use_aria2": bool(int(r.get("use_aria2", 1))),
        "status": r.get("status", _STATUS_PENDING),
        "thumbnail": r.get("thumbnail", ""),
        "category": r.get("category", ""),
        "schedule_repeat": r.get("schedule_repeat", "none"),
        "channel": r.get("channel", ""),
        "source": r.get("source", ""),
        "playlist_url": r.get("playlist_url", ""),
        "playlist_index": int(r.get("playlist_index", 0) or 0),
        "playlist_title": r.get("playlist_title", ""),
        "entry_id": r.get("entry_id", ""),
        "progress": float(r.get("progress", 0) or 0),
        "speed": r.get("speed", "--"),
        "eta": r.get("eta", "--:--"),
        "error_msg": r.get("error_msg", ""),
        "file_path": r.get("file_path", ""),
        "last_output_path": r.get("last_output_path", ""),
        "resume_json": r.get("resume_json", ""),
        "resume": resume,
        "trims": trims,
        "size": r.get("size", "--"),
        "size_text": r.get("size", "--"),
        "size_bytes": int(r.get("size_bytes", 0) or 0),
        "estimated_size_bytes": int(r.get("estimated_size_bytes", 0) or 0),
        "size_is_estimate": bool(int(r.get("size_is_estimate", 1) or 0)),
        "video_id": r.get("video_id", ""),
        "file_hash": r.get("file_hash", ""),
        "post_action": r.get("post_action", "none"),
        "post_download_script": _normalize_post_download_script(r.get("post_download_script", "")),
        "embed_subs": bool(int(r.get("embed_subs", 1))),
        "split_chapters": bool(int(r.get("split_chapters", 0) or 0)),
        "whisper_fallback": bool(int(r.get("whisper_fallback", 0) or 0)),
        "sponsorblock_enabled": bool(int(r.get("sponsorblock_enabled", 0) or 0)),
        "verify_checksum": bool(int(r.get("verify_checksum", 0) or 0)),
        "virus_scan_after_download": bool(int(r.get("virus_scan_after_download", 0) or 0)),
        "use_ytdlp_api": bool(int(r.get("use_ytdlp_api", 0) or 0)),
        "rename_template": str(r.get("rename_template", "Default") or "Default"),
        "use_native_engine": bool(int(r.get("use_native_engine", 0) or 0)),
        "cookies_from_browser": _normalize_cookies_from_browser(r.get("cookies_from_browser", "none")),
        "merge_opts": merge_opts,
    }


def load_queue_items() -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT * FROM queue_items ORDER BY queue_position ASC, id ASC").fetchall()
    return [_decode_queue_row(dict(row)) for row in rows]


def fetch_queue_entries_page_from_db(
    *,
    view: str,
    now_ts: float,
    queue_state_filter: str = "all",
    media_filter: str = "all",
    query: str = "",
    page: int = 1,
    page_size: int = 200,
) -> dict:
    view_key = str(view or "").strip().lower()
    state_filter = str(queue_state_filter or "all").strip().lower()
    media_filter_key = str(media_filter or "all").strip().lower()
    query_text = str(query or "").strip().lower()
    try:
        page_num = max(1, int(page or 1))
    except (TypeError, ValueError, OverflowError):
        page_num = 1
    try:
        per_page = max(1, int(page_size or 200))
    except (TypeError, ValueError, OverflowError):
        per_page = 200
    now_value = float(now_ts or 0.0)

    base_where_clauses = []
    base_params: list = []
    if view_key == "active":
        base_where_clauses.append("LOWER(COALESCE(status, '')) = ?")
        base_params.append("running")
    elif view_key == "queued":
        base_where_clauses.append("LOWER(COALESCE(status, '')) IN (?, ?, ?, ?)")
        base_params.extend(["pending", "paused", "queued", "waiting"])
        base_where_clauses.append("NOT (LOWER(COALESCE(status, '')) = ? AND CAST(COALESCE(scheduled_at, 0) AS REAL) > ?)")
        base_params.extend(["pending", now_value])
    elif view_key == "scheduled":
        base_where_clauses.append("LOWER(COALESCE(status, '')) = ?")
        base_params.append("pending")
        base_where_clauses.append("CAST(COALESCE(scheduled_at, 0) AS REAL) > ?")
        base_params.append(now_value)

    if state_filter != "all":
        if state_filter == "pending":
            if view_key != "scheduled":
                base_where_clauses.append("LOWER(COALESCE(status, '')) = ?")
                base_params.append("pending")
        elif state_filter in {"running", "paused", "failed", "cancelled"}:
            if view_key == "scheduled":
                base_where_clauses.append("1 = 0")
            else:
                base_where_clauses.append("LOWER(COALESCE(status, '')) = ?")
                base_params.append(state_filter)
        else:
            base_where_clauses.append("LOWER(COALESCE(status, '')) = ?")
            base_params.append(state_filter)

    if query_text:
        base_where_clauses.append(
            "(LOWER(COALESCE(title, '')) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(url, '')) LIKE ? ESCAPE '\\')"
        )
        query_pattern = _contains_like_pattern(query_text)
        base_params.extend([query_pattern, query_pattern])

    summary_where_sql = f"WHERE {' AND '.join(base_where_clauses)}" if base_where_clauses else ""
    summary_params = list(base_params)
    where_clauses = list(base_where_clauses)
    params = list(base_params)
    if media_filter_key in {"video", "audio"}:
        where_clauses.append("LOWER(COALESCE(mode, 'video')) = ?")
        params.append(media_filter_key)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    with _get_conn() as conn:
        media_rows = conn.execute(
            f"""
            SELECT
                CASE
                    WHEN LOWER(COALESCE(mode, 'video')) = 'audio' THEN 'audio'
                    ELSE 'video'
                END AS media_mode,
                COUNT(*) AS media_count
            FROM queue_items
            {summary_where_sql}
            GROUP BY media_mode
            """,
            summary_params,
        ).fetchall()
        media_counts = {"all": 0, "video": 0, "audio": 0}
        for row in media_rows:
            mode_key = str(dict(row).get("media_mode", "video") or "video").strip().lower()
            count_value = int(dict(row).get("media_count", 0) or 0)
            media_counts["all"] += count_value
            if mode_key == "audio":
                media_counts["audio"] += count_value
            else:
                media_counts["video"] += count_value
        total_matches = int(
            conn.execute(
                f"SELECT COUNT(*) FROM queue_items {where_sql}",
                params,
            ).fetchone()[0]
            or 0
        )
        total_pages = max(1, (total_matches + per_page - 1) // per_page)
        page_num = min(page_num, total_pages)
        offset = (page_num - 1) * per_page
        rows = conn.execute(
            f"""
            SELECT *
            FROM queue_items
            {where_sql}
            ORDER BY queue_position ASC, id ASC
            LIMIT ? OFFSET ?
            """,
            [*params, per_page, offset],
        ).fetchall()

    entries = []
    for row in rows:
        payload = _decode_queue_row(dict(row))
        payload["queue_index"] = int(dict(row).get("queue_position", 0) or 0)
        if view_key == "scheduled":
            payload["status"] = "scheduled"
        entries.append(payload)
    return {
        "entries": entries,
        "total_matches": total_matches,
        "total_pages": total_pages,
        "page": page_num,
        "page_size": per_page,
        "media_counts": media_counts,
    }


def _serialize_resume_snapshot(item: dict) -> str:
    raw_resume_json = str(item.get("resume_json", "") or "").strip()
    if raw_resume_json:
        return raw_resume_json
    resume_payload = item.get("resume")
    if not isinstance(resume_payload, dict) or not resume_payload:
        return ""
    try:
        return json.dumps(resume_payload, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return ""


def save_session_settings(settings: dict):
    payload = json.dumps(settings or {}, ensure_ascii=False)
    with _write_lock:
        set_stat("session_settings", payload)


def load_session_settings() -> dict:
    raw = get_stat("session_settings", "{}")
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError as exc:
        logger.warning(f"[DB] Failed to parse session settings: {exc}")
    return {}


# ─────────────────────────── Migration ──────────────────────────────────────

def migrate_from_json(json_path: str):
    """One-time migration from the old xd_stats.json format."""
    if not os.path.exists(json_path):
        return 0
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return 0
    except (json.JSONDecodeError, OSError, IOError) as exc:
        logger.warning(f"[DB] Failed to read/parse migration file {json_path}: {exc}")
        return 0

    history_rows = []
    for entry in data.get("download_history", []) or []:
        if not isinstance(entry, dict):
            continue
        history_rows.append(
            (
                str(entry.get("timestamp", datetime.now(timezone.utc).isoformat())),
                str(entry.get("title", "")),
                str(entry.get("url", "")),
                str(entry.get("mode", "video")),
                str(entry.get("format", "mp4")),
                str(entry.get("quality", "")),
                str(entry.get("size", "--")),
                0,
                str(entry.get("status", _STATUS_SUCCESS)),
                str(entry.get("message", "")),
                int(entry.get("attempts", 1) or 1),
                str(entry.get("error", "")),
                str(entry.get("file_path", "")),
                str(entry.get("thumbnail", "")),
                "",
            )
        )

    try:
        with _write_lock:
            with _get_conn() as conn:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    if history_rows:
                        conn.executemany(
                            """
                            INSERT INTO download_history (
                                timestamp, title, url, mode, format, quality,
                                size_text, size_bytes, status, message, attempts,
                                error, file_path, thumbnail, channel
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            history_rows,
                        )
                    conn.executemany(
                        """
                        INSERT INTO stats(key, value) VALUES(?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        [
                            ("total_videos", str(data.get("total_videos", 0))),
                            ("total_audios", str(data.get("total_audios", 0))),
                        ],
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        os.replace(json_path, json_path + ".migrated")
        return len(history_rows)
    except Exception as exc:
        logger.error(f"[DB] Migration failed: {exc}")
        return 0


def update_task_state_fast(
    queue_index: int | None,
    progress: float,
    speed: str,
    eta: str,
    status: str,
    task_uuid: str = "",
):
    """
    دالة صاروخية (Ultra-Fast) لتحديث حالة التحميل كل ثانية في قاعدة البيانات
    لحمايتها من انقطاع الكهرباء بدون استهلاك المعالج أو الهارد.
    """
    with _write_lock:
        with _get_conn() as conn:
            task_uuid_text = str(task_uuid or "").strip()
            if task_uuid_text:
                conn.execute(
                    """
                    UPDATE queue_items
                    SET progress = ?, speed = ?, eta = ?, status = ?
                    WHERE task_uuid = ?
                    """,
                    (float(progress), str(speed), str(eta), str(status), task_uuid_text),
                )
            else:
                conn.execute(
                    """
                    UPDATE queue_items
                    SET progress = ?, speed = ?, eta = ?, status = ?
                    WHERE queue_position = ?
                    """,
                    (float(progress), str(speed), str(eta), str(status), max(0, int(queue_index or 0))),
                )
            conn.commit()


def update_task_resume_snapshot(task_uuid: str, queue_index: int, resume_json: str):
    with _write_lock:
        with _get_conn() as conn:
            task_uuid_text = str(task_uuid or "").strip()
            if task_uuid_text:
                conn.execute(
                    "UPDATE queue_items SET resume_json = ? WHERE task_uuid = ?",
                    (resume_json, task_uuid_text),
                )
            else:
                conn.execute(
                    "UPDATE queue_items SET resume_json = ? WHERE queue_position = ?",
                    (resume_json, max(0, int(queue_index or 0))),
                )
            conn.commit()

def update_task_states_fast_batch(updates: list[dict]):
    """
    Batch progress/state updates in a single SQLite transaction to reduce write amplification.
    Each item may target either `task_uuid` or fallback to `queue_position`.
    """
    # Last update for the same logical target wins within the same flush batch.
    uuid_updates_by_target: dict[str, tuple[float, str, str, str, str]] = {}
    position_updates_by_target: dict[int, tuple[float, str, str, str, int]] = {}
    for item in updates or []:
        if not isinstance(item, dict):
            continue
        task_uuid_text = str(item.get("task_uuid", "") or "").strip()
        payload = (
            float(item.get("progress", 0.0) or 0.0),
            str(item.get("speed", "--") or "--"),
            str(item.get("eta", "--:--") or "--:--"),
            str(item.get("status", _STATUS_PENDING) or _STATUS_PENDING),
        )
        if task_uuid_text:
            uuid_updates_by_target[task_uuid_text] = payload + (task_uuid_text,)
        else:
            queue_index = max(0, int(item.get("queue_index", 0) or 0))
            position_updates_by_target[queue_index] = payload + (queue_index,)
    uuid_updates = list(uuid_updates_by_target.values())
    position_updates = list(position_updates_by_target.values())
    if not uuid_updates and not position_updates:
        return
    with _write_lock:
        with _get_conn() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                if uuid_updates:
                    conn.executemany(
                        """
                        UPDATE queue_items
                        SET progress = ?, speed = ?, eta = ?, status = ?
                        WHERE task_uuid = ?
                        """,
                        uuid_updates,
                    )
                if position_updates:
                    conn.executemany(
                        """
                        UPDATE queue_items
                        SET progress = ?, speed = ?, eta = ?, status = ?
                        WHERE queue_position = ?
                        """,
                        position_updates,
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise


# M-02: init_db() must be called explicitly from app.py, not at import time.
# This allows unit tests to use a separate DB or mock the database.
# Call init_db() from your application entry point before using any DB functions.


# ─────────────────────────── Playlist Cache ──────────────────────────────────

def _upsert_playlist_parent_rows(
    conn: sqlite3.Connection,
    *,
    playlist_url: str,
    playlist_title: str = "",
    channel_id: str = "",
    etag: str = "",
    last_modified: str = "",
    snapshot_id: str = "",
    sync_status: str = "syncing",
    sync_cursor: str = "",
    metadata_json: str = "",
    now_ts: int = 0,
    last_error: str = "",
    last_success_at: int | None = None,
    consecutive_failures: int | None = None,
    next_sync_after: int | None = None,
) -> None:
    timestamp = int(now_ts or 0)
    existing = conn.execute(
        """
        SELECT last_success_at, consecutive_failures, next_sync_after
        FROM playlist_sync_state
        WHERE playlist_url = ?
        """,
        (str(playlist_url or "").strip(),),
    ).fetchone()
    resolved_last_success_at = int(existing[0] or 0) if existing else 0
    resolved_consecutive_failures = int(existing[1] or 0) if existing else 0
    resolved_next_sync_after = int(existing[2] or 0) if existing else 0
    if last_success_at is not None:
        resolved_last_success_at = max(0, int(last_success_at or 0))
    if consecutive_failures is not None:
        resolved_consecutive_failures = max(0, int(consecutive_failures or 0))
    if next_sync_after is not None:
        resolved_next_sync_after = max(0, int(next_sync_after or 0))
    conn.execute(
        """
        INSERT INTO playlists
            (playlist_url, title, channel_id, etag, last_modified, snapshot_id,
             sync_status, sync_cursor, last_sync_at, last_success_at, consecutive_failures, next_sync_after,
             metadata_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(playlist_url) DO UPDATE SET
            title         = excluded.title,
            channel_id    = excluded.channel_id,
            etag          = excluded.etag,
            last_modified = excluded.last_modified,
            snapshot_id   = excluded.snapshot_id,
            sync_status   = excluded.sync_status,
            sync_cursor   = excluded.sync_cursor,
            last_sync_at  = excluded.last_sync_at,
            last_success_at = excluded.last_success_at,
            consecutive_failures = excluded.consecutive_failures,
            next_sync_after = excluded.next_sync_after,
            metadata_json = excluded.metadata_json,
            updated_at    = excluded.updated_at
        """,
        (
            str(playlist_url or "").strip(),
            str(playlist_title or "")[:512],
            str(channel_id or "")[:255],
            str(etag or "")[:255],
            str(last_modified or "")[:255],
            str(snapshot_id or "")[:255],
            str(sync_status or "syncing")[:64],
            str(sync_cursor or "")[:255],
            timestamp,
            resolved_last_success_at,
            resolved_consecutive_failures,
            resolved_next_sync_after,
            str(metadata_json or "")[:4096],
            timestamp,
            timestamp,
        ),
    )
    conn.execute(
        """
        INSERT INTO playlist_sync_state
            (playlist_url, last_sync_at, last_success_at, consecutive_failures, next_sync_after, sync_status, sync_cursor, etag,
             last_modified, snapshot_id, last_error, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(playlist_url) DO UPDATE SET
            last_sync_at  = excluded.last_sync_at,
            last_success_at = excluded.last_success_at,
            consecutive_failures = excluded.consecutive_failures,
            next_sync_after = excluded.next_sync_after,
            sync_status   = excluded.sync_status,
            sync_cursor   = excluded.sync_cursor,
            etag          = excluded.etag,
            last_modified = excluded.last_modified,
            snapshot_id   = excluded.snapshot_id,
            last_error    = excluded.last_error,
            updated_at    = excluded.updated_at
        """,
        (
            str(playlist_url or "").strip(),
            timestamp,
            resolved_last_success_at,
            resolved_consecutive_failures,
            resolved_next_sync_after,
            str(sync_status or "syncing")[:64],
            str(sync_cursor or "")[:255],
            str(etag or "")[:255],
            str(last_modified or "")[:255],
            str(snapshot_id or "")[:255],
            str(last_error or "")[:1024],
            timestamp,
        ),
    )


def update_playlist_sync_state(
    playlist_url: str,
    *,
    playlist_title: str = "",
    channel_id: str = "",
    etag: str = "",
    last_modified: str = "",
    snapshot_id: str = "",
    sync_status: str = "syncing",
    sync_cursor: str = "",
    metadata_json: str = "",
    last_error: str = "",
) -> None:
    url = str(playlist_url or "").strip()
    if not url:
        return
    import time as _time

    now_ts = int(_time.time())
    with _write_lock:
        with _get_conn() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                previous = conn.execute(
                    """
                    SELECT consecutive_failures, last_success_at, next_sync_after
                    FROM playlist_sync_state
                    WHERE playlist_url = ?
                    """,
                    (url,),
                ).fetchone()
                previous_failures = int(previous[0] or 0) if previous else 0
                previous_success = int(previous[1] or 0) if previous else 0
                previous_next_sync_after = int(previous[2] or 0) if previous else 0
                normalized_status = str(sync_status or "syncing").strip().lower()
                if normalized_status == "failed":
                    consecutive_failures = max(0, previous_failures) + 1
                    backoff_seconds = min(6 * 3600, 60 * (2 ** min(consecutive_failures - 1, 8)))
                    next_sync_after = max(previous_next_sync_after, now_ts + backoff_seconds)
                    last_success_at = previous_success
                elif normalized_status in {"synced", "completed", "success"}:
                    consecutive_failures = 0
                    next_sync_after = 0
                    last_success_at = now_ts
                else:
                    consecutive_failures = max(0, previous_failures)
                    next_sync_after = max(0, previous_next_sync_after)
                    last_success_at = previous_success
                _upsert_playlist_parent_rows(
                    conn,
                    playlist_url=url,
                    playlist_title=playlist_title,
                    channel_id=channel_id,
                    etag=etag,
                    last_modified=last_modified,
                    snapshot_id=snapshot_id,
                    sync_status=sync_status,
                    sync_cursor=sync_cursor,
                    metadata_json=metadata_json,
                    now_ts=now_ts,
                    last_error=last_error,
                    last_success_at=last_success_at,
                    consecutive_failures=consecutive_failures,
                    next_sync_after=next_sync_after,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise


def upsert_playlist_entries(
    playlist_url: str,
    entries: list[dict],
    *,
    etag: str = "",
    last_modified: str = "",
    snapshot_id: str = "",
    sync_status: str = "syncing",
    channel_id: str = "",
    playlist_title: str = "",
    sync_cursor: str = "",
) -> None:
    """
    Upsert a batch of playlist entries into the local cache.
    Each entry dict must have: entry_id (str), and optionally title, duration_seconds,
    thumbnail, playlist_index.
    """
    url = str(playlist_url or "").strip()
    if not url:
        return
    import time as _time
    now_ts = int(_time.time())
    rows = []
    rows_entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        eid = str(entry.get("id") or entry.get("entry_id") or entry.get("video_id") or "").strip()
        if not eid:
            continue
        entry_channel_id = str(entry.get("channel_id") or entry.get("channel", "") or "").strip()
        if entry_channel_id and not channel_id:
            channel_id = entry_channel_id
        entry_title = str(entry.get("playlist_title", "") or "").strip()
        if entry_title and not playlist_title:
            playlist_title = entry_title
        rows.append((
            url,
            eid,
            str(entry.get("title", "") or "")[:512],
            int(entry.get("duration_seconds", 0) or 0),
            str(entry.get("thumbnail", "") or "")[:1024],
            int(entry.get("playlist_index", 0) or 0),
            str(etag or "")[:255],
            str(last_modified or "")[:255],
            str(snapshot_id or "")[:255],
            str(sync_status or "syncing")[:64],
            str(channel_id or "")[:255],
            now_ts,
            str(sync_cursor or "")[:255],
            0,
            now_ts,
            now_ts,
        ))
        rows_entries.append((
            url,
            eid,
            str(entry.get("title", "") or "")[:512],
            int(entry.get("duration_seconds", 0) or 0),
            str(entry.get("thumbnail", "") or "")[:1024],
            int(entry.get("playlist_index", 0) or 0),
            str(snapshot_id or "")[:255],
            now_ts,
            now_ts,
            0,
        ))
    with _write_lock:
        with _get_conn() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                metadata_json = json.dumps(
                    {
                        "playlist_url": url,
                        "entry_count": len(rows),
                    },
                    ensure_ascii=False,
                )
                _upsert_playlist_parent_rows(
                    conn,
                    playlist_url=url,
                    playlist_title=playlist_title,
                    channel_id=channel_id,
                    etag=etag,
                    last_modified=last_modified,
                    snapshot_id=snapshot_id,
                    sync_status=sync_status,
                    sync_cursor=sync_cursor,
                    metadata_json=metadata_json,
                    now_ts=now_ts,
                )
                if not rows:
                    conn.commit()
                    return
                conn.executemany(
                    """
                    INSERT INTO playlist_cache
                        (playlist_url, entry_id, title, duration_seconds, thumbnail, playlist_index,
                         etag, last_modified, snapshot_id, sync_status, channel_id, last_sync_at,
                         sync_cursor, removed_at, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(playlist_url, entry_id) DO UPDATE SET
                        last_seen_at   = excluded.last_seen_at,
                        title          = excluded.title,
                        duration_seconds = excluded.duration_seconds,
                        thumbnail      = excluded.thumbnail,
                        playlist_index = excluded.playlist_index,
                        etag           = excluded.etag,
                        last_modified  = excluded.last_modified,
                        snapshot_id    = excluded.snapshot_id,
                        sync_status    = excluded.sync_status,
                        channel_id     = excluded.channel_id,
                        last_sync_at   = excluded.last_sync_at,
                        sync_cursor    = excluded.sync_cursor,
                        removed_at     = 0
                    """,
                    rows,
                )
                conn.executemany(
                    """
                    INSERT INTO playlist_entries
                        (playlist_url, entry_id, title, duration_seconds, thumbnail,
                         playlist_index, snapshot_id, first_seen_at, last_seen_at, removed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(playlist_url, entry_id) DO UPDATE SET
                        title            = excluded.title,
                        duration_seconds = excluded.duration_seconds,
                        thumbnail        = excluded.thumbnail,
                        playlist_index   = excluded.playlist_index,
                        snapshot_id      = excluded.snapshot_id,
                        last_seen_at     = excluded.last_seen_at,
                        removed_at       = 0
                    """,
                    rows_entries,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise


def get_playlist_known_ids(playlist_url: str) -> set[str]:
    """
    Return the set of entry_ids already cached for this playlist_url.
    Used to compute diff (new vs removed) between two fetch cycles.
    """
    url = str(playlist_url or "").strip()
    if not url:
        return set()
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT entry_id FROM playlist_cache WHERE playlist_url = ?",
                (url,),
            ).fetchall()
        return {str(r[0]).strip() for r in rows if r and str(r[0]).strip()}
    except Exception as exc:
        logger.debug(f"[DB] get_playlist_known_ids failed: {exc}")
        return set()


def diff_playlist_entries(
    playlist_url: str,
    current_entry_ids: list[str],
) -> dict:
    """
    Compare current_entry_ids against what is stored in playlist_cache.
    Returns:
        {
            'new_ids':     set[str],   # ids present now but not in cache
            'removed_ids': set[str],   # ids in cache but absent from current fetch
            'known_ids':   set[str],   # ids present in both
            'is_first_fetch': bool,    # True if cache was empty (brand-new playlist)
        }
    """
    url = str(playlist_url or "").strip()
    cached = get_playlist_known_ids(url)
    current = {str(eid).strip() for eid in (current_entry_ids or []) if str(eid).strip()}
    is_first_fetch = len(cached) == 0
    new_ids = current - cached
    removed_ids = cached - current
    known_ids = current & cached
    return {
        "new_ids": new_ids,
        "removed_ids": removed_ids,
        "known_ids": known_ids,
        "is_first_fetch": is_first_fetch,
    }


def sync_playlist_snapshot(
    playlist_url: str,
    current_entry_ids: list[str],
    *,
    etag: str = "",
    last_modified: str = "",
    snapshot_id: str = "",
    channel_id: str = "",
    playlist_title: str = "",
    sync_cursor: str = "",
    last_error: str = "",
) -> int:
    """
    Keep cache rows for playlist_url aligned with the latest fetched snapshot.
    Deletes cached rows that are no longer present in current_entry_ids.
    Returns number of rows deleted.
    """
    url = str(playlist_url or "").strip()
    if not url:
        return 0
    current_ids = {
        str(entry_id).strip()
        for entry_id in (current_entry_ids or [])
        if str(entry_id).strip()
    }
    import time as _time
    now_ts = int(_time.time())
    with _write_lock:
        with _get_conn() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                if not current_ids:
                    conn.execute(
                        "UPDATE playlist_entries SET removed_at = ? WHERE playlist_url = ? AND removed_at = 0",
                        (now_ts, url),
                    )
                    cur = conn.execute(
                        "DELETE FROM playlist_cache WHERE playlist_url = ?",
                        (url,),
                    )
                    deleted = int(cur.rowcount or 0)
                    _upsert_playlist_parent_rows(
                        conn,
                        playlist_url=url,
                        playlist_title=playlist_title,
                        channel_id=channel_id,
                        etag=etag,
                        last_modified=last_modified,
                        snapshot_id=snapshot_id,
                        sync_status="synced",
                        sync_cursor=sync_cursor,
                        now_ts=now_ts,
                        last_error=last_error,
                        last_success_at=now_ts,
                        consecutive_failures=0,
                        next_sync_after=0,
                    )
                    conn.commit()
                    return deleted
                conn.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS _tmp_playlist_snapshot_ids (entry_id TEXT PRIMARY KEY)"
                )
                conn.execute("DELETE FROM _tmp_playlist_snapshot_ids")
                conn.executemany(
                    "INSERT OR IGNORE INTO _tmp_playlist_snapshot_ids(entry_id) VALUES (?)",
                    [(entry_id,) for entry_id in sorted(current_ids)],
                )
                cur = conn.execute(
                    """
                    DELETE FROM playlist_cache
                    WHERE playlist_url = ?
                      AND entry_id NOT IN (SELECT entry_id FROM _tmp_playlist_snapshot_ids)
                    """,
                    (url,),
                )
                deleted = int(cur.rowcount or 0)
                conn.execute(
                    """
                    UPDATE playlist_entries
                    SET removed_at = ?
                    WHERE playlist_url = ?
                      AND entry_id NOT IN (SELECT entry_id FROM _tmp_playlist_snapshot_ids)
                      AND removed_at = 0
                    """,
                    (now_ts, url),
                )
                conn.execute(
                    """
                    UPDATE playlist_entries
                    SET removed_at = 0, last_seen_at = ?
                    WHERE playlist_url = ?
                      AND entry_id IN (SELECT entry_id FROM _tmp_playlist_snapshot_ids)
                    """,
                    (now_ts, url),
                )
                conn.execute("DELETE FROM _tmp_playlist_snapshot_ids")
                _upsert_playlist_parent_rows(
                    conn,
                    playlist_url=url,
                    playlist_title=playlist_title,
                    channel_id=channel_id,
                    etag=etag,
                    last_modified=last_modified,
                    snapshot_id=snapshot_id,
                    sync_status="synced",
                    sync_cursor=sync_cursor,
                    now_ts=now_ts,
                    last_error=last_error,
                    last_success_at=now_ts,
                    consecutive_failures=0,
                    next_sync_after=0,
                )
                conn.commit()
                return deleted
            except Exception:
                conn.rollback()
                raise


def get_playlist_sync_state(playlist_url: str) -> dict:
    url = str(playlist_url or "").strip()
    if not url:
        return {}
    try:
        with _get_conn() as conn:
            row = conn.execute(
                """
                SELECT last_sync_at, sync_status, sync_cursor, etag,
                       last_modified, snapshot_id, last_error,
                       last_success_at, consecutive_failures, next_sync_after
                FROM playlist_sync_state
                WHERE playlist_url = ?
                """,
                (url,),
            ).fetchone()
        if not row:
            return {}
        return {
            "last_sync_at": int(row[0] or 0),
            "sync_status": str(row[1] or ""),
            "sync_cursor": str(row[2] or ""),
            "etag": str(row[3] or ""),
            "last_modified": str(row[4] or ""),
            "snapshot_id": str(row[5] or ""),
            "last_error": str(row[6] or ""),
            "last_success_at": int(row[7] or 0),
            "consecutive_failures": int(row[8] or 0),
            "next_sync_after": int(row[9] or 0),
        }
    except Exception as exc:
        logger.debug(f"[DB] get_playlist_sync_state failed: {exc}")
        return {}


def get_playlists_due_for_sync(
    *,
    now_ts: int | None = None,
    min_age_seconds: int = 1800,
    limit: int = 50,
) -> list[dict]:
    now_value = int(datetime.now(tz=timezone.utc).timestamp()) if now_ts is None else int(now_ts or 0)
    min_age = max(0, int(min_age_seconds or 0))
    max_rows = max(1, int(limit or 1))
    stale_before = now_value - min_age
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                """
                SELECT playlist_url, sync_status, last_sync_at, next_sync_after,
                       consecutive_failures, etag, last_modified, snapshot_id
                FROM playlist_sync_state
                WHERE (next_sync_after <= 0 OR next_sync_after <= ?)
                  AND (last_sync_at <= 0 OR last_sync_at <= ?)
                  AND LOWER(COALESCE(sync_status, '')) != 'syncing'
                ORDER BY last_sync_at ASC
                LIMIT ?
                """,
                (now_value, stale_before, max_rows),
            ).fetchall()
        out: list[dict] = []
        for row in rows or []:
            out.append(
                {
                    "playlist_url": str(row[0] or "").strip(),
                    "sync_status": str(row[1] or "").strip(),
                    "last_sync_at": int(row[2] or 0),
                    "next_sync_after": int(row[3] or 0),
                    "consecutive_failures": int(row[4] or 0),
                    "etag": str(row[5] or ""),
                    "last_modified": str(row[6] or ""),
                    "snapshot_id": str(row[7] or ""),
                }
            )
        return [item for item in out if item.get("playlist_url")]
    except Exception as exc:
        logger.debug(f"[DB] get_playlists_due_for_sync failed: {exc}")
        return []


def purge_playlist_cache(playlist_url: str, keep_days: int = 90) -> int:
    """
    Remove entries older than keep_days from the playlist cache for a given URL.
    Returns number of rows deleted.
    """
    import time as _time
    url = str(playlist_url or "").strip()
    if not url:
        return 0
    cutoff = int(_time.time()) - max(1, int(keep_days)) * 86400
    with _write_lock:
        with _get_conn() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    "DELETE FROM playlist_cache WHERE playlist_url = ? AND last_seen_at < ?",
                    (url, cutoff),
                )
                deleted = cur.rowcount
                conn.commit()
                return int(deleted or 0)
            except Exception:
                conn.rollback()
                raise
