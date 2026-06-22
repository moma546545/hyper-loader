import json
import logging
import os
import tempfile

try:
    from PySide6.QtCore import QObject, Signal
except ImportError:
    from PyQt6.QtCore import QObject, pyqtSignal as Signal


logger = logging.getLogger("SnapDownloader.ConfigManager")


def _harden_config_file_permissions(path: str) -> None:
    target = str(path or "").strip()
    if not target or not os.path.exists(target):
        return
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    if os.name == "nt":
        try:
            from .cookie_importer import _harden_windows_file_permissions

            _harden_windows_file_permissions(target)
        except Exception:
            pass


class ConfigManager(QObject):
    config_changed = Signal(str, object)

    def __init__(self, filepath="settings.json"):
        super().__init__()
        self.filepath = str(filepath or "settings.json")
        self.default_config = {
            "theme": "Midnight Neon",
            "save_path": os.path.expanduser("~/Downloads/X-Downloader"),
            "max_concurrent": 3,
            "use_aria2c": True,
            "use_native_engine": True,
            "play_sound": True,
        }
        self.config = self.load_config()

    def load_config(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {**self.default_config, **data}
            except Exception as exc:
                logger.warning(f"Failed to load config from {self.filepath}: {exc}")
                return self.default_config.copy()
        return self.default_config.copy()

    def save_config(self):
        temp_path = None
        try:
            directory = os.path.dirname(os.path.abspath(self.filepath))
            if directory:
                os.makedirs(directory, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(self.filepath)}.",
                suffix=".tmp",
                dir=directory or None,
                text=True,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.filepath)
            _harden_config_file_permissions(self.filepath)
            return True
        except Exception as exc:
            logger.error(f"Failed to save config to {self.filepath}: {exc}")
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            raise

    def get(self, key):
        return self.config.get(key, self.default_config.get(key))

    def _is_valid_value_type(self, key, value):
        expected_value = self.default_config.get(key, self.config.get(key))
        if expected_value is None:
            return True
        if isinstance(expected_value, bool):
            return isinstance(value, bool)
        if isinstance(expected_value, int):
            return isinstance(value, int) and not isinstance(value, bool)
        if isinstance(expected_value, str):
            return isinstance(value, str)
        return isinstance(value, type(expected_value))

    def set(self, key, value):
        if not self._is_valid_value_type(key, value):
            logger.warning(
                "Rejected config value with invalid type for %s: %s",
                key,
                type(value).__name__,
            )
            return False
        if self.config.get(key) != value:
            had_key = key in self.config
            previous_value = self.config.get(key)
            self.config[key] = value
            try:
                self.save_config()
            except Exception:
                if had_key:
                    self.config[key] = previous_value
                else:
                    self.config.pop(key, None)
                return False
            self.config_changed.emit(str(key), value)
            return True
        return True
