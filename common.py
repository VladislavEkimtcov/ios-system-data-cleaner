"""
Shared primitives for ios-system-data-cleaner (`common.py`).

Single source of truth for things that were previously copied (and drifting) across
modules: the MobileSync backup root, the private runtime state directory, native tool
discovery, ideviceinfo parsing, connected-device enumeration, plist loading, directory
sizing, the host mount, and the atomic pre-wipe clone helper.

Runtime state (password history, snapshots, market ledger, backup status, token
manifests) lives in ~/.ios-system-data-cleaner/ (mode 0700), overridable with
IOS_CLEANER_STATE_DIR, so nothing sensitive is ever written into the repo working tree.
"""

from __future__ import annotations

import os
import plistlib
from datetime import datetime, timezone
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

GB = 1000 ** 3
GIB = 1024 ** 3

# Finder/iTunes "Archive" copies get a -YYYYMMDD-HHMMSS suffix; our safety clone is -prewipe, and older
# generations of it (kept, never overwritten) are -prewipe-YYYYMMDD-HHMMSS.
ARCHIVE_SUFFIX = re.compile(r"-\d{8}-\d{6}$|-prewipe$")
PREWIPE_SUFFIX = "-prewipe"


def safety_copy_enabled() -> bool:
    """The -prewipe safety copy is made only on macOS, where APFS clones it instantly at no space cost.
    On Windows it is a full ~10 GB copy, and the whole-new-backup rename-aside already keeps every previous
    backup, so it is skipped there (decided 2026-11-11)."""
    return sys.platform == "darwin"

REPO_ROOT = Path(__file__).resolve().parent


def ensure_utf8_console() -> None:
    """Makes stdout/stderr tolerate emoji and box-drawing characters.

    Legacy Windows consoles (cp1252 / cp437) raise UnicodeEncodeError on the first ✅.
    Reconfigure to UTF-8 with errors="replace" so output degrades to '?' instead of
    crashing. No-op where the stream is already UTF-8 or cannot be reconfigured.
    """
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if enc == "utf8" or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


# Every entry point imports common (directly or through another module), so doing this once
# here covers them all on Windows.
if sys.platform == "win32":
    ensure_utf8_console()


# ---------------------------------------------------------------------------
# Runtime state directory (never inside the repo)
# ---------------------------------------------------------------------------

def state_dir() -> Path:
    """Returns the private runtime state directory, creating it mode 0700.

    Honors IOS_CLEANER_STATE_DIR; defaults to ~/.ios-system-data-cleaner/.
    """
    override = os.environ.get("IOS_CLEANER_STATE_DIR")
    base = Path(override).expanduser() if override else (Path.home() / ".ios-system-data-cleaner")
    try:
        base.mkdir(parents=True, exist_ok=True)
        os.chmod(base, 0o700)
    except OSError:
        pass
    return base


def state_path(name: str) -> Path:
    """Absolute path to a file inside the state directory."""
    return state_dir() / name


# ---------------------------------------------------------------------------
# Backup root resolution (honors IPHONE_BACKUP_DIR and MOBILESYNC_BACKUP_DIR)
# ---------------------------------------------------------------------------

def windows_backup_root_candidates() -> List[Path]:
    """All places Windows Apple software may keep MobileSync backups, in preference order.

    Win32 iTunes (%APPDATA%), Store iTunes / Apple Devices (%USERPROFILE%\\Apple), and the
    sandboxed Store LocalCache. Existence is not checked here.
    """
    appdata = os.environ.get("APPDATA")
    userprofile = os.environ.get("USERPROFILE") or str(Path.home())
    localappdata = os.environ.get("LOCALAPPDATA")

    cands: List[Path] = []
    if appdata:
        cands.append(Path(appdata) / "Apple Computer" / "MobileSync" / "Backup")
    cands.append(Path(userprofile) / "Apple" / "MobileSync" / "Backup")
    if localappdata:
        packages_dir = Path(localappdata) / "Packages"
        try:
            for p in sorted(packages_dir.glob("AppleInc.iTunes*")):
                cands.append(p / "LocalCache" / "Roaming" / "Apple Computer" / "MobileSync" / "Backup")
        except OSError:
            pass
    return cands


