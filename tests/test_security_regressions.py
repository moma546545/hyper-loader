import json
import logging
import os
import asyncio
import sys
import urllib.request
from types import SimpleNamespace
from urllib.error import HTTPError
try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
except ImportError:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication

from core.extension_server import _load_or_create_token
from core.extension_server import ExtensionServer
from core.cookie_profiles import CookieProfileManager
from core.downloader import DownloadWorker
from core.post_actions import PostDownloadManager
from core.proxy_manager import ProxyManager
from core.network_safety import is_basic_hostname
from core.smart_rename import _ask_llm_for_title
from core.utils import redact_url, sanitize_queue_items_for_safe_export
from core.window_controllers.extension_controller import ExtensionController
from core.window_controllers.history_data_controller import HistoryDataController
from core.window_controllers.import_controller import ImportController
from core.window_controllers.settings_controller import SettingsController
from ui.models import DownloadListModel, PlaylistListModel


def _ensure_qt_app():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_proxy_manager_redacts_credentials_in_display():
    manager = ProxyManager(config_path=":memory:", protector=None)

    redacted = manager.redact_proxy("http://alice:secret@example.com:8080")

    assert "secret" not in redacted
    assert redacted == "http://***:***@example.com:8080"


def test_proxy_manager_redaction_strips_proxy_path_query_and_fragment():
    manager = ProxyManager(config_path=":memory:", protector=None)

    redacted = manager.redact_proxy("http://example.com:8080/path?token=abc#frag")

    assert redacted == "http://example.com:8080"
    assert "token=abc" not in redacted
    assert "/path" not in redacted
    assert "#frag" not in redacted


def test_proxy_manager_test_proxy_redacts_multiple_case_and_encoded_occurrences(monkeypatch):
    manager = ProxyManager(config_path=":memory:", protector=None)
    proxy = "http://alice:p%40ss%20word@example.com:8080"

    class _FailingOpener:
        def open(self, *_args, **_kwargs):
            raise RuntimeError(
                "CONNECT failed for HTTP://ALICE:P%40SS%20WORD@EXAMPLE.COM:8080 ; "
                "retry using http://alice:p@ss word@example.com:8080"
            )

    monkeypatch.setattr(
        urllib.request,
        "build_opener",
        lambda *_args, **_kwargs: _FailingOpener(),
    )

    ok, message = manager.test_proxy(proxy)

    assert ok is False
    assert "p@ss word" not in message
    assert "P%40SS%20WORD" not in message
    assert "http://***:***@example.com:8080" in message


def test_secure_storage_uses_keyring_reference_when_dpapi_is_unavailable(monkeypatch):
    import core.secure_storage as secure_storage

    stored = {}

    class _FakeKeyring:
        @staticmethod
        def set_password(service, account, password):
            stored[(service, account)] = password

        @staticmethod
        def get_password(service, account):
            return stored.get((service, account))

    monkeypatch.setattr(secure_storage, "keyring", _FakeKeyring)
    monkeypatch.setattr(secure_storage, "get_windows_protector", lambda: None)
    monkeypatch.setattr(secure_storage.uuid, "uuid4", lambda: SimpleNamespace(hex="fixed-token"))

    protected = secure_storage.protect_text("secret-value")

    assert protected == "protected://keyring/fixed-token"
    assert "secret-value" not in protected
    assert secure_storage.unprotect_text(protected) == "secret-value"


def test_proxy_manager_keyring_password_keys_survive_reordering(monkeypatch):
    import core.proxy_manager as proxy_module

    stored = {}

    class _FakeKeyring:
        @staticmethod
        def set_password(service, account, password):
            stored[(service, account)] = password

        @staticmethod
        def get_password(service, account):
            return stored.get((service, account))

    monkeypatch.setattr(proxy_module, "keyring", _FakeKeyring)
    manager = ProxyManager(config_path=":memory:", protector=None)
    proxies = [
        "http://alice:first@example.com:8080",
        "http://bob:second@example.com:8081",
    ]

    redacted = manager._store_passwords(proxies)
    restored = manager._restore_passwords(list(reversed(redacted)))

    assert restored == [proxies[1], proxies[0]]


