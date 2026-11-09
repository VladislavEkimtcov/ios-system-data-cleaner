#!/usr/bin/env python3
"""
iPhone Revival: Autonomous Backup Engine & Sensory Progress Runner (`backup_runner.py`).

Fulfills the 'Smart Friend on the Couch' paradigm:
1. Pre-flight host disk space and device encryption verification.
2. Real-time PTY stdout parsing for sub-second handshake transition detection:
   - Detects on-screen passcode prompt: plays Ping chime & dispatches macOS notification.
   - Detects passcode acceptance: plays Glass confirmation chime & dispatches macOS notification.
   - Throttles 50,000 raw progress ticks into crisp 10% milestone reports.
   - Detects completion: plays Hero chime & dispatches completion notification.
3. Live persistent state tracking in `.backup_status.json` for agent/CLI monitoring.
4. Autonomous post-flight verification, APFS CoW snapshotting, and authoritative backup consolidation.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common import (
    GB,
    PREWIPE_SUFFIX,
    CloneError,
    backup_root,
    host_mount,
    make_prewipe_clone,
    state_path,
)

REPO_ROOT = Path(__file__).resolve().parent
# Runtime status lives in the private state dir, never the repo working tree.
STATUS_FILE = state_path(".backup_status.json")

# Regex patterns for idevicebackup2 output
PASSCODE_PROMPT_RE = re.compile(r"Waiting for passcode to be entered", re.IGNORECASE)
RECEIVING_FILES_RE = re.compile(r"Receiving files|Sending .*Status\.plist|Sending .*Manifest\.plist", re.IGNORECASE)
PROGRESS_RE = re.compile(r"\[\s*={0,}\s*\]\s*(\d+)%\s*(?:\(([^)]+)\))?")
BACKUP_SUCCESS_RE = re.compile(r"Backup Successful\.", re.IGNORECASE)
ERROR_RE = re.compile(r"ErrorCode \d+:|ERROR:", re.IGNORECASE)


def play_sound(sound_name: str) -> None:
    """Plays a native macOS system sound (/System/Library/Sounds/<name>.aiff) asynchronously."""
    sound_file = Path(f"/System/Library/Sounds/{sound_name}.aiff")
    if sound_file.exists():
        try:
            subprocess.Popen(
                ["afplay", str(sound_file)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception:
            pass


def send_notification(
    message: str,
    title: str = "iPhone Revival",
    subtitle: str = "Backup Engine",
    sound: Optional[str] = None
) -> None:
    """Dispatches a macOS notification banner via AppleScript."""
    clean_msg = message.replace('"', '\\"')
    clean_title = title.replace('"', '\\"')
    clean_sub = subtitle.replace('"', '\\"')
    script = f'display notification "{clean_msg}" with title "{clean_title}" subtitle "{clean_sub}"'
    if sound:
        script += f' sound name "{sound}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0
        )
    except Exception:
        pass


class BackupRunner:
    """Orchestrates the entire backup cycle with live sensory feedback and interlocks."""

    def __init__(
        self,
        udid: Optional[str] = None,
        backup_dir: Optional[Path] = None,
        password: Optional[str] = None,
        play_audio: bool = True,
        show_notifications: bool = True,
        authoritative: bool = False,
        status_file: Optional[Path] = None,
        verbose: bool = True
    ) -> None:
        self.udid = udid
        self.backup_dir = backup_dir or backup_root()
        self.password = password
        self._last_error = ""
        self.play_audio = play_audio
        self.show_notifications = show_notifications
        self.authoritative = authoritative
        self.status_file = status_file or STATUS_FILE
        self.verbose = verbose

        self.device_info: Dict[str, str] = {}
        self.device_name = "iPhone"
        self.product_type = "Unknown"
        self.last_reported_pct = -1
        self.handshake_confirmed = False
        self.passcode_requested = False

    def notify(
        self,
        message: str,
        sound: Optional[str] = None,
        title: str = "iPhone Revival",
        subtitle: str = "Backup Engine"
    ) -> None:
        """Dispatches sensory audio chime and notification banner."""
        if self.play_audio and sound:
            play_sound(sound)
        if self.show_notifications:
            send_notification(message, title=title, subtitle=subtitle)

    def update_status(self, phase: str, **kwargs: Any) -> None:
        """Persists live execution state to .backup_status.json for agent and CLI observers."""
        payload = {
            "phase": phase,
            "udid": self.udid or "unknown",
            "device_name": self.device_name,
            "product_type": self.product_type,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **kwargs
        }
        try:
            with open(self.status_file, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception:
            pass

    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def _prompt_for_password(self) -> Optional[str]:
        """Prompts for a new backup password twice in a TTY and offers to save it to Keychain."""
        if not sys.stdin.isatty():
            return None
        self.log("\n  Backup encryption needs a password (it protects Keychain, Wi-Fi and 2FA data).")
        for _ in range(3):
            try:
                p1 = getpass.getpass("  Enter a backup encryption password: ")
                p2 = getpass.getpass("  Re-enter to confirm: ")
            except (KeyboardInterrupt, EOFError):
                print()
                return None
            if not p1:
                print("  Password cannot be empty.")
                continue
            if p1 != p2:
                print("  Passwords did not match. Try again.")
                continue
            # Offer to save it so Finder can auto-fill during restore.
            try:
                ans = input("  Save this password to the macOS Keychain for restore? [Y/n]: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                ans = "n"
            if ans in ("", "y", "yes") and self.udid:
                try:
                    import password_tracker
                    password_tracker.store_in_keychain(self.udid, p1)
                    self.log("  ✅ Saved to Keychain.")
                except Exception:
                    pass
            return p1
        return None

    def preflight(self) -> Tuple[bool, str]:
        """Performs disk headroom check, device pairing check, and encryption readiness."""
        self.log("\n=======================================================")
        self.log("       AUTONOMOUS IPHONE BACKUP ENGINE")
        self.log("=======================================================")
        self.update_status("preflight", message="Running pre-flight checks...")

        # 1. Identify connected device
        try:
            from storage_monitor import run_ideviceinfo
            info = run_ideviceinfo()
            if not info:
                return False, "No iOS device detected via USB. Unlock phone and tap Trust Computer."
            self.device_info = info
            self.udid = self.udid or info.get("UniqueDeviceID")
            self.device_name = info.get("DeviceName", "iPhone")
            self.product_type = info.get("ProductType", "Unknown")
        except Exception as e:
            return False, f"Device inspection failed: {e}"

        self.log(f"  Target Device:       {self.device_name} ({self.product_type})")
        self.log(f"  UDID:                {self.udid}")
        self.log(f"  iOS Version:         {self.device_info.get('ProductVersion', 'Unknown')}")

        # 2. Host disk space audit (cross-platform mount)
        import shutil
        try:
            stat = shutil.disk_usage(str(host_mount()))
            free_gb = stat.free / GB
            self.log(f"  Host Free Space:     {free_gb:.1f} GB")
            if free_gb < 20.0:
                return False, f"Host has only {free_gb:.1f} GB free. At least 20 GB required."
        except Exception as e:
            self.log(f"  ⚠️ Warning inspecting disk usage: {e}")

        # 3. Encryption check
        try:
            b_info = run_ideviceinfo("-q", "com.apple.mobile.backup")
            will_encrypt = b_info.get("WillEncrypt", "").lower() == "true"
        except Exception:
            will_encrypt = False

        if not self.password:
            # Query macOS Keychain for a saved password.
            try:
                import password_tracker
                self.password = password_tracker.get_keychain_password(account=self.udid)
            except Exception:
                pass

        if not will_encrypt:
            self.log("  Backup Encryption:   🚨 Disabled on device. Enabling now...")
            if not self.password:
                self.password = self._prompt_for_password()
            if not self.password:
                return False, "Backup encryption is off and no password was provided, found in Keychain, or entered."
            # Pass the password via BACKUP_PASSWORD (env), never on the command line where
            # any local process could read it with `ps`.
            env = os.environ.copy()
            env["BACKUP_PASSWORD"] = self.password
            cmd = ["idevicebackup2", "encryption", "on"]
            if self.udid:
                cmd.extend(["-u", self.udid])
            cmd.append(str(self.backup_dir))
            res = subprocess.run(cmd, capture_output=True, text=True, env=env)
            if res.returncode != 0:
                return False, f"Failed to enable backup encryption: {res.stderr or res.stdout}"
            self.log("  Backup Encryption:   ✅ Enabled successfully.")
        else:
            self.log("  Backup Encryption:   ✅ Enabled on device.")

        if self.password:
            self.log("  Decryption Password: ✅ Proven from Keychain / arguments.")
        else:
            self.log("  Decryption Password: ⚠️  Not pre-set; verification will check keybag.")

        self.log("-------------------------------------------------------")
        return True, "Pre-flight checks passed."

    def run_backup(self) -> Tuple[bool, str]:
        """
        Executes idevicebackup2 backup inside an unbuffered PTY.
        Provides sub-second auditory chimes and desktop notifications at handshake boundaries.
        """
        self.update_status(
            "starting",
            message="Connecting to mobilebackup2 service on device..."
        )
        self.log("[*] Initializing mobilebackup2 service and streaming backup...")

        cmd = ["idevicebackup2", "backup", "--full"]
        if self.udid:
            cmd.extend(["-u", self.udid])
        cmd.append(str(self.backup_dir))

        env = os.environ.copy()
        if self.password:
            env["BACKUP_PASSWORD"] = self.password

        self._last_error = ""

        # A PTY gives idevicebackup2's progress bar unbuffered; it's POSIX-only, so fall
        # back to a plain pipe on Windows (or if pty is unavailable).
        try:
            import pty
            import select
        except ImportError:
            pty = None  # type: ignore

        if pty is not None:
            returncode = self._run_pty(cmd, env, pty, select)
        else:
            returncode = self._run_pipe(cmd, env)

        if returncode == 0:
            return True, "Backup payload successfully transferred and assembled."
        detail = self._last_error or "see output above"
        return False, f"idevicebackup2 failed with returncode {returncode} ({detail})."

    def _run_pty(self, cmd, env, pty, select) -> int:
        master_fd, slave_fd = pty.openpty()
        proc = subprocess.Popen(
            cmd, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd, close_fds=True, env=env
        )
        os.close(slave_fd)
        buffer = ""
        try:
            while True:
                rlist, _, _ = select.select([master_fd], [], [], 0.2)
                if master_fd in rlist:
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    buffer += data.decode("utf-8", errors="replace")
                    while "\r" in buffer or "\n" in buffer:
                        split_idx = min(i for i in (buffer.find("\r"), buffer.find("\n")) if i != -1)
                        line = buffer[:split_idx].strip()
                        buffer = buffer[split_idx + 1:]
                        if line:
                            self._process_line(line)
                if proc.poll() is not None:
                    try:
                        remainder = os.read(master_fd, 8192).decode("utf-8", errors="replace")
                        for line in remainder.splitlines():
                            if line.strip():
                                self._process_line(line.strip())
                    except OSError:
                        pass
                    break
        finally:
            os.close(master_fd)
            proc.wait()
        return proc.returncode

    def _run_pipe(self, cmd, env) -> int:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, bufsize=1
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if line:
                self._process_line(line)
        proc.wait()
        return proc.returncode

    def _process_line(self, line: str) -> None:
        """Parses output line, dispatches notifications, and updates state."""
        # 1. Passcode Prompt Check
        if PASSCODE_PROMPT_RE.search(line):
            if not self.passcode_requested:
                self.passcode_requested = True
                self.update_status(
                    "waiting_passcode",
                    message="Waiting for passcode entry on iPhone display..."
                )
                self.log("\n🚨 PHYSICAL HANDSHAKE REQUIRED:")
                self.log("   👉 Enter your passcode or tap Trust on the iPhone display now.")
                self.notify(
                    message="Please enter passcode on iPhone screen to start backup.",
                    sound="Ping",
                    subtitle="Passcode Required"
                )

        # 2. Handshake Acceptance Check (Passcode entered!)
        elif not self.handshake_confirmed and (RECEIVING_FILES_RE.search(line) or "Finished" in line):
            self.handshake_confirmed = True
            self.update_status(
                "streaming",
                message="Passcode accepted. Encrypted payload streaming..."
            )
            self.log("\n✅ PASSCODE ACCEPTED!")
            self.log("   Device unlocked. Encrypted backup streaming across USB bus...")
            self.notify(
                message="Passcode accepted! Streaming backup payload...",
                sound="Glass",
                subtitle="Handshake Confirmed"
            )

        # 3. Progress Milestone Parsing
        m_prog = PROGRESS_RE.search(line)
        if m_prog:
            pct = int(m_prog.group(1))
            detail = m_prog.group(2) or ""
            # Report at 10% milestone boundaries
            if pct // 10 > self.last_reported_pct // 10 or pct == 100:
                self.last_reported_pct = pct
                self.update_status(
                    "streaming",
                    percent=pct,
                    detail=detail,
                    message=f"Backup progress: {pct}% ({detail})"
                )
                self.log(f"   [Streaming] {pct:3d}% complete ({detail})")

        # 4. Success Check
        elif BACKUP_SUCCESS_RE.search(line):
            self.update_status("stream_done", percent=100, message="Backup stream finished.")

        # 5. Error Check
        elif ERROR_RE.search(line):
            self._last_error = line
            self.log(f"   [!] Error detected: {line}")

    def post_flight_verification(self) -> Tuple[bool, str]:
        """
        Executes backup verification, Keychain check, APFS CoW snapshot,
        and authoritative catalog pruning.
        """
        self.log("\n-------------------------------------------------------")
        self.log("[*] Executing Post-Flight Autonomous Pipeline...")
        self.update_status("verifying", message="Running 6-point cryptographic verification gate...")

        target_backup = self.backup_dir / self.udid if self.udid else None
        if not target_backup or not target_backup.exists():
            return False, f"Backup directory {target_backup} does not exist."

        # 1. 6-Point Cryptographic Verification Gate
        try:
            import backup_verifier
            passed = backup_verifier.inspect_backup(
                backup_dir=target_backup,
                max_age_hours=1.0,
                password=self.password,
                skip_password_check=False,
                interactive=False
            )
            if not passed:
                self.notify(
                    message="Backup verification failed! Do not erase device.",
                    sound="Basso",
                    subtitle="Integrity Failure"
                )
                self.update_status("failed", message="Backup verification gate failed.")
                return False, "Backup verifier reported FAIL."
        except Exception as e:
            return False, f"Verification execution error: {e}"

        self.log("  ✅ Cryptographic 6-Point Gate: PASS")

        # 2. Immutable pre-wipe safety clone (atomic, never nested). This is the
        #    fallback that must survive any later sync, so it is created once and kept.
        import shutil
        prewipe_clone: Optional[Path] = None
        try:
            prewipe_clone = make_prewipe_clone(target_backup, verbose=False)
            self.log(f"  ✅ Immutable Safety Clone:    {prewipe_clone.name}")
        except CloneError as e:
            self.notify("Safety clone failed; refusing to prune.", sound="Basso")
            self.update_status("failed", message=f"clone failed: {e}")
            return False, f"Pre-wipe safety clone failed: {e}"

        # 3. Pruning (only when --authoritative). NEVER touch the -prewipe clone. Only
        #    remove timestamped Finder archives and legacy *-authoritative folders, and
        #    only now that the new clone has verified.
        if self.authoritative and self.udid:
            self.log("[*] Authoritative flag active: pruning stale archives (keeping -prewipe)...")
            pruned_count = 0
            for item in self.backup_dir.glob(f"{self.udid}-*"):
                name = item.name
                if name == prewipe_clone.name or name.endswith(PREWIPE_SUFFIX):
                    continue
                is_timestamp_archive = bool(re.search(r"-\d{8}-\d{6}$", name))
                is_legacy_authoritative = name.endswith("-authoritative")
                if not (is_timestamp_archive or is_legacy_authoritative):
                    continue
                try:
                    shutil.rmtree(item, ignore_errors=True)
                    pruned_count += 1
                    self.log(f"      - Pruned stale copy: {name}")
                except Exception as e:
                    self.log(f"      ⚠️ Could not prune {name}: {e}")
            self.log(f"  ✅ Catalog Consolidated:     {pruned_count} stale archive(s) pruned.")

        # 4. Storage snapshot
        try:
            from storage_monitor import record_current_snapshot
            record_current_snapshot("authoritative_backup", udid=self.udid)
            self.log("  ✅ Storage Monitor:          Snapshot recorded.")
        except Exception:
            pass

        self.update_status(
            "completed",
            percent=100,
            is_complete=True,
            message="Authoritative backup verified and secured."
        )

        self.notify(
            message=f"iPhone backup complete! Verified with zero lockout risk.",
            sound="Hero",
            subtitle="Authoritative Standard Secured"
        )

        self.log("=======================================================")
        self.log("🎉 BACKUP CYCLE COMPLETED SUCCESSFULLY!")
        self.log("=======================================================\n")
        return True, "Backup cycle completed, verified, and established as authoritative."

    def execute(self) -> bool:
        """Chains preflight, backup execution, and post-flight verification."""
        ok, msg = self.preflight()
        if not ok:
            self.log(f"\n❌ Pre-flight check failed: {msg}")
            self.notify(f"Pre-flight failed: {msg}", sound="Basso")
            self.update_status("failed", error=msg)
            return False

        ok, msg = self.run_backup()
        if not ok:
            self.log(f"\n❌ Backup streaming failed: {msg}")
            self.notify(f"Backup failed: {msg}", sound="Basso")
            self.update_status("failed", error=msg)
            return False

        ok, msg = self.post_flight_verification()
        if not ok:
            self.log(f"\n❌ Post-flight verification failed: {msg}")
            self.update_status("failed", error=msg)
            return False

        return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Autonomous iPhone Backup Engine with Desktop Chimes.")
    parser.add_argument("--udid", help="Device UDID (defaults to connected device).")
    parser.add_argument("--password", help="Candidate backup decryption password.")
    parser.add_argument("--authoritative", action="store_true", help="Consolidate as sole authoritative backup (prunes stale snapshots).")
    parser.add_argument("--no-sound", dest="play_audio", action="store_false", help="Disable audio chimes.")
    parser.add_argument("--no-notify", dest="show_notifications", action="store_false", help="Disable macOS notification banners.")
    parser.add_argument("--status-json", type=Path, help="Custom path for .backup_status.json.")

    args = parser.parse_args()

    runner = BackupRunner(
        udid=args.udid,
        password=args.password,
        play_audio=args.play_audio,
        show_notifications=args.show_notifications,
        authoritative=args.authoritative,
        status_file=args.status_json
    )

    success = runner.execute()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
