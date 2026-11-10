"""
iTunes process helpers for Windows (`itunes_automation.py`).

What is left after the 2026-11-10 clean-up: is iTunes running, and start it / bring it forward.
  * Backups are taken by `backup_runner.py` (idevicebackup2, CLI-first) on Windows too; the old
    keystroke-driven "Back Up Now" automation was removed.
  * Restores are driven by `restorer.py ui-restore` (UI Automation over iTunes' controls, verified by the
    next look); the old blind `{ENTER}` restore was removed. Never click "Restore Backup..." on the Summary page.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from common import wait_until

try:
    import win32con
    import win32gui
    HAS_PYWIN32 = True
except Exception:
    HAS_PYWIN32 = False


class ITunesAutomation:
    """Start / find iTunes on Windows. No keystrokes, no clicks."""

    def __init__(self, itunes_exe: Optional[str] = None):
        if not itunes_exe:
            try:
                from itunes_env import restore_app
                route = restore_app()
                itunes_exe = route["exe"] if route.get("kind") == "itunes_win32" else None
            except Exception:  # noqa: BLE001
                itunes_exe = None
        self.itunes_exe = Path(itunes_exe or Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "iTunes" / "iTunes.exe")

    def is_itunes_running(self) -> bool:
        """Checks if iTunes.exe is actively running (psutil if present, else tasklist)."""
        try:
            import psutil
            for p in psutil.process_iter(["name"]):
                if p.info["name"] and "itunes.exe" in p.info["name"].lower():
                    return True
            return False
        except Exception:
            pass
        # Fallback: Windows tasklist (no psutil dependency).
        if sys.platform == "win32":
            try:
                out = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq iTunes.exe", "/NH"],
                    capture_output=True, text=True, timeout=5,
                ).stdout
                return "itunes.exe" in out.lower()
            except Exception:
                pass
        return False

    def launch_or_focus_itunes(self) -> bool:
        """Starts iTunes if it isn't running, then restores its window (best effort). True when iTunes runs."""
        if not self.is_itunes_running():
            if not self.itunes_exe.exists():
                return False
            flags = 0
            if sys.platform == "win32":
                flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            subprocess.Popen([str(self.itunes_exe)], shell=False, creationflags=flags)
            wait_until(self.is_itunes_running, timeout=20, interval=0.5)
            time.sleep(2)  # the process being up does not mean its window is

        if HAS_PYWIN32:
            try:
                def enum_handler(hwnd, _):
                    if win32gui.IsWindowVisible(hwnd) and "itunes" in win32gui.GetWindowText(hwnd).lower():
                        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.EnumWindows(enum_handler, None)
            except Exception:  # noqa: BLE001
                pass
        return self.is_itunes_running()


def main() -> int:
    driver = ITunesAutomation()
    running = driver.is_itunes_running()
    print(f"iTunes running: {running} ({driver.itunes_exe})")
    print("Backups: python backup_runner.py   Restore: python restorer.py ui-restore")
    return 0


if __name__ == "__main__":
    sys.exit(main())
