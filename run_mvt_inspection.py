#!/usr/bin/env python3
"""
MVT (Mobile Verification Toolkit) Runner for local iOS Backups, encrypted or not.
Checks a backup against MVT's STIX2 IOCs and writes a report.

  - Encrypted backups are decrypted to a temporary plaintext copy, which is scanned.
  - Unencrypted backups are scanned through a temporary APFS clone (instant, ~no disk
    space). MVT is never pointed at the real backup because it can rewrite malformed
    databases in place (it moves the original file to *.bak). Unencrypted backups also
    leave out data MVT checks (e.g. call and website history), so coverage is lower.

Safety properties:
  - The backup password is passed via the environment, never on the command line
    (command-line args are visible to every process via `ps`).
  - The working copy (decrypted plaintext or clone) is deleted when the run ends, even
    on errors or Ctrl-C, unless --keep-work-copy is given. The original is never modified.
  - A "clean" verdict is only printed if MVT exited successfully and MVT itself reports
    loading a non-zero number of indicators (read from the command.log it writes).
  - A successful decryption also proves the backup password is correct, which nothing
    else checks before the phone is erased.

After MVT's indicator scan, backup_deep_inspect.py runs heuristic checks on the same
working copy (profiles, apps, permissions, lookalike processes, attachment file types,
Shortcuts, links, clock, jailbreak traces) for things no indicator list can catch.

Usage:
  run_mvt_inspection.py                 # pick a backup interactively
  run_mvt_inspection.py --udid <UDID>   # scan a specific backup folder

Exit code: 0 = clean, 2 = setup problem / no indicators loaded, 3 = indicator match,
           4 = no indicator match but deep inspection raised a FLAG, other = MVT error.
"""

import argparse
import getpass
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

from backup_deep_inspect import inspect as deep_inspect
from backup_verifier import ARCHIVE_SUFFIX, dir_size, fmt_size
from common import backup_root, host_mount

MVT_WORK_DIR = Path(__file__).resolve().parent / "mvt_reports"  # gitignored; holds sensitive output
# MVT loads every *.stix2 file in here automatically (see `mvt-ios download-iocs`).
MVT_IOC_DIR = Path.home() / "Library" / "Application Support" / "mvt" / "indicators"
LOADED_IOCS = re.compile(r"Loaded a total of (\d+) unique indicators")
GB = 1000**3