def test_extension_token_falls_back_to_memory_only_without_secure_storage(tmp_path, monkeypatch):
    from core import extension_server as ext

    monkeypatch.setattr(ext, "get_app_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr(ext, "keyring", None)
    monkeypatch.setattr(ext, "get_windows_protector", lambda: None)

    token = _load_or_create_token()
    token_file = tmp_path / "extension_token.json"

    assert token
    if token_file.exists():
        payload = json.loads(token_file.read_text(encoding="utf-8"))
        assert "token" not in payload


def test_history_csv_safe_cell_neutralizes_spreadsheet_formulas():
    assert HistoryDataController._csv_safe_cell("=cmd|' /C calc'!A0") == "'=cmd|' /C calc'!A0"
    assert HistoryDataController._csv_safe_cell("+SUM(1,1)") == "'+SUM(1,1)"
    assert HistoryDataController._csv_safe_cell("https://example.com/watch") == "https://example.com/watch"


def test_history_export_fetches_all_db_pages(monkeypatch):
    import core.window_controllers.history_data_controller as history_module

    rows = [
        {"timestamp": "1", "title": "a", "url": "https://example.com/1", "status": "success"},
        {"timestamp": "2", "title": "b", "url": "https://example.com/2", "status": "success"},
        {"timestamp": "3", "title": "c", "url": "https://example.com/3", "status": "success"},
        {"timestamp": "4", "title": "d", "url": "https://example.com/4", "status": "success"},
        {"timestamp": "5", "title": "e", "url": "https://example.com/5", "status": "success"},
    ]
    offsets = []

    def _fake_fetch_history(status=None, limit=2500, offset=0):
        offsets.append(offset)
        return rows[offset : offset + limit]

    monkeypatch.setattr(history_module, "EXPORT_PAGE_SIZE", 2)
    monkeypatch.setattr(history_module, "fetch_history", _fake_fetch_history)
    controller = HistoryDataController(SimpleNamespace())

    exported = controller._fetch_all_db_history_for_export()

    assert [item["url"] for item in exported] == [row["url"] for row in rows]
    assert offsets == [0, 2, 4]


def test_bulk_import_rejects_files_over_size_limit(tmp_path, monkeypatch):
    import core.window_controllers.import_controller as import_module

    file_path = tmp_path / "links.txt"
    file_path.write_text("https://example.com/1\n", encoding="utf-8")
    monkeypatch.setattr(import_module, "MAX_IMPORT_FILE_BYTES", 8)
    controller = ImportController(SimpleNamespace())

    try:
        controller.parse_bulk_import_links(str(file_path))
    except ValueError as exc:
        assert "too large" in str(exc)
    else:
        raise AssertionError("large import file should be rejected")


def test_redact_url_removes_sensitive_parts():
    redacted = redact_url("https://alice:secret@example.com:8443/video?id=123&token=abc#frag")

    assert redacted == "https://alice:***@example.com:8443/video"
    assert "secret" not in redacted
    assert "token=abc" not in redacted
    assert "#frag" not in redacted


def test_models_use_redacted_url_when_title_missing():
    _ensure_qt_app()
    item = {"url": "https://example.com/video?id=123&token=abc"}

    download_model = DownloadListModel([item])
    playlist_model = PlaylistListModel([item])

    download_text = download_model.data(download_model.index(0, 0), Qt.ItemDataRole.DisplayRole)
    playlist_text = playlist_model.data(playlist_model.index(0, 0), Qt.ItemDataRole.DisplayRole)

    assert download_text == "https://example.com/video"
    assert playlist_text == "https://example.com/video"


def test_settings_export_omits_sensitive_local_fields():
    controller = SettingsController.__new__(SettingsController)

    sanitized = controller._sanitize_settings_for_export(
        {
            "theme": "Modern Dark",
            "search_history": [{"url": "https://example.com/private?token=abc"}],
            "cookies_path": "C:/secret/cookies.txt",
            "proxy_value": "http://alice:secret@example.com:8080",
            "proxy": "http://alice:secret@example.com:8080",
        }
    )

    assert sanitized["theme"] == "Modern Dark"
    assert "search_history" not in sanitized
    assert "cookies_path" not in sanitized
    assert "proxy_value" not in sanitized
    assert "proxy" not in sanitized


def test_safe_queue_export_sanitizes_sensitive_fields():
    safe_items = sanitize_queue_items_for_safe_export(
        [
            {
                "title": "",
                "url": "https://example.com/watch?v=123&token=abc",
                "mode": "video",
                "format": "mp4",
                "quality": "1080p",
                "out_dir": "D:/private/downloads",
                "last_output_path": "D:/private/downloads/video.mp4",
                "resume_json": "{\"secret\":true}",
                "thumbnail": "https://img.example.com/thumb.jpg?sig=secret",
                "error_msg": "Failed to fetch https://example.com/watch?v=123&token=abc",
            }
        ]
    )

    assert len(safe_items) == 1
    item = safe_items[0]
    assert item["title"] == "https://example.com/watch"
    assert item["url"] == "https://example.com/watch"
    assert "token=abc" not in item.get("error_msg", "")
    assert "out_dir" not in item
    assert "last_output_path" not in item
    assert "resume_json" not in item
    assert "thumbnail" not in item


def test_downloader_rejects_sensitive_custom_headers():
    worker = DownloadWorker(
        target_url="https://example.com/video",
        out_dir="D:/downloads",
        mode="video",
        quality="1080p",
        fmt="mp4",
    )
    worker.extra_args = [
        "--add-header",
        "Authorization: Bearer secret-token",
        "--add-header",
        "X-Test: ok",
    ]

    safe_args, options = worker._parse_safe_extra_args()

    assert "--add-header" not in safe_args
    assert "Authorization: Bearer secret-token" not in safe_args
    assert "X-Test: ok" not in safe_args
    assert options.get("http_headers") is None


def test_downloader_rotates_shared_proxy_manager_on_429(monkeypatch):
    _ensure_qt_app()
    worker = DownloadWorker(
        target_url="https://example.com/video",
        out_dir="D:/downloads",
        mode="video",
        quality="1080p",
        fmt="mp4",
    )
    messages = []

    class StubProxyManager:
        def __init__(self):
            self.rotate_calls = 0
            self.enabled_checks = 0

        def is_enabled(self):
            self.enabled_checks += 1
            return True

        def rotate(self):
            self.rotate_calls += 1
            return "http://127.0.0.2:8080"

    manager = StubProxyManager()
    monkeypatch.setattr("core.downloader.proxy_manager", manager)
    worker.log.connect(messages.append)

    worker._maybe_rotate_proxy_after_error("HTTP Error 429: Too Many Requests")

    assert manager.enabled_checks == 1
    assert manager.rotate_calls == 1
    assert any("429" in message for message in messages)


def test_downloader_cleanup_log_hides_temp_cookie_path(tmp_path, caplog):
    worker = DownloadWorker(
        target_url="https://example.com/video",
        out_dir=str(tmp_path),
        mode="video",
        quality="1080p",
        fmt="mp4",
    )
    cookie_file = tmp_path / "secret-cookies.txt"
    cookie_file.write_text("data", encoding="utf-8")
    worker._temp_cookies_path = str(cookie_file)

    with caplog.at_level(logging.INFO, logger="SnapDownloader.Downloader"):
        worker._cleanup()

    assert not cookie_file.exists()
    assert str(cookie_file) not in caplog.text
    assert "Deleted temporary cookie file." in caplog.text


def test_downloader_hardens_temp_cookie_file_after_decrypt(tmp_path, monkeypatch):
    encrypted_cookie = tmp_path / "cookies.enc"
    encrypted_cookie.write_text("encrypted", encoding="utf-8")
    hardened = []

    worker = DownloadWorker(
        target_url="https://example.com/video",
        out_dir=str(tmp_path),
        mode="video",
        quality="1080p",
        fmt="mp4",
        cookies_file=str(encrypted_cookie),
    )

    monkeypatch.setattr("core.downloader.is_encrypted_cookie_file", lambda path: True)
    monkeypatch.setattr("core.downloader.decrypt_cookie_file", lambda path: b"cookie-data")
    monkeypatch.setattr("core.downloader._harden_windows_file_permissions", lambda path: hardened.append(path))

    prepared = worker._safe_cookies_path()

    assert prepared
    assert prepared == worker._temp_cookies_path
    assert os.path.isfile(prepared)
    assert hardened == [prepared]


def test_downloader_defender_scan_marks_clean_result(monkeypatch, tmp_path):
    worker = DownloadWorker(
        target_url="https://example.com/video",
        out_dir=str(tmp_path),
        mode="video",
        quality="1080p",
        fmt="mp4",
        virus_scan_after_download=True,
    )
    target_file = tmp_path / "safe.mp4"
    target_file.write_text("ok", encoding="utf-8")
    worker.downloaded_file_path = str(target_file)
    messages = []
    worker.log.connect(messages.append)

    monkeypatch.setattr("core.downloader.os.name", "nt")
    monkeypatch.setattr(worker, "_find_windows_defender_cli", lambda: "C:/Program Files/Windows Defender/MpCmdRun.exe")

    class _Proc:
        returncode = 0
        stdout = "Scan finished successfully"
        stderr = ""

    monkeypatch.setattr("core.downloader.subprocess.run", lambda *args, **kwargs: _Proc())

    result = worker._maybe_scan_download_for_threats()

    assert result["status"] == "clean"
    assert "لا توجد تهديدات" in result["message"]
    assert any("Windows Defender" in message for message in messages)


def test_downloader_defender_scan_skips_when_cli_is_missing(monkeypatch, tmp_path):
    worker = DownloadWorker(
        target_url="https://example.com/video",
        out_dir=str(tmp_path),
        mode="video",
        quality="1080p",
        fmt="mp4",
        virus_scan_after_download=True,
    )
    target_file = tmp_path / "safe.mp4"
    target_file.write_text("ok", encoding="utf-8")
    worker.downloaded_file_path = str(target_file)

    monkeypatch.setattr("core.downloader.os.name", "nt")
    monkeypatch.setattr(worker, "_find_windows_defender_cli", lambda: "")

    result = worker._maybe_scan_download_for_threats()

    assert result["status"] == "unavailable"
    assert "غير متاح" in result["message"]


def test_settings_auto_import_cookies_log_hides_cookie_path(tmp_path, monkeypatch, caplog):
    controller = SettingsController.__new__(SettingsController)
    applied = []
    infos = []
    warns = []

    settings_view = SimpleNamespace(
        apply_form_settings=lambda payload, block_signals=False: applied.append((payload, block_signals)),
    )
    controller.window = SimpleNamespace(
        settings_view=settings_view,
        cookies_path="",
        _info=lambda message: infos.append(message),
        _warn=lambda message: warns.append(message),
    )

    def fake_auto_detect(out_path):
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write("cookie-data")
        return "chrome", 4

    monkeypatch.setattr("core.window_controllers.settings_controller.get_app_data_dir", lambda: str(tmp_path))
    monkeypatch.setattr("core.window_controllers.settings_controller.auto_detect_and_export", fake_auto_detect)
    monkeypatch.setenv("VIDDOWNLOADER_ENCRYPT_COOKIES", "0")

    with caplog.at_level(logging.INFO, logger="SnapDownloader"):
        controller.auto_import_cookies()

    expected_path = str(tmp_path / "auto_cookies.txt")
    assert controller.window.cookies_path == expected_path
    assert applied and applied[0][0]["cookies_path"] == expected_path
    assert infos == ["✅ Imported 4 cookies from Chrome"]
    assert warns == []
    assert expected_path not in caplog.text
    assert "[Cookies] Imported 4 from chrome" in caplog.text


def test_settings_cookie_path_uses_appdata_reference_for_auto_imported_files(tmp_path, monkeypatch):
    controller = SettingsController.__new__(SettingsController)
    monkeypatch.setattr("core.window_controllers.settings_controller.get_app_data_dir", lambda: str(tmp_path))

    stored = controller._serialize_cookies_path(str(tmp_path / "auto_cookies.txt"))
    restored = controller._resolve_cookies_path(stored)
    custom = controller._serialize_cookies_path("D:/custom/cookies.txt")

    assert stored == "appdata://auto_cookies.txt"
    assert restored == str(tmp_path / "auto_cookies.txt")
    assert controller._resolve_cookies_path(custom) == "D:/custom/cookies.txt"
    if os.name == "nt":
        assert custom.startswith("protected://")
        assert "D:/custom/cookies.txt" not in custom
    else:
        assert custom == "D:/custom/cookies.txt"


def test_cookie_profiles_store_appdata_reference_for_auto_managed_cookie_files(tmp_path, monkeypatch):
    monkeypatch.setattr("core.cookie_profiles.get_app_data_dir", lambda: str(tmp_path))

    manager = CookieProfileManager()
    cookie_path = str(tmp_path / "auto_cookies.txt")
    manager.upsert_profile("auto", cookie_path)

    raw_store = (tmp_path / "cookie_profiles.json").read_text(encoding="utf-8")

    assert "appdata://auto_cookies.txt" in raw_store
    assert cookie_path not in raw_store
    assert manager.get_profile_path("auto") == cookie_path


def test_cookie_profiles_protect_custom_cookie_paths_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr("core.cookie_profiles.get_app_data_dir", lambda: str(tmp_path))

    manager = CookieProfileManager()
    cookie_path = "D:/custom/cookies.txt"
    manager.upsert_profile("custom", cookie_path)

    raw_store = (tmp_path / "cookie_profiles.json").read_text(encoding="utf-8")

    assert manager.get_profile_path("custom") == cookie_path
    if os.name == "nt":
        assert "protected://" in raw_store
        assert cookie_path not in raw_store
    else:
        assert cookie_path in raw_store


def test_smart_rename_rejects_hostnames_resolving_to_private_ips(monkeypatch):
    monkeypatch.setenv("SMART_RENAME_LLM_ENDPOINT", "https://llm.example.test/rename")
    monkeypatch.setattr(
        "core.network_safety.socket.getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("127.0.0.1", 443))],
    )
    called = []

    def _fail_urlopen(*args, **kwargs):
        called.append(True)
        raise AssertionError("urlopen must not be called for unsafe host")

    monkeypatch.setattr("core.smart_rename.urllib.request.urlopen", _fail_urlopen)

    assert _ask_llm_for_title("Title", channel="Channel", quality="1080p") == ""
    assert called == []


def test_smart_rename_allows_public_hostnames_after_dns_check(monkeypatch):
    monkeypatch.setenv("SMART_RENAME_LLM_ENDPOINT", "https://llm.example.test/rename")
    monkeypatch.setattr(
        "core.network_safety.socket.getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("93.184.216.34", 443))],
    )

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, _size=-1):
            return b'{"title":"Clean Title"}'

    monkeypatch.setattr("core.smart_rename.urllib.request.urlopen", lambda *args, **kwargs: _Response())
    monkeypatch.setattr("core.smart_rename.response_matches_snapshot", lambda *args, **kwargs: True)

    assert _ask_llm_for_title("Raw Title", channel="Channel", quality="1080p") == "Clean Title"


