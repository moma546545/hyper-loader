import re
from typing import Any

from core.config import estimate_file_size_bytes, video_quality_to_height
from core.storage_watchdog import format_bytes


def coerce_size_bytes(value: Any) -> int:
    try:
        size = int(float(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, size)


def compact_format_info(fmt: dict | None) -> dict:
    source = fmt if isinstance(fmt, dict) else {}
    return {
        "format_id": str(source.get("format_id") or ""),
        "ext": str(source.get("ext") or ""),
        "format_note": str(source.get("format_note") or ""),
        "resolution": str(source.get("resolution") or ""),
        "height": coerce_size_bytes(source.get("height")),
        "width": coerce_size_bytes(source.get("width")),
        "vcodec": str(source.get("vcodec") or ""),
        "acodec": str(source.get("acodec") or ""),
        "abr": source.get("abr") or 0,
        "tbr": source.get("tbr") or 0,
        "filesize": coerce_size_bytes(source.get("filesize")),
        "filesize_approx": coerce_size_bytes(source.get("filesize_approx")),
    }


def compact_formats(formats: list | tuple | None) -> list[dict]:
    result = []
    for fmt in formats or []:
        if isinstance(fmt, dict):
            compact = compact_format_info(fmt)
            if compact.get("format_id") or compact.get("filesize") or compact.get("filesize_approx"):
                result.append(compact)
    return result


def format_size_label(size_bytes: int, *, estimated: bool = True, empty: str = "--") -> str:
    size = coerce_size_bytes(size_bytes)
    if size <= 0:
        return empty
    prefix = "~" if estimated else ""
    return f"{prefix}{format_bytes(size)}"


def _format_size(fmt: dict, duration: int = 0) -> tuple[int, bool]:
    exact = coerce_size_bytes((fmt or {}).get("filesize"))
    if exact > 0:
        return exact, True
    approx = coerce_size_bytes((fmt or {}).get("filesize_approx"))
    if approx > 0:
        return approx, False
    
    # Try bitrate-based estimation if duration is available
    if duration > 0:
        tbr = float((fmt or {}).get("tbr") or 0)
        if tbr > 0:
            return int((tbr * 1000 * duration) / 8), False
        
        vbr = float((fmt or {}).get("vbr") or 0)
        abr = float((fmt or {}).get("abr") or 0)
        if vbr > 0 or abr > 0:
            return int(((vbr + abr) * 1000 * duration) / 8), False

    return 0, False


def _first_declared_size(source: dict | None) -> tuple[int, bool]:
    data = source if isinstance(source, dict) else {}
    for key in ("filesize", "size_bytes"):
        size = coerce_size_bytes(data.get(key))
        if size > 0:
            return size, key == "filesize"
    for key in ("filesize_approx", "estimated_size_bytes"):
        size = coerce_size_bytes(data.get(key))
        if size > 0:
            return size, False
    return 0, False


def _is_audio_format(fmt: dict) -> bool:
    vcodec = str((fmt or {}).get("vcodec") or "").lower()
    acodec = str((fmt or {}).get("acodec") or "").lower()
    return acodec not in {"", "none"} and vcodec in {"", "none"}


def _is_video_format(fmt: dict) -> bool:
    vcodec = str((fmt or {}).get("vcodec") or "").lower()
    return vcodec not in {"", "none"}


def _quality_height(quality: str) -> int:
    return coerce_size_bytes(video_quality_to_height(str(quality or ""), default="0"))


def _audio_kbps(quality: str) -> int:
    match = re.search(r"(\d+)", str(quality or ""))
    return int(match.group(1)) if match else 192


def _size_candidates(formats: list[dict], predicate, duration: int = 0) -> list[tuple[int, bool, dict]]:
    candidates = []
    for fmt in formats:
        if not predicate(fmt):
            continue
        size, exact = _format_size(fmt, duration=duration)
        if size > 0:
            candidates.append((size, exact, fmt))
    return candidates


def _best_audio_size(formats: list[dict], quality: str, duration: int = 0) -> tuple[int, bool]:
    candidates = _size_candidates(formats, _is_audio_format, duration=duration)
    if not candidates:
        return 0, False
    target = _audio_kbps(quality)

    def score(candidate):
        _size, exact, fmt = candidate
        abr = float(fmt.get("abr") or fmt.get("tbr") or 0)
        distance = abs(abr - target) if abr > 0 else 10_000
        return (distance, 0 if exact else 1, -_size)

    size, exact, _fmt = min(candidates, key=score)
    return size, exact


def _best_video_size(formats: list[dict], quality: str, duration: int = 0) -> tuple[int, bool]:
    candidates = _size_candidates(formats, _is_video_format, duration=duration)
    if not candidates:
        return 0, False
    target_height = _quality_height(quality)

    def score(candidate):
        size, exact, fmt = candidate
        height = coerce_size_bytes(fmt.get("height"))
        if target_height > 0 and height > 0:
            distance = abs(height - target_height)
            over_penalty = 1 if height > target_height else 0
        else:
            note = f"{fmt.get('format_note', '')} {fmt.get('resolution', '')}".lower()
            distance = 0 if str(quality or "").lower().replace(" ", "") in note.replace(" ", "") else 10_000
            over_penalty = 0
        return (distance, over_penalty, 0 if exact else 1, -size)

    size, exact, _fmt = min(candidates, key=score)
    audio_size, audio_exact = _best_audio_size(formats, "192kbps", duration=duration)
    if audio_size > 0:
        return size + audio_size, exact and audio_exact
    return size, exact


def estimate_media_size_bytes(
    source: dict | None,
    *,
    duration_seconds: int = 0,
    mode: str = "video",
    quality: str = "",
    fmt: str = "",
) -> tuple[int, bool]:
    data = source if isinstance(source, dict) else {}
    declared_size, declared_exact = _first_declared_size(data)
    if declared_size > 0 and not data.get("formats"):
        return declared_size, declared_exact

    formats = compact_formats(data.get("formats")) if isinstance(data.get("formats"), (list, tuple)) else []
    mode_text = str(mode or "").strip().lower()
    fmt_text = str(fmt or "").strip().lower()
    quality_text = str(quality or "").strip().lower()
    is_audio = mode_text in {"audio", "صوت"} or fmt_text in {"mp3", "m4a", "wav", "flac", "opus", "aac"}
    dur = int(duration_seconds or data.get("duration_seconds") or data.get("duration") or 0)

    fallback = estimate_file_size_bytes(
        duration_seconds=dur,
        mode=mode,
        quality=quality,
    )

    if formats:
        if is_audio:
            target_abr = _audio_kbps(quality)
            max_abr = 0
            for f in formats:
                if _is_audio_format(f):
                    abr = float(f.get("abr") or f.get("tbr") or 0)
                    if abr > max_abr:
                        max_abr = abr

            if target_abr > 0 and max_abr > 0 and (target_abr - max_abr) >= 32 and quality_text != "original":
                if fallback > 0:
                    return fallback, False

            size, exact = _best_audio_size(formats, quality, duration=dur)
        else:
            target_height = _quality_height(quality)
            max_h = 0
            for f in formats:
                if _is_video_format(f):
                    h = coerce_size_bytes(f.get("height"))
                    if h > max_h:
                        max_h = h

            if target_height > 0 and max_h > 0 and (target_height - max_h) >= 100:
                if fallback > 0:
                    return fallback, False

            size, exact = _best_video_size(formats, quality, duration=dur)

        if size > 0:
            return size, exact

    if declared_size > 0:
        return declared_size, declared_exact

    return fallback, False


def apply_estimated_size(
    task: dict,
    source: dict | None = None,
    *,
    duration_seconds: int | None = None,
    mode: str | None = None,
    quality: str | None = None,
    fmt: str | None = None,
) -> dict:
    if not isinstance(task, dict):
        return task
    task_mode = str(mode if mode is not None else task.get("mode", "video"))
    task_quality = str(quality if quality is not None else task.get("quality", ""))
    task_format = str(fmt if fmt is not None else task.get("format", ""))
    duration = int(duration_seconds if duration_seconds is not None else task.get("duration_seconds", 0) or 0)
    size, exact = estimate_media_size_bytes(
        source or task,
        duration_seconds=duration,
        mode=task_mode,
        quality=task_quality,
        fmt=task_format,
    )
    if size <= 0:
        return task
    task["estimated_size_bytes"] = int(size)
    task["size_bytes"] = int(size)
    task["size"] = format_size_label(size, estimated=not exact)
    task["size_text"] = task["size"]
    task["size_is_estimate"] = not exact
    return task
