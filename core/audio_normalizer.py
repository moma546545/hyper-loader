
"""
core/audio_normalizer.py — FFMPEG-Based Audio Normalization
Uses FFMPEG's loudnorm filter to normalize audio levels across all downloaded files.
Supports folder-wide batch normalization.
"""
import os
import re
import json
import shutil
import subprocess
import threading
import logging
from typing import Callable, Optional

from core.qt_compat import QApplication, QObject, Signal

logger = logging.getLogger("SnapDownloader.AudioNorm")

# LUFS target level (EBU R128 standard = -23 LUFS, streaming = -14 LUFS)
STREAMING_TARGET_LUFS = -14.0
BROADCAST_TARGET_LUFS = -23.0
_CALLBACK_PROXY = None


class _CallbackProxy(QObject):
    invoke = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.invoke.connect(self._dispatch)

    def _dispatch(self, callback, args):
        if callable(callback):
            callback(*tuple(args or ()))


def _get_callback_proxy() -> Optional[_CallbackProxy]:
    global _CALLBACK_PROXY
    app = QApplication.instance()
    if app is None:
        return None
    if _CALLBACK_PROXY is None:
        _CALLBACK_PROXY = _CallbackProxy(app)
    return _CALLBACK_PROXY


def _invoke_on_ui_thread(callback: Optional[Callable], *args) -> None:
    if not callable(callback):
        return
    proxy = _get_callback_proxy()
    if proxy is None:
        callback(*args)
        return
    proxy.invoke.emit(callback, args)


def _find_ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def normalize_file(
    input_path: str,
    output_path: Optional[str] = None,
    target_lufs: float = STREAMING_TARGET_LUFS,
    true_peak: float = -1.0,
    loudness_range: float = 11.0,
    in_place: bool = True,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> tuple[bool, str]:
    """
    Normalize audio of a media file using FFMPEG loudnorm filter.

    Args:
        input_path: Path to source file.
        output_path: Path for output. If None and in_place=True, replaces original.
        target_lufs: Target loudness in LUFS (default -14 for streaming).
        true_peak: Maximum true peak level in dBTP.
        loudness_range: Target loudness range (LRA).
        in_place: Replace original file after conversion.
        progress_callback: Called with status messages.

    Returns:
        (success: bool, message: str)
    """
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return False, "FFMPEG not found. Please install FFMPEG."

    if not os.path.isfile(input_path):
        return False, f"File not found: {input_path}"

    ext = os.path.splitext(input_path)[1].lower()
    tmp_path = input_path + ".norm_tmp" + ext

    if output_path is None:
        output_path = input_path if in_place else input_path + "_normalized" + ext

    try:
        # ── Pass 1: Measure loudness ──────────────────────────────────────────
        if progress_callback:
            progress_callback(f"Analyzing: {os.path.basename(input_path)}")

        measure_cmd = [
            ffmpeg, "-hide_banner", "-i", input_path,
            "-af", f"loudnorm=I={target_lufs}:TP={true_peak}:LRA={loudness_range}:print_format=json",
            "-vn", "-f", "null", "-",
        ]
        result = subprocess.run(
            measure_cmd,
            capture_output=True,
            text=True,
            timeout=300,
            encoding="utf-8",
            errors="replace",
        )
        # Parse JSON from stderr
        json_match = re.search(r'\{[^{}]+\}', result.stderr, re.DOTALL)
        if json_match:
            stats = json.loads(json_match.group())
            measured_i    = stats.get("input_i", str(target_lufs))
            measured_tp   = stats.get("input_tp", str(true_peak))
            measured_lra  = stats.get("input_lra", str(loudness_range))
            measured_thresh = stats.get("input_thresh", "-70.0")
            offset        = stats.get("target_offset", "0.0")
        else:
            # Fallback: single-pass without stats
            measured_i = str(target_lufs)
            measured_tp = str(true_peak)
            measured_lra = str(loudness_range)
            measured_thresh = "-70.0"
            offset = "0.0"

        # ── Pass 2: Apply normalization ───────────────────────────────────────
        if progress_callback:
            progress_callback(f"Normalizing: {os.path.basename(input_path)}")

        norm_filter = (
            f"loudnorm=I={target_lufs}:TP={true_peak}:LRA={loudness_range}"
            f":measured_I={measured_i}:measured_TP={measured_tp}"
            f":measured_LRA={measured_lra}:measured_thresh={measured_thresh}"
            f":offset={offset}:linear=true:print_format=summary"
        )

        apply_cmd = [
            ffmpeg, "-hide_banner", "-i", input_path,
            "-c:v", "copy",
            "-af", norm_filter,
            "-ar", "48000",       # 48 kHz standard
            "-y", tmp_path,
        ]
        apply_result = subprocess.run(
            apply_cmd,
            capture_output=True,
            text=True,
            timeout=600,
            encoding="utf-8",
            errors="replace",
        )

        if apply_result.returncode != 0:
            return False, f"Normalization failed: {apply_result.stderr[-300:]}"

        # ── Swap files ────────────────────────────────────────────────────────
        if in_place:
            os.replace(tmp_path, output_path)
        else:
            os.rename(tmp_path, output_path)

        msg = f"✅ Normalized: {os.path.basename(input_path)}"
        if progress_callback:
            progress_callback(msg)
        return True, msg

    except subprocess.TimeoutExpired:
        _cleanup(tmp_path)
        return False, "Normalization timed out."
    except Exception as exc:
        _cleanup(tmp_path)
        return False, f"Error: {exc}"


def normalize_folder(
    folder: str,
    extensions: tuple = (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg"),
    target_lufs: float = STREAMING_TARGET_LUFS,
    progress_callback: Optional[Callable[[str], None]] = None,
    done_callback: Optional[Callable[[int, int], None]] = None,
):
    """
    Normalize all audio files in a folder (non-blocking, runs in background thread).
    Progress and completion callbacks are marshalled onto the Qt UI thread when
    a Qt application is available.
    """
    def _run():
        def _safe_progress(msg):
            _invoke_on_ui_thread(progress_callback, msg)

        def _safe_done(s, t):
            _invoke_on_ui_thread(done_callback, s, t)

        files = [
            os.path.join(folder, f)
            for f in os.listdir(folder)
            if os.path.isfile(os.path.join(folder, f))
            and os.path.splitext(f)[1].lower() in extensions
        ]
        total = len(files)
        success = 0
        for fp in files:
            ok, msg = normalize_file(fp, target_lufs=target_lufs, progress_callback=_safe_progress)
            if ok:
                success += 1
        _safe_done(success, total)

    threading.Thread(target=_run, daemon=True, name="AudioNormalizer").start()


def _cleanup(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as exc:
        logger.warning(f"[Normalizer] Cleanup failed for {path}: {exc}")