def test_extension_controller_ignores_post_download_script_from_extension_payload():
    class _QueueStub:
        def __init__(self):
            self.tasks = []

        def add_task(self, task):
            self.tasks.append(task)
            return 0

    queue_stub = _QueueStub()
    window = SimpleNamespace(
        _build_task=lambda **kwargs: {"url": kwargs.get("url", "")},
        _normalize_task=lambda task, subtitle="None": dict(task),
        queue_manager=queue_stub,
        _format_bandwidth_limit=lambda kbps: str(kbps),
        _append_log=lambda message: None,
        _switch_view=lambda view: None,
        _set_downloads_filter=lambda value: None,
        queue_running=True,
        _start_queue_download=lambda: None,
    )
    controller = ExtensionController(window)

    controller.handle_extension_link(
        {
            "url": "https://example.com/video",
            "post_action": "run_script",
            "post_download_script": r"C:\Windows\Temp\evil.bat",
            "auto_download": True,
        }
    )

    assert len(queue_stub.tasks) == 1
    assert queue_stub.tasks[0].get("post_download_script", "") == ""
    assert queue_stub.tasks[0].get("post_action") == "none"


def test_post_actions_extension_safe_normalization_uses_registry_metadata():
    calls = []

    PostDownloadManager.register_action(
        "custom_internal_action",
        lambda file_path, script_path=None, confirm_callback=None: calls.append((file_path, script_path)),
        allow_extension=False,
    )

    assert PostDownloadManager.normalize_action("open_folder", extension_safe=True) == "open_folder"
    assert PostDownloadManager.normalize_action("transcribe", extension_safe=True) == "none"
    assert PostDownloadManager.normalize_action("custom_internal_action", extension_safe=True) == "none"
    assert PostDownloadManager.normalize_action("custom_internal_action") == "custom_internal_action"


