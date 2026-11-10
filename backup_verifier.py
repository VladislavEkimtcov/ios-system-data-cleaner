#!/usr/bin/env python3
"""
iOS Backup Verifier for MobileSync Backups.

Acts as the go/no-go gate before erasing a phone. A backup PASSES only if:
  - Info.plist, Manifest.plist and Manifest.db are present and readable
  - Manifest.plist reports IsEncrypted = True (Keychain / 2FA data included)
  - Status.plist reports SnapshotState = "finished" (backup actually completed)
  - The backup is newer than --max-age-hours (default 24)

Usage:
  backup_verifier.py                 # check every backup (archives listed, not gated)
  backup_verifier.py --connected     # check only the phone plugged in via USB
  backup_verifier.py --udid <UDID>   # check one specific device

Exit code: 0 = all gated backups PASS, 1 = at least one FAIL, 2 = setup error.
"""

import argparse
import os
import plistlib
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common import (
    ARCHIVE_SUFFIX,
    GB,
    backup_root,
    connected_udids,
    count_files,
    dir_size,
    find_tool,
    keybag_fingerprint,
    load_plist,
)

# Minimum plausible size for a real full backup (configurable so test fixtures pass).
MIN_BACKUP_BYTES = int(os.environ.get("IOS_CLEANER_MIN_BACKUP_BYTES", str(100 * 1000 * 1000)))

# Back-compat module constant (tests monkeypatch this). Code that must observe a
# mid-run change should call backup_root() instead.
BACKUP_ROOT = backup_root()

# Back-compat alias for modules that imported this name from here.
get_itunes_backup_root = backup_root


@dataclass
class BackupMetadata:
    """Structured representation of an inspected iOS backup."""

    backup_path: str
    folder_name: str
    device_name: str
    product_name: str
    product_type: str
    product_version: str
    build_version: str
    serial_number: str
    udid: str
    is_encrypted: bool
    last_backup_date: Optional[str]
    snapshot_state: str
    is_full_backup: bool
    size_bytes: int
    size_gb: float
    installed_apps: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def parse_backup_metadata(backup_dir: Path | str) -> BackupMetadata:
    """Extracts device identifiers, OS build, encryption status, and apps from a backup directory."""
    path = Path(backup_dir).expanduser().resolve()
    if not path.is_dir():
        raise FileNotFoundError(f"Backup folder not found: {path}")

    info_path = path / "Info.plist"
    manifest_path = path / "Manifest.plist"
    status_path = path / "Status.plist"

    if not info_path.exists():
        raise ValueError(f"Missing Info.plist in {path}")

    with info_path.open("rb") as f:
        info_data = plistlib.load(f)

    manifest_data: Dict[str, Any] = {}
    if manifest_path.exists():
        try:
            with manifest_path.open("rb") as f:
                manifest_data = plistlib.load(f)
        except Exception:
            pass

    status_data: Dict[str, Any] = {}
    if status_path.exists():
        try:
            with status_path.open("rb") as f:
                status_data = plistlib.load(f)
        except Exception:
            pass

    last_dt = info_data.get("Last Backup Date")
    last_backup_str = (
        last_dt.isoformat() if isinstance(last_dt, datetime) else str(last_dt or "")
    )

    size = dir_size(path) or 0
    apps = list(info_data.get("Installed Applications") or [])

    return BackupMetadata(
        backup_path=str(path),
        folder_name=path.name,
        device_name=(lambda n: n or "Unknown Device")(__import__("common").backup_device_name(info_data)),
        product_name=str(info_data.get("Product Name") or "iPhone"),
        product_type=str(info_data.get("Product Type") or ""),
        product_version=str(info_data.get("Product Version") or ""),
        build_version=str(info_data.get("Build Version") or ""),
        serial_number=str(info_data.get("Serial Number") or ""),
        udid=str(info_data.get("Unique Identifier") or info_data.get("Target Identifier") or path.name),
        is_encrypted=bool(manifest_data.get("IsEncrypted", False)),
        last_backup_date=last_backup_str,
        snapshot_state=str(status_data.get("SnapshotState") or "unknown"),
        is_full_backup=bool(status_data.get("IsFullBackup", False)),
        size_bytes=size,
        size_gb=round(size / GB, 2),
        installed_apps=apps,
    )


