
"""
core/duplicate_finder.py — Smart Duplicate Detection Engine
Checks if a URL has already been downloaded before starting a new download.
Also scans local files to detect content-level duplicates by filename.
"""
import os
import re
import hashlib
import sqlite3
import logging
import time
import urllib.parse
from io import BytesIO
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, urlopen, HTTPRedirectHandler
try:
    from core.network_safety import (
        is_basic_hostname as _is_basic_hostname,
        resolve_safe_host_snapshot as _resolve_safe_host_snapshot,
        response_matches_snapshot as _response_matches_snapshot,
        resolve_tcp_host_ips as _resolve_tcp_host_ips,
    )
except ImportError:
    _resolve_safe_host_snapshot = None
    _response_matches_snapshot = None
    _is_basic_hostname = None
    _resolve_tcp_host_ips = None

logger = logging.getLogger("SnapDownloader.DuplicateFinder")
try:
    from PIL import Image
    import imagehash
except Exception:
    Image = None
    imagehash = None


_PHASH_CACHE: dict[str, tuple[float, Optional[str]]] = {}
_PHASH_CACHE_TTL_SECONDS = 30 * 60
_MAX_REMOTE_IMAGE_BYTES = 2 * 1024 * 1024
_MAX_REMOTE_REDIRECTS = 2
_FAST_HASH_HEAD_SIZE = 65536
_FAST_HASH_TAIL_SIZE = 65536


def get_file_hash(filepath: str, chunk_size: int = 8192) -> Optional[str]:
    """توليد SHA-256 كامل للملف لتأكيد التطابق الحقيقي."""
    if not filepath or not os.path.isfile(filepath):
        return None
    hasher = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            while True:
                chunk = f.read(max(1, int(chunk_size or 8192)))
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()
    except Exception as exc:
        logger.error(f"[DuplicateFinder] Error hashing file {filepath}: {exc}")
        return None


def _get_fast_file_fingerprint(filepath: str) -> Optional[str]:
    """بصمة سريعة للرأس/الذيل/الحجم لتجميع المرشحين قبل تأكيدهم بـ SHA-256 كامل."""
    if not filepath or not os.path.isfile(filepath):
        return None
    try:
        size = os.path.getsize(filepath)
        hasher = hashlib.sha256()
        with open(filepath, "rb") as f:
            hasher.update(f.read(_FAST_HASH_HEAD_SIZE))
            if size > (_FAST_HASH_HEAD_SIZE + _FAST_HASH_TAIL_SIZE):
                f.seek(-_FAST_HASH_TAIL_SIZE, os.SEEK_END)
                hasher.update(f.read(_FAST_HASH_TAIL_SIZE))
            hasher.update(str(size).encode("utf-8"))
        return hasher.hexdigest()
    except Exception as exc:
        logger.error(f"[DuplicateFinder] Error fingerprinting file {filepath}: {exc}")
        return None


def check_url_duplicate(url: str) -> Optional[dict]:
    """
    Check if a URL was previously downloaded successfully and the stored file
    still exists on disk. Returns existing record dict or None.
    """
    try:
        from core.database import url_exists_in_history
        record = url_exists_in_history(url.strip())
        if not isinstance(record, dict):
            return None
        file_path = str(record.get("file_path", "") or "").strip()
        if not file_path or not os.path.exists(file_path):
            return None
        return record
    except (ImportError, sqlite3.Error) as exc:
        logger.debug(f"[DuplicateFinder] URL duplicate check failed: {exc}")
        return None


def is_duplicate_video_id(video_id: str) -> bool:
    """فحص إذا كان هذا الـ Video ID قد تم تحميله مسبقاً (تقريبياً عبر البحث في السجل)."""
    text = str(video_id or "").strip()
    if not text:
        return False
    try:
        from core.database import fetch_history
        rows = fetch_history(status="success", limit=1, offset=0, search=text)
        return bool(rows)
    except (ImportError, sqlite3.Error) as exc:
        logger.debug(f"[DuplicateFinder] video-id duplicate check failed: {exc}")
        return False


def scan_directory_for_duplicates(directory: str) -> list[tuple[str, str]]:
    """فحص مجلد كامل بمرحلتين: بصمة سريعة ثم SHA-256 كامل لتأكيد التطابق."""
    root_dir = str(directory or "").strip()
    if not root_dir or not os.path.isdir(root_dir):
        return []
    candidate_groups: dict[str, list[str]] = {}
    full_hash_cache: dict[str, str] = {}
    duplicates: list[tuple[str, str]] = []
    for root, _dirs, files in os.walk(root_dir):
        files.sort()
        for filename in files:
            path = os.path.join(root, filename)
            fingerprint = _get_fast_file_fingerprint(path)
            if not fingerprint:
                continue
            group = candidate_groups.setdefault(fingerprint, [])
            if not group:
                group.append(path)
                continue
            current_hash = full_hash_cache.get(path)
            if current_hash is None:
                current_hash = get_file_hash(path)
                if not current_hash:
                    continue
                full_hash_cache[path] = current_hash
            matched_path = None
            for other in group:
                other_hash = full_hash_cache.get(other)
                if other_hash is None:
                    other_hash = get_file_hash(other)
                    if not other_hash:
                        continue
                    full_hash_cache[other] = other_hash
                if other_hash == current_hash:
                    matched_path = other
                    break
            if matched_path:
                duplicates.append((path, matched_path))
            group.append(path)
    return duplicates