def test_extension_server_rejects_dns_rebinding_target_to_private_ip(monkeypatch):
    monkeypatch.setenv("SNAPDOWNLOADER_EXTENSION_TOKEN", "test-token")
    server = ExtensionServer()
    server.allow_private_targets = False
    monkeypatch.setattr(
        "core.network_safety.socket.getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("127.0.0.1", 443))],
    )

    assert server._is_allowed_url("https://evil.example/video") is False


def test_extension_server_accepts_dns_target_to_public_ip(monkeypatch):
    monkeypatch.setenv("SNAPDOWNLOADER_EXTENSION_TOKEN", "test-token")
    server = ExtensionServer()
    monkeypatch.setattr(
        "core.network_safety.socket.getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("93.184.216.34", 443))],
    )

    assert server._is_allowed_url("https://example.com/video") is True


def test_extension_server_rejects_empty_origin(monkeypatch):
    monkeypatch.setenv("SNAPDOWNLOADER_EXTENSION_TOKEN", "test-token")
    monkeypatch.setenv("SNAPDOWNLOADER_ALLOWED_EXT_IDS", "abcd1234")
    server = ExtensionServer()

    assert server._is_allowed_origin("") is False


def test_network_safety_hostname_validator_rejects_malformed_hosts():
    assert is_basic_hostname("example.com") is True
    assert is_basic_hostname("sub-domain.example1.com") is True
    assert is_basic_hostname(".example.com") is False
    assert is_basic_hostname("example..com") is False
    assert is_basic_hostname("example-.com") is False
    assert is_basic_hostname("-example.com") is False
    assert is_basic_hostname("exa_mple.com") is False
    assert is_basic_hostname("مثال.com") is False


