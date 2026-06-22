from __future__ import annotations

import os
import shutil
from typing import Iterable


_KNOWN_SIDECAR_SUFFIXES = (
    ".info.json",
    ".description",
    ".srt",
    ".vtt",
    ".ass",
    ".ssa",
    ".lrc",
    ".nfo",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".txt",
)

_MODE_FOLDERS = {
    "video": "Videos",
    "audio": "Audio",
}


def normalize_auto_categorize_mode(value: str) -> str:
    raw = str(value or "").strip().lower()
    mapping = {
        "": "off",
        "off": "off",
        "none": "off",
        "disabled": "off",
        "mode": "mode",
        "by_mode": "mode",
        "type": "mode",
        "extension": "extension",
        "ext": "extension",
        "by_extension": "extension",
        "mode_then_extension": "mode_then_extension",
        "mode+extension": "mode_then_extension",
        "both": "mode_then_extension",
    }
    return mapping.get(raw, "off")


def _safe_extension_folder(path: str) -> str:
    ext = str(os.path.splitext(str(path or ""))[1] or "").strip().lower().lstrip(".")
    if not ext:
        return "Other"
    return "".join(ch for ch in ext.upper() if ch.isalnum()) or "Other"


def _mode_folder(task: dict | None) -> str:
    mode = str((task or {}).get("mode", "") or "").strip().lower()
    return _MODE_FOLDERS.get(mode, "Other")


def _target_parts(file_path: str, strategy: str, task: dict | None) -> list[str]:
    normalized = normalize_auto_categorize_mode(strategy)
    if normalized == "mode":
        return [_mode_folder(task)]
    if normalized == "extension":
        return [_safe_extension_folder(file_path)]
    if normalized == "mode_then_extension":
        return [_mode_folder(task), _safe_extension_folder(file_path)]
    return []


def _unique_destination(path: str) -> str:
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    counter = 1
    while counter <= 9999:
        candidate = f"{root} ({counter}){ext}"
        if not os.path.exists(candidate):
            return candidate
        counter += 1
    raise RuntimeError("Too many file name collisions while organizing downloads.")


def _iter_related_sidecars(file_path: str) -> Iterable[tuple[str, str]]:
    directory = os.path.dirname(file_path)
    base_name = os.path.basename(file_path)
    stem, _ext = os.path.splitext(base_name)
    prefix = stem.lower() + "."
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return []
    related: list[tuple[str, str]] = []
    for entry in entries:
        if not entry.is_file():
            continue
        name = entry.name
        lower_name = name.lower()
        if lower_name == base_name.lower():
            continue
        if not lower_name.startswith(prefix):
            continue
        if not any(lower_name.endswith(suffix) for suffix in _KNOWN_SIDECAR_SUFFIXES):
            continue
        suffix = name[len(stem):]
        related.append((entry.path, suffix))
    return related


def organize_download_output(file_path: str, strategy: str, task: dict | None = None) -> dict:
    source_path = str(file_path or "").strip()
    normalized_strategy = normalize_auto_categorize_mode(strategy)
    if not source_path or normalized_strategy == "off":
        return {"moved": False, "file_path": source_path, "target_dir": "", "moved_paths": []}
    if not os.path.isfile(source_path):
        return {"moved": False, "file_path": source_path, "target_dir": "", "moved_paths": []}

    source_dir = os.path.dirname(source_path)
    parts = _target_parts(source_path, normalized_strategy, task)
    if not parts:
        return {"moved": False, "file_path": source_path, "target_dir": source_dir, "moved_paths": []}

    target_dir = os.path.join(source_dir, *parts)
    try:
        same_dir = os.path.normcase(os.path.abspath(target_dir)) == os.path.normcase(os.path.abspath(source_dir))
    except Exception:
        same_dir = target_dir == source_dir
    if same_dir:
        return {"moved": False, "file_path": source_path, "target_dir": target_dir, "moved_paths": []}

    os.makedirs(target_dir, exist_ok=True)
    target_path = _unique_destination(os.path.join(target_dir, os.path.basename(source_path)))
    moved_paths: list[str] = []
    rollback_moves: list[tuple[str, str]] = []

    try:
        shutil.move(source_path, target_path)
        rollback_moves.append((target_path, source_path))
        moved_paths.append(target_path)

        old_stem = os.path.splitext(os.path.basename(source_path))[0]
        new_stem = os.path.splitext(os.path.basename(target_path))[0]
        for sidecar_path, suffix in _iter_related_sidecars(source_path):
            sidecar_target = os.path.join(target_dir, new_stem + suffix)
            if os.path.exists(sidecar_target):
                sidecar_target = _unique_destination(sidecar_target)
            shutil.move(sidecar_path, sidecar_target)
            rollback_moves.append((sidecar_target, sidecar_path))
            moved_paths.append(sidecar_target)
    except Exception:
        for moved, original in reversed(rollback_moves):
            try:
                if os.path.exists(moved) and not os.path.exists(original):
                    os.makedirs(os.path.dirname(original) or os.getcwd(), exist_ok=True)
                    shutil.move(moved, original)
            except Exception:
                pass
        raise

    return {
        "moved": True,
        "file_path": target_path,
        "target_dir": target_dir,
        "moved_paths": moved_paths,
        "source_dir": source_dir,
        "source_stem": old_stem,
    }