def get_perceptual_hash(filepath: str) -> Optional[str]:
    """Perceptual hash for local or remote thumbnails; requires Pillow + imagehash."""
    if Image is None or imagehash is None:
        return None
    p = str(filepath or "").strip()
    if not p:
        return None
    now = time.time()
    cached = _PHASH_CACHE.get(p)
    if isinstance(cached, tuple) and len(cached) == 2:
        ts, value = cached
        if (now - float(ts or 0.0)) <= _PHASH_CACHE_TTL_SECONDS:
            return value
    try:
        if os.path.isfile(p):
            with Image.open(p) as img:
                value = str(imagehash.phash(img))
        elif p.lower().startswith(("http://", "https://")):
            try:
                if _resolve_safe_host_snapshot is None:
                    return None
                parsed = urllib.parse.urlparse(p)
                if not parsed.hostname:
                    return None
                if str(parsed.scheme or "").lower() != "https":
                    return None

                if _resolve_tcp_host_ips is None or _is_basic_hostname is None:
                    return None
                start_snapshot = _resolve_safe_host_snapshot(
                    parsed.hostname,
                    allow_private=False,
                    resolver=_resolve_tcp_host_ips,
                    host_validator=_is_basic_hostname,
                )
                if start_snapshot is None:
                    return None
            except Exception:
                return None

            class _NoAutoRedirect(HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
                    return None

            raw = b""
            current_url = p
            redirects = 0
            opener = build_opener(_NoAutoRedirect())
            while True:
                request = Request(current_url, headers={"User-Agent": "VidDownloader/1.0"})
                try:
                    req = opener.open(request, timeout=2.5)
                except HTTPError as http_exc:
                    status = int(getattr(http_exc, "code", 0) or 0)
                    if status not in {301, 302, 303, 307, 308}:
                        raw = b""
                        break
                    if redirects >= _MAX_REMOTE_REDIRECTS:
                        raw = b""
                        break
                    next_url = str(http_exc.headers.get("Location", "") or "").strip()
                    if not next_url:
                        raw = b""
                        break
                    candidate = urllib.parse.urljoin(current_url, next_url)
                    parsed_next = urllib.parse.urlparse(candidate)
                    if str(parsed_next.scheme or "").lower() != "https":
                        raw = b""
                        break
                    if not parsed_next.hostname:
                        raw = b""
                        break
                    next_snapshot = _resolve_safe_host_snapshot(
                        parsed_next.hostname,
                        allow_private=False,
                        resolver=_resolve_tcp_host_ips,
                        host_validator=_is_basic_hostname,
                    )
                    if next_snapshot is None:
                        raw = b""
                        break
                    current_url = candidate
                    start_snapshot = next_snapshot
                    redirects += 1
                    continue
                try:
                    if _response_matches_snapshot is not None:
                        if not _response_matches_snapshot(req, start_snapshot):
                            raw = b""
                            break
                    content_type = ""
                    content_length = ""
                    try:
                        headers = getattr(req, "headers", None)
                        if headers is not None and hasattr(headers, "get"):
                            content_type = str(headers.get("Content-Type", "") or "").strip().lower()
                            content_length = str(headers.get("Content-Length", "") or "").strip()
                        elif hasattr(req, "getheader"):
                            content_type = str(req.getheader("Content-Type", "") or "").strip().lower()
                            content_length = str(req.getheader("Content-Length", "") or "").strip()
                    except Exception:
                        content_type = ""
                        content_length = ""
                    if content_type and not content_type.startswith("image/"):
                        raw = b""
                        break
                    if content_length:
                        try:
                            if int(content_length) > _MAX_REMOTE_IMAGE_BYTES:
                                raw = b""
                                break
                        except (TypeError, ValueError):
                            pass
                    raw = req.read(_MAX_REMOTE_IMAGE_BYTES + 1)
                finally:
                    try:
                        req.close()
                    except Exception:
                        pass
                break
            if not raw or len(raw) > _MAX_REMOTE_IMAGE_BYTES:
                value = None
            else:
                with Image.open(BytesIO(raw)) as img:
                    value = str(imagehash.phash(img))
        else:
            value = None
    except (OSError, ValueError, URLError):
        value = None
    except Exception:
        value = None
    _PHASH_CACHE[p] = (now, value)
    while len(_PHASH_CACHE) > 256:
        stale_key = next(iter(_PHASH_CACHE.keys()))
        _PHASH_CACHE.pop(stale_key, None)
    return value


def find_visual_duplicates(image_paths: list[str], max_distance: int = 6) -> list[tuple[str, str, int]]:
    """Return visually-similar image pairs using perceptual hash distance."""
    prepared: list[tuple[str, str]] = []
    for p in image_paths or []:
        h = get_perceptual_hash(p)
        if h:
            prepared.append((str(p), h))
    out: list[tuple[str, str, int]] = []
    for i in range(len(prepared)):
        p1, h1 = prepared[i]
        for j in range(i + 1, len(prepared)):
            p2, h2 = prepared[j]
            try:
                dist = imagehash.hex_to_hash(h1) - imagehash.hex_to_hash(h2)
            except Exception:
                continue
            if dist <= int(max_distance):
                out.append((p1, p2, int(dist)))
    return out


def find_best_visual_duplicate(
    thumbnail: str,
    candidates: list[dict],
    *,
    max_distance: int = 6,
    exclude_url: str = "",
) -> Optional[dict]:
    current_thumb = str(thumbnail or "").strip()
    if not current_thumb:
        return None
    current_hash = get_perceptual_hash(current_thumb)
    if not current_hash or imagehash is None:
        return None
    exclude_url_text = str(exclude_url or "").strip()
    best = None
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        candidate_thumb = str(candidate.get("thumbnail", "") or "").strip()
        candidate_url = str(candidate.get("url", "") or "").strip()
        if not candidate_thumb:
            continue
        if exclude_url_text and candidate_url and candidate_url == exclude_url_text:
            continue
        if candidate_thumb == current_thumb:
            continue
        candidate_hash = get_perceptual_hash(candidate_thumb)
        if not candidate_hash:
            continue
        try:
            distance = int(imagehash.hex_to_hash(current_hash) - imagehash.hex_to_hash(candidate_hash))
        except Exception:
            continue
        if distance > int(max_distance):
            continue
        row = {
            "title": str(candidate.get("title", "") or "").strip(),
            "url": candidate_url,
            "thumbnail": candidate_thumb,
            "timestamp": str(candidate.get("timestamp", "") or "").strip(),
            "distance": distance,
            "file_path": str(candidate.get("file_path", "") or "").strip(),
        }
        if best is None or int(row["distance"]) < int(best.get("distance", 9999) or 9999):
            best = row
    return best


def scan_for_local_duplicates(directory: str, title: str) -> list[str]:
    """
    Scan download directory for files with similar names.
    Returns list of matching file paths.
    """
    if not os.path.isdir(directory):
        return []

    title_clean = _normalize(title)
    matches = []
    try:
        for filename in os.listdir(directory):
            if _normalize(os.path.splitext(filename)[0]) == title_clean:
                matches.append(os.path.join(directory, filename))
    except (OSError, PermissionError) as exc:
        logger.debug(f"[DuplicateFinder] Local duplicate scan failed for {directory}: {exc}")
    return matches


def _normalize(text: str) -> str:
    """Normalize text for fuzzy comparison."""
    text = str(text or "").lower().strip()
    # Remove common video ID patterns [abc123]
    text = re.sub(r'\[.*?\]', '', text)
    # Remove non-alphanumeric
    text = re.sub(r'[^a-z0-9\u0600-\u06ff ]', '', text)
    return " ".join(text.split())


def build_duplicate_report(
    url: str,
    out_dir: str,
    title: str,
    *,
    thumbnail: str = "",
    visual_candidates: list[dict] | None = None,
    max_visual_distance: int = 6,
) -> dict:
    """
    Build a full duplicate detection report before starting a download.
    Returns a dict with:
        - url_duplicate: existing DB record or None
        - local_files: list of matching local file paths
        - is_duplicate: True if any duplicate found
    """
    url_dup = check_url_duplicate(url)
    local_files = scan_for_local_duplicates(out_dir, title) if title else []
    visual_dup = None
    if thumbnail and Image is not None and imagehash is not None:
        candidates = list(visual_candidates or [])
        if not candidates:
            try:
                from core.database import fetch_history
                history_rows = fetch_history(status="success", limit=40, offset=0)
                candidates = [row for row in history_rows if str((row or {}).get("thumbnail", "") or "").strip()]
            except (ImportError, sqlite3.Error) as exc:
                logger.debug(f"[DuplicateFinder] visual duplicate history lookup failed: {exc}")
                candidates = []
        visual_dup = find_best_visual_duplicate(
            thumbnail,
            candidates,
            max_distance=max_visual_distance,
            exclude_url=url,
        )

    return {
        "url_duplicate": url_dup,
        "local_files": local_files,
        "visual_duplicate": visual_dup,
        # Only prompt when there is a real existing local artifact.
        "is_duplicate": url_dup is not None or len(local_files) > 0,
    }



