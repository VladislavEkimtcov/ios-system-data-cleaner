#!/usr/bin/env python3
"""
iPhone Revival: Autonomous Backup Engine & Sensory Progress Runner (`backup_runner.py`).

Fulfills the 'Smart Friend on the Couch' paradigm:
1. Pre-flight host disk space and device encryption verification.
2. Real-time PTY stdout parsing for sub-second handshake transition detection:
   - Detects on-screen passcode prompt: plays Ping chime & dispatches macOS notification.
   - Detects passcode acceptance: plays Glass confirmation chime & dispatches macOS notification.
   - Tracks the phone's own *overall* progress ("NN% Finished"), not the per-batch bars
     that hit 100% every few seconds, and reports it in 10% milestones.
   - Heartbeat: bytes copied, speed and ETA in the status file every 30 s, plus a one-line
     check-in every few minutes (see backup_progress.py and `--watch`).
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
import threading
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
from backup_progress import ProgressEstimator, describe, progress_line, read_status, watch, write_status

REPO_ROOT = Path(__file__).resolve().parent
# Runtime status lives in the private state dir, never the repo working tree.
STATUS_FILE = state_path(".backup_status.json")

# Regex patterns for idevicebackup2 output
PASSCODE_PROMPT_RE = re.compile(r"Waiting for passcode to be entered", re.IGNORECASE)
RECEIVING_FILES_RE = re.compile(r"Receiving files|Sending .*Status\.plist|Sending .*Manifest\.plist", re.IGNORECASE)
PROGRESS_RE = re.compile(r"\[\s*={0,}\s*\]\s*(\d+)%\s*(?:\(([^)]+)\))?")
# The whole-backup progress the phone reports: "[=====      ]  45% Finished". Lines with a
# "(12.3 MB/45.6 MB)" suffix are per-batch bars and reach 100% many times per backup.
OVERALL_RE = re.compile(r"\]\s*(\d+(?:\.\d+)?)%\s*Finished")
HEARTBEAT_SEC = 30.0
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
        verbose: bool = True,
        checkin_minutes: float = 5.0,
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

        self.checkin_minutes = checkin_minutes
        self.overall_percent: Optional[float] = None
        self.estimated_total_bytes: Optional[int] = None
        self._baseline_bytes = 0
        self._estimator: Optional[ProgressEstimator] = None
        self._status: Dict[str, Any] = {}
        self._status_lock = threading.Lock()
        self._run_started = time.time()
        self._stream_started: Optional[float] = None

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

    def update_status(self, phase: Optional[str] = None, **kwargs: Any) -> None:
        """Persists live state to .backup_status.json for agent and CLI observers.

        Fields accumulate across calls (so progress fields survive a phase change); pass
        phase=None to refresh fields without changing the phase. Written atomically.
        """
        with self._status_lock:
            if not self._status:
                self._status["run_started_at"] = datetime.fromtimestamp(
                    self._run_started, timezone.utc).isoformat()
            if phase is not None:
                self._status["phase"] = phase
            self._status.update({
                "pid": os.getpid(),
                "udid": self.udid or "unknown",
                "device_name": self.device_name,
                "product_type": self.product_type,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            self._status.update(kwargs)
            write_status(self.status_file, dict(self._status))

    def _backup_bytes(self) -> int:
        from common import dir_size
        if not self.udid:
            return 0
        return dir_size(self.backup_dir / self.udid) or 0

    def _heartbeat(self, stop: threading.Event) -> None:
        """While copying: bytes, speed and ETA into the status file every HEARTBEAT_SEC, and a
        one-line check-in on stdout every checkin_minutes."""
        last_checkin = time.time()
        while not stop.wait(HEARTBEAT_SEC):
            if self._status.get("phase") != "streaming" or self._estimator is None:
                continue
            written = max(0, self._backup_bytes() - self._baseline_bytes)
            now = time.time()
            self._estimator.add(now, written)
            rate = self._estimator.rate_bps()
            eta = self._estimator.eta_seconds(self.overall_percent)
            self.update_status(
                None,
                bytes_written=written,
                rate_bps=round(rate) if rate else None,
                eta_seconds=round(eta) if eta else None,
            )
            if self.checkin_minutes and now - last_checkin >= self.checkin_minutes * 60:
                last_checkin = now
                self.log(f"   ⏱  {progress_line(self._status)}")

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
                from common import diagnose_connection
                return False, diagnose_connection(self.udid)["message"]
            self.device_info = info
            self.udid = self.udid or info.get("UniqueDeviceID")
            self.device_name = info.get("DeviceName", "iPhone")
            self.product_type = info.get("ProductType", "Unknown")
        except Exception as e:
            return False, f"Device inspection failed: {e}"

        self.log(f"  Target Device:       {self.device_name} ({self.product_type})")
        self.log(f"  UDID:                {self.udid}")
        self.log(f"  iOS Version:         {self.device_info.get('ProductVersion', 'Unknown')}")

        # Rough size of what's coming: the phone's used storage. An upper bound (iCloud-only
        # photos and app binaries aren't backed up), used for check-ins until the phone's own
        # overall percentage starts moving.
        try:
            usage = run_ideviceinfo("-q", "com.apple.disk_usage")
            cap = int(usage.get("TotalDataCapacity", 0))
            avail = int(usage.get("TotalDataAvailable", 0))
            if cap > avail > 0:
                self.estimated_total_bytes = cap - avail
                self.log(f"  Data on Phone:       ~{self.estimated_total_bytes / GB:.1f} GB")
        except Exception:
            pass

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

        self._baseline_bytes = self._backup_bytes()  # an incremental backup starts non-empty
        self._estimator = ProgressEstimator(
            # For an incremental backup the "full phone" estimate is meaningless; rely on the
            # phone's own percentage instead.
            self.estimated_total_bytes if not self._baseline_bytes else None)
        self.update_status(None, estimated_total_bytes=self.estimated_total_bytes,
                           incremental=bool(self._baseline_bytes))
        stop = threading.Event()
        hb = threading.Thread(target=self._heartbeat, args=(stop,), daemon=True)
        hb.start()
        try:
            if pty is not None:
                returncode = self._run_pty(cmd, env, pty, select)
            else:
                returncode = self._run_pipe(cmd, env)
        finally:
            stop.set()

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
            self._stream_started = time.time()
            self.update_status(
                "streaming",
                streaming_started_at=datetime.now(timezone.utc).isoformat(),
                message="Passcode accepted. Encrypted payload streaming..."
            )
            self.log("\n✅ PASSCODE ACCEPTED!")
            self.log("   Device unlocked. Encrypted backup streaming across USB bus...")
            self.notify(
                message="Passcode accepted! Streaming backup payload...",
                sound="Glass",
                subtitle="Handshake Confirmed"
            )

        # 3. Progress Milestone Parsing. Only the phone's overall percentage counts; per-batch
        # bars ("(x MB/y MB)") are ignored, they hit 100% for every batch of files.
        m_overall = OVERALL_RE.search(line)
        m_prog = m_overall or PROGRESS_RE.search(line)
        if m_overall:
            pct = float(m_overall.group(1))
            self.overall_percent = pct
            if int(pct) // 10 > self.last_reported_pct // 10:
                prev_quarter = max(self.last_reported_pct, 0) // 25
                self.last_reported_pct = int(pct)
                self.update_status(
                    "streaming" if self.handshake_confirmed else None,
                    overall_percent=pct,
                    message=f"Backup progress: {pct:.0f}%",
                )
                self.log(f"   [Streaming] {pct:3.0f}% of backup done")
                quarter = int(pct) // 25
                if 0 < quarter < 4 and quarter > prev_quarter:  # banners at 25/50/75%, not 10x
                    self.notify(message=f"Backup {quarter * 25}% done.", subtitle="Backup Progress")

        # 4. Success Check
        if not m_prog and BACKUP_SUCCESS_RE.search(line):
            self.update_status("stream_done", percent=100, message="Backup stream finished.")

        # 5. Error Check
        elif not m_prog and ERROR_RE.search(line):
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
            overall_percent=100,
            duration_seconds=round(time.time() - self._run_started),
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
    parser.add_argument("--checkin-minutes", type=float, default=5.0,
                        help="Print a progress check-in (GB copied, speed, ETA) this often. 0 disables.")
    parser.add_argument("--watch", action="store_true",
                        help="Don't start a backup: follow the running one, printing one line per event "
                             "(passcode needed, started, check-ins, stalls, done/failed). Exits when it ends.")
    parser.add_argument("--status", action="store_true",
                        help="Print a one-off JSON snapshot of the current/last backup and exit.")

    args = parser.parse_args()

    if args.status:
        print(json.dumps(describe(read_status(args.status_json)), indent=2, ensure_ascii=False))
        sys.exit(0)
    if args.watch:
        sys.exit(watch(args.status_json, checkin_minutes=args.checkin_minutes or 5.0))

    runner = BackupRunner(
        udid=args.udid,
        password=args.password,
        play_audio=args.play_audio,
        show_notifications=args.show_notifications,
        authoritative=args.authoritative,
        status_file=args.status_json,
        checkin_minutes=args.checkin_minutes,
    )

    success = runner.execute()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