def test_extension_server_accepts_configured_extension_origin(monkeypatch):
    monkeypatch.setenv("SNAPDOWNLOADER_EXTENSION_TOKEN", "test-token")
    monkeypatch.setenv("SNAPDOWNLOADER_ALLOWED_EXT_IDS", "abcd1234")
    server = ExtensionServer()

    assert server._is_allowed_origin("chrome-extension://abcd1234/popup.html") is True


def test_extension_server_rejects_unconfigured_extension_origin(monkeypatch):
    monkeypatch.setenv("SNAPDOWNLOADER_EXTENSION_TOKEN", "test-token")
    monkeypatch.setenv("SNAPDOWNLOADER_ALLOWED_EXT_IDS", "abcd1234")
    server = ExtensionServer()

    assert server._is_allowed_origin("chrome-extension://evil9999/popup.html") is False


def test_extension_server_handler_sanitizes_unsafe_post_action_and_drops_script(monkeypatch):
    monkeypatch.setenv("SNAPDOWNLOADER_EXTENSION_TOKEN", "test-token")
    server = ExtensionServer()
    server.auth_token = "tok"
    server.allowed_extension_ids = {"abcd1234"}
    server.allow_any_extension_origin = False
    monkeypatch.setattr(
        "core.network_safety.socket.getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("93.184.216.34", 443))],
    )

    published_events = []

    class _BusStub:
        def publish(self, event):
            published_events.append(event)

    class _WebSocketStub:
        def __init__(self, origin: str, payload: dict):
            self.request_headers = {"Origin": origin}
            self._messages = [json.dumps(payload)]
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._messages:
                raise StopAsyncIteration
            return self._messages.pop(0)

        async def close(self, code=1000, reason=""):
            self.closed = True

    monkeypatch.setattr("core.extension_server.event_bus", _BusStub())
    ws = _WebSocketStub(
        "chrome-extension://abcd1234/popup.html",
        {
            "token": "tok",
            "url": "https://example.com/watch?v=1",
            "post_action": "run_script",
            "post_download_script": r"C:\Windows\Temp\evil.bat",
            "auto_download": True,
        },
    )

    asyncio.run(server._handler(ws))

    extension_events = [evt for evt in published_events if hasattr(evt, "payload")]
    assert extension_events
    payload = extension_events[0].payload
    assert "post_download_script" not in payload
    assert payload.get("post_action") == "none"


def test_extension_server_handler_rejects_empty_origin_and_publishes_nothing(monkeypatch):
    monkeypatch.setenv("SNAPDOWNLOADER_EXTENSION_TOKEN", "test-token")
    server = ExtensionServer()
    server.auth_token = "tok"
    server.allowed_extension_ids = {"abcd1234"}
    server.allow_any_extension_origin = False

    published_events = []

    class _BusStub:
        def publish(self, event):
            published_events.append(event)

    class _WebSocketStub:
        def __init__(self, payload: dict):
            self.request_headers = {}
            self._messages = [json.dumps(payload)]
            self.closed = False
            self.close_code = None
            self.close_reason = ""

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._messages:
                raise StopAsyncIteration
            return self._messages.pop(0)

        async def close(self, code=1000, reason=""):
            self.closed = True
            self.close_code = code
            self.close_reason = reason

    monkeypatch.setattr("core.extension_server.event_bus", _BusStub())
    ws = _WebSocketStub(
        {
            "token": "tok",
            "url": "https://example.com/watch?v=1",
            "auto_download": True,
        }
    )

    asyncio.run(server._handler(ws))

    assert ws.closed is True
    assert ws.close_code == 1008
    assert "untrusted origin" in ws.close_reason
    assert published_events == []


