import logging
import os
import subprocess
import sys
import threading
from xml.sax.saxutils import escape


logger = logging.getLogger("SnapDownloader.WindowsToast")
_DEFAULT_APP_ID = "VidDownloader"


def _powershell_path() -> str:
    if os.name != "nt":
        return "powershell"
    system_root = str(os.environ.get("SystemRoot", r"C:\Windows")).strip() or r"C:\Windows"
    candidate = os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return candidate if os.path.isfile(candidate) else "powershell"


def build_powershell_toast_script(title: str, message: str, app_id: str = _DEFAULT_APP_ID) -> str:
    safe_title = escape(str(title or "").strip())
    safe_message = escape(str(message or "").strip())
    safe_app_id = str(app_id or _DEFAULT_APP_ID).strip().replace("'", "''") or _DEFAULT_APP_ID
    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null",
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null",
            '$toastXml = @"',
            "<toast>",
            "  <visual>",
            '    <binding template="ToastGeneric">',
            f"      <text>{safe_title}</text>",
            f"      <text>{safe_message}</text>",
            "    </binding>",
            "  </visual>",
            "</toast>",
            '"@',
            "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument",
            "$xml.LoadXml($toastXml)",
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)",
            f"$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{safe_app_id}')",
            "$notifier.Show($toast)",
        ]
    )


def build_powershell_toast_command(title: str, message: str, app_id: str = _DEFAULT_APP_ID) -> list[str]:
    script = build_powershell_toast_script(title, message, app_id=app_id)
    return [
        _powershell_path(),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-Command",
        script,
    ]


def _dispatch_win11toast(title: str, message: str, app_id: str = _DEFAULT_APP_ID) -> bool:
    try:
        import win11toast
    except Exception:
        return False

    def _runner():
        try:
            win11toast.toast(title, message, app_id=app_id)
        except Exception as exc:
            logger.debug("win11toast dispatch failed: %s", exc)

    threading.Thread(target=_runner, daemon=True).start()
    return True


def _dispatch_powershell_toast(title: str, message: str, app_id: str = _DEFAULT_APP_ID) -> bool:
    command = build_powershell_toast_command(title, message, app_id=app_id)
    try:
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except Exception as exc:
        logger.debug("PowerShell toast dispatch failed: %s", exc)
        return False


def show_native_toast(title: str, message: str, app_id: str = _DEFAULT_APP_ID) -> bool:
    if sys.platform != "win32":
        return False
    text_title = str(title or "").strip()
    text_message = str(message or "").strip()
    if not text_title or not text_message:
        return False
    if _dispatch_win11toast(text_title, text_message, app_id=app_id):
        return True
    return _dispatch_powershell_toast(text_title, text_message, app_id=app_id)
