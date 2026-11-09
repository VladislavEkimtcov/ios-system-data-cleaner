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

# Finder/iTunes "Archive" copies get a -YYYYMMDD-HHMMSS suffix; our safety clone is -prewipe.
ARCHIVE_SUFFIX = re.compile(r"-\d{8}-\d{6}$|-prewipe$")
PREWIPE_SUFFIX = "-prewipe"

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
            out = subprocess.check_output([str(idevice_id_bin), "-l"], stderr=subprocess.DEVNULL, text=True)
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
    "tools_missing": "libimobiledevice tools not found. Install them: python3 dep_installer.py --tools",
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
                                 text=True, timeout=10).stdout
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
                                 text=True, timeout=15)
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


def make_prewipe_clone(src: Path, verbose: bool = True) -> Path:
    """
    Creates an immutable ``<src>-prewipe`` safety clone, atomically and idempotently.

    - Never nests a copy inside an existing clone: if the final clone already exists and
      verifies, it is kept as-is (immutable) and returned unchanged.
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
        if ok:
            if verbose:
                print(f"  ✅ Pre-wipe clone already present and valid: {final.name} (kept immutable)")
            return final
        # Present but broken: remove so we can re-create cleanly.
        shutil.rmtree(final, ignore_errors=True)

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
                ["robocopy", str(src), str(staging), "/E", "/COPY:DAT", "/R:1", "/W:1", "/NFL", "/NDL", "/NP"],
                capture_output=True, text=True,
            )
            # robocopy exit codes < 8 indicate success (0-7 are informational).
            if res.returncode >= 8:
                raise CloneError(f"robocopy clone failed (exit {res.returncode})")
        else:
            shutil.copytree(src, staging, symlinks=True)

        ok, why = _plausible_backup(staging)
        if not ok:
            raise CloneError(f"cloned copy failed verification ({why})")

        # Atomic swap into place.
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
