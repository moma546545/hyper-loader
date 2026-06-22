
"""
core/smart_rename.py — Smart File Renaming Engine
Organizes downloaded files with clean, structured naming conventions.
Supports templates: {channel} - {date} - {title} - {quality}.{ext}
"""
import os
import re
import json
import unicodedata
import urllib.request
import urllib.parse
from datetime import datetime
from .network_safety import (
    is_basic_hostname,
    resolve_tcp_host_ips,
    resolve_safe_host_snapshot,
    response_matches_snapshot,
)


# ── Naming Templates ──────────────────────────────────────────────────────────

TEMPLATES = {
    "Default": "{title}.{ext}",
    "Channel - Title": "{channel} - {title}.{ext}",
    "Date - Title": "{date} - {title}.{ext}",
    "Channel - Date - Title": "{channel} - {date} - {title}.{ext}",
    "Title - Quality": "{title} [{quality}].{ext}",
    "Channel - Title - Quality": "{channel} - {title} [{quality}].{ext}",
    "Full (Organized)": "{channel}/{date}/{title} [{quality}].{ext}",
    "AI Smart": "{title}.{ext}",
}

DEFAULT_TEMPLATE = "Default"
_MAX_COLLISION_ATTEMPTS = 9999


def _is_safe_endpoint_host(host: str, allow_private: bool = False) -> bool:
    return resolve_safe_host_snapshot(
        host,
        allow_private=allow_private,
        resolver=resolve_tcp_host_ips,
        host_validator=is_basic_hostname,
    ) is not None


def build_filename(
    template: str,
    title: str,
    ext: str,
    channel: str = "",
    quality: str = "",
    date: str = "",
    sanitize: bool = True,
) -> str:
    """
    Build a clean filename from a template and metadata.

    Args:
        template: A key from TEMPLATES dict, or a raw template string.
        title: Video title.
        ext: File extension (without dot).
        channel: Channel/uploader name.
        quality: Quality label (e.g., 1080p).
        date: Upload or download date string. Defaults to today.
        sanitize: Whether to remove invalid filename characters.

    Returns:
        Relative path string (may include subdirectories from template).
    """
    # Resolve template
    if str(template).strip() == "AI Smart":
        return _build_ai_filename(
            title=title,
            ext=ext,
            channel=channel,
            quality=quality,
            date=date,
            sanitize=sanitize,
        )
    raw = TEMPLATES.get(template, template)
    if not raw:
        raw = TEMPLATES[DEFAULT_TEMPLATE]

    date = date or datetime.now().strftime("%Y-%m-%d")

    # Substitute placeholders
    result = raw.format(
        title=_clean(title) if sanitize else title,
        ext=ext.lstrip(".").lower(),
        channel=_clean(channel) if sanitize else channel,
        quality=_clean(quality) if sanitize else quality,
        date=date,
    )

    # Normalize path separators
    parts = result.replace("\\", "/").split("/")
    parts = [_sanitize_path_component(p) for p in parts if p.strip()]
    return os.path.join(*parts) if len(parts) > 1 else parts[0]


def _build_ai_filename(
    title: str,
    ext: str,
    channel: str = "",
    quality: str = "",
    date: str = "",
    sanitize: bool = True,
) -> str:
    ai_title = _ask_llm_for_title(title=title, channel=channel, quality=quality) or _heuristic_ai_title(title)
    return build_filename(
        "Channel - Date - Title",
        title=ai_title,
        ext=ext,
        channel=channel or "Unknown",
        quality=quality,
        date=date or datetime.now().strftime("%Y-%m-%d"),
        sanitize=sanitize,
    )


