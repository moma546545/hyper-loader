import json
import os
from dataclasses import dataclass
from typing import Optional

from core.utils import get_app_data_dir
from core.secure_storage import protect_text, unprotect_text

AUTO_MANAGED_COOKIES_REF_PREFIX = "appdata://"


@dataclass
class CookieProfile:
    name: str
    file_path: str


class CookieProfileManager:
    def __init__(self):
        self._base_dir = get_app_data_dir()
        self._store_path = os.path.join(self._base_dir, "cookie_profiles.json")

    def list_profiles(self) -> list[CookieProfile]:
        raw = self._read_store()
        out: list[CookieProfile] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            path = self._resolve_path(str(item.get("file_path", "")).strip())
            if not name:
                continue
            out.append(CookieProfile(name=name, file_path=path))
        return out

    def get_profile_path(self, name: str) -> Optional[str]:
        target = str(name or "").strip().lower()
        if not target:
            return None
        for profile in self.list_profiles():
            if profile.name.strip().lower() == target:
                return profile.file_path
        return None

    def upsert_profile(self, name: str, file_path: str):
        key = str(name or "").strip()
        path = self._serialize_path(str(file_path or "").strip())
        if not key:
            return
        rows = self._read_store()
        updated = False
        for row in rows:
            if str(row.get("name", "")).strip().lower() == key.lower():
                row["file_path"] = path
                updated = True
                break
        if not updated:
            rows.append({"name": key, "file_path": path})
        self._write_store(rows)

    def remove_profile(self, name: str):
        key = str(name or "").strip().lower()
        rows = [row for row in self._read_store() if str(row.get("name", "")).strip().lower() != key]
        self._write_store(rows)

    def _read_store(self) -> list[dict]:
        try:
            with open(self._store_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, list):
                return payload
        except Exception:
            return []
        return []

    def _serialize_path(self, path_value: str) -> str:
        path_text = str(path_value or "").strip()
        if not path_text:
            return ""
        try:
            absolute = os.path.abspath(path_text)
            app_data_dir = os.path.abspath(self._base_dir)
            filename = os.path.basename(absolute)
            if (
                filename.lower().startswith("auto_cookies")
                and os.path.commonpath([absolute, app_data_dir]) == app_data_dir
            ):
                return f"{AUTO_MANAGED_COOKIES_REF_PREFIX}{filename}"
        except Exception:
            return protect_text(path_text)
        return protect_text(path_text)

    def _resolve_path(self, path_value: str) -> str:
        path_text = str(path_value or "").strip()
        if not path_text.startswith(AUTO_MANAGED_COOKIES_REF_PREFIX):
            return unprotect_text(path_text)
        filename = os.path.basename(path_text[len(AUTO_MANAGED_COOKIES_REF_PREFIX):].strip())
        if not filename:
            return ""
        return os.path.join(self._base_dir, filename)

    def _write_store(self, rows: list[dict]):
        os.makedirs(os.path.dirname(self._store_path), exist_ok=True)
        with open(self._store_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
