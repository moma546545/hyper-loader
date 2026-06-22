import importlib.util
import os
import shutil
import subprocess
import logging
import sys
from datetime import datetime
from dataclasses import dataclass
from typing import Callable, Optional
from .task_types import PostActionType, normalize_post_action
from .utils import get_app_data_dir


logger = logging.getLogger("SnapDownloader.PostActions")
_SAFE_SCRIPTS_DIR = os.path.join(get_app_data_dir(), "scripts")
_POST_ACTION_TIMEOUT_SECONDS = 30
_TRANSCRIBE_TIMEOUT_SECONDS = 900


@dataclass(frozen=True)
class RegisteredPostAction:
    name: str
    handler: Callable[[str, Optional[str], Optional[Callable]], None]
    allow_extension: bool = False


class PostActionRegistry:
    def __init__(self):
        self._actions: dict[str, RegisteredPostAction] = {}

    def register(
        self,
        name: str,
        handler: Callable[[str, Optional[str], Optional[Callable]], None],
        *,
        allow_extension: bool = False,
    ) -> str:
        normalized = normalize_post_action(name, allow_unknown=True)
        if not normalized:
            raise ValueError("post action name cannot be empty")
        self._actions[normalized] = RegisteredPostAction(
            name=normalized,
            handler=handler,
            allow_extension=bool(allow_extension),
        )
        return normalized

    def get(self, name: str) -> Optional[RegisteredPostAction]:
        normalized = normalize_post_action(name, allow_unknown=True)
        if not normalized:
            return None
        return self._actions.get(normalized)

    def allowed_extension_actions(self) -> set[str]:
        return {
            action.name
            for action in self._actions.values()
            if action.allow_extension
        }

    def dispatch(
        self,
        action_type: str,
        file_path: str,
        *,
        script_path: Optional[str] = None,
        confirm_callback=None,
    ) -> bool:
        entry = self.get(action_type)
        if entry is None:
            return False
        entry.handler(file_path, script_path, confirm_callback)
        return True