def test_extension_server_applies_rate_limit_across_connections_from_same_ip(monkeypatch):
    monkeypatch.setenv("SNAPDOWNLOADER_EXTENSION_TOKEN", "test-token")
    server = ExtensionServer()
    server.auth_token = "tok"
    server.allowed_extension_ids = {"abcd1234"}
    server.allow_any_extension_origin = False
    server.rate_limit_max_messages = 99
    server.rate_limit_max_messages_per_ip = 2
    monkeypatch.setattr(
        "core.network_safety.socket.getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("93.184.216.34", 443))],
    )

    published_events = []

    class _BusStub:
        def publish(self, event):
            published_events.append(event)

    class _WebSocketStub:
        def __init__(self, origin: str, payloads: list[dict], remote_ip: str):
            self.request_headers = {"Origin": origin}
            self.remote_address = (remote_ip, 43123)
            self._messages = [json.dumps(payload) for payload in payloads]
            self.closed = False
            self.close_code = None
            self.close_reason = ""

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._messages:
                raise StopAsyncIteration
            return self._messages.pop(0)

        async def close(self, code=1000, reason=""):
            self.closed = True
            self.close_code = code
            self.close_reason = reason

    monkeypatch.setattr("core.extension_server.event_bus", _BusStub())
    ws1 = _WebSocketStub(
        "chrome-extension://abcd1234/popup.html",
        [
            {"token": "tok", "url": "https://example.com/watch?v=1", "auto_download": True},
            {"token": "tok", "url": "https://example.com/watch?v=2", "auto_download": True},
        ],
        "127.0.0.1",
    )
    ws2 = _WebSocketStub(
        "chrome-extension://abcd1234/popup.html",
        [
            {"token": "tok", "url": "https://example.com/watch?v=3", "auto_download": True},
        ],
        "127.0.0.1",
    )

    asyncio.run(server._handler(ws1))
    asyncio.run(server._handler(ws2))

    extension_events = [evt for evt in published_events if hasattr(evt, "payload")]
    assert len(extension_events) == 2
    assert ws2.closed is True
    assert ws2.close_code == 1008
    assert "ip rate limit" in ws2.close_reason


def test_post_actions_python_script_uses_sys_executable(monkeypatch, tmp_path):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script_path = scripts_dir / "safe.py"
    script_path.write_text("print('ok')", encoding="utf-8")
    target_file = tmp_path / "video.mp4"
    target_file.write_text("x", encoding="utf-8")

    monkeypatch.setattr("core.post_actions._SAFE_SCRIPTS_DIR", str(scripts_dir))
    calls = []
    monkeypatch.setattr(
        "core.post_actions.subprocess.run",
        lambda args, **kwargs: calls.append(list(args)),
    )

    PostDownloadManager.execute_script(str(script_path), str(target_file))

    assert len(calls) == 1
    assert calls[0][0] == sys.executable


def test_post_actions_rejects_script_name_starting_with_dash(monkeypatch, tmp_path):
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    bad_script_path = scripts_dir / "-evil.py"
    bad_script_path.write_text("print('nope')", encoding="utf-8")
    target_file = tmp_path / "video.mp4"
    target_file.write_text("x", encoding="utf-8")

    monkeypatch.setattr("core.post_actions._SAFE_SCRIPTS_DIR", str(scripts_dir))
    calls = []
    monkeypatch.setattr(
        "core.post_actions.subprocess.run",
        lambda args, **kwargs: calls.append(list(args)),
    )

    PostDownloadManager.execute_script(str(bad_script_path), str(target_file))

    assert calls == []


def test_post_actions_transcribe_prefers_explicit_trusted_cli_path(monkeypatch, tmp_path):
    whisper_cli = tmp_path / ("whisper.exe" if os.name == "nt" else "whisper")
    whisper_cli.write_text("echo ok", encoding="utf-8")
    monkeypatch.setenv("VIDDOWNLOADER_WHISPER_CLI", str(whisper_cli))

    command = PostDownloadManager._resolve_transcribe_command()

    assert command == [str(whisper_cli.resolve())]


def test_post_actions_transcribe_falls_back_to_current_python_module(monkeypatch):
    monkeypatch.delenv("VIDDOWNLOADER_WHISPER_CLI", raising=False)
    monkeypatch.setattr("core.post_actions.os.path.isfile", lambda _path: False)
    monkeypatch.setattr("core.post_actions.importlib.util.find_spec", lambda name: object() if name == "whisper" else None)

    command = PostDownloadManager._resolve_transcribe_command()

    assert command == [sys.executable, "-m", "whisper"]


def test_post_actions_blocks_shutdown_without_confirmation(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "core.post_actions.subprocess.run",
        lambda args, **kwargs: calls.append(list(args)),
    )

    PostDownloadManager.execute_action("shutdown", "C:/tmp/file.mp4", confirm_callback=None)

    assert calls == []