def _newest_backup_mtime(root: Path, udid: Optional[str] = None) -> float:
    """Newest Status.plist/Info.plist mtime under root (only root/udid when udid is given)."""
    dirs: List[Path]
    if udid:
        dirs = [root / udid]
    else:
        try:
            dirs = [d for d in root.iterdir() if d.is_dir()]
        except OSError:
            return -1.0
    newest = -1.0
    for d in dirs:
        for marker in ("Status.plist", "Info.plist"):
            try:
                newest = max(newest, (d / marker).stat().st_mtime)
            except OSError:
                continue
    return newest


def select_backup_root(candidates: List[Path], udid: Optional[str] = None) -> Optional[Path]:
    """Picks the backup root among existing candidates.

    An old iTunes install can leave one folder behind while the Apple Devices app writes to
    another, so "first existing" is not good enough. Preference:
      1. a candidate that holds backup <udid> (newest one if several do);
      2. the candidate holding the most recently written backup;
      3. the first existing candidate (empty folders, ties).
    Returns None when no candidate exists.
    """
    existing = [c for c in candidates if c.is_dir()]
    if not existing:
        return None
    if udid:
        holding = [c for c in existing if (c / udid).is_dir()]
        if holding:
            return max(holding, key=lambda c: _newest_backup_mtime(c, udid))
    best = max(existing, key=lambda c: _newest_backup_mtime(c))  # max() keeps the first on ties
    return best


def backup_root(udid: Optional[str] = None) -> Path:
    """
    Resolves the MobileSync/iTunes backup directory.

    Order:
      1. IPHONE_BACKUP_DIR (primary override, used by tests)
      2. MOBILESYNC_BACKUP_DIR (documented in .env.example)
      3. Windows: the existing candidate chosen by select_backup_root (prefers the one that
         holds `udid`, then the most recently written one), else the Win32 default
      4. macOS / Linux default
    """
    for var in ("IPHONE_BACKUP_DIR", "MOBILESYNC_BACKUP_DIR"):
        custom = os.environ.get(var)
        if custom:
            return Path(custom).expanduser().resolve()

    if sys.platform == "win32":
        cands = windows_backup_root_candidates()
        chosen = select_backup_root(cands, udid)
        if chosen is not None:
            return chosen
        if os.environ.get("APPDATA"):
            return cands[0]
        return Path.home() / "Apple Computer" / "MobileSync" / "Backup"

    return Path.home() / "Library" / "Application Support" / "MobileSync" / "Backup"


# ---------------------------------------------------------------------------
# Condition waits (instead of fixed sleeps)
# ---------------------------------------------------------------------------

def wait_until(
    predicate: Callable[[], Any],
    timeout: float,
    interval: float = 0.5,
    *,
    clock: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], None]] = None,
) -> Any:
    """Polls predicate() until it returns a truthy value or `timeout` seconds pass.

    Returns that truthy value, or None on timeout. The predicate is checked immediately
    and once more at the deadline, so a condition that is already true costs no wait.
    Exceptions from the predicate count as "not yet".
    """
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    deadline = clock() + max(0.0, timeout)
    while True:
        try:
            result = predicate()
        except Exception:
            result = None
        if result:
            return result
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        sleep(min(interval, remaining))


# Convenience module-level constant computed at import. Code that must observe a
# mid-run env change (or a test monkeypatch) should call backup_root() instead.
BACKUP_ROOT = backup_root()


def host_mount() -> Path:
    """Returns the host volume where backups live, for free-space checks."""
    if sys.platform == "win32":
        root = backup_root()
        if root.anchor:
            return Path(root.anchor)
        return Path(os.environ.get("SystemDrive", "C:") + "\\")
    data_vol = Path("/System/Volumes/Data")
    return data_vol if data_vol.exists() else Path("/")


# ---------------------------------------------------------------------------
# Words for the person: "Mac"/"Finder" on macOS, "PC"/"iTunes" on Windows
# ---------------------------------------------------------------------------

def host_words(platform: Optional[str] = None) -> Dict[str, str]:
    """The words messages use for this computer, so one string serves both platforms:
    computer ("Mac"/"PC"), app (the restore app: "Finder"/"iTunes"), python (the command to type),
    and keychain (where a proven password is kept)."""
    plat = platform or sys.platform
    if plat == "win32":
        return {"computer": "PC", "app": "iTunes", "python": "python", "keychain": "Windows Credential Manager",
                "tools_install": "`python dep_installer.py --tools --yes`"}
    return {"computer": "Mac", "app": "Finder", "python": "python3", "keychain": "Keychain",
            "tools_install": "`python3 dep_installer.py --tools --yes`, or run `brew install libimobiledevice` yourself"}


