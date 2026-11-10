"""
Automated Dependency Installer for Phone Revival (`dep_installer.py`).

Provides automated and interactive installation of missing prerequisites from reputable sources:
1. Native `libimobiledevice` CLI suite (ideviceinfo.exe, idevicerestore.exe, etc.):
   Downloaded from verified GitHub release builds into `./tools/bin/`.
2. iTunes & Apple Mobile Device Drivers:
   Downloaded directly from the official Apple website (https://www.apple.com/itunes/).
3. Mobile Verification Toolkit (MVT):
   Installed via Python pip.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import webbrowser
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent
LOCAL_TOOLS_BIN = REPO_ROOT / "tools" / "bin"

# Official Apple iTunes Website & Dynamic Direct Download URLs
APPLE_ITUNES_WEBSITE = "https://www.apple.com/itunes/"
APPLE_ITUNES_WIN64_URL = "https://www.apple.com/itunes/download/win64"
APPLE_ITUNES_WIN32_URL = "https://www.apple.com/itunes/download/win32"


def get_apple_itunes_download_url() -> str:
    """Returns the official Apple direct download URL based on detected system bits (win64 vs win32)."""
    is_64 = (
        sys.maxsize > 2**32
        or bool(os.environ.get("PROCESSOR_ARCHITEW6432"))
        or "64" in os.environ.get("PROCESSOR_ARCHITECTURE", "")
    )
    return APPLE_ITUNES_WIN64_URL if is_64 else APPLE_ITUNES_WIN32_URL


# Pinned GitHub community release of the Windows libimobiledevice suite. A new release needs a deliberate bump
# of all three constants: download it, compare the SHA-256 with the digest GitHub shows for the asset, then edit.
# (v20261101: digest checked against GitHub's asset digest and byte-identical to the build used on the test PC.)
LIBIMOBILE_TAG = "v20261101-74585f8"
LIBIMOBILE_URL = ("https://github.com/jrjr/libimobiledevice-windows/releases/download/"
                  f"{LIBIMOBILE_TAG}/libimobile-suite-latest_w64.zip")
LIBIMOBILE_SHA256 = "b456e396866744190009d6193d6cc383e23473addd6f8b7f467de36d6abb631f"


def is_admin() -> bool:
    """Checks whether the current process is running with administrative privileges."""
    if sys.platform != "win32":
        return os.getuid() == 0 if hasattr(os, "getuid") else False
    try:
        import ctypes
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def prompt_yes_no(prompt: str, default: bool = False) -> bool:
    """Prompts the user for yes/no in interactive terminal sessions."""
    if not sys.stdin.isatty():
        return default
    suffix = " [Y/n]: " if default else " [y/N]: "
    try:
        ans = input(prompt + suffix).strip().lower()
        if not ans:
            return default
        return ans in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def download_file_with_progress(url: str, dest_path: Path, label: str = "Downloading") -> None:
    """Downloads a file over HTTP(S) with live MB progress display."""
    req = urllib.request.Request(url, headers={"User-Agent": "phone-revival-dep-installer/1.0"})
    with urllib.request.urlopen(req) as resp, dest_path.open("wb") as out_f:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        chunk_size = 64 * 1024
        # On a terminal, redraw one line in place. Captured output (an agent, CI, a log) gets one line per 10%:
        # a \r redraw per chunk there was ~150 KB of text for one iTunes download.
        live = sys.stdout.isatty()
        next_mark = 10
        while True:
            chunk = resp.read(chunk_size)
            if not chunk:
                break
            out_f.write(chunk)
            downloaded += len(chunk)
            dl_mb = downloaded / (1024 * 1024)
            if total > 0:
                pct = (downloaded / total) * 100.0
                text = f"  {label}: {dl_mb:.1f} / {total / (1024 * 1024):.1f} MB ({pct:.0f}%)"
                if live:
                    sys.stdout.write("\r" + text)
                elif pct >= next_mark:
                    sys.stdout.write(text + "\n")
                    next_mark = (int(pct) // 10 + 1) * 10
            elif live:
                sys.stdout.write(f"\r  {label}: {dl_mb:.1f} MB")
            sys.stdout.flush()
    if live or total <= 0:
        sys.stdout.write(f"\n" if live else f"  {label}: {downloaded / (1024 * 1024):.1f} MB\n")


def compute_sha256(path: Path) -> str:
    """Computes SHA256 checksum of a local file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(128 * 1024):
            h.update(chunk)
    return h.hexdigest().lower()


HOMEBREW_INSTALL_CMD = (
    '/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
)
MAC_CLI_TOOLS = ("idevicebackup2", "ideviceinfo", "idevice_id")


