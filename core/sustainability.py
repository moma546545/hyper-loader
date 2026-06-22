
"""
core/sustainability.py — Sustainability & Power Management Mode
Performs configurable actions when the download queue is fully completed.
Actions: do nothing | show notification | sleep | hibernate | shutdown.

C-05 FIX: Added confirm_callback — destructive actions (sleep/hibernate/shutdown)
now require explicit user confirmation before executing.
"""
import os
import sys
import time
import subprocess
import platform
import logging
import threading
from typing import Optional, Callable

logger = logging.getLogger("SnapDownloader.Sustainability")

ACTIONS = {
    "none":       "Do Nothing",
    "notify":     "Show Notification Only",
    "sleep":      "Sleep Computer",
    "hibernate":  "Hibernate Computer",
    "shutdown":   "Shutdown Computer",
}

DESTRUCTIVE_ACTIONS = {"sleep", "hibernate", "shutdown"}


def _run_system_action(command: list[str]) -> None:
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if int(getattr(result, "returncode", 0) or 0) != 0:
            stderr = str(getattr(result, "stderr", "") or "").strip()
            logger.warning(
                "[Sustainability] Command failed (%s): %s",
                result.returncode,
                stderr[:300],
            )
    except Exception as exc:
        logger.warning(f"[Sustainability] Failed to run command {command}: {exc}")


class SustainabilityMode:
    def __init__(self):
        self.action: str = "none"
        self.delay_seconds: int = 60   # grace period before executing action
        self.enabled: bool = False
        self._countdown_timer = None
        self._cancel_event = threading.Event()
        # C-05: Callback that must return True to allow destructive actions.
        # Signature: confirm_callback(action_label: str) -> bool
        self.confirm_callback: Optional[Callable[[str], bool]] = None

    # ── Configuration ─────────────────────────────────────────────────────────

    def configure(self, action: str, delay_seconds: int = 60):
        action = action.strip().lower()
        if action not in ACTIONS:
            raise ValueError(f"Unknown action: {action!r}. Must be one of {list(ACTIONS)}")
        self.action = action
        self.delay_seconds = max(0, delay_seconds)
        self.enabled = action != "none"
        self._cancel_event.clear()

    def set_enabled(self, enabled: bool):
        self.enabled = enabled
        if not self.enabled:
            self._cancel_event.set()
        else:
            self._cancel_event.clear()

    def get_action_label(self) -> str:
        return ACTIONS.get(self.action, "Unknown")

    # ── Trigger ───────────────────────────────────────────────────────────────

    def on_queue_complete(self, notify_callback=None):
        """
        Called when the download queue finishes.
        notify_callback(message) — optional toast/notification function.
        """
        if not self.enabled or self.action == "none":
            return

        label = self.get_action_label()
        msg = f"Queue complete. Will {label} in {self.delay_seconds}s..."
        logger.info(f"[Sustainability] {msg}")

        if notify_callback:
            try:
                notify_callback(msg)
            except Exception as exc:
                logger.debug(f"[Sustainability] Notify callback failed: {exc}")

        # Execute in a background thread to not block any UI
        t = threading.Thread(
            target=self._execute_after_delay,
            args=(notify_callback,),
            daemon=True,
            name="SustainabilityAction"
        )
        t.start()

    def cancel(self):
        """Cancel a pending action."""
        self.enabled = False  # simple flag — thread checks this
        self._cancel_event.set()
        logger.info("[Sustainability] Action cancelled.")

    def _execute_after_delay(self, notify_callback=None):
        """Wait delay_seconds then execute the configured action."""
        for remaining in range(self.delay_seconds, 0, -1):
            if not self.enabled:
                logger.info("[Sustainability] Cancelled during countdown.")
                return
            if remaining % 10 == 0 and notify_callback:
                try:
                    notify_callback(f"⏱ {self.get_action_label()} in {remaining}s (re-enable downloads to cancel)")
                except Exception as exc:
                    logger.debug(f"[Sustainability] Countdown notify failed: {exc}")
            if self._cancel_event.wait(timeout=1):
                logger.info("[Sustainability] Cancelled during countdown.")
                return

        if not self.enabled:
            return

        # C-05: For destructive actions, require explicit confirmation
        if self.action in DESTRUCTIVE_ACTIONS:
            if self.confirm_callback is None:
                logger.warning(f"[Sustainability] No confirm_callback set — cancelling {self.action} for safety.")
                if notify_callback:
                    try:
                        notify_callback(f"⚠️ {self.get_action_label()} cancelled — no confirmation handler set.")
                    except Exception:
                        pass
                return
            try:
                label = self.get_action_label()
                confirmed = self.confirm_callback(label)
                if not confirmed:
                    logger.info(f"[Sustainability] User declined {self.action}.")
                    if notify_callback:
                        try:
                            notify_callback(f"❌ {label} cancelled by user.")
                        except Exception:
                            pass
                    return
            except Exception as exc:
                logger.warning(f"[Sustainability] Confirm callback error: {exc} — cancelling action.")
                return

        self._execute()

    def _execute(self):
        system = platform.system()
        action = self.action
        logger.info(f"[Sustainability] Executing: {action} on {system}")

        try:
            if action == "sleep":
                if system == "Windows":
                    _run_system_action(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
                elif system == "Darwin":
                    _run_system_action(["pmset", "sleepnow"])
                else:
                    _run_system_action(["systemctl", "suspend"])

            elif action == "hibernate":
                if system == "Windows":
                    _run_system_action(["shutdown", "/h"])
                elif system == "Darwin":
                    # Do not mutate the global hibernatemode setting.
                    _run_system_action(["pmset", "sleepnow"])
                else:
                    _run_system_action(["systemctl", "hibernate"])

            elif action == "shutdown":
                if system == "Windows":
                    _run_system_action(["shutdown", "/s", "/t", "0"])
                elif system == "Darwin":
                    _run_system_action(["shutdown", "-h", "now"])
                else:
                    _run_system_action(["shutdown", "now"])

            elif action == "notify":
                # Already handled via toast — just log
                logger.info("[Sustainability] Notification action complete.")

        except Exception as exc:
            logger.error(f"[Sustainability] Execution failed: {exc}")


# Singleton
sustainability = SustainabilityMode()



