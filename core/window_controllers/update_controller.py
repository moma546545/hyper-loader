
import os
import sys
import re
import tempfile
import subprocess
import logging
import time
import shutil
from contextlib import suppress
try:
    from PySide6.QtCore import QObject, QTimer
    from PySide6.QtWidgets import QApplication, QMessageBox
except ImportError:
    from PyQt6.QtCore import QObject, QTimer
    from PyQt6.QtWidgets import QApplication, QMessageBox
from core.background_workers import (
    AppUpdateCheckWorker,
    AppUpdateDownloadWorker,
    YtDlpCoreUpdateWorker,
    YtDlpPipUpdateWorker,
)
from core.database import load_session_settings, save_session_settings
from core.utils import get_app_data_dir

logger = logging.getLogger("SnapDownloader.UpdateController")

class UpdateController(QObject):
    def __init__(self, main_window):
        super().__init__(main_window)
        self.window = main_window
        self._app_update_check_worker = None
        self._app_update_download_worker = None
        self._yt_dlp_update_worker = None
        self._yt_dlp_pip_update_worker = None
        self.update_retry_count = 0
        self._pre_update_status_text = ""
        self._app_update_status_active = False
        self._background_update_cycle_active = False
        self._background_app_check_pending = False
        self._background_ytdlp_terminal_pending = False
        self._last_update_check_was_background = False
        self._update_runtime_dir = self._ensure_update_runtime_dir()
        self._cleanup_stale_update_artifacts()

    @staticmethod
    def _connect_worker_delete_later(worker):
        if worker is None or not hasattr(worker, "finished") or not hasattr(worker, "deleteLater"):
            return
        try:
            worker.finished.connect(worker.deleteLater)
        except Exception:
            logger.debug("Failed to connect worker.deleteLater", exc_info=True)

    @staticmethod
    def _sanitize_path_arg(path: str) -> str:
        cleaned = str(path or "").strip()
        dangerous = ['"', "'", "`", "$", "\n", "\r", ";", "&", "|"]
        for ch in dangerous:
            if ch in cleaned:
                raise ValueError(f"Unsafe character in path argument: {repr(ch)}")
        return cleaned

    @staticmethod
    def _safe_update_filename(value: str, *, fallback: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip(".-")
        return cleaned or fallback

    def _ensure_update_runtime_dir(self) -> str:
        base = os.path.join(get_app_data_dir(), "update_runtime")
        os.makedirs(base, exist_ok=True)
        return base

    def _cleanup_stale_update_artifacts(self, max_age_seconds: int = 3 * 24 * 60 * 60):
        runtime_dir = str(getattr(self, "_update_runtime_dir", "") or "").strip()
        if not runtime_dir or not os.path.isdir(runtime_dir):
            return
        cutoff = time.time() - max(0, int(max_age_seconds))
        for name in os.listdir(runtime_dir):
            path = os.path.join(runtime_dir, name)
            try:
                if os.path.getmtime(path) >= cutoff:
                    continue
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
            except Exception:
                logger.debug("Failed to clean stale update artifact: %s", path, exc_info=True)

    def _allocate_update_download_path(self, version: str) -> str:
        safe_version = self._safe_update_filename(version, fallback="update")
        return os.path.join(self._update_runtime_dir, f"VidDownloader-{safe_version}.zip")

    def _write_update_script(self, script_text: str) -> str:
        fd, script_path = tempfile.mkstemp(
            prefix="apply_update_",
            suffix=".ps1",
            dir=self._update_runtime_dir,
            text=True,
        )
        if os.name != "nt":
            os.chmod(script_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(script_text.strip() + "\n")
        return script_path

    def schedule_background_update_check(self, initial_delay_ms: int = 5000):
        day_seconds = 24 * 60 * 60
        delay_ms = max(0, int(initial_delay_ms))
        try:
            settings = load_session_settings()
            last_check_ts = float(settings.get("last_ytdlp_auto_check_ts", 0) or 0)
        except (AttributeError, TypeError, ValueError):
            last_check_ts = 0.0
            
        if last_check_ts > 0:
            elapsed = max(0.0, time.time() - last_check_ts)
            if elapsed < day_seconds:
                remaining = day_seconds - elapsed
                delay_ms = max(delay_ms, int(remaining * 1000))
        
        QTimer.singleShot(delay_ms, self.check_updates_background)

    def _current_status_text(self) -> str:
        try:
            search_view = getattr(self.window, "search_view", None)
            label = getattr(search_view, "state_label", None) if search_view is not None else None
            if label is not None and hasattr(label, "text"):
                return str(label.text() or "")
        except Exception:
            pass
        return ""

    def _begin_app_update_status(self):
        if not self._app_update_status_active:
            self._pre_update_status_text = self._current_status_text()
        self._app_update_status_active = True

    def _restore_status_after_app_update(self, fallback: str = ""):
        if not self._app_update_status_active:
            return
        self._app_update_status_active = False
        target = str(self._pre_update_status_text or fallback or "").strip()
        self._pre_update_status_text = ""
        if target:
            self.window._set_status(target)

    def _record_background_check_timestamp(self):
        try:
            settings = load_session_settings()
            settings["last_ytdlp_auto_check_ts"] = time.time()
            save_session_settings(settings)
        except Exception:
            logger.debug("Failed to persist background update check timestamp", exc_info=True)

    def _begin_background_update_cycle(self, *, app_check_pending: bool, ytdlp_pending: bool):
        self._background_update_cycle_active = True
        self._background_app_check_pending = bool(app_check_pending)
        self._background_ytdlp_terminal_pending = bool(ytdlp_pending)

    def _clear_background_update_cycle(self):
        self._background_update_cycle_active = False
        self._background_app_check_pending = False
        self._background_ytdlp_terminal_pending = False

    def _maybe_finalize_background_update_cycle(self):
        if not self._background_update_cycle_active:
            return
        if self._background_app_check_pending or self._background_ytdlp_terminal_pending:
            return
        self._record_background_check_timestamp()
        self._clear_background_update_cycle()

    def check_updates_background(self):
        self._check_updates(background=True)

    def check_updates_manual(self):
        self._check_updates(background=False)

    def update_ytdlp_manual(self):
        current_worker = self._yt_dlp_pip_update_worker
        if current_worker is not None and current_worker.isRunning():
            self.window._warn("yt-dlp update is already running")
            return
        self.window._append_log("[Update] Checking for yt-dlp updates...")
        logger.info("Checking for yt-dlp updates...")

        worker = YtDlpPipUpdateWorker(self.window)
        worker.finished.connect(self._on_ytdlp_manual_update_finished)
        self._connect_worker_delete_later(worker)
        self._yt_dlp_pip_update_worker = worker
        worker.start()

    def _check_updates(self, background: bool = True):
        if self._yt_dlp_update_worker is not None and self._yt_dlp_update_worker.isRunning():
            return
        if self._app_update_check_worker is not None and self._app_update_check_worker.isRunning():
            return
        if self._app_update_download_worker is not None and self._app_update_download_worker.isRunning():
            return
        self._last_update_check_was_background = bool(background)
            
        from core.constants import APP_VERSION
        manifest_url = os.getenv("VIDDOWNLOADER_UPDATE_MANIFEST_URL", "")
        started_app_update_check = False
        if manifest_url:
            if not background:
                self.window._append_log("[Update] Checking for app updates...")
            self._app_update_check_worker = AppUpdateCheckWorker(APP_VERSION, manifest_url)
            self._app_update_check_worker.finished.connect(lambda s, m, e, b=background: self._on_app_update_check_finished(s, m, e, b))
            self._connect_worker_delete_later(self._app_update_check_worker)
            self._app_update_check_worker.start()
            started_app_update_check = True
            
        # Also check for yt-dlp core updates
        self._yt_dlp_update_worker = YtDlpCoreUpdateWorker()
        self._yt_dlp_update_worker.finished.connect(self._on_yt_dlp_update_finished)
        self._connect_worker_delete_later(self._yt_dlp_update_worker)
        self._yt_dlp_update_worker.start()
        if background:
            self._begin_background_update_cycle(
                app_check_pending=started_app_update_check,
                ytdlp_pending=True,
            )
        else:
            self._clear_background_update_cycle()

    def _on_app_update_check_finished(self, success, manifest, error, background):
        self._app_update_check_worker = None
        if background:
            self._background_app_check_pending = False
            self._maybe_finalize_background_update_cycle()
        if not success:
            if not background:
                self.window._warn(f"تعذر فحص التحديث: {error}")
            return
            
        if not manifest.get("available"):
            if not background:
                self.window._info("لا يوجد تحديث متاح حالياً")
            return
            
        version = manifest.get("version", "New")
        notes = manifest.get("notes", "")
        from core.constants import APP_VERSION
        msg = f"يتوفر تحديث جديد: {version}\nإصدارك الحالي: {APP_VERSION}"
        if notes:
            msg += f"\n\n{notes[:1200]}"
            
        resp = QMessageBox.question(
            self.window,
            "تحديث متوفر",
            f"{msg}\n\nهل تريد تحميل التحديث الآن؟",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if resp == QMessageBox.StandardButton.Yes:
            self.start_app_update_download(manifest)

    def start_app_update_download(self, manifest):
        current_worker = self._app_update_download_worker
        if current_worker is not None and current_worker.isRunning():
            self.window._warn("يوجد تنزيل تحديث جارٍ بالفعل")
            return
        platform_block = manifest.get("windows") if sys.platform == "win32" else manifest.get("linux")
        if not isinstance(platform_block, dict):
            platform_block = manifest
            
        url = str(platform_block.get("url", "")).strip()
        sha256 = str(platform_block.get("sha256", "")).strip()
        version = str(manifest.get("version", "update")).strip()
        
        if not url or not re.fullmatch(r"[A-Fa-f0-9]{64}", sha256):
            self.window._warn("بيانات التحديث غير صالحة")
            return
            
        out_path = self._allocate_update_download_path(version)
        
        self.window._append_log(f"[Update] Downloading update: {version}")
        self._begin_app_update_status()
        self.window._set_status("تحميل التحديث: 0%")
        
        self._app_update_download_worker = AppUpdateDownloadWorker(url, sha256, out_path)
        self._app_update_download_worker.progress.connect(self._on_app_update_download_progress)
        self._app_update_download_worker.finished.connect(self._on_app_update_download_finished)
        self._connect_worker_delete_later(self._app_update_download_worker)
        self._app_update_download_worker.start()

    def shutdown(self):
        self.update_retry_count = 0
        self._last_update_check_was_background = False
        self._clear_background_update_cycle()
        worker_attr_names = (
            "_app_update_check_worker",
            "_app_update_download_worker",
            "_yt_dlp_update_worker",
            "_yt_dlp_pip_update_worker",
        )
        for attr_name in worker_attr_names:
            worker = getattr(self, attr_name, None)
            setattr(self, attr_name, None)
            if worker is None:
                continue
            try:
                if hasattr(worker, "isRunning") and worker.isRunning():
                    if hasattr(worker, "requestInterruption"):
                        worker.requestInterruption()
                    if hasattr(worker, "quit"):
                        worker.quit()
                    if hasattr(worker, "wait"):
                        worker.wait(1000)
            except Exception:
                logger.debug("Failed to stop update worker during shutdown", exc_info=True)
            try:
                if hasattr(worker, "deleteLater"):
                    worker.deleteLater()
            except Exception:
                logger.debug("Failed to delete update worker during shutdown", exc_info=True)

    def cancel_app_update_download(self):
        worker = self._app_update_download_worker
        if worker is None or not worker.isRunning():
            self.window._warn("لا يوجد تنزيل تحديث جارٍ لإلغائه")
            return False
        try:
            worker.requestInterruption()
            self.window._append_log("[Update] Cancellation requested for update download.")
            self.window._set_status("جارٍ إلغاء تنزيل التحديث...")
            return True
        except Exception as exc:
            self.window._warn(f"تعذر طلب إلغاء تنزيل التحديث: {exc}")
            return False

    def _on_app_update_download_progress(self, downloaded, total):
        if total > 0:
            pct = int(round((downloaded / total) * 100))
            self.window._set_status(f"تحميل التحديث: {max(0, min(100, pct))}%")

    def _on_app_update_download_finished(self, ok, path, error):
        self._app_update_download_worker = None
        if not ok:
            self._restore_status_after_app_update(fallback="جاهز")
            error_text = str(error or "").strip()
            if "cancelled" in error_text.lower():
                self.window._append_log("[Update] Update download cancelled by user.")
                self.window._info("تم إلغاء تنزيل التحديث")
            else:
                self.window._warn(f"تعذر تنزيل التحديث: {error_text}")
            return
            
        self.window._append_log("[Update] Update downloaded successfully.")
        self.window._set_status("اكتمل تنزيل التحديث")
        resp = QMessageBox.question(
            self.window,
            "تحديث جاهز",
            "تم تنزيل التحديث بنجاح.\nهل تريد تثبيته الآن وإعادة تشغيل البرنامج؟",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes
        )
        if resp == QMessageBox.StandardButton.Yes:
            self.install_app_update(path)
            return
        self._restore_status_after_app_update(fallback="جاهز")

    def install_app_update(self, zip_path):
        zip_path = str(zip_path or "").strip()
        if not zip_path or not os.path.isfile(zip_path):
            self.window._warn("ملف التحديث غير موجود")
            return
        if not bool(getattr(sys, "frozen", False)):
            self.window._warn("التحديث التلقائي يعمل فقط على النسخة المعبأة (Packaging)")
            return
            
        exe_path = sys.executable
        install_dir = os.path.dirname(exe_path)
        if not install_dir:
            self.window._warn("تعذر تحديد مسار التثبيت")
            return
        script = r"""
param(
  [Parameter(Mandatory=$true)][string]$ZipPath,
  [Parameter(Mandatory=$true)][string]$InstallDir,
  [Parameter(Mandatory=$true)][string]$ExeName,
  [Parameter(Mandatory=$true)][int]$ParentPid
)
try { Wait-Process -Id $ParentPid -ErrorAction SilentlyContinue } catch {}
$tmp = Join-Path $env:TEMP ("viddl-upd-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
Expand-Archive -LiteralPath $ZipPath -DestinationPath $tmp -Force
$root = $tmp
$items = Get-ChildItem -LiteralPath $tmp
if ($items.Count -eq 1 -and $items[0].PSIsContainer) { $root = $items[0].FullName }
$rcArgs = @($root, $InstallDir, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NC", "/NS", "/NP", "/R:2", "/W:1")
& robocopy @rcArgs | Out-Null
$destExe = Join-Path $InstallDir $ExeName
if (Test-Path -LiteralPath $destExe) {
  Start-Process -FilePath $destExe -WorkingDirectory $InstallDir
}
Remove-Item -LiteralPath $ZipPath -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
# Self-delete this script
Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
"""
        try:
            script_path = self._write_update_script(script)
            pid = os.getpid()
            zip_path = self._sanitize_path_arg(zip_path)
            install_dir = self._sanitize_path_arg(install_dir)
            exe_name = self._sanitize_path_arg(os.path.basename(exe_path))
            subprocess.Popen([
                "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", script_path, "-ZipPath", zip_path,
                "-InstallDir", install_dir, "-ExeName", exe_name, "-ParentPid", str(pid)
            ], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            
            QApplication.instance().quit()
        except Exception as exc:
            with suppress(Exception):
                if 'script_path' in locals() and script_path and os.path.exists(script_path):
                    os.remove(script_path)
            self.window._warn(f"تعذر تثبيت التحديث: {exc}")

    def _on_ytdlp_manual_update_finished(self, success, message):
        self._yt_dlp_pip_update_worker = None
        if success:
            self.window._info("تم تحديث yt-dlp بنجاح")
            logger.info("yt-dlp updated successfully.")
            return
        self.window._warn(f"فشل تحديث yt-dlp: {message}")
        logger.error(f"yt-dlp update failed: {message}")

    def _on_yt_dlp_update_finished(self, return_code, stdout_text, stderr_text):
        self._yt_dlp_update_worker = None
        was_background = bool(
            getattr(self, "_last_update_check_was_background", False)
            or getattr(self, "_background_update_cycle_active", False)
        )
        if return_code == 0:
            self.window._append_log("[Auto-Update] yt-dlp core updated successfully.")
            self.update_retry_count = 0
            if was_background:
                self._background_ytdlp_terminal_pending = False
                self._maybe_finalize_background_update_cycle()
                self.schedule_background_update_check()
            return
            
        error_message = (stderr_text or stdout_text or "Unknown error").strip()
        self.window._append_log(f"[Auto-Update] Failed to update yt-dlp core. Error: {error_message}")

        if not was_background:
            self.update_retry_count = 0
            self._background_ytdlp_terminal_pending = False
            self._maybe_finalize_background_update_cycle()
            return

        if self.update_retry_count < 3:
            self.update_retry_count += 1
            delay_ms = min(60_000, 5000 * (2 ** self.update_retry_count))
            self.window._append_log(f"[Auto-Update] Retrying in {delay_ms // 1000} seconds...")
            QTimer.singleShot(delay_ms, self.check_updates_background)
        else:
            self.window._append_log("[Auto-Update] Max retries reached. Next auto-check in 24 hours.")
            self.update_retry_count = 0
            self._background_ytdlp_terminal_pending = False
            self._maybe_finalize_background_update_cycle()
            QTimer.singleShot(24 * 60 * 60 * 1000, self.check_updates_background)
