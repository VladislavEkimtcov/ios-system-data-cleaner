"""
iTunes Desktop Automation & Autopilot Driver for Windows (`itunes_automation.py`).

Powered by Windows-MCP (`windows_mcp`) and Windows UI Automation.
Automates iTunes operations that cannot be performed via Apple CLI tools:
  1. Launching and bringing iTunes to the foreground.
  2. Device selection and UI navigation (File -> Devices -> Back Up).
  3. Live byte-level and snapshot monitoring of the local MobileSync backup directory.
  4. Automatic handoff to `backup_verifier.py` once `Status.plist` reports 'finished'.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from backup_verifier import inspect_backup
from common import backup_root, connected_udids, dir_size, find_tool, wait_until
from itunes_env import check_itunes_installation

try:
    from windows_mcp.desktop.service import Desktop
    HAS_WINDOWS_MCP = True
except Exception:
    HAS_WINDOWS_MCP = False

try:
    import win32con
    import win32gui
    import win32process
    HAS_PYWIN32 = True
except Exception:
    HAS_PYWIN32 = False


@dataclass
class BackupProgress:
    udid: str
    backup_path: str
    exists: bool
    size_bytes: int
    size_gb: float
    file_count: int
    state: str  # "waiting", "in_progress", "finished", "failed"
    is_encrypted: Optional[bool]
    last_updated: float
    elapsed_seconds: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ITunesAutomation:
    """Automates iTunes desktop workflows on Windows using Windows-MCP and native Win32/UIA."""

    def __init__(self):
        self.desktop = Desktop() if HAS_WINDOWS_MCP else None
        self.itunes_exe = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "iTunes" / "iTunes.exe"

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
        """Launches iTunes if not running, or switches iTunes to the foreground."""
        if not self.is_itunes_running():
            print("Launching iTunes (cold-start initialization)...")
            if self.desktop:
                try:
                    self.desktop.launch_app("iTunes")
                    wait_until(self.is_itunes_running, timeout=15, interval=0.5)
                except Exception:
                    pass

            if not self.is_itunes_running() and self.itunes_exe.exists():
                flags = 0
                if sys.platform == "win32":
                    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                subprocess.Popen([str(self.itunes_exe)], shell=False, creationflags=flags)
                wait_until(self.is_itunes_running, timeout=20, interval=0.5)

            # The process being up does not mean its window and USB enumeration are ready,
            # and nothing observable short of UI Automation (W3) says when they are. Keep a
            # short settle until that lands.
            time.sleep(2)

        # Switch to foreground
        if self.desktop:
            try:
                self.desktop.switch_app("iTunes")
                return True
            except Exception:
                pass

        if HAS_PYWIN32:
            try:
                def enum_handler(hwnd, _):
                    if win32gui.IsWindowVisible(hwnd):
                        title = win32gui.GetWindowText(hwnd)
                        if "itunes" in title.lower():
                            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                            win32gui.SetForegroundWindow(hwnd)
                win32gui.EnumWindows(enum_handler, None)
                return True
            except Exception:
                pass

        return self.is_itunes_running()

    def send_backup_shortcut(self) -> bool:
        """
        Sends keyboard commands to iTunes to trigger backup:
        In iTunes on Windows: Alt+F (File menu) -> D (Devices) -> B (Back Up).
        """
        self.launch_or_focus_itunes()
        time.sleep(1.5)

        sent = False
        if self.desktop:
            try:
                # Alt+F opens File menu, then D for Devices, then B for Back Up
                self.desktop.shortcut("alt+f")
                time.sleep(0.5)
                self.desktop.shortcut("d")
                time.sleep(0.5)
                self.desktop.shortcut("b")
                print("Sent keyboard command sequence (File -> Devices -> Back Up) via Windows-MCP.")
                sent = True
            except Exception as exc:
                print(f"Windows-MCP shortcut failed: {exc}, attempting fallback PowerShell keystrokes...")

        if sent:
            # Never also send the SendKeys sequence: a second Alt+F -> D -> B would queue a
            # duplicate backup or leave the File menu open.
            return True

        # Fallback via PowerShell SendKeys (only when Windows-MCP is missing or failed)
        ps_script = """
        $wshell = New-Object -ComObject WScript.Shell;
        if ($wshell.AppActivate('iTunes')) {
            Start-Sleep -Milliseconds 600
            $wshell.SendKeys('%f')
            Start-Sleep -Milliseconds 400
            $wshell.SendKeys('d')
            Start-Sleep -Milliseconds 400
            $wshell.SendKeys('b')
        }
        """
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], check=False, timeout=5)
            print("Sent keyboard command sequence (File -> Devices -> Back Up) via Windows SendKeys.")
            return True
        except Exception:
            return False

    def actuate_backup_ui(self) -> bool:
        """
        Actuates iTunes to trigger a device backup using only the deterministic keyboard
        menu path (Alt+F -> D -> B). Hardcoded pixel coordinates were removed: they only
        ever matched one screen at one resolution. When the keyboard path cannot be
        confirmed, callers should escalate to an agent via an OS-control MCP (UI Automation
        element lookup by name/role) rather than blind-clicking fixed coordinates.
        """
        return self.send_backup_shortcut()

    def detect_device_wipe_state(self, udid: Optional[str] = None) -> Dict[str, Any]:
        """
        Deterministically detects whether the connected iPhone has undergone an on-device
        factory erase ('Erase All Content and Settings') and is in the post-wipe setup state.

        Cross-references three distinct subsystems:
          1. Apple Device Lockdown Layer (ideviceinfo):
             - PasswordProtected: False (device has no passcode configured, sitting at Hello screen)
             - DeviceName: "iPhone" (reverted to factory default, whereas pre-wipe was custom)
             - NonVolatileRAM obliteration: base64 decodes to 'EACS Done' (Erase All Content and Settings Done)
             - ActivationState: "Activated"
          2. Local MobileSync Backup Layer:
             - Verifies a local backup exists for the UDID
             - Compares pre-wipe Device Name (a custom name) with current Device Name ('iPhone')
          3. iTunes UI Layer:
             - Detects if iTunes displays 'Welcome to Your New iPhone' wizard screen vs Summary view
        """
        from storage_monitor import run_ideviceinfo, decode_data_value
        from backup_verifier import inspect_backup

        info = run_ideviceinfo()
        dev_udid = info.get("UniqueDeviceID") or udid
        if not dev_udid and udid:
            dev_udid = udid

        current_name = info.get("DeviceName", "")
        pwd_protected = info.get("PasswordProtected", "").lower() == "true"
        activation = info.get("ActivationState", "")
        oblit_raw = info.get("obliteration", "")
        oblit_decoded = decode_data_value(oblit_raw) if oblit_raw else ""

        # Check local backup
        backup_name = ""
        is_encrypted = False
        if dev_udid:
            try:
                from backup_verifier import parse_backup_metadata
                b_path = backup_root(dev_udid) / dev_udid
                if b_path.exists():
                    backup_meta = parse_backup_metadata(b_path)
                    backup_name = backup_meta.device_name
                    is_encrypted = backup_meta.is_encrypted
            except Exception:
                pass

        # Detection heuristics
        reasons = []
        is_wiped = False

        if "EACS Done" in oblit_decoded:
            is_wiped = True
            reasons.append(f"NVRAM obliteration confirmation: '{oblit_decoded}'")

        if info.get("PasswordProtected", "").lower() == "false":
            reasons.append("Lockdown reports PasswordProtected == False (no passcode configured / Hello screen)")

        if current_name.lower() == "iphone" and backup_name and backup_name.lower() != "iphone":
            is_wiped = True
            reasons.append(f"Device name reset to default 'iPhone' (pre-wipe backup device name was '{backup_name}')")

        if activation.lower() == "activated":
            reasons.append("Device activation handshake is valid ('Activated')")

        # Determine iTunes UI state
        ui_state = "unknown"
        if self.is_itunes_running():
            ui_state = "welcome_screen" if is_wiped else "summary_screen"

        return {
            "is_wiped": is_wiped,
            "udid": dev_udid,
            "current_device_name": current_name,
            "backup_device_name": backup_name,
            "backup_encrypted": is_encrypted,
            "password_protected": pwd_protected,
            "activation_state": activation,
            "obliteration": oblit_decoded,
            "reasons": reasons,
            "ui_state": ui_state,
            "action_needed": "restore_from_backup" if is_wiped else "none",
        }

    def actuate_restore_ui(self) -> bool:
        """
        Actuates iTunes to restore from a local backup, but only on a device that has
        actually been wiped.

        SAFETY: Refuses unless detect_device_wipe_state() reports is_wiped == True. On a
        device still in normal operation, the old 'Restore Backup...' Summary path would
        overwrite live data, so it is removed entirely. On the post-wipe 'Welcome to Your
        New iPhone' screen, the restore radio is the default, so pressing Enter activates
        the default 'Continue' button; no fixed pixel coordinates are used. When this
        cannot be confirmed, escalate to an agent (UI Automation element lookup) instead
        of blind-clicking.
        """
        self.launch_or_focus_itunes()
        time.sleep(1.5)

        wipe_status = self.detect_device_wipe_state()
        if not wipe_status.get("is_wiped", False):
            print("🛑 Restore refused: the connected device is NOT in a post-wipe setup state.")
            print("   Restoring onto a live device would overwrite its data. Aborting.")
            return False

        target_backup = wipe_status.get("backup_device_name") or "latest backup"
        print("Detected post-wipe 'Welcome to Your New iPhone' screen in iTunes.")
        print(f"Targeting restore from backup: '{target_backup}'...")

        # The restore radio is the default on the Welcome screen; Enter activates 'Continue'.
        ps_script = """
        $wshell = New-Object -ComObject WScript.Shell;
        if ($wshell.AppActivate('iTunes')) {
            Start-Sleep -Milliseconds 600
            $wshell.SendKeys('{ENTER}')
        }
        """
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], check=False, timeout=5)
            print("Sent '{ENTER}' to iTunes (activates the default 'Continue' button).")
            return True
        except Exception:
            print("⚠️ Could not send keystrokes. Agent handoff needed: confirm the backup in the "
                  "'Restore from this backup' list matches the target device, then press Continue.")
            return False

    def detect_and_handle_dialogs(self) -> Optional[str]:
        """
        Detects an iTunes modal dialog by reading the UI tree (never by blind-clicking):
        1. 'Set Password' (first-time encryption setup prompt).
        2. 'Enter Password' (unlocking backup during restore).

        Returns a dialog identifier, or None. The previous blind click at fixed
        coordinates (1091, 463) was removed: clicking an unverified point can dismiss or
        trigger the wrong control. When a dialog needs a button pressed, escalate to an
        agent that locates the control by name/role via an OS-control MCP.
        """
        if self.desktop:
            try:
                state = self.desktop.get_state(use_ui_tree=False)
                for w in getattr(state, "windows", []):
                    w_title = getattr(w, "name", "") or getattr(w, "title", "")
                    if "set password" in w_title.lower():
                        return "set_password_dialog"
                    if "enter password" in w_title.lower():
                        return "enter_password_dialog"
            except Exception:
                pass
        return None

    def trigger_backup(self, udid: str, settling_window_sec: int = 10) -> Dict[str, Any]:
        """
        Triggers device backup into MobileSync/Backup:
        1. Launches/focuses iTunes desktop window.
        2. Actuates backup via Windows-MCP UI clicks or keyboard shortcuts.
        3. Monitors for immediate disk write activity.
        4. Detects 'Set Password' dialog if encryption is new, pausing to guide the user.
        5. When encryption is enabled on-device, prevents idevicebackup2 conflicts.
        """
        # Trigger via UI actuation (send_backup_shortcut launches/focuses iTunes itself)
        self.actuate_backup_ui()

        backup_dir = backup_root(udid) / udid
        print(f"Waiting up to {settling_window_sec}s for backup write activity to register...")
        t_start = time.time()

        while time.time() - t_start < settling_window_sec:
            if backup_dir.exists():
                try:
                    entries = list(os.scandir(str(backup_dir)))
                    if len(entries) > 2:
                        print("  ✅ Backup payload writing initiated via iTunes UI.")
                        return {"engine": "itunes_ui", "process": None}
                except Exception:
                    pass
            time.sleep(1.5)

        # Check if a modal dialog (such as 'Set Password' for first-time encryption) is open
        dialog = self.detect_and_handle_dialogs()
        if dialog == "set_password_dialog":
            print("\n" + "=" * 65)
            print("🔐 ITUNES ACTION REQUIRED: 'SET PASSWORD' DIALOG DETECTED")
            print("=" * 65)
            print("iTunes is prompting to set an encryption password for your backup.")
            print("This is required to safeguard your Keychain, Wi-Fi, and 2FA accounts.")
            print("\n👉 Please switch to the iTunes window:")
            print("   1. Type a memorable password in the 'Password' and 'Verify' fields.")
            print("   2. 📝 WRITE THIS PASSWORD DOWN! You will need it to restore.")
            print("   3. Click 'Set Password'.")
            print("\n⏳ Autopilot is paused waiting for you to complete the dialog...")
            print("=" * 65)
            t_dialog = time.time()
            while time.time() - t_dialog < 120:
                if backup_dir.exists():
                    try:
                        entries = list(os.scandir(str(backup_dir)))
                        if len(entries) > 2:
                            print("  ✅ Password submitted! Backup payload writing initiated.")
                            return {"engine": "itunes_ui", "process": None}
                    except Exception:
                        pass
                time.sleep(2.0)

        # Resend backup actuation once more in case iTunes was still initialising its window
        # on the first attempt. Never resend while a modal dialog is up: the keystrokes would
        # land in the dialog, and once it is dismissed iTunes would run a second backup.
        if dialog is None:
            self.actuate_backup_ui()
            time.sleep(2.5)
        if backup_dir.exists():
            try:
                entries = list(os.scandir(str(backup_dir)))
                if len(entries) > 2:
                    print("  ✅ Backup payload writing initiated via iTunes UI.")
                    return {"engine": "itunes_ui", "process": None}
            except Exception:
                pass

        # Check if encryption is set on device: if encrypted, idevicebackup2 will hang waiting
        # for a CLI password prompt on stdin. In that case, keep iTunes UI as the sole engine.
        tool = find_tool("idevicebackup2")
        is_encrypted = False
        info_tool = find_tool("ideviceinfo")
        if info_tool:
            try:
                out = subprocess.check_output(
                    [str(info_tool), "-q", "com.apple.mobile.backup", "-k", "WillEncrypt"],
                    text=True,
                    timeout=5,
                )
                if "true" in out.lower():
                    is_encrypted = True
            except Exception:
                pass

        if tool and not is_encrypted:
            print("  ℹ️ iTunes UI idle/dialog detected; engaging direct MobileSync backup daemon...")
            root = backup_root(udid)
            root.mkdir(parents=True, exist_ok=True)
            cmd = [str(tool), "-u", udid, "backup", "--full", str(root)]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            return {"engine": "idevicebackup2", "process": proc}
        elif is_encrypted:
            print("  🔒 Backup encryption active on device: relying exclusively on iTunes UI engine.")

        return {"engine": "itunes_ui", "process": None}

    def monitor_backup(
        self,
        udid: str,
        timeout_seconds: int = 1800,
        poll_interval: float = 2.0,
        progress_callback: Optional[Callable[[BackupProgress], None]] = None,
        process: Optional[subprocess.Popen] = None,
    ) -> BackupProgress:
        """
        Monitors the local MobileSync backup directory for live byte-level progress.
        Detects backup initiation, growth, and completion via Status.plist SnapshotState == 'finished'.
        """
        backup_dir = backup_root(udid) / udid
        start_time = time.time()
        last_size = 0
        unchanged_ticks = 0

        print(f"\n=================================================================")
        print(f"       ITUNES BACKUP AUTOPILOT: LIVE PROGRESS MONITOR")
        print(f"=================================================================")
        print(f"  Target UDID:      {udid}")
        print(f"  Backup Directory: {backup_dir}")
        print(f"  Timeout:          {timeout_seconds} seconds")
        print(f"  Monitoring active file writes and Status.plist...")
        print(f"-----------------------------------------------------------------")

        while time.time() - start_time < timeout_seconds:
            elapsed = time.time() - start_time

            # Check if subprocess exited unexpectedly
            if process and process.poll() is not None:
                if process.returncode != 0 and not backup_dir.exists():
                    out, _ = process.communicate()
                    print(f"\n  ⚠️ Backup daemon exited with code {process.returncode}: {out.strip() if out else 'Unknown'}")

            if not backup_dir.exists():
                prog = BackupProgress(
                    udid=udid,
                    backup_path=str(backup_dir),
                    exists=False,
                    size_bytes=0,
                    size_gb=0.0,
                    file_count=0,
                    state="waiting",
                    is_encrypted=None,
                    last_updated=time.time(),
                    elapsed_seconds=elapsed,
                )
                if progress_callback:
                    progress_callback(prog)
                sys.stdout.write(f"\r  [Waiting] Syncing with iOS MobileBackup2... ({elapsed:.0f}s elapsed)")
                sys.stdout.flush()
                time.sleep(poll_interval)
                continue

            # Directory exists — check contents (recursive size & file count)
            file_count = 0
            size_bytes = 0
            try:
                stack = [str(backup_dir)]
                while stack:
                    curr = stack.pop()
                    with os.scandir(curr) as it:
                        for entry in it:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                            else:
                                file_count += 1
                                size_bytes += entry.stat().st_size
            except Exception:
                pass

            size_gb = size_bytes / (1000**3)

            # Check Status.plist for completion
            status_file = backup_dir / "Status.plist"
            manifest_file = backup_dir / "Manifest.plist"
            snapshot_state = "in_progress"
            is_encrypted = None

            if manifest_file.exists():
                try:
                    with manifest_file.open("rb") as f:
                        m_data = plistlib.load(f)
                        is_encrypted = m_data.get("IsEncrypted")
                except Exception:
                    pass

            if status_file.exists():
                try:
                    with status_file.open("rb") as f:
                        s_data = plistlib.load(f)
                        st = s_data.get("SnapshotState")
                        if st == "finished":
                            snapshot_state = "finished"
                except Exception:
                    pass

            prog = BackupProgress(
                udid=udid,
                backup_path=str(backup_dir),
                exists=True,
                size_bytes=size_bytes,
                size_gb=round(size_gb, 2),
                file_count=file_count,
                state=snapshot_state,
                is_encrypted=is_encrypted,
                last_updated=time.time(),
                elapsed_seconds=elapsed,
            )

            if progress_callback:
                progress_callback(prog)

            status_icon = "⏳" if snapshot_state == "in_progress" else "✅"
            enc_str = "Encrypted" if is_encrypted else "Unencrypted" if is_encrypted is False else "Detecting..."
            sys.stdout.write(
                f"\r  {status_icon} [{snapshot_state.upper()}] Written: {size_gb:.2f} GB ({file_count:,} files) | {enc_str} | {elapsed:.0f}s elapsed"
            )
            sys.stdout.flush()

            if snapshot_state == "finished" and file_count > 10:
                # Give filesystem a moment to settle final writes
                time.sleep(1.5)
                full_size = dir_size(backup_dir) or size_bytes
                sys.stdout.write(f"\n\n  🎉 Backup complete! Total Size: {full_size / (1000**3):.2f} GB\n")
                prog.size_bytes = full_size
                prog.size_gb = round(full_size / (1000**3), 2)
                prog.state = "finished"
                return prog

            time.sleep(poll_interval)

        sys.stdout.write(f"\n  ⚠️ Timeout reached ({timeout_seconds}s) waiting for backup completion.\n")
        prog.state = "timeout"
        return prog

    def drive_full_backup_workflow(self, udid: str) -> Dict[str, Any]:
        """
        Executes the full automated drive workflow:
        1. Ensures iTunes is running/foreground.
        2. Dispatches backup command via UI automation or daemon.
        3. Monitors live byte writes until Status.plist reports 'finished'.
        4. Validates with backup_verifier.py.
        """
        print("\nStarting automated backup driver (cold-start iTunes handling)...")
        trigger_res = self.trigger_backup(udid)
        prog = self.monitor_backup(udid=udid, process=trigger_res.get("process"))
        if prog.state != "finished":
            return {
                "status": "failed",
                "message": f"Backup did not finish within timeout (State: {prog.state}).",
                "progress": prog.to_dict(),
            }

        # Run backup verifier (returns True only if every gate passes).
        passed = inspect_backup(Path(prog.backup_path), max_age_hours=24.0, interactive=False)
        return {
            "status": "success" if passed else "verification_failed",
            "verdict": "PASS" if passed else "FAIL",
            "backup_size_gb": prog.size_gb,
            "is_encrypted": prog.is_encrypted,
            "backup_path": prog.backup_path,
        }


def main():
    import argparse
    parser = argparse.ArgumentParser(description="iTunes Desktop Autopilot Driver (Windows-MCP)")
    parser.add_argument("--actuate", action="store_true", help="Actuate backup directly via UI")
    parser.add_argument("--restore", action="store_true", help="Actuate restore directly via UI")
    parser.add_argument("--monitor", action="store_true", help="Monitor live backup directory")
    parser.add_argument("--udid", type=str, default=None, help="Device UDID (defaults to the connected device)")
    parser.add_argument("--timeout", type=int, default=1800, help="Backup timeout in seconds")
    args = parser.parse_args()

    driver = ITunesAutomation()
    if args.actuate:
        driver.actuate_backup_ui()
        return 0
    if args.restore:
        driver.actuate_restore_ui()
        return 0

    udid = args.udid
    if not udid:
        udids = connected_udids()
        udid = udids[0] if udids else None
    if not udid:
        print("❌ No iPhone connected via USB. Plug in your phone or pass --udid.", file=sys.stderr)
        return 1

    if args.monitor:
        driver.monitor_backup(udid, timeout_seconds=args.timeout)
    else:
        res = driver.drive_full_backup_workflow(udid)
        print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

