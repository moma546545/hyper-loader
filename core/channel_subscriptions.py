
"""
core/channel_subscriptions.py — Smart Channel Subscription Engine
Monitor YouTube channels/playlists every 24 hours and auto-download new videos.
Runs in a background daemon thread — never blocks the UI.
"""
import json
import os
import sys
import threading
import time
import logging
import subprocess
import sqlite3
import hashlib
from datetime import datetime, timedelta
from typing import Callable, Optional

from .utils import get_app_data_dir

logger = logging.getLogger("SnapDownloader.Subscriptions")
SUBS_PATH = os.path.join(get_app_data_dir(), "subscriptions.json")
SUBS_DB_PATH = os.path.join(get_app_data_dir(), "subscriptions_state.db")


class ChannelSubscription:
    """Represents a single subscribed channel or playlist."""

    def __init__(self, data: dict):
        self.url: str          = data.get("url", "")
        self.name: str         = data.get("name", self.url[:50])
        self.out_dir: str      = data.get("out_dir", "downloads")
        self.format: str       = data.get("format", "mp4")
        self.quality: str      = data.get("quality", "1080p")
        self.check_interval_h: int = int(data.get("check_interval_h", 24))
        self.last_check: float = float(data.get("last_check", 0))
        self.known_ids: list   = list(data.get("known_ids", []))
        self.enabled: bool     = bool(data.get("enabled", True))
        self.max_downloads: int = int(data.get("max_downloads", 5))

    def to_dict(self) -> dict:
        return {
            "url":              self.url,
            "name":             self.name,
            "out_dir":          self.out_dir,
            "format":           self.format,
            "quality":          self.quality,
            "check_interval_h": self.check_interval_h,
            "last_check":       self.last_check,
            "known_ids":        self.known_ids[-5000:],   # increased history limit for memory-based fallback
            "enabled":          self.enabled,
            "max_downloads":    self.max_downloads,
        }

    def is_due(self) -> bool:
        if not self.enabled:
            return False
        elapsed_hours = (time.time() - self.last_check) / 3600
        return elapsed_hours >= self.check_interval_h