def find_brew() -> Optional[str]:
    """Locate Homebrew even when the shell PATH doesn't include it (Apple Silicon default)."""
    return shutil.which("brew") or next(
        (p for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if os.access(p, os.X_OK)), None
    )


def install_cli_tools_macos(consent: bool = False) -> Dict[str, Any]:
    """
    macOS: the phone helper tools (libimobiledevice) come from Homebrew.

    Never installs anything without `consent`. Without it (or without Homebrew) the result
    carries plain-language `options` for the agent to read to the user, so the person always
    chooses between alternatives instead of being handed one command.
    """
    if all(shutil.which(t) for t in MAC_CLI_TOOLS):
        return {"status": "already_installed", "message": "The phone helper tools are already installed."}

    finder_option = {
        "id": "finder_fallback",
        "label": "Skip the helper tools and let me drive the Finder window on your Mac for you "
                 "(slower and less reliable, and you must be OK with me taking over Finder).",
    }
    brew = find_brew()
    if not brew:
        return {
            "status": "homebrew_missing",
            "message": "Your Mac needs a small free helper toolkit to talk to the iPhone. It is "
                       "normally installed with Homebrew, which isn't on this Mac yet.",
            "homebrew_install_command": HOMEBREW_INSTALL_CMD,
            "options": [
                {
                    "id": "user_installs_homebrew",
                    "label": "Install Homebrew yourself: open the Terminal app, paste the command, and "
                             "type your Mac password when asked (it won't show as you type). "
                             "Tell me when it finishes and I'll install the rest.",
                    "command": HOMEBREW_INSTALL_CMD,
                },
                finder_option,
                {
                    "id": "stop",
                    "label": "Stop here for now; nothing has been changed.",
                },
            ],
        }

    cmd = [brew, "install", "libimobiledevice"]
    if not consent:
        return {
            "status": "needs_consent",
            "message": "Your Mac needs a small free helper toolkit (libimobiledevice) to talk to "
                       "the iPhone. Homebrew is available to install it.",
            "options": [
                {"id": "agent_installs", "label": "Let me install it for you (takes about a minute).",
                 "command": " ".join(cmd)},
                {"id": "user_installs", "label": "Run it yourself in Terminal: " + " ".join(cmd)},
                finder_option,
            ],
        }

    res = subprocess.run(cmd, check=False)
    bindir = os.path.dirname(brew)
    ok = res.returncode == 0 and all(
        shutil.which(t) or os.access(os.path.join(bindir, t), os.X_OK) for t in MAC_CLI_TOOLS
    )
    return {
        "status": "success" if ok else "failed",
        "returncode": res.returncode,
        "message": "Phone helper tools installed." if ok else
                   "The install didn't finish. Run `brew doctor` or choose the Finder option instead.",
    }


def install_cli_tools(
    target_dir: Optional[Path] = None,
    force: bool = False,
    progress_callback: Optional[Callable[[str], None]] = None,
    consent: bool = False,
) -> Dict[str, Any]:
    """
    Downloads and extracts the precompiled libimobiledevice suite into ./tools/bin/.
    Extracts ideviceinfo.exe, idevicerestore.exe, idevice_id.exe, idevicebackup2.exe, etc.
    """
    if sys.platform == "darwin":
        return install_cli_tools_macos(consent=consent)
    # These are Windows .exe/.dll builds; refuse to run anywhere else.
    if sys.platform != "win32":
        return {
            "status": "unsupported",
            "message": "The prebuilt libimobiledevice suite is Windows-only. Install "
                       "libimobiledevice from your package manager.",
        }

    out_dir = target_dir or LOCAL_TOOLS_BIN
    out_dir.mkdir(parents=True, exist_ok=True)

    # Check if tools are already present
    test_exe = out_dir / "ideviceinfo.exe"
    if test_exe.exists() and not force:
        from win_tools import smoke_test_all_tools
        report = smoke_test_all_tools()
        if report.get("core_tools_installed"):
            return {
                "status": "already_installed",
                "message": f"CLI tools are already installed in {out_dir}",
                "report": report,
            }

    dl_url, tag_name = LIBIMOBILE_URL, LIBIMOBILE_TAG
    if progress_callback:
        progress_callback(f"Downloading libimobiledevice suite ({tag_name}) from GitHub...")
    else:
        print(f"Downloading native libimobiledevice suite ({tag_name}) from GitHub...")

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp_f:
        tmp_zip = Path(tmp_f.name)

    try:
        download_file_with_progress(dl_url, tmp_zip, label="Downloading libimobile-suite")

        # Nothing is extracted unless the archive is exactly the pinned one.
        digest = compute_sha256(tmp_zip)
        if digest.lower() != LIBIMOBILE_SHA256:
            return {
                "status": "error",
                "message": (f"The downloaded archive does not match the pinned SHA-256 (got {digest}, expected "
                            f"{LIBIMOBILE_SHA256}). Nothing was installed and the download was deleted."),
            }

        if progress_callback:
            progress_callback(f"Extracting suite to {out_dir}...")
        else:
            print(f"Extracting tools to: {out_dir}")

        with zipfile.ZipFile(tmp_zip, "r") as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                filename = Path(member.filename).name
                # Only extract executables / libraries, never arbitrary archive members,
                # and never overwrite a file that already exists.
                if not filename or Path(filename).suffix.lower() not in (".exe", ".dll"):
                    continue
                target_file = out_dir / filename
                if target_file.exists():
                    continue
                with zf.open(member) as source, target_file.open("wb") as target:
                    shutil.copyfileobj(source, target)

        # Smoke-test post installation
        from win_tools import smoke_test_all_tools
        report = smoke_test_all_tools()
        # idevicerestore / irecovery are optional and not in this suite: judge by the core tools.
        passed = report.get("core_smoke_tests_passed", False)
        return {
            "status": "success" if passed else "installed_with_warnings",
            "message": "libimobiledevice CLI suite installed successfully." if passed else "Tools installed, but some smoke tests failed.",
            "directory": str(out_dir),
            "smoke_test": report,
        }
    except Exception as exc:
        return {
            "status": "error",
            "message": f"Failed to download or extract CLI suite: {exc}",
        }
    finally:
        tmp_zip.unlink(missing_ok=True)


