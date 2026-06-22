import json
import threading

import core.bootstrap as bootstrap_module
import core.config_manager as config_manager_module
import core.i18n as i18n_module

from core.config_manager import ConfigManager
from core.i18n import I18nManager


def test_config_manager_set_rolls_back_when_atomic_replace_fails(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"theme": "Initial Theme"}, ensure_ascii=False),
        encoding="utf-8",
    )
    manager = ConfigManager(filepath=str(settings_path))
    emitted = []
    manager.config_changed.connect(lambda key, value: emitted.append((key, value)))

    def _raise_replace_error(_src, _dst):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(config_manager_module.os, "replace", _raise_replace_error)

    assert manager.set("theme", "Broken Theme") is False
    assert manager.get("theme") == "Initial Theme"
    assert emitted == []
    assert json.loads(settings_path.read_text(encoding="utf-8"))["theme"] == "Initial Theme"
    assert all(not path.name.endswith(".tmp") for path in tmp_path.iterdir())


def test_config_manager_rejects_invalid_value_type_and_exposes_native_engine_default(tmp_path):
    manager = ConfigManager(filepath=str(tmp_path / "settings.json"))

    assert manager.get("use_native_engine") is True
    assert manager.set("max_concurrent", "4") is False
    assert manager.get("max_concurrent") == 3


def test_config_manager_hardens_saved_file_permissions(monkeypatch, tmp_path):
    manager = ConfigManager(filepath=str(tmp_path / "settings.json"))
    hardened = []
    monkeypatch.setattr(
        config_manager_module,
        "_harden_config_file_permissions",
        lambda path: hardened.append(str(path)),
    )

    assert manager.set("theme", "Secured Theme") is True
    assert hardened == [str(tmp_path / "settings.json")]


def test_i18n_set_language_from_worker_thread_queues_layout_update(monkeypatch, tmp_path):
    queued_callbacks = []
    monkeypatch.setattr(i18n_module, "LANG_DIR", str(tmp_path))
    monkeypatch.setattr(
        i18n_module,
        "run_on_qt_main_thread",
        lambda callback, *args, **kwargs: queued_callbacks.append((callback, args, kwargs)) or True,
    )

    manager = I18nManager()

    worker = threading.Thread(target=lambda: manager.set_language("en"), name="I18nWorker")
    worker.start()
    worker.join()

    assert manager.current_lang == "en"
    assert manager.tr("Ready") == "Ready"
    assert len(queued_callbacks) == 1

    callback, args, kwargs = queued_callbacks[0]
    callback(*args, **kwargs)


def test_start_app_returns_exec_exit_code_without_raising(monkeypatch):
    events = []

    class _FakeApp:
        _instance = None

        def __init__(self, argv):
            events.append(("app_init", list(argv)))
            _FakeApp._instance = self

        @staticmethod
        def instance():
            return _FakeApp._instance

        def exec(self):
            events.append("exec")
            return 23

    class _FakeWindow:
        def show(self):
            events.append("show")

    monkeypatch.setattr(bootstrap_module, "QApplication", _FakeApp)
    monkeypatch.setattr(bootstrap_module, "install_global_error_handlers", lambda: events.append("install"))

    assert bootstrap_module.start_app(_FakeWindow) == 23
    assert events[1:] == ["install", "show", "exec"]