class SubscriptionManager:
    def __init__(self):
        self.subscriptions: list[ChannelSubscription] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._check_threads: set[threading.Thread] = set()
        self._stop_event = threading.Event()
        self._running = False
        self._on_new_video: Optional[Callable] = None
        self._ensure_db_schema()
        self.load()

    def _ensure_db_schema(self):
        try:
            with sqlite3.connect(SUBS_DB_PATH, timeout=5) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS subscription_seen (
                        subscription_url TEXT NOT NULL,
                        video_id TEXT NOT NULL,
                        first_seen_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                        PRIMARY KEY (subscription_url, video_id)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_subscription_seen_url_time
                    ON subscription_seen (subscription_url, first_seen_at DESC)
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS subscription_state (
                        subscription_url TEXT PRIMARY KEY,
                        last_seen_video_id TEXT NOT NULL DEFAULT '',
                        snapshot_hash TEXT NOT NULL DEFAULT '',
                        fetched_count INTEGER NOT NULL DEFAULT 0,
                        checked_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
                    )
                    """
                )
                conn.commit()
        except Exception as exc:
            logger.warning(f"[Subs] DB schema init failed: {exc}")

    def _load_state_from_db(self, subscription_url: str) -> dict:
        url = str(subscription_url or "").strip()
        if not url:
            return {}
        try:
            with sqlite3.connect(SUBS_DB_PATH, timeout=5) as conn:
                row = conn.execute(
                    """
                    SELECT last_seen_video_id, snapshot_hash, fetched_count, checked_at
                    FROM subscription_state
                    WHERE subscription_url = ?
                    """,
                    (url,),
                ).fetchone()
            if not row:
                return {}
            return {
                "last_seen_video_id": str(row[0] or "").strip(),
                "snapshot_hash": str(row[1] or "").strip(),
                "fetched_count": int(row[2] or 0),
                "checked_at": int(row[3] or 0),
            }
        except Exception as exc:
            logger.warning(f"[Subs] DB load state failed: {exc}")
            return {}

    def _save_state_to_db(self, subscription_url: str, *, last_seen_video_id: str, snapshot_hash: str, fetched_count: int):
        url = str(subscription_url or "").strip()
        if not url:
            return
        try:
            with sqlite3.connect(SUBS_DB_PATH, timeout=10) as conn:
                conn.execute(
                    """
                    INSERT INTO subscription_state (
                        subscription_url,
                        last_seen_video_id,
                        snapshot_hash,
                        fetched_count,
                        checked_at
                    ) VALUES (?, ?, ?, ?, strftime('%s','now'))
                    ON CONFLICT(subscription_url) DO UPDATE SET
                        last_seen_video_id = excluded.last_seen_video_id,
                        snapshot_hash = excluded.snapshot_hash,
                        fetched_count = excluded.fetched_count,
                        checked_at = excluded.checked_at
                    """,
                    (
                        url,
                        str(last_seen_video_id or "").strip(),
                        str(snapshot_hash or "").strip(),
                        max(0, int(fetched_count or 0)),
                    ),
                )
                conn.commit()
        except Exception as exc:
            logger.warning(f"[Subs] DB save state failed: {exc}")

    def _load_known_ids_from_db(self, subscription_url: str, limit: int = 5000) -> set[str]:
        url = str(subscription_url or "").strip()
        if not url:
            return set()
        safe_limit = max(1, min(50000, int(limit or 5000)))
        try:
            with sqlite3.connect(SUBS_DB_PATH, timeout=5) as conn:
                rows = conn.execute(
                    """
                    SELECT video_id FROM subscription_seen
                    WHERE subscription_url = ?
                    ORDER BY first_seen_at DESC
                    LIMIT ?
                    """,
                    (url, safe_limit),
                ).fetchall()
            return {str(row[0]).strip() for row in rows if row and str(row[0]).strip()}
        except Exception as exc:
            logger.warning(f"[Subs] DB load known IDs failed: {exc}")
            return set()

    def _save_seen_ids_to_db(self, subscription_url: str, ids: list[str]):
        url = str(subscription_url or "").strip()
        if not url or not ids:
            return
        normalized = [str(v).strip() for v in ids if str(v).strip()]
        if not normalized:
            return
        # Keep writes bounded so large channels do not lock DB for too long.
        bounded = normalized[:5000]
        try:
            with sqlite3.connect(SUBS_DB_PATH, timeout=10) as conn:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO subscription_seen (subscription_url, video_id)
                    VALUES (?, ?)
                    """,
                    [(url, vid) for vid in bounded],
                )
                conn.commit()
        except Exception as exc:
            logger.warning(f"[Subs] DB save IDs failed: {exc}")

    def _prune_seen_ids_in_db(self, subscription_url: str, keep_limit: int = 50000):
        url = str(subscription_url or "").strip()
        if not url:
            return
        limit = max(1000, min(200000, int(keep_limit or 50000)))
        try:
            with sqlite3.connect(SUBS_DB_PATH, timeout=10) as conn:
                conn.execute(
                    """
                    DELETE FROM subscription_seen
                    WHERE subscription_url = ?
                      AND video_id NOT IN (
                        SELECT video_id
                        FROM subscription_seen
                        WHERE subscription_url = ?
                        ORDER BY first_seen_at DESC
                        LIMIT ?
                      )
                    """,
                    (url, url, limit),
                )
                conn.commit()
        except Exception as exc:
            logger.warning(f"[Subs] DB prune failed: {exc}")

    @staticmethod
    def _snapshot_hash(ids: list[str], window: int = 500) -> str:
        normalized = [str(v).strip() for v in (ids or []) if str(v).strip()]
        if not normalized:
            return ""
        bounded = normalized[: max(1, int(window or 500))]
        payload = "\n".join(bounded).encode("utf-8", errors="ignore")
        return hashlib.sha1(payload).hexdigest()

    @staticmethod
    def _collect_new_video_ids(
        fetched_ids: list[str],
        known_ids: set[str],
        *,
        last_seen_video_id: str = "",
        max_downloads: int = 0,
    ) -> list[str]:
        ordered = [str(v).strip() for v in (fetched_ids or []) if str(v).strip()]
        if not ordered:
            return []
        boundary = str(last_seen_video_id or "").strip()
        if boundary and boundary in ordered:
            candidates = ordered[: ordered.index(boundary)]
        else:
            known = {str(v).strip() for v in (known_ids or set()) if str(v).strip()}
            candidates = [vid for vid in ordered if vid not in known]
        seen = set()
        unique: list[str] = []
        for vid in candidates:
            if vid in seen:
                continue
            seen.add(vid)
            unique.append(vid)
        if max_downloads and max_downloads > 0:
            return unique[: int(max_downloads)]
        return unique

    # ── Persistence ──────────────────────────────────────────────────────────

    def load(self):
        if not os.path.exists(SUBS_PATH):
            return
        try:
            with open(SUBS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            with self._lock:
                self.subscriptions = [ChannelSubscription(s) for s in data.get("subscriptions", [])]
            logger.info(f"[Subs] Loaded {len(self.subscriptions)} subscriptions")
        except Exception as exc:
            logger.warning(f"[Subs] Load failed: {exc}")

    def save(self):
        try:
            with self._lock:
                subs_data = [s.to_dict() for s in self.subscriptions]
            with open(SUBS_PATH, "w", encoding="utf-8") as f:
                json.dump({"subscriptions": subs_data}, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.warning(f"[Subs] Save failed: {exc}")

    # ── Subscription CRUD ─────────────────────────────────────────────────────

    def add(self, url: str, name: str = "", out_dir: str = "downloads",
            fmt: str = "mp4", quality: str = "1080p",
            check_interval_h: int = 24, max_downloads: int = 5) -> ChannelSubscription:
        sub = ChannelSubscription({
            "url": url.strip(),
            "name": name or url[:50],
            "out_dir": out_dir,
            "format": fmt,
            "quality": quality,
            "check_interval_h": check_interval_h,
            "max_downloads": max_downloads,
        })
        with self._lock:
            # Prevent duplicates
            existing_urls = {s.url for s in self.subscriptions}
            if sub.url not in existing_urls:
                self.subscriptions.append(sub)
        self.save()
        logger.info(f"[Subs] Added: {sub.name}")
        return sub

    def remove(self, url: str):
        with self._lock:
            self.subscriptions = [s for s in self.subscriptions if s.url != url]
        self.save()

    def get_all(self) -> list[ChannelSubscription]:
        with self._lock:
            return list(self.subscriptions)

    def get_due(self) -> list[ChannelSubscription]:
        with self._lock:
            return [s for s in self.subscriptions if s.is_due()]

    # ── Background Daemon ─────────────────────────────────────────────────────

    def set_callback(self, callback: Callable):
        """callback(sub: ChannelSubscription, new_urls: list[str])"""
        self._on_new_video = callback

    def start(self):
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="SubsWatcher")
        self._thread.start()
        logger.info("[Subs] Background watcher started")

    def stop(self, join_timeout: float = 2.0):
        self._running = False
        self._stop_event.set()
        current = threading.current_thread()
        with self._lock:
            watcher_thread = self._thread
            spawned_checks = list(self._check_threads)
        if watcher_thread is not None and watcher_thread is not current and watcher_thread.is_alive():
            watcher_thread.join(timeout=max(0.0, float(join_timeout or 0.0)))
        for worker_thread in spawned_checks:
            if worker_thread is current or not worker_thread.is_alive():
                continue
            worker_thread.join(timeout=min(1.0, max(0.0, float(join_timeout or 0.0))))

    def check_now(self, sub: ChannelSubscription):
        """Manually trigger a check for one subscription."""
        self._spawn_check_thread(sub)

    def _spawn_check_thread(self, sub: ChannelSubscription) -> None:
        def _runner():
            current = threading.current_thread()
            try:
                self._check_subscription(sub)
            finally:
                with self._lock:
                    self._check_threads.discard(current)

        worker = threading.Thread(target=_runner, daemon=True)
        with self._lock:
            self._check_threads.add(worker)
        worker.start()

    def _loop(self):
        """Background loop: check every 30 minutes if any sub is due."""
        try:
            while self._running:
                try:
                    due = self.get_due()
                    for sub in due:
                        if not self._running:
                            break
                        self._check_subscription(sub)
                except Exception as exc:
                    logger.exception(f"[Subs] Loop error: {exc}")
                
                # Use stop_event.wait() instead of time.sleep() for instant responsiveness
                if self._stop_event.wait(timeout=1800):
                    break
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None

    def _check_subscription(self, sub: ChannelSubscription):
        """Fetch the latest video IDs from the channel and find new ones."""
        logger.info(f"[Subs] Checking: {sub.name}")
        try:
            import urllib.parse
            try:
                parsed = urllib.parse.urlsplit(sub.url)
                if parsed.scheme.lower() not in {"http", "https", "ftp", "ftps"}:
                    logger.warning(f"[Subs] Rejected checking unsafe subscription URL: {sub.url}")
                    return
            except Exception as exc:
                logger.warning(f"[Subs] Failed parsing URL for sub {sub.name}: {exc}")
                return

            # High cap for large channels.
            fetch_cap = max(100, min(10000, int(sub.max_downloads or 5) * 50))
            cmd = [
                sys.executable, "-m", "yt_dlp",
                "--flat-playlist",
                "--print", "id",
                "--playlist-end", str(fetch_cap),
                "--no-warnings",
                "--",
                sub.url,
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
            sub.last_check = time.time()

            if proc.returncode != 0:
                logger.warning(f"[Subs] yt-dlp failed for {sub.name}: {proc.stderr[:200]}")
                self.save()
                return

            fetched_ids = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
            latest_id = fetched_ids[0] if fetched_ids else ""
            snapshot = self._snapshot_hash(fetched_ids)
            state = self._load_state_from_db(sub.url)
            previous_last_seen = str(state.get("last_seen_video_id", "")).strip()
            db_known = self._load_known_ids_from_db(sub.url, limit=10000)
            known = set(sub.known_ids) | db_known
            new_ids = self._collect_new_video_ids(
                fetched_ids,
                known,
                last_seen_video_id=previous_last_seen,
                max_downloads=int(sub.max_downloads or 0),
            )

            if new_ids:
                logger.info(f"[Subs] {len(new_ids)} new videos in {sub.name}")
                new_urls = [f"https://www.youtube.com/watch?v={vid}" for vid in new_ids]
                sub.known_ids.extend(new_ids)
                self._save_seen_ids_to_db(sub.url, fetched_ids)
                self._prune_seen_ids_in_db(sub.url, keep_limit=50000)
                self._save_state_to_db(
                    sub.url,
                    last_seen_video_id=latest_id,
                    snapshot_hash=snapshot,
                    fetched_count=len(fetched_ids),
                )
                self.save()
                if self._on_new_video:
                    try:
                        self._on_new_video(sub, new_urls)
                    except Exception as cb_exc:
                        logger.warning(f"[Subs] Callback error: {cb_exc}")
            else:
                same_snapshot = bool(snapshot and snapshot == str(state.get("snapshot_hash", "")).strip())
                if same_snapshot:
                    logger.info(f"[Subs] No new videos in {sub.name} (snapshot unchanged)")
                else:
                    logger.info(f"[Subs] No new videos in {sub.name}")
                if fetched_ids:
                    refresh_window = max(200, min(5000, int(sub.max_downloads or 5) * 50))
                    sub.known_ids.extend(fetched_ids[:refresh_window])
                    self._save_seen_ids_to_db(sub.url, fetched_ids)
                    self._prune_seen_ids_in_db(sub.url, keep_limit=50000)
                self._save_state_to_db(
                    sub.url,
                    last_seen_video_id=latest_id,
                    snapshot_hash=snapshot,
                    fetched_count=len(fetched_ids),
                )
                self.save()

        except subprocess.TimeoutExpired:
            logger.warning(f"[Subs] Timeout checking {sub.name}")
        except Exception as exc:
            logger.exception(f"[Subs] Check failed for {sub.name}: {exc}")


# Singleton
subscription_manager = SubscriptionManager()