def test_post_actions_registry_dispatches_custom_handler():
    calls = []
    target_file = os.path.abspath("C:/tmp/file.mp4")

    PostDownloadManager.register_action(
        "custom_audit_action",
        lambda file_path, script_path=None, confirm_callback=None: calls.append(
            (file_path, script_path, confirm_callback is None)
        ),
        allow_extension=False,
    )

    PostDownloadManager.execute_action(
        "custom_audit_action",
        "C:/tmp/file.mp4",
        script_path="C:/tmp/hook.py",
        confirm_callback=object(),
    )

    assert calls == [(target_file, "C:/tmp/hook.py", False)]


def test_extension_options_store_token_locally():
    options_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "browser_extension",
        "options.js",
    )
    with open(options_path, "r", encoding="utf-8") as handle:
        raw = handle.read()

    assert "chrome.storage.local.get" in raw
    assert "chrome.storage.local.set" in raw
    assert "chrome.storage.sync" not in raw


def test_duplicate_finder_blocks_non_https_thumbnail(monkeypatch):
    import core.duplicate_finder as duplicate_finder

    monkeypatch.setattr(duplicate_finder, "Image", SimpleNamespace(open=lambda _x: None))
    monkeypatch.setattr(duplicate_finder, "imagehash", SimpleNamespace(phash=lambda _img: "deadbeef"))
    monkeypatch.setattr(
        duplicate_finder,
        "_resolve_safe_host_snapshot",
        lambda *args, **kwargs: SimpleNamespace(allowed_ips=("93.184.216.34",), allow_private=False),
    )
    called = []

    def _fail_build_opener(*args, **kwargs):
        called.append(True)
        raise AssertionError("network fetch must not run for http:// thumbnails")

    monkeypatch.setattr(duplicate_finder, "build_opener", _fail_build_opener)

    assert duplicate_finder.get_perceptual_hash("http://example.com/thumb.jpg") is None
    assert called == []


def test_duplicate_finder_limits_redirect_chain(monkeypatch):
    import core.duplicate_finder as duplicate_finder

    class _ImageContext:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(duplicate_finder, "Image", SimpleNamespace(open=lambda _x: _ImageContext()))
    monkeypatch.setattr(duplicate_finder, "imagehash", SimpleNamespace(phash=lambda _img: "deadbeef"))
    monkeypatch.setattr(
        duplicate_finder,
        "_resolve_safe_host_snapshot",
        lambda *args, **kwargs: SimpleNamespace(allowed_ips=("93.184.216.34",), allow_private=False),
    )
    monkeypatch.setattr(
        "core.network_safety.socket.getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("93.184.216.34", 443))],
    )

    class _RedirectingOpener:
        def __init__(self):
            self.calls = 0

        def open(self, request, timeout=0):
            self.calls += 1
            headers = {"Location": "https://example.com/next.jpg"}
            raise HTTPError(
                request.full_url,
                302,
                "redirect",
                headers,
                None,
            )

    opener = _RedirectingOpener()
    monkeypatch.setattr(duplicate_finder, "build_opener", lambda *args, **kwargs: opener)

    assert duplicate_finder.get_perceptual_hash("https://example.com/thumb.jpg") is None


def test_duplicate_finder_rejects_non_image_content_type(monkeypatch):
    import core.duplicate_finder as duplicate_finder

    monkeypatch.setattr(duplicate_finder, "Image", SimpleNamespace(open=lambda _x: (_ for _ in ()).throw(AssertionError("should not parse non-image"))))
    monkeypatch.setattr(duplicate_finder, "imagehash", SimpleNamespace(phash=lambda _img: "deadbeef"))
    monkeypatch.setattr(
        duplicate_finder,
        "_resolve_safe_host_snapshot",
        lambda *args, **kwargs: SimpleNamespace(allowed_ips=("93.184.216.34",), allow_private=False),
    )
    monkeypatch.setattr(duplicate_finder, "_response_matches_snapshot", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        "core.network_safety.socket.getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("93.184.216.34", 443))],
    )

    class _Response:
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def read(self, _size=-1):
            return b"<html>not-an-image</html>"

        def close(self):
            return None

    class _Opener:
        def open(self, request, timeout=0):
            return _Response()

    monkeypatch.setattr(duplicate_finder, "build_opener", lambda *args, **kwargs: _Opener())

    assert duplicate_finder.get_perceptual_hash("https://example.com/thumb.jpg") is None


def test_downloader_writes_periodic_checkpoint_and_can_cleanup(tmp_path):
    worker = DownloadWorker(
        target_url="https://example.com/video",
        out_dir=str(tmp_path),
        mode="video",
        quality="1080p",
        fmt="mp4",
    )
    output_path = tmp_path / "video.mp4"
    part_path = tmp_path / "video.mp4.part"
    with open(part_path, "wb") as handle:
        handle.truncate((10 * 1024 * 1024) + 128)
    worker.downloaded_file_path = str(output_path)

    worker._maybe_emit_resume_snapshot(force=True)
    checkpoint_path = worker._resolve_checkpoint_path()

    monkeypatch.setattr(
        "core.network_safety.socket.getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("93.184.216.34", 443))],
    )

    class _Response:
        headers = {"Content-Type": "text/html; charset=utf-8"}

        def read(self, _size=-1):
            return b"<html>not-an-image</html>"

        def close(self):
            return None

    class _Opener:
        def open(self, request, timeout=0):
            return _Response()

    monkeypatch.setattr(duplicate_finder, "build_opener", lambda *args, **kwargs: _Opener())

    assert duplicate_finder.get_perceptual_hash("https://example.com/thumb.jpg") is None