def install_itunes(
    open_browser_only: bool = False,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    Downloads and installs official iTunes directly from Apple (https://www.apple.com/itunes/).
    Direct Apple distribution is the only supported installation option.
    """
    if sys.platform != "win32":
        return {
            "status": "unsupported",
            "message": "iTunes installation is only applicable to Windows.",
        }

    itunes_url = get_apple_itunes_download_url()
    is_64 = "win64" in itunes_url
    filename = "iTunes64Setup.exe" if is_64 else "iTunesSetup.exe"

    if open_browser_only:
        webbrowser.open(itunes_url)
        return {
            "status": "browser_opened",
            "url": itunes_url,
            "message": f"Opened official Apple iTunes direct download page in browser: {itunes_url}",
        }

    # Download official iTunes directly from Apple CDN
    msg = f"Downloading official iTunes installer from Apple ({itunes_url})..."
    if progress_callback:
        progress_callback(msg)
    else:
        print(f"\n{msg}")

    # Save to user's Downloads folder or temp so they can find it
    downloads_dir = Path.home() / "Downloads"
    if downloads_dir.exists():
        installer_path = downloads_dir / filename
        cleanup_temp = False
    else:
        tmp_f = tempfile.NamedTemporaryFile(suffix=".exe", delete=False)
        installer_path = Path(tmp_f.name)
        tmp_f.close()
        cleanup_temp = True

    try:
        download_file_with_progress(itunes_url, installer_path, label="Downloading from Apple")

        print(f"Official Apple installer saved to: {installer_path}")
        print("Launching official Apple installer wizard...")

        # Run the official Apple GUI installer so the user can interactively install/repair with full visibility
        if not is_admin():
            print("👉 Please click 'YES' on the Windows permission prompt to complete Apple driver setup.")
            res = subprocess.run([
                "powershell", "-NoProfile", "-Command",
                f'Start-Process -FilePath "{installer_path}" -Verb RunAs -Wait'
            ], check=False)
        else:
            res = subprocess.run([str(installer_path)], check=False)

        from itunes_env import check_itunes_installation
        st = check_itunes_installation()
        return {
            "status": "success" if (st.itunes_installed and st.amds_service_installed) else "setup_invoked",
            "method": "official_apple_website",
            "returncode": res.returncode,
            "installer_path": str(installer_path),
            "itunes_status": st.to_dict(),
        }
    except Exception as exc:
        return {
            "status": "error",
            "method": "official_apple_website",
            "message": f"Failed to install iTunes from Apple: {exc}",
        }
    finally:
        if cleanup_temp:
            installer_path.unlink(missing_ok=True)


def install_mvt() -> Dict[str, Any]:
    """Installs Mobile Verification Toolkit (MVT) via pip."""
    print("Installing Mobile Verification Toolkit (MVT) via pip...")
    cmd = [sys.executable, "-m", "pip", "install", "mvt"]
    res = subprocess.run(cmd, check=False)
    return {
        "status": "success" if res.returncode == 0 else "failed",
        "returncode": res.returncode,
    }


def install_proof_tool() -> Dict[str, Any]:
    """Installs iOSbackup (pip), the fast tool that proves the backup password before an erase. Without a proof
    tool the pre-erase gate stays closed. Works on Windows too (checked 2026-11-10, Python 3.12)."""
    import password_tracker
    if password_tracker.proof_tool_available():
        return {"status": "already_installed"}
    res = subprocess.run([sys.executable, "-m", "pip", "install", "iOSbackup"], check=False)
    ok = res.returncode == 0 and password_tracker.proof_tool_available()
    return {"status": "success" if ok else "failed", "returncode": res.returncode}


def run_interactive_or_automated_setup(
    install_all: bool = False,
    yes: bool = False,
) -> None:
    """Master interactive / automated setup workflow for all Phone Revival dependencies."""
    from itunes_env import check_itunes_installation
    from win_tools import smoke_test_all_tools

    print("=" * 70)
    print("       PHONE REVIVAL: DEPENDENCY & ENVIRONMENT SETUP")
    print("=" * 70)

    # 1. Native CLI Suite Check
    report = smoke_test_all_tools()
    tools_installed = report.get("core_tools_installed", False)
    print(f"\n[1. NATIVE CLI SUITE (libimobiledevice)]")
    if tools_installed:
        print("  ✅ libimobiledevice CLI tools are already installed and smoke-tested.")
    else:
        print("  ❌ libimobiledevice suite is NOT installed.")
        should_install = install_all or yes or prompt_yes_no("  -> Download and install native CLI suite into ./tools/bin?", default=False)
        if should_install:
            res = install_cli_tools(consent=True)
            print(f"  Result: {res.get('message')}")

    # 2. Password proof tool (the pre-erase gate needs it)
    import password_tracker
    print("\n[2. PASSWORD PROOF TOOL (iOSbackup)]")
    if password_tracker.proof_tool_available():
        print("  ✅ A password proof tool is installed.")
    else:
        print("  ❌ No password proof tool: the phone can't be cleared for erasing until one is installed.")
        if install_all or yes or prompt_yes_no("  -> Install iOSbackup with pip?", default=True):
            print(f"  Result: {install_proof_tool().get('status')}")

    # 3. iTunes & AMDS Check
    it_status = check_itunes_installation()
    print(f"\n[3. ITUNES & APPLE MOBILE DEVICE DRIVERS]")
    if sys.platform == "win32":
        print("  (Backups only need Apple Mobile Device Support. iTunes is needed for the RESTORE.)")
    if it_status.itunes_installed:
        print(f"  ✅ iTunes is installed ({it_status.install_type}, v{it_status.itunes_version}).")
        if it_status.amds_service_running:
            print("  ✅ Apple Mobile Device Service is running.")
        else:
            print(f"  ⚠️ Apple Mobile Device Service is not running. (Repair via official Apple installer: {get_apple_itunes_download_url()})")
    else:
        dl_url = get_apple_itunes_download_url()
        print("  🚨 iTunes is NOT installed.")
        print(f"  Official Apple download link: {dl_url}")
        should_install = install_all or yes or prompt_yes_no("  -> Download official iTunes from Apple website?", default=True)
        if should_install:
            res = install_itunes()
            print(f"  Result: {res.get('status')} via {res.get('method')}")

    print("\n" + "=" * 70)
    print("Setup check complete! Run 'python iphone_manager.py status' to verify.")
    print("=" * 70)


if __name__ == "__main__":
    import argparse

    from common import ensure_utf8_console
    ensure_utf8_console()
    parser = argparse.ArgumentParser(description="Automated Dependency Installer for Phone Revival.")
    parser.add_argument("--tools", action="store_true", help="Install native libimobiledevice CLI tools.")
    parser.add_argument("--itunes", action="store_true", help="Install iTunes (needed on Windows to RESTORE; backups only need Apple Mobile Device Support).")
    parser.add_argument("--proof-tool", action="store_true", help="Install iOSbackup, which proves the backup password.")
    parser.add_argument("--all", "-a", action="store_true", help="Install all missing dependencies.")
    parser.add_argument("--yes", "-y", action="store_true", help="Non-interactive mode (accept prompts).")
    args = parser.parse_args()

    if args.tools:
        # Without --yes, macOS only reports the choices; the agent relays them to the user.
        res = install_cli_tools(consent=args.yes)
        print(json.dumps(res, indent=2))
    elif args.proof_tool:
        print(json.dumps(install_proof_tool(), indent=2))
    elif args.itunes:
        res = install_itunes()
        print(json.dumps(res, indent=2))
    else:
        run_interactive_or_automated_setup(install_all=args.all, yes=args.yes)
