
"""
core/merge_worker.py - Advanced FFmpeg Merger

This worker takes separate video and audio files and merges them using
custom FFmpeg parameters for advanced control over the output.
"""
import logging
import os
import queue
import re
import shutil
import subprocess
import threading
import time

try:
    from PySide6.QtCore import QThread, Signal
except ImportError:
    from PyQt6.QtCore import pyqtSignal as Signal, QThread

logger = logging.getLogger("SnapDownloader.MergeWorker")

READ_IDLE_TIMEOUT_SECONDS = 300.0
PROCESS_TERMINATION_TIMEOUT = 8.0
PROCESS_KILL_TIMEOUT = 5.0

class MergeWorker(QThread):
    """
    A QThread worker for merging video and audio streams with FFmpeg.
    """
    progress = Signal(float)  # Emits percentage (0.0 to 100.0)
    finished = Signal(bool, str)  # Emits success (bool) and final_path (str)
    log = Signal(str)

    def __init__(self, video_path: str, audio_path: str, output_path: str, ffmpeg_opts: dict, parent=None):
        super().__init__(parent)
        self.video_path = video_path
        self.audio_path = audio_path
        self.output_path = output_path
        self.ffmpeg_opts = dict(ffmpeg_opts or {})
        self._cancel_event = threading.Event()
        self.process = None
        self._termination_reason = ""
        self._stdout_closed = False
        self._reader_thread = None

    def _close_stdout(self, process=None):
        proc = process or self.process
        if proc is None or self._stdout_closed:
            return
        stdout = getattr(proc, "stdout", None)
        if stdout is None:
            self._stdout_closed = True
            return
        try:
            stdout.close()
        except Exception:
            pass
        finally:
            self._stdout_closed = True

    def _terminate_process(self, error_prefix: str = "[ERROR] Failed to terminate FFmpeg process"):
        process = self.process
        if process is None:
            return
        if process.poll() is not None:
            self._close_stdout(process)
            return
        try:
            process.terminate()
            process.wait(timeout=PROCESS_TERMINATION_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=PROCESS_KILL_TIMEOUT)
            except Exception as exc:
                self.log.emit(f"{error_prefix}: {exc}")
        except Exception as exc:
            self.log.emit(f"{error_prefix}: {exc}")
        finally:
            self._close_stdout(process)

    def _read_lines_safely(self, stdout):
        q = queue.Queue()

        def _enqueue(out, sink):
            try:
                for line in iter(out.readline, ""):
                    if not line:
                        break
                    sink.put(line)
            except Exception:
                pass
            finally:
                sink.put(None)

        t = threading.Thread(target=_enqueue, args=(stdout, q), daemon=True, name="MergeWorkerStdout")
        t.start()
        self._reader_thread = t
        last_output_ts = time.time()

        try:
            while True:
                if self._cancel_event.is_set():
                    self._termination_reason = "cancelled"
                    self._terminate_process()
                    break
                try:
                    line = q.get(timeout=0.5)
                except queue.Empty:
                    if not t.is_alive():
                        break
                    if (time.time() - last_output_ts) >= READ_IDLE_TIMEOUT_SECONDS:
                        self._termination_reason = "idle_timeout"
                        self.log.emit("[ERROR] FFmpeg merge timed out due to no output.")
                        self._terminate_process("[ERROR] Failed to stop FFmpeg after idle timeout")
                        break
                    continue
                if line is None:
                    break
                last_output_ts = time.time()
                yield line
        finally:
            t.join(timeout=2.0)
            if self._reader_thread is t:
                self._reader_thread = None

    def run(self):
        """
        Constructs and runs the FFmpeg command.
        """
        self._termination_reason = ""
        self.process = None
        self._stdout_closed = False
        if not os.path.exists(self.video_path) or not os.path.exists(self.audio_path):
            error_msg = "Input video or audio file not found."
            self.log.emit(f"[ERROR] {error_msg}")
            self.finished.emit(False, "")
            return

        total_duration_sec = self._get_duration(self.video_path)
        if total_duration_sec <= 0:
            error_msg = "Could not determine video duration."
            self.log.emit(f"[ERROR] {error_msg}")
            self.finished.emit(False, "")
            return

        cmd = self._build_command()
        if not cmd:
            self.finished.emit(False, "")
            return

        self.log.emit(f"Starting FFmpeg merge: {' '.join(cmd)}")
        
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, # Redirect stderr to stdout
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            for line in self._read_lines_safely(self.process.stdout):
                if self._cancel_event.is_set():
                    self._termination_reason = "cancelled"
                    self._terminate_process("[ERROR] Failed to stop cancelled FFmpeg merge")
                    self.log.emit("Merge cancelled by user.")
                    self.finished.emit(False, "")
                    return

                clean = str(line or "").strip()
                if not clean:
                    continue

                if clean.startswith("out_time_ms="):
                    try:
                        current_sec = int(clean.split("=", 1)[1].strip() or "0") / 1_000_000.0
                    except Exception:
                        current_sec = 0.0
                    if total_duration_sec > 0:
                        progress_pct = (current_sec / total_duration_sec) * 100.0
                        self.progress.emit(min(100.0, max(0.0, progress_pct)))
                    continue

                if clean.startswith(("progress=", "frame=", "fps=", "speed=", "bitrate=", "total_size=", "dup_frames=", "drop_frames=", "out_time=")):
                    continue

                self.log.emit(clean)

            if self._termination_reason == "cancelled":
                self.log.emit("Merge cancelled by user.")
                self.finished.emit(False, "")
                return
            if self._termination_reason == "idle_timeout":
                self.finished.emit(False, "")
                return
            if self.process.poll() is None:
                try:
                    self.process.wait(timeout=PROCESS_TERMINATION_TIMEOUT + PROCESS_KILL_TIMEOUT + 1.0)
                except subprocess.TimeoutExpired:
                    self._termination_reason = "wait_timeout"
                    self.log.emit("[ERROR] FFmpeg merge did not exit in time; terminating process.")
                    self._terminate_process("[ERROR] Failed to stop timed out FFmpeg merge")
            if self._termination_reason == "wait_timeout":
                self.finished.emit(False, "")
                return
            if self.process.returncode == 0:
                self.log.emit("Merge finished successfully.")
                self.progress.emit(100.0)
                self.finished.emit(True, self.output_path)
            else:
                self.log.emit(f"FFmpeg process exited with code {self.process.returncode}")
                self.finished.emit(False, "")

        except Exception as e:
            error_msg = f"An unexpected error occurred: {e}"
            self.log.emit(f"[ERROR] {error_msg}")
            logger.error(error_msg, exc_info=True)
            self.finished.emit(False, "")
        finally:
            self._close_stdout(self.process)
            if self._reader_thread is not None and self._reader_thread.is_alive():
                self._reader_thread.join(timeout=2.0)
            self._reader_thread = None
            self.process = None

    def _get_duration(self, file_path: str) -> float:
        """Get video duration in seconds using ffprobe."""
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            file_path
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return float(result.stdout.strip())
        except (subprocess.CalledProcessError, FileNotFoundError, ValueError, subprocess.TimeoutExpired) as e:
            self.log.emit(f"[ERROR] Could not get video duration: {e}")
            return 0.0

    def _build_command(self) -> list[str]:
        """
        Builds the FFmpeg command list from the provided options.
        Implements Smart-remux, Stream-copy, Smart-transcode, and HDR preservation.
        """
        ffmpeg_path = shutil.which("ffmpeg") or "ffmpeg"
        cmd = [ffmpeg_path, "-y"]
        
        start_time = self.ffmpeg_opts.get("start_time")
        if start_time:
            cmd.extend(["-ss", str(start_time)])
            
        cmd.extend(["-i", self.video_path])
        
        if start_time:
            cmd.extend(["-ss", str(start_time)])
            
        cmd.extend(["-i", self.audio_path])
        cmd.extend(["-map", "0:v:0", "-map", "1:a:0", "-shortest"])
        
        end_time = self.ffmpeg_opts.get("end_time")
        if end_time:
            cmd.extend(["-to", str(end_time)])

        v_codec = str(self.ffmpeg_opts.get("video_codec", "copy") or "copy").strip()
        a_codec = str(self.ffmpeg_opts.get("audio_codec", "copy") or "copy").strip()

        is_stream_copy = (v_codec == "copy" and a_codec == "copy")

        if start_time or end_time:
            cmd.extend(["-avoid_negative_ts", "make_zero", "-fflags", "+genpts"])

        if is_stream_copy:
            # Smart-remux / Stream-copy: Preserve HDR metadata and experimental container combinations
            cmd.extend(["-c", "copy", "-strict", "unofficial", "-map_metadata", "0"])
        else:
            # Smart-transcode
            cmd.extend(["-c:v", v_codec])
            if v_codec not in {"", "copy"}:
                try:
                    crf_val = int(self.ffmpeg_opts.get("video_crf", 23))
                    if 0 <= crf_val <= 51:
                        cmd.extend(["-crf", str(crf_val)])
                except (TypeError, ValueError):
                    pass

            cmd.extend(["-c:a", a_codec])
            if a_codec != "copy":
                bitrate = str(self.ffmpeg_opts.get("audio_bitrate", "192k") or "192k").strip()
                if re.match(r"^\d+[kKmM]?$", bitrate):
                    cmd.extend(["-b:a", bitrate])

        cmd.extend(["-progress", "pipe:1", "-nostats", self.output_path])
        return cmd

    def stop(self):
        """
        Requests the merge process to stop.
        """
        self._cancel_event.set()
        if self.process and self.process.poll() is None:
            self.log.emit("Attempting to terminate FFmpeg process...")
            self._terminate_process("[ERROR] Failed to stop FFmpeg merge")

    def request_stop(self):
        self.stop()

    def wait_for_stop(self, timeout_ms: int = 5000) -> bool:
        if not self.isRunning():
            return True
        if QThread.currentThread() is self:
            return False
        try:
            return bool(self.wait(max(0, int(timeout_ms or 0))))
        except Exception:
            return False