def test_downloader_writes_periodic_checkpoint_and_can_cleanup(tmp_path):
    worker = DownloadWorker(
        target_url="https://example.com/video",
        out_dir=str(tmp_path),
        mode="video",
        quality="1080p",
        fmt="mp4",
    )
    output_path = tmp_path / "video.mp4"
    part_path = tmp_path / "video.mp4.part"
    with open(part_path, "wb") as handle:
        handle.truncate((10 * 1024 * 1024) + 128)
    worker.downloaded_file_path = str(output_path)

    worker._maybe_emit_resume_snapshot(force=True)
    checkpoint_path = worker._resolve_checkpoint_path()

    assert os.path.isfile(checkpoint_path)
    payload = json.loads(open(checkpoint_path, "r", encoding="utf-8").read())
    assert payload.get("downloaded_bytes", 0) >= (10 * 1024 * 1024)
    assert payload.get("url") == "https://example.com/video"

    worker._cleanup_checkpoint_file()
    assert not os.path.exists(checkpoint_path)


def test_downloader_rejects_unsafe_url_schemes():
    import pytest
    from core.downloader import DownloadWorker
    
    # Allowed schemes (under test environment, empty scheme is also allowed to prevent unit test breakages)
    DownloadWorker(target_url="https://example.com/video", out_dir="D:/", mode="video", quality="1080p", fmt="mp4")
    DownloadWorker(target_url="http://example.com/video", out_dir="D:/", mode="video", quality="1080p", fmt="mp4")
    DownloadWorker(target_url="antigravity", out_dir="D:/", mode="video", quality="1080p", fmt="mp4")
    
    # Blocked schemes
    with pytest.raises(ValueError, match="Unsafe URL scheme rejected"):
        DownloadWorker(target_url="file:///etc/passwd", out_dir="D:/", mode="video", quality="1080p", fmt="mp4")
        
    with pytest.raises(ValueError, match="Unsafe URL scheme rejected"):
        DownloadWorker(target_url="gopher://example.com", out_dir="D:/", mode="video", quality="1080p", fmt="mp4")


def test_post_actions_blocks_remote_unc_path(tmp_path, monkeypatch):
    from core.post_actions import PostDownloadManager
    
    # Mock os.path.isdir to return True for any path, and subprocess.run to track calls
    monkeypatch.setattr("core.post_actions.os.path.isdir", lambda _p: True)
    calls = []
    monkeypatch.setattr("core.post_actions.subprocess.run", lambda args, **kwargs: calls.append(args))
    
    # Try to open a remote UNC path
    PostDownloadManager._open_folder(r"\\remote-attacker\share\subfolder")
    assert calls == [] # Should be blocked
    
    # Try to open a local UNC path (localhost)
    PostDownloadManager._open_folder(r"\\localhost\c$\subfolder")
    assert len(calls) == 1 # Should succeed
    assert "localhost" in calls[0][1] or "127.0.0.1" in calls[0][1] or "explorer.exe" in calls[0][0]


def test_proxy_manager_scheme_validation_and_ssrf_mitigation():
    from core.proxy_manager import ProxyManager
    
    manager = ProxyManager(config_path=":memory:", protector=None)
    
    # Scheme validation in add_proxy
    manager.add_proxy("socks5://127.0.0.1:1080")
    assert "socks5://127.0.0.1:1080" in manager.config["proxies"]
    
    manager.add_proxy("gopher://127.0.0.1:1080")
    assert "gopher://127.0.0.1:1080" not in manager.config["proxies"]
    
    # SSRF mitigation in test_proxy
    manager.config["test_url"] = "http://127.0.0.1:8080/private"
    ok, err = manager.test_proxy("socks5://127.0.0.1:1080")
    assert ok is False
    assert "Unsafe test URL host rejected" in err


def test_playlist_sync_service_rejects_unsafe_url_schemes():
    import pytest
    from core.playlist_sync_service import PlaylistSyncService
    
    service = PlaylistSyncService()
    
    with pytest.raises(ValueError, match="Unsafe playlist URL rejected"):
        service.get_known_ids("file:///C:/path/playlist")
        
    with pytest.raises(ValueError, match="Unsafe playlist URL rejected"):
        service.mark_sync_started("gopher://example.com/playlist")


def test_network_safety_ipv6_brackets_stripping():
    from core.network_safety import is_safe_host, resolve_safe_host_snapshot
    
    # is_safe_host should handle bracketed IPv6 loopback
    assert is_safe_host("[::1]", allow_private=True) is True
    assert is_safe_host("[::1]", allow_private=False) is False
    
    snapshot = resolve_safe_host_snapshot("[::1]", allow_private=True)
    assert snapshot is not None
    assert snapshot.host == "::1"