def find_mvt_ios() -> str:
    """Return the first mvt-ios that actually runs, or "" if none does.

    PATH alone isn't enough: a pyenv shim is found there even when the active pyenv
    version lacks MVT, and running it fails with "command not found" (exit 127).
    So each candidate is probed, PATH first, then every pyenv version (newest install first).
    """
    candidates = [shutil.which("mvt-ios")]
    candidates += sorted(Path.home().glob(".pyenv/versions/*/bin/mvt-ios"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
    for c in dict.fromkeys(str(c) for c in candidates if c):
        try:
            if subprocess.run([c, "version"], capture_output=True, timeout=60).returncode == 0:
                return c
        except (OSError, subprocess.TimeoutExpired):
            continue
    return ""


def die(msg: str, code: int = 1):
    print(f"❌ {msg}")
    sys.exit(code)


def read_plist(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return plistlib.load(f)
    except Exception:  # noqa: BLE001
        return {}


def list_backups() -> list[tuple[Path, dict, bool]]:
    root = backup_root()
    try:
        dirs = [d for d in root.iterdir() if d.is_dir() and (d / "Manifest.plist").exists()]
    except FileNotFoundError:
        return []
    except PermissionError:
        die(f"Permission denied reading {root}. Grant your terminal Full Disk Access.", 2)
    out = []
    for d in sorted(dirs, key=lambda x: x.stat().st_mtime, reverse=True):
        info = read_plist(d / "Info.plist")
        encrypted = bool(read_plist(d / "Manifest.plist").get("IsEncrypted", False))
        out.append((d, info, encrypted))
    return out


def choose_backup(udid: str | None) -> tuple[Path, bool]:
    backups = list_backups()
    if not backups:
        die(f"No backups found in {backup_root()}")

    if udid:
        match = [b for b in backups if b[0].name == udid]
        if not match:
            die(f"No backup folder named {udid}")
        chosen, _, encrypted = match[0]
    else:
        print("\nAvailable backups (newest first):")
        for i, (d, info, enc) in enumerate(backups, 1):
            label = f"{info.get('Device Name', '?')} - {info.get('Product Name', '?')}"
            if ARCHIVE_SUFFIX.search(d.name):
                label += " [archive]"
            when = info.get("Last Backup Date", "?")
            print(f"  [{i}] {label} | {when} | {'encrypted' if enc else 'UNENCRYPTED'}\n      {d.name}")
        # No interactive prompt when stdin isn't a TTY (e.g. invoked with captured output):
        # input() would hang or raise EOFError. Require --udid instead.
        if not sys.stdin.isatty():
            die("Multiple backups found and no TTY to choose from. Re-run with --udid <UDID> "
                "to pick one non-interactively.", 2)
        try:
            raw = input("\nSelect backup number: ").strip()
        except EOFError:
            die("No selection available (EOF). Re-run with --udid <UDID>.", 2)
        if not raw.isdigit() or not 1 <= int(raw) <= len(backups):
            die("Invalid selection.")
        chosen, _, encrypted = backups[int(raw) - 1]

    if not encrypted:
        print("\n⚠️ Unencrypted backup: no password needed, but Apple leaves call history, website "
              "history, Health data and saved passwords out of it, so MVT has less to check.")
    return chosen, encrypted


def ioc_file_count() -> int:
    """Indicator files MVT will auto-load (it only reads *.stix2 from MVT_IOC_DIR)."""
    if not MVT_IOC_DIR.exists():
        return 0
    return len(list(MVT_IOC_DIR.glob("*.stix2")))


def loaded_ioc_count(report_dir: Path) -> int | None:
    """Indicator count MVT itself logged for this run, or None if the line isn't there."""
    try:
        found = LOADED_IOCS.findall((report_dir / "command.log").read_text(errors="replace"))
    except OSError:
        return None
    return int(found[-1]) if found else None


def confirm(prompt: str) -> bool:
    return input(f"{prompt} (y/N): ").strip().lower() == "y"


def main():
    parser = argparse.ArgumentParser(description="Run MVT against a local iOS backup (encrypted or not).")
    parser.add_argument("--udid", help="backup folder name (device UDID) to scan")
    parser.add_argument("--keep-work-copy", "--keep-decrypted", dest="keep_work_copy", action="store_true",
                        help="keep the working copy (decrypted plaintext or clone) after the scan "
                             "(not recommended)")
    parser.add_argument("--skip-ioc-update", action="store_true", help="don't refresh MVT indicators first")
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # keep our lines in order with MVT's when piped/teed

    mvt_bin = find_mvt_ios()
    if not mvt_bin:
        die("No working mvt-ios found (checked PATH and ~/.pyenv/versions/*/bin). "
            "Install it with `pip3 install mvt`.", 2)

    print("=" * 70)
    print("   MOBILE VERIFICATION TOOLKIT (MVT) - iOS BACKUP RUNNER")
    print("=" * 70)
    print(f"Using: {mvt_bin}")

    target, encrypted = choose_backup(args.udid)
    print(f"\nTarget Backup: {target.name} ({'encrypted' if encrypted else 'UNENCRYPTED'})\nPath: {target}")

    # Indicators: without them check-backup finds nothing and "clean" would be meaningless.
    if not args.skip_ioc_update:
        print("\nRefreshing MVT indicators ...")
        if subprocess.run([mvt_bin, "download-iocs"]).returncode != 0:
            print("⚠️ Indicator download failed; using whatever is already on disk.")
    n_ioc_files = ioc_file_count()
    print(f"Indicator files available: {n_ioc_files}")
    if n_ioc_files == 0:
        die("No MVT indicator files found - a scan would not be able to detect anything. "
            "Run `mvt-ios download-iocs` and retry.")

    # Encrypted: decryption writes a full plaintext copy. Unencrypted: an APFS clone, which
    # takes almost no space when the work dir is on the same volume as the backup.
    needs_full_copy = encrypted or os.stat(target).st_dev != os.stat(MVT_WORK_DIR.parent).st_dev
    free_gb = shutil.disk_usage(str(host_mount())).free / GB
    backup_bytes = dir_size(target)
    print(f"Backup Size:    {fmt_size(backup_bytes)}\nMac Free Space: {free_gb:.2f} GB")
    if needs_full_copy and (backup_bytes is None or free_gb < backup_bytes / GB * 1.5):
        print("\n⚠️ Low or unknown disk headroom: the scan writes a full second copy of the backup.")
        if not confirm("Continue anyway?"):
            sys.exit(1)

    if encrypted:
        password = os.environ.get("MVT_IOS_BACKUP_PASSWORD") or getpass.getpass("\nEnter Backup Encryption Password: ")
        if not password:
            die("Password cannot be empty.")
        mvt_env = {**os.environ, "MVT_IOS_BACKUP_PASSWORD": password}

    MVT_WORK_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(MVT_WORK_DIR, 0o700)  # backup data and reports inside are sensitive
    work_copy = MVT_WORK_DIR / f"{target.name}_{'decrypted' if encrypted else 'clone'}"
    work_label = "plaintext decrypted copy" if encrypted else "working clone"
    report_dir = MVT_WORK_DIR / f"{target.name}_report"
    if report_dir.exists():
        shutil.rmtree(report_dir)  # avoid stale *_detected.json from a previous run
    report_dir.mkdir(parents=True)

    exit_code = 0
    try:
        if work_copy.exists():
            shutil.rmtree(work_copy)  # leftover from an interrupted run
        if encrypted:
            print(f"\n[1/4] Decrypting backup to: {work_copy} ...")
            work_copy.mkdir(parents=True)
            res = subprocess.run([mvt_bin, "decrypt-backup", "-d", str(work_copy), str(target)], env=mvt_env)
            if res.returncode != 0:
                die(f"Decryption failed (mvt-ios exit {res.returncode}). Most often this is a wrong "
                    "backup password - see MVT's message above.", res.returncode)
            print("✅ Decryption successful - the backup password is correct.")
        else:
            print(f"\n[1/4] Cloning backup to: {work_copy} ...")
            # On macOS use clonefile(2) (cp -c): copy-on-write, anything MVT rewrites lands
            # only in the clone. Elsewhere fall back to a plain recursive copy. work_copy
            # must not exist yet, or cp would nest the backup inside it.
            if sys.platform == "darwin":
                res = subprocess.run(["cp", "-ac", str(target), str(work_copy)])
                if res.returncode != 0:
                    die(f"Could not clone the backup (cp exit {res.returncode}).", res.returncode)
            else:
                try:
                    shutil.copytree(target, work_copy, symlinks=True)
                except Exception as e:  # noqa: BLE001
                    die(f"Could not copy the backup: {e}")
            print("✅ Working clone ready; the original backup won't be touched.")

        print("\n[2/4] Analyzing backup with MVT ...")
        # No --iocs here: MVT auto-loads MVT_IOC_DIR, and --iocs only accepts files
        # (passing the folder just makes MVT warn "No indicators file exists at path").
        res = subprocess.run([mvt_bin, "check-backup", "-o", str(report_dir), str(work_copy)])
        n_loaded = loaded_ioc_count(report_dir)
        limited = "" if encrypted else " (limited coverage: unencrypted backup)"

        print("\n[3/4] Deep inspection (heuristics beyond MVT's indicators) ...")
        try:
            n_flag, n_review = deep_inspect(report_dir, work_copy, encrypted)
        except Exception as e:  # noqa: BLE001 - the extra checks must never break the scan
            print(f"⚠️ Deep inspection failed: {type(e).__name__}: {e}")
            n_flag = n_review = 0

        print("\n[4/4] Results")
        print(f"Reports saved to: {report_dir}")
        hits = sorted(report_dir.glob("*detected*"))
        if hits:
            print("\n🚨 ALERT: POTENTIAL INDICATORS OF COMPROMISE (IOCs) DETECTED:")
            for h in hits:
                print(f"   - {h.name}")
            print("   Do not erase yet; keep the report and get expert review (e.g. Amnesty Security Lab / Access Now helpline).")
            exit_code = 3
        elif res.returncode != 0:
            print(f"\n⚠️ INCONCLUSIVE: mvt-ios check-backup exited with code {res.returncode}. "
                  "Review the output above; this is NOT a clean result.")
            exit_code = res.returncode
        elif n_loaded == 0:
            print("\n⚠️ INCONCLUSIVE: MVT reports loading 0 indicators, so it could not have detected "
                  "anything. Run `mvt-ios download-iocs` and retry; this is NOT a clean result.")
            exit_code = 2
        elif n_loaded is None:
            print(f"\n✅ Clean: no matches against {n_ioc_files} indicator file(s){limited}. "
                  "(MVT's own indicator count wasn't found in command.log.)")
        else:
            print(f"\n✅ Clean: no matches against {n_loaded} indicators{limited}.")
        if n_flag:
            print(f"🚩 Deep inspection raised {n_flag} FLAG(s) - read them above before erasing.")
            exit_code = exit_code or 4
        elif n_review:
            print(f"🔎 Deep inspection: no FLAGs; {n_review} item(s) above are worth a glance.")
    finally:
        if work_copy.exists():
            if args.keep_work_copy:
                print(f"\n⚠️ {work_label.capitalize()} kept at {work_copy} - delete it when done.")
            else:
                print(f"\nRemoving {work_label} {work_copy} ...")
                shutil.rmtree(work_copy, ignore_errors=True)
                print("✅ Removed. Reports retained.")

    sys.exit(exit_code)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:  # main()'s finally block has already removed the working copy
        print("\nInterrupted.")
        sys.exit(130)