# ---------------------------------------------------------------------------
# Alerts: a sound and a banner for the person on the couch (macOS and Windows)
# ---------------------------------------------------------------------------

# macOS sound names used across the repo -> winsound.MessageBeep types (MB_ICONASTERISK, MB_OK, MB_ICONHAND,
# MB_ICONEXCLAMATION). Unknown names get the plain MB_OK beep.
_WIN_BEEPS = {"Ping": 0x40, "Glass": 0x0, "Hero": 0x0, "Basso": 0x10, "Funk": 0x30}

# Toast through WinRT, with the classic tray balloon as the fallback. The text arrives in environment
# variables, never interpolated into the script, so quotes or Cyrillic in a phone name can't break it.
_WIN_TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
$t = $env:IOSC_ALERT_TITLE; $m = $env:IOSC_ALERT_BODY
try {
  [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
  [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
  $x = New-Object Windows.Data.Xml.Dom.XmlDocument
  $x.LoadXml('<toast><visual><binding template="ToastGeneric"><text/><text/></binding></visual><audio silent="true"/></toast>')
  $n = $x.GetElementsByTagName('text')
  $n.Item(0).AppendChild($x.CreateTextNode($t)) | Out-Null
  $n.Item(1).AppendChild($x.CreateTextNode($m)) | Out-Null
  $app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
  [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show(
    [Windows.UI.Notifications.ToastNotification]::new($x))
  'TOAST'
} catch {
  Add-Type -AssemblyName System.Windows.Forms, System.Drawing
  $i = New-Object System.Windows.Forms.NotifyIcon
  $i.Icon = [System.Drawing.SystemIcons]::Information; $i.Visible = $true
  $i.ShowBalloonTip(8000, $t, $m, 'Info'); Start-Sleep -Seconds 4; $i.Dispose()
  'BALLOON'
}
"""


def play_sound(sound_name: str) -> None:
    """Plays a short alert sound without blocking. macOS: /System/Library/Sounds/<name>.aiff via afplay.
    Windows: the matching system beep (winsound.MessageBeep) in a thread. Every failure is swallowed."""
    if sys.platform == "win32":
        def _beep() -> None:
            try:
                import winsound
                winsound.MessageBeep(_WIN_BEEPS.get(sound_name, 0x0))
            except Exception:  # noqa: BLE001
                pass
        import threading
        threading.Thread(target=_beep, daemon=True).start()
        return
    sound_file = Path(f"/System/Library/Sounds/{sound_name}.aiff")
    if sound_file.exists():
        try:
            subprocess.Popen(["afplay", str(sound_file)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            pass


def send_notification(message: str, title: str = "iPhone Revival", subtitle: str = "Backup Engine",
                      sound: Optional[str] = None) -> Optional[str]:
    """Shows a desktop banner. macOS: `display notification`. Windows: a toast (fallback: a tray balloon),
    run in the background so it never blocks. Returns how it was sent ("osascript", "windows") or None."""
    if sys.platform == "win32":
        env = os.environ.copy()
        env["IOSC_ALERT_TITLE"] = f"{title}: {subtitle}" if subtitle else title
        env["IOSC_ALERT_BODY"] = message
        try:
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-Command", _WIN_TOAST_PS],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return "windows"
        except Exception:  # noqa: BLE001
            return None
    clean = lambda v: v.replace("\\", "\\\\").replace('"', '\\"')  # noqa: E731
    script = f'display notification "{clean(message)}" with title "{clean(title)}" subtitle "{clean(subtitle)}"'
    if sound:
        script += f' sound name "{clean(sound)}"'
    try:
        subprocess.run(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0)
        return "osascript"
    except Exception:  # noqa: BLE001
        return None


def alert(message: str, title: str = "iPhone Revival", subtitle: str = "", sound: Optional[str] = None) -> None:
    """One call for 'the person needs to know this now': a sound (if given) plus a banner, on macOS or Windows."""
    if sound:
        play_sound(sound)
    send_notification(message, title=title, subtitle=subtitle)


# ---------------------------------------------------------------------------
# Native tool discovery
# ---------------------------------------------------------------------------

LOCAL_TOOLS_BIN = REPO_ROOT / "tools" / "bin"


def find_tool(tool_name: str) -> Optional[Path]:
    """Finds a CLI tool across repo tools/bin, PATH, and Scoop. Appends .exe on Windows."""
    candidates = [tool_name]
    if sys.platform == "win32" and not tool_name.lower().endswith(".exe"):
        candidates = [f"{tool_name}.exe", tool_name]

    for c in candidates:
        local_cand = LOCAL_TOOLS_BIN / c
        if local_cand.exists() and os.access(str(local_cand), os.X_OK):
            return local_cand

    for c in candidates:
        which_path = shutil.which(c)
        if which_path:
            return Path(which_path)

    if sys.platform == "win32":
        scoop_dir = Path.home() / "scoop" / "apps" / "libimobiledevice" / "current"
        for c in candidates:
            scoop_cand = scoop_dir / c
            if scoop_cand.exists():
                return scoop_cand
            scoop_bin = scoop_dir / "bin" / c
            if scoop_bin.exists():
                return scoop_bin

    return None


def run_ideviceinfo(*extra: str) -> Dict[str, str]:
    """Runs ideviceinfo and parses 'Key: Value' lines. Returns {} on failure."""
    ideviceinfo_bin = find_tool("ideviceinfo")
    if not ideviceinfo_bin:
        return {}
    try:
        raw = subprocess.check_output([str(ideviceinfo_bin), *extra], stderr=subprocess.DEVNULL, timeout=20)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {}
    out: Dict[str, str] = {}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            out[k.strip()] = v.strip()
    return out


def connected_udids() -> List[str]:
    """Returns connected iOS device UDIDs via idevice_id -l."""
    idevice_id_bin = find_tool("idevice_id")
    if idevice_id_bin:
        try:
            out = subprocess.check_output([str(idevice_id_bin), "-l"], stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
            return [line.strip() for line in out.splitlines() if line.strip()]
        except (OSError, subprocess.CalledProcessError):
            pass
    return []


# ---------------------------------------------------------------------------
# Connection diagnosis: "why can't we see the phone?"
# ---------------------------------------------------------------------------

APPLE_USB_VID = 0x05AC
# iPhone/iPad/iPod in normal mode enumerate with PIDs 0x1290-0x12FF; recovery and DFU use
# their own PIDs. Anything else with Apple's VID (keyboards, hubs, displays) is ignored.
_IOS_NORMAL_PIDS = range(0x1290, 0x1300)
_IOS_RECOVERY_PIDS = {0x1280, 0x1281, 0x1282, 0x1283}
_IOS_DFU_PIDS = {0x1222, 0x1227}

CONNECTION_MESSAGES = {
    "ready": "iPhone connected and trusted.",
    "tools_missing": "The small helper tools that let this Mac talk to the iPhone aren't installed. Run `python3 dep_installer.py --tools` to see your choices (let the agent install them, install them yourself, or use the slower Finder route).",
    "no_usb_device": "No iPhone on USB. Use a data cable (not charge-only), plug it straight into "
                     "the computer, and unlock the phone.",
    "recovery_mode": "An iPhone is attached in Recovery/DFU mode. It can only be restored, not "
                     "backed up, in this state.",
    "usb_not_listed": "An iPhone is on USB but the Apple device service can't talk to it yet. Unlock "
                      "the phone and tap 'Trust' if asked; if that doesn't help, unplug and replug it.",
    "needs_trust": "Unlock your iPhone and tap 'Trust' on the 'Trust This Computer?' prompt, then "
                   "enter your passcode.",
    "locked": "Unlock your iPhone (enter the passcode), then try again.",
    "trust_denied": "The iPhone's 'Trust This Computer?' prompt was declined. Unplug and replug it, "
                    "then tap 'Trust'. No prompt? On the phone: Settings > General > Transfer or "
                    "Reset iPhone > Reset > Reset Location & Privacy.",
    "lockdown_unreachable": "The iPhone is listed but not answering. Unlock it; if that doesn't help, "
                            "unplug and replug it.",
}


def usb_ios_devices() -> Optional[List[str]]:
    """Physically attached iOS devices from the OS USB tree, as modes ('normal', 'recovery',
    'dfu'). Independent of libimobiledevice and of pairing. None when it can't be determined."""
    pairs: List[Tuple[int, int]] = []
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["ioreg", "-p", "IOUSB", "-l", "-w0"], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=10).stdout
            for block in out.split("+-o "):
                v = re.search(r'"idVendor" = (\d+)', block)
                pr = re.search(r'"idProduct" = (\d+)', block)
                if v and pr:
                    pairs.append((int(v.group(1)), int(pr.group(1))))
        elif sys.platform.startswith("linux"):
            for dev in Path("/sys/bus/usb/devices").glob("*"):
                try:
                    pairs.append((int((dev / "idVendor").read_text().strip(), 16),
                                  int((dev / "idProduct").read_text().strip(), 16)))
                except (OSError, ValueError):
                    continue
        elif sys.platform == "win32":
            from win_tools import detect_apple_pnp_devices
            for d in detect_apple_pnp_devices():
                m = re.search(r"VID_05AC&PID_([0-9A-Fa-f]{4})", d.instance_id or "")
                if m:
                    pairs.append((APPLE_USB_VID, int(m.group(1), 16)))
        else:
            return None
    except Exception:
        return None
    modes: List[str] = []
    for vid, pid in pairs:
        if vid != APPLE_USB_VID:
            continue
        if pid in _IOS_NORMAL_PIDS:
            modes.append("normal")
        elif pid in _IOS_RECOVERY_PIDS:
            modes.append("recovery")
        elif pid in _IOS_DFU_PIDS:
            modes.append("dfu")
    return modes


def _pairing_state(idevicepair: Path, udid: str, prompt_trust: bool) -> str:
    """Maps `idevicepair validate` (and, when unpaired, `pair`) output to a state name."""
    def run(cmd: str) -> str:
        try:
            res = subprocess.run([str(idevicepair), "-u", udid, cmd], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=15)
        except Exception:
            return "ERROR: timeout"
        return (res.stdout + res.stderr).lower()

    out = run("validate")
    if "success" in out:
        return "ready"
    if "not paired" in out and prompt_trust:
        out = run("pair")  # raises the "Trust This Computer?" prompt on the phone
        if "success" in out:
            return "ready"
    if "accept the trust dialog" in out or "not paired" in out:
        return "needs_trust"
    if "passcode is set" in out:
        return "locked"
    if "denied the trust dialog" in out:
        return "trust_denied"
    return "lockdown_unreachable"


def diagnose_connection(udid: Optional[str] = None, prompt_trust: bool = True) -> Dict[str, Any]:
    """Explains whether the phone is usable and, if not, the one thing the user should do.

    Returns {"state", "ready", "message", "udids", "usb"}. `state` is a key of
    CONNECTION_MESSAGES. With prompt_trust (default), an attached but unpaired phone gets a
    pairing request so the Trust prompt actually appears on its screen.
    """
    def result(state: str, udids: List[str], usb: Optional[List[str]]) -> Dict[str, Any]:
        return {"state": state, "ready": state == "ready", "message": CONNECTION_MESSAGES[state],
                "udids": udids, "usb": usb}

    udids = connected_udids()
    usb = usb_ios_devices()
    if udid:
        udids = [u for u in udids if u == udid]

    if udids:
        idevicepair = find_tool("idevicepair")
        if not idevicepair:
            return result("ready", udids, usb)  # can't check pairing; don't block on it
        states = [_pairing_state(idevicepair, u, prompt_trust) for u in udids]
        bad = [s for s in states if s != "ready"]
        return result(bad[0] if bad else "ready", udids, usb)

    if not find_tool("idevice_id"):
        return result("tools_missing", [], usb)
    if usb and "normal" not in usb:
        return result("recovery_mode", [], usb)
    if usb:
        return result("usb_not_listed", [], usb)
    return result("no_usb_device", [], usb)


# ---------------------------------------------------------------------------
# Plist / sizing helpers
# ---------------------------------------------------------------------------

def load_plist(path: Path) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Returns (data, error_message). error_message is None on success."""
    if not path.exists():
        return None, f"{path.name} missing"
    try:
        with open(path, "rb") as f:
            return plistlib.load(f), None
    except Exception as e:  # noqa: BLE001 - report any parse failure
        return None, f"{path.name} unreadable: {e}"


def dir_size(path: Path) -> Optional[int]:
    """Bytes on disk under `path` via iterative scandir, with du fallback on POSIX."""
    if not path.exists():
        return 0
    try:
        total = 0
        stack = [str(path)]
        while stack:
            current = stack.pop()
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_file(follow_symlinks=False):
                            total += entry.stat().st_size
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                    except (PermissionError, OSError):
                        continue
        return total
    except (PermissionError, OSError):
        pass

    if sys.platform != "win32" and shutil.which("du"):
        try:
            out = subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True, check=True).stdout
            return int(out.split()[0]) * 1024
        except (OSError, subprocess.CalledProcessError, ValueError, IndexError):
            return None
    return None


def keybag_fingerprint(backup_dir: Path) -> Optional[str]:
    """
    Returns a short stable fingerprint of the backup's encryption keybag, or None if the
    backup is unencrypted / unreadable. Derived from the BackupKeyBag blob in
    Manifest.plist, so it changes whenever the backup password (and thus the keybag salt /
    wrapped keys) changes. Used to invalidate a "password proven" record once the backup
    is re-encrypted with a different password.
    """
    import hashlib
    manifest, err = load_plist(backup_dir / "Manifest.plist")
    if err or not manifest:
        return None
    if not manifest.get("IsEncrypted"):
        return None
    blob = manifest.get("BackupKeyBag")
    if not isinstance(blob, (bytes, bytearray)) or not blob:
        return None
    return hashlib.sha256(bytes(blob)).hexdigest()[:16]


def count_files(path: Path) -> int:
    """Counts regular files under `path`."""
    total = 0
    try:
        stack = [str(path)]
        while stack:
            current = stack.pop()
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += 1
                    except (PermissionError, OSError):
                        continue
    except (PermissionError, OSError):
        pass
    return total


# ---------------------------------------------------------------------------
# Atomic pre-wipe clone (shared by revive and backup_runner)
# ---------------------------------------------------------------------------

class CloneError(Exception):
    """Raised when the pre-wipe safety clone cannot be created or verified."""


def _plausible_backup(path: Path) -> Tuple[bool, str]:
    """Checks that a directory looks like a complete backup (plists + non-trivial Manifest.db)."""
    for required in ("Info.plist", "Manifest.plist", "Status.plist"):
        if not (path / required).is_file():
            return False, f"missing {required}"
    mdb = path / "Manifest.db"
    if not mdb.is_file() or mdb.stat().st_size == 0:
        return False, "Manifest.db missing or empty"
    return True, "ok"


def _backup_date(path: Path) -> Optional[datetime]:
    """The backup's own completion time (Status.plist `Date`), or None if unreadable."""
    try:
        with (path / "Status.plist").open("rb") as f:
            d = plistlib.load(f).get("Date")
        return d if isinstance(d, datetime) else None
    except Exception:  # noqa: BLE001
        return None


def archive_stamp(path: Path) -> str:
    """YYYYMMDD-HHMMSS (local time) of when this backup was made, for naming an archived generation."""
    d = _backup_date(path)
    if d is not None:
        d = d.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None) if d.tzinfo is None else d.astimezone().replace(tzinfo=None)
    else:
        try:
            d = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            d = datetime.now()
    return d.strftime("%Y%m%d-%H%M%S")


def _free_name(parent: Path, base: str) -> Path:
    cand, n = parent / base, 1
    while cand.exists():
        n += 1
        cand = parent / f"{base}-{n}"
    return cand


def rotate_backup_aside(src: Path) -> Optional[Path]:
    """Moves an existing backup folder to `<name>-<its own timestamp>` (an instant rename, nothing is
    copied or deleted) so a new backup never overwrites it. Returns the archive path, or None when
    there was nothing to move."""
    src = Path(src)
    if not src.is_dir():
        return None
    dest = _free_name(src.parent, f"{src.name}-{archive_stamp(src)}")
    os.replace(str(src), str(dest))
    return dest


def _clone_is_current(src: Path, clone: Path) -> bool:
    """True when the clone holds the same (or a newer) backup than `src`."""
    s, c = _backup_date(src), _backup_date(clone)
    if s is None or c is None:
        return True  # can't tell: keep the existing clone rather than destroy it
    return c >= s


def backup_device_name(info: Dict[str, Any]) -> str:
    """The phone's real name from a backup's Info.plist. `backup_labels.py` rewrites `Device Name` into a
    descriptive label for Finder's pop-up and keeps the real name in `Original Device Name`."""
    return str(info.get("Original Device Name") or info.get("Device Name") or "")


FACTORY_DEVICE_NAMES = {"iphone", "ipad", "ipod touch", "ipod"}


def _parse_kv(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            out[k.strip()] = v.strip()
    return out


def _ideviceinfo(udid: Optional[str], *extra: str) -> Dict[str, str]:
    tool = find_tool("ideviceinfo")
    if not tool:
        return {}
    cmd = [str(tool)] + (["-u", udid] if udid else []) + list(extra)
    try:
        return _parse_kv(subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", timeout=10))
    except (OSError, subprocess.SubprocessError):
        return {}


def device_setup_verdict(udid: Optional[str] = None) -> Dict[str, Any]:
    """Is the phone sitting at the Hello / Setup Assistant screen right now?

    Returns {"verdict": "hello" | "set_up" | "unknown", "reasons": [...], ...}.

    The phone's own `SetupState` (com.apple.purplebuddy) is authoritative: any value means the
    phone is already set up. The NVRAM `obliteration` record is NOT proof of anything on its own,
    because it survives a restore, and `PasswordProtected` is unreliable. "hello" needs the
    setup state to be empty AND positive evidence of a fresh erase. Everything ambiguous is
    "unknown", which callers must treat as "do not restore".
    """
    info = _ideviceinfo(udid)
    if not info:
        return {"verdict": "unknown", "reasons": ["the phone did not answer"], "udid": udid}
    setup_state = _ideviceinfo(udid, "-q", "com.apple.purplebuddy").get("SetupState", "")
    name = info.get("DeviceName", "")
    activation = info.get("ActivationState", "")
    oblit = info.get("obliteration", "")
    base = {"setup_state": setup_state, "device_name": name, "activation_state": activation,
            "udid": info.get("UniqueDeviceID", udid), "product_type": info.get("ProductType", ""),
            "product_version": info.get("ProductVersion", "")}
    if setup_state:
        return {**base, "verdict": "set_up", "reasons": [f"the phone reports it is set up ({setup_state})"]}
    # Positive evidence of a fresh erase: the factory name, or a phone that is not activated yet.
    # The NVRAM erase record is only supporting detail: it survives a restore, and a restored phone
    # can report an empty SetupState (seen 2026-11-09: named "Яблоко", Finder on its Summary page,
    # erase record present), so on its own it must never read as "Hello".
    strong: List[str] = []
    if name.strip().lower() in FACTORY_DEVICE_NAMES:
        strong.append("the phone has its factory name")
    if activation and activation != "Activated":
        strong.append(f"activation state is {activation}")
    if not strong:
        why = "no setup state, but also no sign of a fresh erase"
        if oblit:
            why = "it has an erase record, but that survives a restore, and the phone has its own name and is activated"
        return {**base, "verdict": "unknown", "reasons": [why]}
    return {**base, "verdict": "hello", "reasons": strong + (["the phone has an erase record"] if oblit else [])}


def process_running(*names: str) -> bool:
    """Is any process with one of these names running? macOS/Linux: `pgrep -f` (matches the command line).
    Windows: `tasklist` image names (".exe" added when missing). Never raises; False when it can't tell."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, errors="replace",
                                 timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            running = {line.split('","', 1)[0].strip('"').lower() for line in out.splitlines() if line}
            wanted = {(n if n.lower().endswith(".exe") else n + ".exe").lower() for n in names}
            return bool(running & wanted)
        return subprocess.run(["pgrep", "-f", "|".join(names)], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


MIN_BACKUP_FREE_GB = 20.0
NON_COW_SPACE_FACTOR = 1.2  # the new backup plus 20% headroom (no safety copy off macOS)


def space_needed_gb(estimate_bytes: Optional[int], platform: Optional[str] = None) -> Tuple[float, str]:
    """Free space a backup cycle needs, with the reason in plain words. On a Mac (APFS) the safety copy is a
    copy-on-write clone that costs ~nothing, so a flat 20 GB is enough. Elsewhere there is no safety copy
    (see safety_copy_enabled): the new backup plus headroom, about 1.2x (the previous backup is only renamed aside)."""
    plat = platform or sys.platform
    if plat == "darwin" or not estimate_bytes:
        return MIN_BACKUP_FREE_GB, "a fixed minimum"
    est_gb = estimate_bytes / GB
    need = max(MIN_BACKUP_FREE_GB, round(est_gb * NON_COW_SPACE_FACTOR, 1))
    return need, f"the backup, about {est_gb:.1f} GB, plus some headroom"


def make_prewipe_clone(src: Path, verbose: bool = True) -> Path:
    """
    Creates an immutable ``<src>-prewipe`` safety clone, atomically and idempotently.

    - Never nests a copy inside an existing clone: if the final clone already exists,
      verifies and is as new as the source backup, it is kept as-is (immutable). A clone
      of an OLDER backup is refreshed (new clone built and verified first, then swapped in);
      the older clone is kept as `<src>-prewipe-<timestamp>`, never overwritten or deleted.
    - Clones to a temp sibling first, verifies the plists and a non-empty Manifest.db,
      then swaps it into place with an atomic rename.
    - Uses APFS copy-on-write (``cp -c``) on macOS when possible, robocopy on Windows,
      and a plain recursive copy otherwise, after a free-space check.

    Returns the path to the verified clone. Raises CloneError on failure.
    """
    src = Path(src)
    if not src.is_dir():
        raise CloneError(f"source backup {src} does not exist")
    ok, why = _plausible_backup(src)
    if not ok:
        raise CloneError(f"refusing to clone an incomplete backup ({why}): {src}")

    final = src.parent / f"{src.name}{PREWIPE_SUFFIX}"
    if final.exists():
        ok, why = _plausible_backup(final)
        if ok and _clone_is_current(src, final):
            if verbose:
                print(f"  ✅ Pre-wipe clone already present and valid: {final.name} (kept immutable)")
            return final
        if not ok:
            # Present but broken: remove so we can re-create cleanly.
            shutil.rmtree(final, ignore_errors=True)
        # else: valid but stale; it stays in place until the fresh clone is verified.

    # Free-space check for the worst case (a full copy).
    src_bytes = dir_size(src) or 0
    try:
        free = shutil.disk_usage(str(src.parent)).free
    except OSError:
        free = None
    if free is not None and src_bytes and free < src_bytes * 1.1 and sys.platform != "darwin":
        # On APFS (darwin) a CoW clone needs ~no space; elsewhere a full copy needs headroom.
        raise CloneError(
            f"insufficient free space for safety clone: need ~{src_bytes / GB:.1f} GB, have {free / GB:.1f} GB"
        )

    tmp = Path(tempfile.mkdtemp(prefix=f".{src.name}-prewipe-", dir=str(src.parent)))
    staging = tmp / src.name
    try:
        if sys.platform == "darwin":
            res = subprocess.run(["cp", "-ac", str(src), str(staging)], capture_output=True, text=True)
            if res.returncode != 0:
                raise CloneError(f"cp clone failed: {res.stderr.strip() or res.stdout.strip()}")
        elif sys.platform == "win32":
            res = subprocess.run(
                ["robocopy", str(src), str(staging), "/E", "/COPY:DAT", "/DCOPY:T", "/XJ", "/MT:8",
                 "/R:1", "/W:1", "/NFL", "/NDL", "/NP", "/NJH", "/NJS"],
                capture_output=True, text=True, errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            # robocopy exit codes < 8 indicate success (0-7 are informational).
            if verbose or res.returncode:
                print(f"  (robocopy exit {res.returncode}: {'ok' if res.returncode < 8 else 'FAILED'})")
            if res.returncode >= 8:
                raise CloneError(f"robocopy clone failed (exit {res.returncode}): {(res.stdout or '')[-300:].strip()}")
        else:
            shutil.copytree(src, staging, symlinks=True)

        ok, why = _plausible_backup(staging)
        if not ok:
            raise CloneError(f"cloned copy failed verification ({why})")

        # Swap into place. A stale clone is KEPT: renamed to `<name>-prewipe-<its timestamp>`
        # (never deleted here; the agent proposes cleanup from the backup list), and only once
        # its replacement is verified.
        if final.exists():
            os.replace(str(final), str(_free_name(final.parent, f"{final.name}-{archive_stamp(final)}")))
        os.replace(str(staging), str(final))
    except CloneError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        raise CloneError(f"clone failed: {type(e).__name__}: {e}") from e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if verbose:
        print(f"  ✅ Pre-wipe safety clone created: {final.name} ({dir_size(final) or 0 // GB} bytes)")
    return final