class PostDownloadManager:
    confirm_callback = None
    _registry = PostActionRegistry()

    @staticmethod
    def _resolve_script_path(path: str) -> str:
        raw = str(path or "").strip()
        if not raw:
            return ""
        if os.path.isabs(raw):
            return os.path.realpath(os.path.abspath(raw))
        return os.path.realpath(os.path.abspath(os.path.join(_SAFE_SCRIPTS_DIR, raw)))

    @staticmethod
    def _is_safe_script_path(path: str) -> bool:
        p = PostDownloadManager._resolve_script_path(path)
        if not p:
            return False
        safe_dir = os.path.realpath(_SAFE_SCRIPTS_DIR)
        if not p.startswith(safe_dir + os.sep):
            logger.warning(f"[PostActions] Script outside safe dir: {p}")
            return False
        if not os.path.isfile(p):
            return False
        if os.path.basename(p).startswith("-"):
            logger.warning(f"[PostActions] Refused script path starting with dash: {p}")
            return False
        return os.path.splitext(p)[1].lower() in {".py", ".ps1", ".bat", ".cmd"}

    @staticmethod
    def _powershell_path() -> str:
        if os.name != "nt":
            return "powershell"
        system_root = str(os.environ.get("SystemRoot", r"C:\Windows")).strip() or r"C:\Windows"
        candidate = os.path.join(system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        return candidate if os.path.isfile(candidate) else "powershell"

    @staticmethod
    def _ffmpeg_path() -> str:
        candidate = shutil.which("ffmpeg")
        return candidate or "ffmpeg"

    @staticmethod
    def _resolve_transcribe_command() -> list[str]:
        configured = str(os.getenv("VIDDOWNLOADER_WHISPER_CLI", "")).strip()
        if configured:
            candidate = os.path.realpath(os.path.abspath(configured))
            if os.path.isfile(candidate):
                return [candidate]
            logger.warning(f"[PostActions] Configured whisper CLI was not found: {candidate}")
            return []

        exe_dir = os.path.dirname(os.path.realpath(sys.executable))
        executable_name = "whisper.exe" if os.name == "nt" else "whisper"
        candidates = [os.path.join(exe_dir, executable_name)]
        if os.name == "nt" and os.path.basename(exe_dir).lower() != "scripts":
            candidates.append(os.path.join(exe_dir, "Scripts", executable_name))

        for candidate in candidates:
            if os.path.isfile(candidate):
                return [candidate]

        try:
            if importlib.util.find_spec("whisper") is not None:
                return [sys.executable, "-m", "whisper"]
        except Exception:
            pass

        logger.warning(
            "[PostActions] Whisper CLI not found in the current environment. "
            "Set VIDDOWNLOADER_WHISPER_CLI to an absolute trusted path if needed."
        )
        return []

    @staticmethod
    def _open_folder(folder_path: str) -> None:
        safe_folder = os.path.realpath(os.path.abspath(str(folder_path or "").strip()))
        if not safe_folder or not os.path.isdir(safe_folder):
            return
        if safe_folder.startswith("\\\\"):
            parts = safe_folder[2:].split("\\", 1)
            host = parts[0].strip().lower()
            if host not in {"localhost", "127.0.0.1", "::1"}:
                logger.warning(f"[PostActions] Blocked UNC path folder opening to remote host: {host}")
                return
        if os.name == "nt":
            explorer_path = os.path.join(
                str(os.environ.get("SystemRoot", r"C:\Windows")).strip() or r"C:\Windows",
                "explorer.exe",
            )
            opener = explorer_path if os.path.isfile(explorer_path) else "explorer.exe"
            subprocess.run([opener, safe_folder], check=False, timeout=_POST_ACTION_TIMEOUT_SECONDS)
            return
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.run([opener, safe_folder], check=False, timeout=_POST_ACTION_TIMEOUT_SECONDS)

    @staticmethod
    def execute_script(script_path: str, file_path: str):
        PostDownloadManager._execute_script(script_path, file_path, force_runner=None)

    @staticmethod
    def _execute_script(script_path: str, file_path: str, *, force_runner: str | None):
        script = PostDownloadManager._resolve_script_path(script_path)
        target = os.path.abspath(str(file_path or "").strip())
        if not script or not target:
            return
        if not PostDownloadManager._is_safe_script_path(script):
            logger.warning(f"[PostActions] Refused unsafe script path: {script}")
            return
        ext = os.path.splitext(script)[1].lower()
        try:
            if force_runner == "python" or ext == ".py":
                subprocess.run(
                    [sys.executable, script, target],
                    check=False,
                    timeout=_POST_ACTION_TIMEOUT_SECONDS,
                )
            elif force_runner == "powershell" or ext == ".ps1":
                subprocess.run(
                    [PostDownloadManager._powershell_path(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, target],
                    check=False,
                    timeout=_POST_ACTION_TIMEOUT_SECONDS,
                )
            else:
                subprocess.run(
                    [script, target],
                    check=False,
                    timeout=_POST_ACTION_TIMEOUT_SECONDS,
                )
        except subprocess.TimeoutExpired:
            logger.warning(f"[PostActions] Script timed out and was terminated: {script}")
        except Exception as exc:
            logger.error(f"[PostActions] Failed running script {script}: {exc}")

    @staticmethod
    def _normalize_pipeline_step(step: dict | None) -> dict:
        raw = dict(step or {})
        return {
            "action": str(raw.get("action", "") or "").strip().lower(),
            "label": str(raw.get("label", "") or "").strip(),
            "script_path": str(raw.get("script_path", "") or "").strip(),
            "args": str(raw.get("args", "") or "").strip(),
        }

    @staticmethod
    def _convert_to_mp3(file_path: str, args: str = "") -> str:
        source = os.path.abspath(str(file_path or "").strip())
        if not source or not os.path.isfile(source):
            return source
        target = os.path.splitext(source)[0] + ".mp3"
        bitrate = str(args or "").strip() or "192k"
        try:
            subprocess.run(
                [
                    PostDownloadManager._ffmpeg_path(),
                    "-y",
                    "-i",
                    source,
                    "-vn",
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    bitrate,
                    target,
                ],
                check=False,
                timeout=_TRANSCRIBE_TIMEOUT_SECONDS,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
        except Exception as exc:
            logger.error("[PostActions] Failed MP3 conversion for %s: %s", source, exc)
            return source
        return target if os.path.isfile(target) else source

    @classmethod
    def execute_pipeline(
        cls,
        file_path: str,
        pipeline: list[dict],
        *,
        confirm_callback=None,
    ) -> str:
        current_path = os.path.abspath(str(file_path or "").strip())
        for raw_step in pipeline or []:
            step = cls._normalize_pipeline_step(raw_step)
            action = step.get("action", "")
            if not action:
                continue
            try:
                if action == "convert_mp3":
                    current_path = cls._convert_to_mp3(current_path, step.get("args", ""))
                elif action == "run_python":
                    cls._execute_script(step.get("script_path", ""), current_path, force_runner="python")
                elif action == "run_powershell":
                    cls._execute_script(step.get("script_path", ""), current_path, force_runner="powershell")
                elif action == "run_script":
                    cls._execute_script(step.get("script_path", ""), current_path, force_runner=None)
                else:
                    cls.execute_action(
                        action,
                        current_path,
                        script_path=step.get("script_path", ""),
                        confirm_callback=confirm_callback,
                    )
            except Exception as exc:
                logger.error("[PostActions] Pipeline step failed (%s): %s", action, exc)
        return current_path

    @staticmethod
    def _is_safe_transcribe_target(path: str) -> bool:
        target = os.path.realpath(os.path.abspath(str(path or "").strip()))
        if not target:
            return False
        if not os.path.isfile(target):
            return False
        # Prevent argument confusion where a file name starts like a CLI option.
        if os.path.basename(target).startswith("-"):
            return False
        return True

    @classmethod
    def register_action(
        cls,
        name: str,
        handler: Callable[[str, Optional[str], Optional[Callable]], None],
        *,
        allow_extension: bool = False,
    ) -> str:
        return cls._registry.register(name, handler, allow_extension=allow_extension)

    @classmethod
    def get_allowed_extension_actions(cls) -> set[str]:
        return cls._registry.allowed_extension_actions()

    @classmethod
    def normalize_action(cls, action_type: str, *, extension_safe: bool = False) -> str:
        normalized = normalize_post_action(action_type, allow_unknown=True)
        if not normalized:
            return PostActionType.NONE.value
        if extension_safe:
            allowed = cls.get_allowed_extension_actions()
            return normalized if normalized in allowed else PostActionType.NONE.value
        return normalized if cls._registry.get(normalized) is not None else PostActionType.NONE.value

    @staticmethod
    def _action_open_folder(file_path: str, _script_path: Optional[str] = None, _confirm_callback=None) -> None:
        folder_path = os.path.dirname(file_path) if file_path else ""
        if folder_path and os.path.isdir(folder_path):
            PostDownloadManager._open_folder(folder_path)

    @staticmethod
    def _action_play_sound(_file_path: str, _script_path: Optional[str] = None, _confirm_callback=None) -> None:
        import winsound

        winsound.MessageBeep(winsound.MB_ICONASTERISK)

    @staticmethod
    def _action_shutdown(_file_path: str, _script_path: Optional[str] = None, _confirm_callback=None) -> None:
        logger.warning("[PostActions] 'shutdown' action is disabled in production.")

    @staticmethod
    def _action_run_script(file_path: str, script_path: Optional[str] = None, _confirm_callback=None) -> None:
        default_script = os.path.join(_SAFE_SCRIPTS_DIR, "compress_video.py")
        selected = str(script_path or "").strip() or default_script
        PostDownloadManager.execute_script(selected, file_path)

    @staticmethod
    def _action_transcribe(file_path: str, _script_path: Optional[str] = None, _confirm_callback=None) -> None:
        path = os.path.abspath(str(file_path or "").strip()) if file_path else ""
        if not PostDownloadManager._is_safe_transcribe_target(path):
            logger.warning(f"[PostActions] Refused transcribe target: {path}")
            return

        safe_path = os.path.realpath(os.path.abspath(path))
        if os.path.basename(safe_path).startswith("-"):
            logger.warning(f"[PostActions] Refused transcribe target starting with dash: {safe_path}")
            return

        output_dir = os.path.dirname(safe_path)
        out_txt = os.path.splitext(safe_path)[0] + ".txt"
        base_cmd = PostDownloadManager._resolve_transcribe_command()
        if not base_cmd:
            return

        logger.info(f"Preparing to transcribe: {safe_path}")
        cmd = [*base_cmd, safe_path, "--output_format", "txt", "--output_dir", output_dir]

        subprocess.run(
            cmd,
            check=False,
            timeout=_TRANSCRIBE_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        )
        if os.path.isfile(out_txt):
            logger.info(f"[PostActions] Transcription generated: {out_txt}")

    @staticmethod
    def _action_summarize(file_path: str, _script_path: Optional[str] = None, _confirm_callback=None) -> None:
        """
        Generates a transcription and a smart summary using Whisper.
        """
        path = os.path.abspath(str(file_path or "").strip()) if file_path else ""
        if not PostDownloadManager._is_safe_transcribe_target(path):
            return

        PostDownloadManager._action_transcribe(file_path, _script_path, _confirm_callback)
        
        out_txt = os.path.splitext(path)[0] + ".txt"
        out_summary = os.path.splitext(path)[0] + ".summary.txt"
        
        if os.path.isfile(out_txt):
            try:
                with open(out_txt, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                
                if len(lines) > 10:
                    summary = [
                        "=== SMART VIDEO SUMMARY ===\n",
                        f"Source: {os.path.basename(path)}\n",
                        f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n",
                        "--- Introduction ---\n",
                        *lines[:5],
                        "\n--- Key Points ---\n",
                        *lines[len(lines)//2 : len(lines)//2 + 5],
                        "\n--- Conclusion ---\n",
                        *lines[-5:],
                    ]
                else:
                    summary = lines
                
                with open(out_summary, "w", encoding="utf-8") as f:
                    f.writelines(summary)
                logger.info(f"[PostActions] Summary generated: {out_summary}")
            except Exception as exc:
                logger.error(f"[PostActions] Failed to generate summary: {exc}")

    @staticmethod
    def execute_action(
        action_type: str,
        file_path: str,
        script_path: Optional[str] = None,
        confirm_callback=None,
    ):
        try:
            action = PostDownloadManager.normalize_action(action_type)
            path = os.path.abspath(str(file_path or "").strip()) if file_path else ""
            if action == PostActionType.NONE.value:
                return
            if PostDownloadManager._registry.dispatch(
                action,
                path,
                script_path=script_path,
                confirm_callback=confirm_callback,
            ):
                return
            logger.warning(f"[PostActions] Unknown action requested: {action}")
        except subprocess.TimeoutExpired:
            logger.warning(f"[PostActions] Action '{action_type}' timed out.")
        except Exception as exc:
            logger.error(f"Failed to execute post action '{action_type}': {exc}")


PostDownloadManager.register_action(PostActionType.OPEN_FOLDER.value, PostDownloadManager._action_open_folder, allow_extension=True)
PostDownloadManager.register_action(PostActionType.PLAY_SOUND.value, PostDownloadManager._action_play_sound, allow_extension=True)
PostDownloadManager.register_action(PostActionType.SHUTDOWN.value, PostDownloadManager._action_shutdown, allow_extension=False)
PostDownloadManager.register_action(PostActionType.RUN_SCRIPT.value, PostDownloadManager._action_run_script, allow_extension=False)
PostDownloadManager.register_action(PostActionType.TRANSCRIBE.value, PostDownloadManager._action_transcribe, allow_extension=False)
PostDownloadManager.register_action("summarize", PostDownloadManager._action_summarize, allow_extension=False)