def _ask_llm_for_title(title: str, channel: str = "", quality: str = "") -> str:
    endpoint = str(os.getenv("SMART_RENAME_LLM_ENDPOINT", "")).strip()
    if not endpoint:
        return ""
    allow_private = str(os.getenv("SMART_RENAME_ALLOW_PRIVATE_ENDPOINT", "")).strip().lower() in {"1", "true", "yes", "on"}
    endpoint_snapshot = None
    try:
        parsed_endpoint = urllib.parse.urlparse(endpoint)
        if parsed_endpoint.scheme.lower() != "https" or not parsed_endpoint.netloc:
            return ""
        endpoint_snapshot = resolve_safe_host_snapshot(
            parsed_endpoint.hostname or "",
            allow_private=allow_private,
            resolver=resolve_tcp_host_ips,
            host_validator=is_basic_hostname,
        )
        if endpoint_snapshot is None:
            return ""
    except Exception:
        return ""
    payload = {
        "instruction": "Rewrite this media title into a concise clean filename-friendly phrase. Keep language. Return plain text only.",
        "title": str(title or ""),
        "channel": str(channel or ""),
        "quality": str(quality or ""),
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            if not response_matches_snapshot(resp, endpoint_snapshot):
                return ""
            raw = resp.read(8192).decode("utf-8", errors="replace").strip()
        if not raw:
            return ""
        parsed = json.loads(raw) if raw.startswith("{") else {"title": raw}
        candidate = str(parsed.get("title", "")).strip()
        return _clean(candidate) if candidate else ""
    except Exception:
        return ""


def _heuristic_ai_title(title: str) -> str:
    txt = str(title or "").strip()
    if not txt:
        return "Untitled"
    txt = re.sub(r"\[[^\]]+\]", " ", txt)
    txt = re.sub(r"\([^)]+\)", " ", txt)
    # M-14: Only remove standalone noise words (bounded by word boundaries AND separators)
    # This prevents removing 'audio' from 'Audio Engineering Tutorial'
    noise_pattern = r'(?:^|[\s\-_,|])(?:official|lyrics?|video|hd|4k|1080p|720p|full)(?:[\s\-_,|]|$)'
    txt = re.sub(noise_pattern, ' ', txt, flags=re.IGNORECASE)
    txt = re.sub(r"\s+", " ", txt).strip(" -_")
    words = txt.split(" ")
    if len(words) > 8:
        txt = " ".join(words[:8])
    return _clean(txt) or "Untitled"


def get_output_path(
    out_dir: str,
    template: str,
    title: str,
    ext: str,
    channel: str = "",
    quality: str = "",
    date: str = "",
    avoid_collision: bool = True,
) -> str:
    """
    Build the full output file path. Handles collision avoidance.
    """
    relative = build_filename(template, title, ext, channel, quality, date)
    full_path = os.path.realpath(os.path.join(out_dir, relative))
    base_real = os.path.realpath(out_dir)
    if not (full_path == base_real or full_path.startswith(base_real + os.sep)):
        raise ValueError(f"Path traversal detected: {full_path}")
    base_dir = os.path.dirname(full_path)
    os.makedirs(base_dir, exist_ok=True)

    if avoid_collision and os.path.exists(full_path):
        base, dot_ext = os.path.splitext(full_path)
        counter = 1
        while os.path.exists(f"{base} ({counter}){dot_ext}"):
            if counter > _MAX_COLLISION_ATTEMPTS:
                raise RuntimeError("Too many filename collision attempts.")
            counter += 1
        full_path = f"{base} ({counter}){dot_ext}"

    return full_path


def _clean(text: str, max_len: int = 120) -> str:
    """Remove characters invalid in filenames and trim length."""
    text = str(text or "Unknown").strip()
    # Replace path separators
    text = text.replace("/", "-").replace("\\", "-")
    # Remove truly invalid chars on Windows
    text = re.sub(r'[<>:"|?*\x00-\x1f]', "", text)
    # Collapse multiple spaces/dashes
    text = re.sub(r'[ \-]{2,}', " ", text).strip(" -")
    return text[:max_len] if len(text) > max_len else text


def _sanitize_path_component(component: str, max_len: int = 80) -> str:
    """Sanitize a single path component (dir or filename part)."""
    component = unicodedata.normalize("NFKC", str(component or "").strip())
    for _ in range(3):
        decoded = urllib.parse.unquote(component)
        if decoded == component:
            break
        component = decoded
    component = component.replace("/", " ").replace("\\", " ")
    component = component.replace("..", "")
    component = re.sub(r'[<>:"|?*\x00-\x1f]', "", component)
    component = re.sub(r'[ \-]{2,}', " ", component).strip(" -.")
    if component in {"", "."}:
        component = "_"
    return component[:max_len] if len(component) > max_len else component


def rename_existing_file(old_path: str, new_path: str) -> bool:
    """
    Rename an existing file to new_path. Returns True on success.
    Creates parent directories if needed.
    """
    if not os.path.isfile(old_path):
        return False
    os.makedirs(os.path.dirname(new_path), exist_ok=True)
    try:
        os.rename(old_path, new_path)
        return True
    except OSError:
        return False



