"""
iTunes Environment & Storage Enforcer for Windows (`itunes_env.py`).

Detects and verifies iTunes installations, the Apple Mobile Device Service (AMDS),
USB drivers, and resolves the official iTunes MobileSync backup directory paths.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
if sys.platform == "win32":
    try:
        import winreg
    except ImportError:
        winreg = None
else:
    winreg = None
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from common import backup_root


@dataclass
class ITunesStatus:
    is_windows: bool
    itunes_installed: bool
    itunes_version: Optional[str]
    install_type: Optional[str]  # "Win32", "Store", "None"
    amds_service_installed: bool
    amds_service_running: bool
    backup_root: str
    backup_root_exists: bool
    backup_count: int
    recommendations: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def get_apple_itunes_download_url() -> str:
    """Returns the official Apple direct download URL based on detected system bits (win64 vs win32)."""
    is_64 = (
        sys.maxsize > 2**32
        or bool(os.environ.get("PROCESSOR_ARCHITEW6432"))
        or "64" in os.environ.get("PROCESSOR_ARCHITECTURE", "")
    )
    return "https://www.apple.com/itunes/download/win64" if is_64 else "https://www.apple.com/itunes/download/win32"


# Kept as a name for callers/tests; the single implementation lives in common.backup_root
# (the old copy here had drifted: it ignored MOBILESYNC_BACKUP_DIR).
get_itunes_backup_root = backup_root


def check_itunes_installation() -> ITunesStatus:
    """Probes the Windows system for iTunes, Apple Mobile Device Support, and backup directories."""
    is_win = sys.platform == "win32"
    backup_root = get_itunes_backup_root()
    backup_root_exists = backup_root.exists()
    backup_count = 0
    if backup_root_exists:
        try:
            backup_count = sum(1 for d in backup_root.iterdir() if d.is_dir() and (d / "Info.plist").exists())
        except Exception:
            backup_count = 0

    if not is_win:
        return ITunesStatus(
            is_windows=False,
            itunes_installed=True,
            itunes_version="macOS-Native",
            install_type="macOS-Finder",
            amds_service_installed=True,
            amds_service_running=True,
            backup_root=str(backup_root),
            backup_root_exists=backup_root_exists,
            backup_count=backup_count,
            recommendations=[],
        )

    itunes_installed = False
    itunes_version = None
    install_type = "None"
    amds_installed = False
    amds_running = False
    recommendations: List[str] = []

    # 1. Check Win32 iTunes via Registry
    reg_paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Apple Computer, Inc.\iTunes"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Apple Computer, Inc.\iTunes"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Apple Computer, Inc.\iTunes"),
    ]
    for root, subkey in reg_paths:
        try:
            with winreg.OpenKey(root, subkey) as k:
                ver, _ = winreg.QueryValueEx(k, "Version")
                itunes_installed = True
                itunes_version = str(ver)
                install_type = "Win32"
                break
        except OSError:
            pass

    # 2. Check Store iTunes via AppxPackage if not found in registry
    if not itunes_installed:
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-AppxPackage -Name '*iTunes*' | Select-Object -ExpandProperty Version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            store_ver = proc.stdout.strip()
            if store_ver:
                itunes_installed = True
                itunes_version = store_ver
                install_type = "Microsoft Store"
        except Exception:
            pass

    # Check executable in Program Files
    if not itunes_installed:
        prog_paths = [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "iTunes" / "iTunes.exe",
            Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "iTunes" / "iTunes.exe",
        ]
        for p in prog_paths:
            if p.exists():
                itunes_installed = True
                install_type = "Win32"
                itunes_version = "Detected on disk"
                break

    # 3. Check Apple Mobile Device Service (AMDS)
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Service -Name 'Apple Mobile Device Service' -ErrorAction SilentlyContinue | Select-Object -Property Status"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        out = proc.stdout.strip()
        if out:
            amds_installed = True
            if "Running" in out:
                amds_running = True
    except Exception:
        pass

    if not amds_installed:
        # Check registry for Apple Mobile Device Support
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Apple Inc.\Apple Mobile Device Support") as k:
                amds_installed = True
        except OSError:
            pass

    # Build recommendations
    dl_url = get_apple_itunes_download_url()
    if not itunes_installed:
        recommendations.append(f"iTunes is not installed. Download official iTunes directly from Apple: {dl_url}")
    elif not amds_installed:
        recommendations.append(
            f"Apple Mobile Device Support driver is missing! Reinstall/repair official iTunes from Apple ({dl_url}) to install USB drivers."
        )
    elif not amds_running:
        recommendations.append("Apple Mobile Device Service is stopped. Start it via: Start-Service 'Apple Mobile Device Service'")
    if not backup_root_exists:
        recommendations.append(f"iTunes backup folder does not exist yet ({backup_root}). Create a backup in iTunes first.")

    return ITunesStatus(
        is_windows=True,
        itunes_installed=itunes_installed,
        itunes_version=itunes_version,
        install_type=install_type,
        amds_service_installed=amds_installed,
        amds_service_running=amds_running,
        backup_root=str(backup_root),
        backup_root_exists=backup_root_exists,
        backup_count=backup_count,
        recommendations=recommendations,
    )


def print_itunes_report(
    install: bool = False,
    yes: bool = False,
    interactive: bool = True,
) -> None:
    """Prints a friendly terminal status report of iTunes on Windows and offers installation."""
    status = check_itunes_installation()
    print("\n[ITUNES WINDOWS ENVIRONMENT & ENFORCEMENT]")
    if status.itunes_installed:
        print(f"  Status:                 ✅ Installed ({status.install_type}, Version: {status.itunes_version})")
    else:
        print("  Status:                 🚨 NOT INSTALLED")

    if status.amds_service_running:
        print("  Apple Mobile Device:    ✅ Running (USB Drivers & Daemons Active)")
    elif status.amds_service_installed:
        print("  Apple Mobile Device:    ⚠️ Installed but STOPPED")
    else:
        print("  Apple Mobile Device:    🚨 Not Found (Requires iTunes)")

    print(f"  Enforced Backup Path:   {status.backup_root}")
    print(f"  Backup Path Exists:     {'✅ Yes' if status.backup_root_exists else '⚠️ Not yet created'}")
    print(f"  Valid Backups Found:    {status.backup_count}")

    if status.recommendations:
        print("\n  ⚠️ Actions Needed:")
        for r in status.recommendations:
            print(f"     - {r}")

    # Automated / interactive installation trigger
    if not status.itunes_installed and sys.platform == "win32":
        from dep_installer import install_itunes, prompt_yes_no
        should_install = install or yes
        if not should_install and interactive and sys.stdin.isatty():
            should_install = prompt_yes_no("\n  -> Would you like to download official iTunes from Apple website now?", default=True)
        if should_install:
            res = install_itunes()
            print(f"\n  Installation outcome: {res.get('status')} via {res.get('method')}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="iTunes Environment & Storage Enforcer.")
    parser.add_argument("--install", action="store_true", help="Install iTunes if missing.")
    parser.add_argument("--yes", "-y", action="store_true", help="Non-interactive mode.")
    parser.add_argument("--no-prompt", action="store_true", help="Do not prompt interactively.")
    args = parser.parse_args()
    print_itunes_report(install=args.install, yes=args.yes, interactive=not args.no_prompt)
