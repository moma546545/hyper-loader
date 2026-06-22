import sys

import core.windows_toast as windows_toast


def test_build_powershell_toast_script_escapes_xml_content():
    script = windows_toast.build_powershell_toast_script(
        "Title <One>",
        "Body & details",
        app_id="Vid'Downloader",
    )

    assert "&lt;One&gt;" in script
    assert "Body &amp; details" in script
    assert "CreateToastNotifier('Vid''Downloader')" in script


def test_show_native_toast_uses_powershell_fallback(monkeypatch):
    calls = []

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(windows_toast, "_dispatch_win11toast", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        windows_toast,
        "_dispatch_powershell_toast",
        lambda title, message, app_id="VidDownloader": calls.append((title, message, app_id)) or True,
    )

    assert windows_toast.show_native_toast("Done", "Queue finished") is True
    assert calls == [("Done", "Queue finished", "VidDownloader")]


def test_show_native_toast_returns_false_for_empty_payload(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    assert windows_toast.show_native_toast("", "message") is False
    assert windows_toast.show_native_toast("title", "") is False