def list_available_backups(include_archives: bool = False) -> List[BackupMetadata]:
    """Scans BACKUP_ROOT for valid iOS backups and returns their metadata."""
    if not BACKUP_ROOT.exists():
        return []

    backups: List[BackupMetadata] = []
    for item in sorted(BACKUP_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not item.is_dir():
            continue
        if not include_archives and ARCHIVE_SUFFIX.search(item.name):
            continue
        if (item / "Info.plist").exists():
            try:
                meta = parse_backup_metadata(item)
                backups.append(meta)
            except Exception:
                continue
    return backups


def find_backup_for_target(target: Optional[str] = None) -> Optional[BackupMetadata]:
    """Finds a backup by UDID, folder name, or selects the most recent active backup."""
    backups = list_available_backups(include_archives=False)
    if not backups:
        backups = list_available_backups(include_archives=True)
    if not backups:
        return None

    if not target:
        return backups[0]

    clean_target = target.strip()
    for b in backups:
        if b.udid == clean_target or b.folder_name == clean_target or Path(b.backup_path).name == clean_target:
            return b

    target_path = Path(clean_target).expanduser().resolve()
    if target_path.is_dir() and (target_path / "Info.plist").exists():
        return parse_backup_metadata(target_path)

    return None


def fmt_size(total_bytes: Optional[float]) -> str:
    if total_bytes is None:
        return "unknown (unreadable)"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if total_bytes < 1000.0:
            return f"{total_bytes:.2f} {unit}"
        total_bytes /= 1000.0
    return f"{total_bytes:.2f} PB"


def to_utc(dt: datetime) -> datetime:
    # plistlib returns naive datetimes that are in UTC.
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def check_proof_of_password(
    backup_dir: Path,
    candidate_password: Optional[str] = None,
    interactive: bool = True
) -> Tuple[bool, str]:
    """
    Cryptographically verifies that the backup's encryption key can be decrypted.
    Checks:
      1. candidate_password argument if passed
      2. Environment variables: BACKUP_PASSWORD, MVT_IOS_BACKUP_PASSWORD
      3. tried_passwords.json (if previously proven match exists)
      4. Smart Keychain Probe: automatically tests stored passwords in macOS Keychain.
         - If match found: prompts human sanity check.
         - If human fails/rejects: navigates UI to Keychain Access.
         - If not present: operator must prove it themselves.
    Returns (verified: bool, message: str)
    """
    manifest, err = load_plist(backup_dir / "Manifest.plist")
    if err or not manifest or not manifest.get("IsEncrypted"):
        return True, "Backup is unencrypted; no proof of password required."

    udid = backup_dir.name
    import password_tracker

    kb_fp = keybag_fingerprint(backup_dir)

    # Fail closed: if no proving tool is installed, proof cannot be established.
    if not password_tracker.proof_tool_available():
        return False, (
            "No password-proving tool installed (need iOSbackup or mvt-ios). "
            "Install one: pip3 install iOSbackup  (or)  pip3 install mvt"
        )

    # 1. Candidate argument
    if candidate_password:
        try:
            ok, dur = password_tracker.test_backup_password(udid, candidate_password, backup_root_dir=backup_dir.parent)
        except password_tracker.ProofToolUnavailable as e:
            return False, str(e)
        password_tracker.save_attempt(udid, candidate_password, ok, dur, keybag_fp=kb_fp)
        if ok:
            return True, f"Verified via provided candidate password ({dur:.2f}s)"
        return False, "Provided candidate password failed decryption"

    # 2. Environment variables
    env_pwd = os.environ.get("BACKUP_PASSWORD") or os.environ.get("MVT_IOS_BACKUP_PASSWORD")
    if env_pwd:
        try:
            ok, dur = password_tracker.test_backup_password(udid, env_pwd, backup_root_dir=backup_dir.parent)
        except password_tracker.ProofToolUnavailable as e:
            return False, str(e)
        if ok:
            password_tracker.save_attempt(udid, env_pwd, ok, dur, keybag_fp=kb_fp)
            return True, f"Verified via environment variable ({dur:.2f}s)"

    # 3. Prior proof, tied to THIS keybag fingerprint (invalidated if the password changed).
    if password_tracker.proven_for_keybag(udid, kb_fp):
        return True, "Verified via a prior proof against the current backup keybag."

    # 4. Smart Keychain Probe (test stored passwords, human sanity check).
    try:
        ok, pwd, msg = password_tracker.smart_keychain_probe(udid, interactive=interactive, backup_root_dir=backup_dir.parent)
    except password_tracker.ProofToolUnavailable as e:
        return False, str(e)
    return (True, msg) if ok else (False, msg)


def print_remediation_guide(
    failures: list[str],
    udid: str,
    interactive: bool = True,
    steer_ui: bool = False
) -> None:
    """Prints copy-paste shell commands and steers macOS UI to the exact location to unblock gates."""
    if not failures:
        return

    print("\n  🛠️  REQUIRED ACTION TO UNBLOCK THIS SAFETY GATE:")

    has_pwd_failure = any("PROOF OF PASSWORD REQUIRED" in f for f in failures)
    has_unencrypted = any("backup is NOT encrypted" in f for f in failures)
    has_incomplete = any("SnapshotState is" in f or "incomplete" in f for f in failures)
    has_stale = any("limit" in f and "old" in f for f in failures)
    has_db_missing = any("Manifest.db" in f for f in failures)

    if has_pwd_failure:
        print("     [Action: Prove Backup Encryption Password]")
        print("     1. Test candidate password cryptographically against the keybag:")
        print(f"        python3 iphone_manager.py verify --udid {udid} --password \"<YOUR_PASSWORD>\"")
        print("     2. Or run interactive password hunter to test variants or inspect history:")
        print(f"        python3 iphone_manager.py passwords --udid {udid} --cli")
        print("     3. Or inspect stored passwords in macOS Keychain Access:")
        print("        Run: python3 -c \"import password_tracker; password_tracker.open_keychain_ui()\"")

        if steer_ui or (interactive and sys.stdin.isatty()):
            try:
                import password_tracker
                if password_tracker.open_keychain_ui():
                    print("     👉 UI Steered: Brought Keychain Access forward filtered to 'iOS Backup'.")
            except Exception:
                pass

    if has_unencrypted:
        print("     [Action: Enable Backup Encryption]")
        print("     Unencrypted backups omit Keychain items, saved Wi-Fi networks, and 2FA tokens.")
        print("     1. In macOS Finder -> Select iPhone in sidebar -> Locations.")
        print("     2. Check 'Encrypt local backup' and click 'Back Up Now'.")
        print("     Or execute via CLI:")
        print("        idevicebackup2 encryption on")
        print("        idevicebackup2 backup \"$HOME/Library/Application Support/MobileSync/Backup\"")

        if steer_ui or (interactive and sys.stdin.isatty()):
            try:
                subprocess.run(["osascript", "-e", 'tell application "Finder" to activate'], capture_output=True, timeout=5)
                print("     👉 UI Steered: Brought macOS Finder forward.")
            except Exception:
                pass

    if has_incomplete or has_stale or has_db_missing:
        print("     [Action: Capture Fresh Complete Backup Snapshot]")
        print("     1. In macOS Finder, click 'Back Up Now' to complete sync before wiping.")
        print("     Or execute clean CLI backup:")
        print("        idevicebackup2 backup \"$HOME/Library/Application Support/MobileSync/Backup\"")


def inspect_backup(
    backup_dir: Path,
    max_age_hours: float,
    password: Optional[str] = None,
    skip_password_check: bool = False,
    interactive: bool = True,
    steer_ui: bool = False,
    min_bytes: Optional[int] = None,
) -> bool:
    """Prints a report and returns True if the backup passes every check."""
    print(f"\n{'=' * 70}")
    print(f"Backup: {backup_dir.name}")
    print(f"Path:   {backup_dir}")

    failures: list[str] = []
    warnings: list[str] = []

    info, err = load_plist(backup_dir / "Info.plist")
    if err:
        failures.append(err)
        info = {}

    print(f"  Device Name:      {info.get('Device Name', 'Unknown')}")
    ptype = info.get("Product Type", "Unknown")
    # idevicebackup2 doesn't write "Product Name" (Finder does); fall back to the model table.
    from market_pricing import PRODUCT_TYPE_MAP
    pname = info.get("Product Name") or PRODUCT_TYPE_MAP.get(ptype, "Unknown")
    print(f"  Product:          {pname} ({ptype})")
    print(f"  iOS Version:      {info.get('Product Version', 'Unknown')}")
    print(f"  Serial Number:    {info.get('Serial Number', 'Unknown')}")

    # --- Recency ---
    last_backup = info.get("Last Backup Date")
    if isinstance(last_backup, datetime):
        last_utc = to_utc(last_backup)
        age_h = (datetime.now(timezone.utc) - last_utc).total_seconds() / 3600
        local = last_utc.astimezone().strftime("%Y-%m-%d %H:%M %Z")
        print(f"  Last Backup:      {local} ({age_h:.1f} h ago)")
        if age_h > max_age_hours:
            failures.append(f"backup is {age_h:.0f} h old (limit {max_age_hours:.0f} h) - make a fresh one")
    else:
        failures.append("no Last Backup Date in Info.plist")

    # --- Completion ---
    status, err = load_plist(backup_dir / "Status.plist")
    if err:
        failures.append(err)
    else:
        snapshot = status.get("SnapshotState", "Unknown")
        print(f"  Snapshot State:   {snapshot}")
        print(f"  Full Backup:      {status.get('IsFullBackup', 'Unknown')}")
        if snapshot != "finished":
            failures.append(f"SnapshotState is '{snapshot}', not 'finished' (backup incomplete or interrupted)")

    manifest_db = backup_dir / "Manifest.db"
    if not manifest_db.exists() or manifest_db.stat().st_size == 0:
        failures.append("Manifest.db missing or empty (file index lost)")

    # --- Encryption & Proof of Password Gate ---
    manifest, err = load_plist(backup_dir / "Manifest.plist")
    if err:
        failures.append(err)
    else:
        if bool(manifest.get("IsEncrypted", False)):
            print("  Encryption:       ✅ ENABLED - Keychain, passwords, Wi-Fi and 2FA data included")
            if skip_password_check:
                warnings.append("PROOF OF PASSWORD BYPASSED (--skip-password-check). DANGER: Device may be unrecoverable!")
                print("  Proof of Password: ⚠️  BYPASSED (--skip-password-check active)")
            else:
                pwd_ok, pwd_msg = check_proof_of_password(backup_dir, candidate_password=password, interactive=interactive)
                if pwd_ok:
                    print(f"  Proof of Password: ✅ VERIFIED - {pwd_msg}")
                else:
                    print(f"  Proof of Password: 🚨 UNPROVEN - {pwd_msg}")
                    failures.append(
                        f"PROOF OF PASSWORD REQUIRED: Encrypted backup cannot be decrypted without verified password. "
                        f"Phone reset is strictly BLOCKED. ({pwd_msg})"
                    )
        else:
            print("  Encryption:       🚨 DISABLED - Keychain, passwords and 2FA data NOT included")
            failures.append("backup is NOT encrypted")
        if not manifest.get("WasPasscodeSet", True):
            warnings.append("device had no passcode set when backed up")

    # --- Size & file-count sanity (README/SECURITY promise both) ---
    size_bytes = dir_size(backup_dir)
    file_count = count_files(backup_dir)
    print(f"  Disk Space Used:  {fmt_size(size_bytes)}")
    print(f"  File Count:       {file_count:,}")
    threshold = MIN_BACKUP_BYTES if min_bytes is None else min_bytes
    if size_bytes is None:
        warnings.append("backup size unreadable (permissions?)")
    elif size_bytes < threshold:
        failures.append(
            f"backup is only {fmt_size(size_bytes)} (< {fmt_size(threshold)} minimum) "
            "- it looks empty or truncated"
        )
    if file_count < 2:
        failures.append(f"backup holds {file_count} file(s) - no real payload present")

    # --- Folder-name vs Info.plist UDID consistency ---
    info_udid = str((info or {}).get("Unique Identifier") or (info or {}).get("Target Identifier") or "")
    if info_udid and backup_dir.name and info_udid.lower() != backup_dir.name.lower() and not ARCHIVE_SUFFIX.search(backup_dir.name):
        warnings.append(
            f"Info.plist UDID ({info_udid}) does not match folder name ({backup_dir.name})"
        )

    for w in warnings:
        print(f"  ⚠️  {w}")
    if failures:
        print("\n  ❌ RESULT: FAIL - DO NOT ERASE THIS PHONE")
        for f in failures:
            print(f"     - {f}")
        print_remediation_guide(failures, backup_dir.name, interactive=interactive, steer_ui=steer_ui)
    else:
        print("\n  ✅ RESULT: PASS - safe to proceed to erase")
    print("=" * 70)
    return not failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify iOS backups before erasing a device.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--udid", help="only check the backup for this device UDID")
    group.add_argument("--connected", action="store_true", help="only check the device plugged in via USB")
    parser.add_argument("--max-age-hours", type=float, default=24.0,
                        help="fail backups older than this (default: 24)")
    parser.add_argument("--password", help="Candidate backup password to cryptographically prove")
    parser.add_argument("--skip-password-check", action="store_true",
                        help="DANGEROUS: Bypass mandatory proof-of-password gate before erase")
    parser.add_argument("--non-interactive", action="store_true",
                        help="Do not prompt for interactive sanity-check confirmations")
    parser.add_argument("--steer-ui", action="store_true",
                        help="Actively steer macOS UI (Keychain Access / Finder) to the required gate location")
    args = parser.parse_args()

    try:
        if not BACKUP_ROOT.exists():
            print(f"No MobileSync backup directory found at {BACKUP_ROOT}")
            return 2
        all_dirs = sorted(d for d in BACKUP_ROOT.iterdir() if d.is_dir())
    except PermissionError:
        print(f"❌ Permission denied reading {BACKUP_ROOT}")
        print("   Grant your terminal Full Disk Access: System Settings -> Privacy & Security -> Full Disk Access.")
        return 2

    if args.connected:
        udids = connected_udids()
        if not udids:
            from common import diagnose_connection
            print(f"❌ {diagnose_connection()['message']}")
            return 2
        targets = [d for d in all_dirs if d.name in udids]
        if not targets:
            print(f"❌ No backup found for connected device(s): {', '.join(udids)}")
            return 1
    elif args.udid:
        targets = [d for d in all_dirs if d.name == args.udid]
        if not targets:
            print(f"❌ No backup found for UDID {args.udid}")
            return 1
    else:
        targets = all_dirs

    gated = [d for d in targets if not ARCHIVE_SUFFIX.search(d.name)]
    archives = [d for d in targets if ARCHIVE_SUFFIX.search(d.name)]

    if not gated:
        print(f"No active backups found in {BACKUP_ROOT}")
        return 1

    print(f"Checking {len(gated)} active backup(s) in {BACKUP_ROOT}")
    results = {
        d.name: inspect_backup(
            d,
            args.max_age_hours,
            password=args.password,
            skip_password_check=args.skip_password_check,
            interactive=not args.non_interactive,
            steer_ui=args.steer_ui
        )
        for d in gated
    }

    if archives:
        print("\nArchived / pre-wipe copies (not gated):")
        for d in archives:
            manifest, _ = load_plist(d / "Manifest.plist")
            enc = "encrypted" if manifest and manifest.get("IsEncrypted") else "UNENCRYPTED"
            print(f"  - {d.name}  ({enc}, {fmt_size(dir_size(d))})")

    print("\nSUMMARY")
    for name, ok in results.items():
        print(f"  {'✅ PASS' if ok else '❌ FAIL'}  {name}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
