#!/usr/bin/env python3
"""
iOS Backup Password Prover & Attempt Tracker (`password_tracker.py`).

Helps you confirm the encryption password of a local iOS backup before you erase the
phone, without ever writing the password to disk.

Security properties:
  1. No plaintext on disk. Each attempt is recorded as a salted hash plus its result
     (and the keybag fingerprint it was tested against), only so duplicates can be
     skipped and a prior success can be re-used. Plaintext candidates live only in this
     process's memory, masked, for the current session's table.
  2. Passwords are never passed on the command line (visible to `ps`). Proof uses
     iOSbackup over stdin, or `mvt-ios decrypt-backup` over the environment.
  3. A proven password is tied to the current backup's keybag fingerprint, so changing
     the backup password invalidates the proof.
  4. The optional web UI binds to 127.0.0.1 with a per-session token and a Host
     allowlist, and never returns a password in any response.
  5. Fails closed: if no proving tool is installed, proof is refused with an install hint.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import re
import secrets
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common import ARCHIVE_SUFFIX, backup_root, connected_udids, find_tool, host_words, keybag_fingerprint, state_path

HISTORY_JSON = state_path("tried_passwords.json")
SALT_FILE = state_path(".pw_salt")

# Plaintext candidates for THIS process only (masked), never persisted.
_SESSION_ATTEMPTS: List[Dict[str, Any]] = []
_SESSION_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Hashing (no plaintext persistence)
# ---------------------------------------------------------------------------

def _install_salt() -> bytes:
    """Per-install random salt so stored hashes aren't a plain dictionary of attempts."""
    try:
        if SALT_FILE.exists():
            return SALT_FILE.read_bytes()
    except OSError:
        pass
    salt = secrets.token_bytes(16)
    try:
        SALT_FILE.write_bytes(salt)
        os.chmod(SALT_FILE, 0o600)
    except OSError:
        pass
    return salt


def hash_password(udid: str, password: str) -> str:
    """Deterministic salted hash of (udid, password) for dedup / proof records."""
    h = hashlib.sha256()
    h.update(_install_salt())
    h.update(udid.encode("utf-8"))
    h.update(b":")
    h.update(password.encode("utf-8"))
    return h.hexdigest()


def _mask(password: str) -> str:
    s = password or ""
    if len(s) <= 2:
        return "•" * len(s)
    if len(s) <= 6:
        return s[0] + "•" * (len(s) - 1)
    return f"{s[0]}{'•' * (len(s) - 2)}{s[-1]} ({len(s)} chars)"


# ---------------------------------------------------------------------------
# History (hashed on disk; masked in-memory for display)
# ---------------------------------------------------------------------------

def load_history() -> List[Dict[str, Any]]:
    """Loads hashed attempt records from the private state directory."""
    if not HISTORY_JSON.exists():
        return []
    try:
        with open(HISTORY_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_attempt(
    udid: str,
    password: str,
    success: bool,
    duration_sec: float,
    keybag_fp: Optional[str] = None,
) -> Dict[str, Any]:
    """Records an attempt as a salted hash + result (no plaintext) and a masked session row."""
    history = load_history()
    entry = {
        "id": len(history) + 1,
        "udid": udid,
        "pw_hash": hash_password(udid, password),
        "success": success,
        "keybag_fingerprint": keybag_fp,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "duration_sec": round(duration_sec, 2),
    }
    history.append(entry)
    try:
        with open(HISTORY_JSON, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
        os.chmod(HISTORY_JSON, 0o600)
    except OSError:
        pass

    with _SESSION_LOCK:
        _SESSION_ATTEMPTS.append({
            "id": entry["id"],
            "masked": _mask(password),
            "success": success,
            "timestamp": entry["timestamp"],
            "duration_sec": entry["duration_sec"],
        })
    return entry


def has_been_tried(udid: str, password: str) -> Optional[Dict[str, Any]]:
    """Returns the prior record for this (udid, password) if already attempted."""
    target = hash_password(udid, password)
    for h in load_history():
        if h.get("udid") == udid and h.get("pw_hash") == target:
            return h
    return None


def proven_for_keybag(udid: str, keybag_fp: Optional[str]) -> bool:
    """True only if a prior success was recorded against this exact keybag fingerprint."""
    if not keybag_fp:
        return False
    for h in load_history():
        if h.get("udid") == udid and h.get("success") and h.get("keybag_fingerprint") == keybag_fp:
            return True
    return False


# ---------------------------------------------------------------------------
# Password proving (no argv; fails closed)
# ---------------------------------------------------------------------------

def _find_iosbackup_python() -> str:
    """Finds a python interpreter with iOSbackup importable (PATH python, then pyenv)."""
    candidates = [sys.executable]
    candidates += sorted(
        [str(p) for p in Path.home().glob(".pyenv/versions/*/bin/python*")
         if p.is_file() and not p.name.endswith("-config")],
        reverse=True,
    )
    for py in dict.fromkeys(candidates):
        try:
            if subprocess.run([py, "-c", "import iOSbackup"], capture_output=True, timeout=10).returncode == 0:
                return py
        except Exception:
            continue
    return ""


def _find_mvt_ios() -> str:
    """Finds a working mvt-ios (PATH, then pyenv)."""
    candidates = [shutil.which("mvt-ios")]
    candidates += [str(p) for p in sorted(
        Path.home().glob(".pyenv/versions/*/bin/mvt-ios"),
        key=lambda p: p.stat().st_mtime, reverse=True)]
    for c in dict.fromkeys(str(x) for x in candidates if x):
        try:
            if subprocess.run([c, "version"], capture_output=True, timeout=60).returncode == 0:
                return c
        except (OSError, subprocess.TimeoutExpired):
            continue
    return ""


_IOSBACKUP_PROBE = """
import sys, iOSbackup
root, udid = sys.argv[1], sys.argv[2]
pwd = sys.stdin.readline().rstrip("\\n")
try:
    iOSbackup.iOSbackup(backuproot=root, udid=udid, cleartextpassword=pwd)
    sys.exit(0)
except Exception:
    sys.exit(1)
"""


class ProofToolUnavailable(RuntimeError):
    """Raised when no tool is installed that can cryptographically prove the password."""


def test_backup_password(
    udid: str,
    password: str,
    backup_root_dir: Optional[Path] = None,
) -> Tuple[bool, float]:
    """
    Cryptographically tests a candidate password against the backup's keybag.

    Order (both prove the password; neither exposes it via argv):
      1. iOSbackup (fast ManifestKey unwrap) — password over stdin.
      2. mvt-ios decrypt-backup (heavier, full decrypt to a temp dir) — password via env.
    Fails closed: raises ProofToolUnavailable if neither tool is installed.

    Returns (success, duration_seconds).
    """
    root = str(backup_root_dir or backup_root())
    backup_dir = Path(root) / udid

    py_bin = _find_iosbackup_python()
    if py_bin:
        t0 = time.time()
        res = subprocess.run(
            [py_bin, "-c", _IOSBACKUP_PROBE, root, udid],
            input=password + "\n",
            capture_output=True,
            text=True,
        )
        return res.returncode == 0, time.time() - t0

    mvt = _find_mvt_ios()
    if mvt and backup_dir.is_dir():
        t0 = time.time()
        with tempfile.TemporaryDirectory(prefix="pwproof_") as tmp:
            env = {**os.environ, "MVT_IOS_BACKUP_PASSWORD": password}
            res = subprocess.run(
                [mvt, "decrypt-backup", "-d", tmp, str(backup_dir)],
                capture_output=True, text=True, env=env,
            )
        return res.returncode == 0, time.time() - t0

    raise ProofToolUnavailable(
        "No password-proving tool is installed. Install one of:\n"
        f"  {host_words()['python']} dep_installer.py --proof-tool   (iOSbackup: fast keybag unwrap), or\n"
        f"  {host_words()['python']} -m pip install mvt   (mvt-ios decrypt-backup)\n"
        "Proof of password is required before the phone can be erased."
    )


def proof_tool_available() -> bool:
    """True if at least one password-proving tool is installed."""
    return bool(_find_iosbackup_python() or _find_mvt_ios())


def generate_variants(base: str) -> List[str]:
    """Generates common password variations (casing, digits, symbols, recent years)."""
    base = base.strip()
    if not base:
        return []
    variants: List[str] = [base, base.lower(), base.upper(), base.capitalize()]

    year = datetime.now().year
    years = [str(y) for y in (year - 1, year, year + 1)]
    suffixes = ["1", "123", "!", "?", "@", "#", "1234", *years]
    for s in suffixes:
        variants.append(f"{base}{s}")
        variants.append(f"{base.capitalize()}{s}")

    seen = set()
    deduped = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            deduped.append(v)
    return deduped


# ---------------------------------------------------------------------------
# Windows Credential Manager (the Windows twin of the Keychain item below)
# ---------------------------------------------------------------------------
# A generic credential "iOS Backup:<UDID>" per phone, persisted for this Windows user (LOCAL_MACHINE
# persistence = this user on this PC, not roaming). Through advapi32 with ctypes: `cmdkey` cannot read a
# secret back and `keyring` is a third-party package. Windows never shows a generic secret in its UI, so
# there is no "look it up" step on Windows (see open_keychain_ui).

WINCRED_PREFIX = "iOS Backup:"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2


def _wincred_api():
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR), ("LastWritten", FILETIME), ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
        ]

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    PCRED = ctypes.POINTER(CREDENTIALW)
    adv.CredWriteW.argtypes = [PCRED, wintypes.DWORD]
    adv.CredWriteW.restype = wintypes.BOOL
    adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(PCRED)]
    adv.CredReadW.restype = wintypes.BOOL
    adv.CredEnumerateW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                   ctypes.POINTER(ctypes.POINTER(PCRED))]
    adv.CredEnumerateW.restype = wintypes.BOOL
    adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    adv.CredDeleteW.restype = wintypes.BOOL
    adv.CredFree.argtypes = [ctypes.c_void_p]
    adv.CredFree.restype = None
    return ctypes, CREDENTIALW, PCRED, adv


def wincred_write(target: str, user: str, secret: str) -> Tuple[bool, str]:
    try:
        ctypes, CREDENTIALW, _PCRED, adv = _wincred_api()
        blob = secret.encode("utf-16-le")
        buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob) if blob else (ctypes.c_ubyte * 1)()
        cred = CREDENTIALW()
        cred.Type = _CRED_TYPE_GENERIC
        cred.TargetName = target
        cred.UserName = user
        cred.Comment = "iPhone backup password, proven against the backup by ios-system-data-cleaner"
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
        if not adv.CredWriteW(ctypes.byref(cred), 0):
            return False, f"Credential Manager refused the write (error {ctypes.get_last_error()})."
        return True, "ok"
    except Exception as e:  # noqa: BLE001
        return False, f"Credential Manager not available: {e}"


def wincred_read(target: str) -> Optional[str]:
    try:
        ctypes, _CREDENTIALW, PCRED, adv = _wincred_api()
        p = PCRED()
        if not adv.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
            return None
        try:
            c = p.contents
            raw = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize) if c.CredentialBlobSize else b""
            return raw.decode("utf-16-le") or None
        finally:
            adv.CredFree(p)
    except Exception:  # noqa: BLE001
        return None


def wincred_targets(prefix: str = WINCRED_PREFIX) -> List[str]:
    """Target names (never secrets) of our generic credentials."""
    try:
        ctypes, _CREDENTIALW, PCRED, adv = _wincred_api()
        from ctypes import wintypes
        count = wintypes.DWORD()
        arr = ctypes.POINTER(PCRED)()
        if not adv.CredEnumerateW(prefix + "*", 0, ctypes.byref(count), ctypes.byref(arr)):
            return []
        try:
            return [arr[i].contents.TargetName for i in range(count.value)
                    if arr[i].contents.Type == _CRED_TYPE_GENERIC]
        finally:
            adv.CredFree(arr)
    except Exception:  # noqa: BLE001
        return []


def wincred_delete(target: str) -> bool:
    try:
        _ctypes, _CREDENTIALW, _PCRED, adv = _wincred_api()
        return bool(adv.CredDeleteW(target, _CRED_TYPE_GENERIC, 0))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# macOS Keychain integration (dispatches to Credential Manager on Windows)
# ---------------------------------------------------------------------------

_KEYCHAIN_CACHE: Dict[Tuple[Optional[str], str], Optional[str]] = {}


def store_in_keychain(udid: str, password: str) -> Tuple[bool, str]:
    """
    Stores a proven password in the macOS Keychain for Finder auto-fill.

    The item is created WITHOUT -A: access is not granted to every application. Finder
    prompts once to use it, which is the intended, safe behavior.
    Windows: a generic credential "iOS Backup:<UDID>" in Credential Manager (this Windows user only).
    """
    if sys.platform == "win32":
        ok, why = wincred_write(WINCRED_PREFIX + udid, udid, password)
        if ok:
            _KEYCHAIN_CACHE[(udid, "iOS Backup")] = password
            return True, "Stored the password in Windows Credential Manager (for this Windows user)."
        return False, why
    cmd = [
        "security", "add-generic-password",
        "-a", udid,
        "-s", "iOS Backup",
        "-l", "iOS Backup",
        "-w", password,
        "-U",
        "-T", "/usr/bin/security",
    ]
    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
        _KEYCHAIN_CACHE[(udid, "iOS Backup")] = password
        return True, "Stored password in macOS Keychain (Finder will prompt to use it)."
    except subprocess.CalledProcessError as e:
        return False, f"Keychain store error: {e.output}"
    except FileNotFoundError:
        return False, "Keychain not available (macOS only)."


KEYCHAIN_BACKUP_SERVICE = "iOS Backup"
_ACCT_LINE = re.compile(r'^\s*"acct"<blob>="(.*)"\s*$', re.MULTILINE)


def _keychain_item_account(account: Optional[str] = None) -> Optional[str]:
    """Metadata-only lookup of one 'iOS Backup' item (no -w: the secret is never read,
    so no authorization prompt). Returns the item's account name, or None if absent."""
    if sys.platform == "win32":
        targets = wincred_targets()
        if account:
            return account if (WINCRED_PREFIX + account) in targets else None
        return targets[0][len(WINCRED_PREFIX):] if targets else None
    cmd = ["security", "find-generic-password", "-s", KEYCHAIN_BACKUP_SERVICE]
    if account:
        cmd.extend(["-a", account])
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    if res.returncode != 0:
        return None
    m = _ACCT_LINE.search(res.stdout)
    return m.group(1) if m else account


def _known_udids(root: Optional[Path] = None) -> List[str]:
    """UDIDs we have a reason to look up: connected devices plus backup folder names."""
    udids: List[str] = []
    try:
        udids.extend(connected_udids())
    except Exception:
        pass
    root = root or backup_root()
    try:
        for d in root.iterdir():
            if d.is_dir() and (d / "Info.plist").exists():
                udids.append(ARCHIVE_SUFFIX.sub("", d.name))
    except OSError:
        pass
    return udids


def list_keychain_backup_accounts(
    candidate_udids: Optional[List[str]] = None,
    root: Optional[Path] = None,
) -> List[str]:
    """Account names (UDIDs) stored under service 'iOS Backup' in the macOS Keychain.

    Looks up only the UDIDs we know about (candidate_udids, connected devices, backup folder
    names) plus the first 'iOS Backup' item, each with a metadata-only query. It never runs
    `security dump-keychain`, which reads every login-keychain item and can raise prompts.
    """
    if sys.platform == "win32":
        return [t[len(WINCRED_PREFIX):] for t in wincred_targets()]
    if sys.platform != "darwin":
        return []
    wanted = list(candidate_udids or []) + _known_udids(root)
    accounts: List[str] = []
    for udid in dict.fromkeys(u for u in wanted if u):
        if _keychain_item_account(udid):
            accounts.append(udid)
    first = _keychain_item_account(None)
    if first:
        accounts.append(first)
    return list(dict.fromkeys(accounts))


def get_keychain_password(account: Optional[str] = None, service: str = "iOS Backup", timeout_sec: float = 25.0) -> Optional[str]:
    """Retrieves a password from the macOS Keychain, cached to query authorization at most once."""
    cache_key = (account, service)
    if cache_key in _KEYCHAIN_CACHE:
        return _KEYCHAIN_CACHE[cache_key]
    if sys.platform == "win32":
        acct = account or _keychain_item_account(None)
        pwd = wincred_read(WINCRED_PREFIX + acct) if acct else None
        _KEYCHAIN_CACHE[cache_key] = pwd
        return pwd

    cmd = ["security", "find-generic-password", "-s", service]
    if account:
        cmd.extend(["-a", account])
    cmd.append("-w")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
        if res.returncode == 0:
            pwd = res.stdout.strip()
            if pwd:
                _KEYCHAIN_CACHE[cache_key] = pwd
                return pwd
    except Exception:
        pass
    _KEYCHAIN_CACHE[cache_key] = None
    return None


def open_keychain_ui(search_query: str = "iOS Backup") -> bool:
    """Brings macOS Keychain Access forward, filtered to search_query, for manual inspection.
    Windows has no equivalent (Credential Manager never shows a generic secret): returns False."""
    if sys.platform != "darwin":
        return False
    script = f'''
    tell application "Keychain Access"
        activate
    end tell
    tell application "System Events"
        tell process "Keychain Access"
            try
                click menu item "Keychain Access" of menu "Window" of menu bar 1
            end try
            delay 0.5
            try
                set w to window "Keychain Access"
                set tf to text field 1 of group 1 of toolbar 1 of w
                set value of tf to "{search_query}"
            end try
        end tell
    end tell
    '''
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=5)
        return True
    except Exception:
        try:
            subprocess.run(["open", "-a", "Keychain Access"], timeout=5)
            return True
        except Exception:
            return False


def smart_keychain_probe(
    udid: str,
    interactive: bool = True,
    backup_root_dir: Optional[Path] = None,
) -> Tuple[bool, Optional[str], str]:
    """
    Tries passwords stored under 'iOS Backup' in the Keychain against the backup.

    On a verified match: a simple human sanity check (interactive), then returns
    (True, password, msg). On rejection: navigates to Keychain Access, returns
    (False, None, msg). If none match: (False, None, msg).
    """
    accounts = list_keychain_backup_accounts([udid], root=backup_root_dir)
    candidates: List[Tuple[str, str]] = []
    if udid in accounts:
        candidates.append(("Exact UDID match", udid))
    else:
        candidates.extend((f"Keychain account {a[:8]}...", a) for a in accounts)

    root = Path(backup_root_dir) if backup_root_dir else backup_root()
    kb_fp = keybag_fingerprint(root / udid)

    proven_password: Optional[str] = None
    proven_source = ""
    for label, acct in candidates:
        pwd = get_keychain_password(account=acct)
        if not pwd:
            continue
        try:
            ok, dur = test_backup_password(udid, pwd, backup_root_dir=root)
        except ProofToolUnavailable as e:
            return False, None, str(e)
        if ok:
            proven_password = pwd
            proven_source = label
            save_attempt(udid, pwd, success=True, duration_sec=dur, keybag_fp=kb_fp)
            break

    if not proven_password:
        return False, None, (f"No matching password found in {host_words()['keychain']}. "
                             "The operator must prove the password themselves.")

    if interactive and sys.stdin.isatty():
        print("\n=======================================================")
        print("  🔐 SMART KEYCHAIN CHECK: Password Found & Verified!")
        print("=======================================================")
        print(f"  Target Device:    {udid}")
        print(f"  Keychain Source:  {proven_source}")
        print(f"  Cryptographic Probe: ✅ SUCCESS (keybag unwrapped)")
        print("=======================================================\n")
        try:
            ans = input("❓ SANITY CHECK: Use this verified Keychain password to proceed? [Y/n]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n[!] Sanity check aborted. Navigating to Keychain Access...")
            open_keychain_ui("iOS Backup")
            return False, None, "Sanity check aborted. Navigated UI to Keychain Access."
        if ans in ("", "y", "yes"):
            print("✅ Sanity check PASSED: Operator confirmed Keychain credentials.")
            return True, proven_password, f"Verified via Keychain ({proven_source}) and confirmed by operator"
        print("❌ Sanity check REJECTED by operator. Navigating to Keychain Access...")
        open_keychain_ui("iOS Backup")
        return False, None, "Sanity check rejected by operator. Navigated UI to Keychain Access."

    return True, proven_password, f"Verified via Keychain ({proven_source}) in non-interactive mode"


# ---------------------------------------------------------------------------
# Web UI (localhost, per-session token, Host allowlist, never returns passwords)
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>iOS Backup Password Prover</title>
    <style>
        :root { --bg:#0f172a; --surface:#1e293b; --border:#334155; --text:#f8fafc; --muted:#94a3b8;
                --primary:#38bdf8; --primary-hover:#0284c7; --success:#22c55e; --fail:#ef4444; --warn:#f59e0b; }
        * { box-sizing:border-box; margin:0; padding:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
        body { background:var(--bg); color:var(--text); padding:24px; max-width:960px; margin:0 auto; line-height:1.5; }
        header { display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--border); padding-bottom:16px; margin-bottom:24px; }
        h1 { font-size:1.4rem; color:var(--primary); }
        .badge { background:#0369a1; color:#e0f2fe; padding:3px 10px; border-radius:9999px; font-size:.75rem; font-weight:600; }
        .device-info { font-size:.85rem; color:var(--muted); background:var(--surface); padding:10px 16px; border-radius:8px; border:1px solid var(--border); margin-bottom:20px; }
        .input-card { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:20px; margin-bottom:24px; }
        .input-row { display:flex; gap:12px; margin-bottom:12px; }
        input[type=text],input[type=password] { flex:1; padding:12px 16px; border-radius:8px; border:1px solid var(--border); background:#0f172a; color:#fff; font-size:1.1rem; font-family:monospace; outline:none; }
        button { padding:12px 24px; border-radius:8px; border:none; font-weight:600; cursor:pointer; font-size:.95rem; }
        .btn-primary { background:var(--primary); color:#0f172a; }
        .btn-secondary { background:var(--border); color:var(--text); padding:8px 14px; font-size:.85rem; }
        .toggle-row { display:flex; justify-content:space-between; align-items:center; font-size:.85rem; color:var(--muted); }
        .status-box { margin-top:16px; padding:12px 16px; border-radius:8px; font-weight:500; display:none; }
        .status-box.active { display:block; }
        .status-box.error { background:rgba(239,68,68,.15); border:1px solid var(--fail); color:#fca5a5; }
        .status-box.success { background:rgba(34,197,94,.15); border:1px solid var(--success); color:#86efac; }
        .status-box.info { background:rgba(56,189,248,.15); border:1px solid var(--primary); color:#7dd3fc; }
        .status-box.warn { background:rgba(245,158,11,.15); border:1px solid var(--warn); color:#fde047; }
        .section-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; }
        .table-wrap { background:var(--surface); border:1px solid var(--border); border-radius:12px; overflow:hidden; max-height:440px; overflow-y:auto; }
        table { width:100%; border-collapse:collapse; text-align:left; font-size:.9rem; }
        th { background:#111827; padding:12px 16px; color:var(--muted); font-size:.8rem; text-transform:uppercase; position:sticky; top:0; }
        td { padding:10px 16px; border-top:1px solid var(--border); }
        .pill-fail { background:rgba(239,68,68,.2); color:#f87171; padding:2px 8px; border-radius:6px; font-weight:600; font-size:.8rem; }
        .pill-pass { background:rgba(34,197,94,.2); color:#4ade80; padding:2px 8px; border-radius:6px; font-weight:600; font-size:.8rem; }
        .pwd-cell { font-family:monospace; color:#38bdf8; }
    </style>
</head>
<body>
    <header>
        <h1>🔐 iOS Backup Password Prover</h1>
        <span class="badge">Local • No plaintext stored</span>
    </header>

    <div class="device-info">
        <strong>Target Device:</strong> <code>__UDID__</code> &nbsp;|&nbsp;
        Candidates are tested locally against the backup keybag. Passwords are never saved to disk or returned over the network.
    </div>

    <div class="input-card">
        <form id="pwd-form" onsubmit="event.preventDefault(); submitCandidate();">
            <div class="input-row">
                <input type="text" id="pwd-input" placeholder="Type a candidate password..." autocomplete="off" autofocus>
                <button type="submit" id="btn-submit" class="btn-primary">Test</button>
            </div>
            <div class="toggle-row">
                <label><input type="checkbox" id="chk-mask" onchange="toggleMask(this.checked)"> Mask input</label>
                <div style="display:flex; gap:8px; flex-wrap:wrap;">
                    <button type="button" class="btn-secondary" onclick="checkKeychain()">__CHECK_LABEL__</button>
                    __OPEN_KEYCHAIN_BUTTON__
                    <button type="button" class="btn-secondary" onclick="tryVariants()">Generate Variations</button>
                </div>
            </div>
        </form>
        <div id="status-box" class="status-box"></div>
    </div>

    <div class="section-header">
        <h2 style="font-size:1.1rem;">This Session's Attempts (<span id="count-span">0</span>)</h2>
        <button class="btn-secondary" onclick="clearHistory()">Clear</button>
    </div>
    <div class="table-wrap">
        <table>
            <thead><tr><th style="width:50px;">#</th><th>Candidate (masked)</th><th style="width:120px;">Result</th><th style="width:160px;">Time</th></tr></thead>
            <tbody id="history-body"></tbody>
        </table>
    </div>

    <script>
        const TOKEN = "__TOKEN__";
        function hdrs() { return { "Content-Type": "application/json", "X-Session-Token": TOKEN }; }
        let historyData = [];
        function toggleMask(m) { document.getElementById("pwd-input").type = m ? "password" : "text"; }
        async function fetchHistory() {
            try {
                const res = await fetch("/api/history", { headers: { "X-Session-Token": TOKEN } });
                historyData = await res.json(); renderTable();
            } catch (e) {}
        }
        function renderTable() {
            const tb = document.getElementById("history-body"); tb.innerHTML = "";
            historyData.slice().reverse().forEach(it => {
                const tr = document.createElement("tr");
                const idTd = document.createElement("td"); idTd.textContent = it.id; idTd.style.color = "var(--muted)";
                const pwTd = document.createElement("td"); pwTd.className = "pwd-cell"; pwTd.textContent = it.masked;
                const rTd = document.createElement("td");
                const span = document.createElement("span");
                span.className = it.success ? "pill-pass" : "pill-fail";
                span.textContent = it.success ? "✅ MATCH" : "❌ fail";
                rTd.appendChild(span);
                const tTd = document.createElement("td"); tTd.textContent = it.timestamp; tTd.style.color = "var(--muted)";
                tr.append(idTd, pwTd, rTd, tTd); tb.appendChild(tr);
            });
            document.getElementById("count-span").textContent = historyData.length;
        }
        function showStatus(msg, type) {
            const b = document.getElementById("status-box");
            b.className = "status-box active " + type; b.textContent = msg;
        }
        async function submitCandidate() {
            const inp = document.getElementById("pwd-input");
            const pwd = inp.value.trim(); if (!pwd) return;
            const btn = document.getElementById("btn-submit"); btn.disabled = true; btn.textContent = "Testing...";
            showStatus("Testing candidate against the backup keybag...", "info");
            try {
                const res = await fetch("/api/check", { method:"POST", headers:hdrs(), body:JSON.stringify({password:pwd}) });
                const d = await res.json();
                if (d.success) { showStatus("🎉 Correct! This password unlocks the backup. You can proceed to restore.", "success"); inp.value=""; }
                else if (d.error) { showStatus("Error: " + d.error, "error"); }
                else { showStatus("❌ Not correct (" + d.duration_sec + "s). Try another candidate.", "error"); }
                await fetchHistory();
            } catch (e) { showStatus("Error: " + e.message, "error"); }
            finally { btn.disabled=false; btn.textContent="Test"; }
        }
        async function tryVariants() {
            const base = document.getElementById("pwd-input").value.trim();
            if (!base) { showStatus("Type a base word first.", "warn"); return; }
            showStatus("Generating and testing variations...", "info");
            const res = await fetch("/api/variants", { method:"POST", headers:hdrs(), body:JSON.stringify({base}) });
            const d = await res.json(); await fetchHistory();
            if (d.error) showStatus("Error: " + d.error, "error");
            else if (d.matched) showStatus("🎉 A generated variation unlocked the backup!", "success");
            else showStatus("Tested " + d.count + " variations. No match.", "error");
        }
        async function checkKeychain() {
            showStatus("Searching __KEYCHAIN__ and testing stored credentials...", "info");
            try {
                const res = await fetch("/api/keychain-check", { method:"POST", headers:hdrs() });
                const d = await res.json(); await fetchHistory();
                if (d.success) showStatus("🎉 A stored Keychain password unlocks the backup.", "success");
                else showStatus(d.message || "No working password found in __KEYCHAIN__.", "warn");
            } catch (e) { showStatus("Error: " + e.message, "error"); }
        }
        async function openKeychain() {
            showStatus("Opening Keychain Access...", "info");
            await fetch("/api/open-keychain", { headers: { "X-Session-Token": TOKEN } });
        }
        async function clearHistory() {
            if (!confirm("Clear this session's attempt history?")) return;
            await fetch("/api/clear", { method:"POST", headers:hdrs() }); fetchHistory();
        }
        fetchHistory();
    </script>
</body>
</html>
"""


def render_page(udid: str, token: str, platform: Optional[str] = None) -> str:
    """The helper page for this computer. Windows has no 'Open Keychain Access': Credential Manager never shows a
    saved backup password, so that button is left out there."""
    plat = platform or sys.platform
    words = host_words(plat)
    mac = plat == "darwin"
    button = ('<button type="button" class="btn-secondary" onclick="openKeychain()">Open Keychain Access</button>'
              if mac else "")
    return (HTML_TEMPLATE.replace("__UDID__", udid).replace("__TOKEN__", token)
            .replace("__CHECK_LABEL__", "Smart Keychain Check" if mac else "Check saved passwords")
            .replace("__OPEN_KEYCHAIN_BUTTON__", button)
            .replace("__KEYCHAIN__", "macOS Keychain" if mac else words["keychain"]))


class PasswordServerHandler(http.server.BaseHTTPRequestHandler):
    target_udid: str = ""
    session_token: str = ""
    allowed_hosts: Tuple[str, ...] = ()
    # Set the moment any password (typed, a variant, or a Keychain probe) is cryptographically proven.
    proven: threading.Event = threading.Event()

    def log_message(self, *args):
        return

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        return host in self.allowed_hosts

    def _token_ok(self) -> bool:
        tok = self.headers.get("X-Session-Token") or urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query).get("token", [""])[0]
        return bool(self.session_token) and secrets.compare_digest(tok, self.session_token)

    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200):
        self._send(code, json.dumps(obj).encode("utf-8"))

    def do_GET(self):
        if not self._host_ok():
            self._send(403, b"forbidden", "text/plain")
            return
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            html = render_page(self.target_udid, self.session_token)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return
        if not self._token_ok():
            self._json({"error": "invalid session token"}, 403)
            return
        if path == "/api/history":
            with _SESSION_LOCK:
                self._json(list(_SESSION_ATTEMPTS))
        elif path == "/api/open-keychain":
            ok = open_keychain_ui("iOS Backup")
            self._json({"status": "navigated" if ok else "failed"})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._host_ok():
            self._send(403, b"forbidden", "text/plain")
            return
        if not self._token_ok():
            self._json({"error": "invalid session token"}, 403)
            return
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception:
            req = {}

        kb_fp = keybag_fingerprint(backup_root() / self.target_udid)

        if path == "/api/check":
            pwd = req.get("password", "")
            if not pwd:
                self._json({"error": "empty password"}); return
            prior = has_been_tried(self.target_udid, pwd)
            try:
                success, dur = test_backup_password(self.target_udid, pwd)
            except ProofToolUnavailable as e:
                self._json({"error": str(e)}); return
            save_attempt(self.target_udid, pwd, success, dur, keybag_fp=kb_fp)
            if success:
                store_in_keychain(self.target_udid, pwd)
                self.proven.set()
            self._json({"success": success, "duration_sec": round(dur, 2), "already_tried": bool(prior)})

        elif path == "/api/variants":
            base = req.get("base", "")
            matched = False
            tested = 0
            try:
                for v in generate_variants(base):
                    tested += 1
                    ok, dur = test_backup_password(self.target_udid, v)
                    save_attempt(self.target_udid, v, ok, dur, keybag_fp=kb_fp)
                    if ok:
                        matched = True
                        store_in_keychain(self.target_udid, v)
                        self.proven.set()
                        break
            except ProofToolUnavailable as e:
                self._json({"error": str(e)}); return
            self._json({"count": tested, "matched": matched})

        elif path == "/api/keychain-check":
            try:
                ok, _pwd, msg = smart_keychain_probe(self.target_udid, interactive=False)
            except ProofToolUnavailable as e:
                self._json({"error": str(e)}); return
            if ok:
                self.proven.set()
            self._json({"success": ok, "message": msg})

        elif path == "/api/clear":
            with _SESSION_LOCK:
                _SESSION_ATTEMPTS.clear()
            try:
                if HISTORY_JSON.exists():
                    HISTORY_JSON.unlink()
            except OSError:
                pass
            self._json({"status": "cleared"})
        else:
            self._json({"error": "not found"}, 404)


def launch_web_ui(udid: str, port: int = 8765, exit_on_success: bool = False,
                  timeout_minutes: float = 30.0, open_browser: bool = True) -> bool:
    """Launches the localhost web UI with a fresh per-session token and Host allowlist.

    With exit_on_success (for agents) it stops itself a moment after a password is proven,
    prints `PROOF_OK`, and returns True; it returns False on timeout. Otherwise it serves
    until Ctrl+C and returns whether a password was proven.
    """
    token = secrets.token_urlsafe(32)
    PasswordServerHandler.target_udid = udid
    PasswordServerHandler.session_token = token
    PasswordServerHandler.proven = threading.Event()

    socketserver.TCPServer.allow_reuse_address = True
    httpd = None
    for candidate in range(port, port + 10):  # first free port at or after the requested one
        try:
            httpd = socketserver.TCPServer(("127.0.0.1", candidate), PasswordServerHandler)
            port = candidate
            break
        except OSError:
            continue
    if httpd is None:
        raise OSError(f"no free port in {port}-{port + 9}")

    PasswordServerHandler.allowed_hosts = (f"127.0.0.1:{port}", f"localhost:{port}")
    url = f"http://127.0.0.1:{port}/?token={token}"
    print("\n=======================================================")
    print("  🔐 iOS Backup Password Prover (local web UI)")
    print("=======================================================")
    print(f"  Target Device:  {udid}")
    print(f"  Web Interface:  {url}")
    print(f"  (Only reachable from this {host_words()['computer']}, with a one-time session token.)")
    print("=======================================================\n", flush=True)
    if open_browser:
        webbrowser.open(url)

    if not exit_on_success:
        print("[*] Server running. Press Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[+] Password prover stopped.")
        finally:
            httpd.server_close()
        return PasswordServerHandler.proven.is_set()

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    print(f"[*] Waiting for a correct password (up to {timeout_minutes:g} min)...", flush=True)
    try:
        ok = PasswordServerHandler.proven.wait(timeout=timeout_minutes * 60)
        if ok:
            time.sleep(2.0)  # let the page show its success message before the server goes away
    except KeyboardInterrupt:
        ok = False
    finally:
        httpd.shutdown()
        httpd.server_close()
    print(f"PROOF_OK: password proven and saved to your {host_words()['keychain']}." if ok else
          "PROOF_TIMEOUT: no correct password was entered.", flush=True)
    return ok


def launch_cli_interactive(udid: str):
    """Interactive CLI prover. Typed candidates stay in this terminal; nothing is written in plaintext."""
    print("\n=======================================================")
    print("  🔐 iOS Backup Password Prover (CLI Mode)")
    print("=======================================================")
    print(f"  Target Device:  {udid}")
    print("  Type a candidate and press Enter. ':variants <word>' tests variations.")
    print("  'q' to quit.")
    print("=======================================================\n")

    if not proof_tool_available():
        print("🚨 No password-proving tool installed. Install one:")
        print(f"     {host_words()['python']} dep_installer.py --proof-tool   (installs iOSbackup)")
        return

    print(f"[*] Probing {host_words()['keychain']} for stored credentials...")
    try:
        kc_ok, kc_pwd, _ = smart_keychain_probe(udid, interactive=True)
    except ProofToolUnavailable as e:
        print(f"🚨 {e}")
        return
    if kc_ok and kc_pwd:
        print("\n🎉 SMART KEYCHAIN CHECK PASSED: a stored password unlocks the backup!")
        return

    kb_fp = keybag_fingerprint(backup_root() / udid)
    while True:
        try:
            candidate = input("Enter candidate password > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting.")
            break
        if not candidate:
            continue
        if candidate.lower() in ("q", "quit", "exit"):
            break
        if candidate.startswith(":variants "):
            base = candidate.split(" ", 1)[1].strip()
            vs = generate_variants(base)
            print(f"[*] Testing {len(vs)} variants...")
            found = False
            for v in vs:
                ok, dur = test_backup_password(udid, v)
                save_attempt(udid, v, ok, dur, keybag_fp=kb_fp)
                if ok:
                    print(f"✅ MATCH ({dur:.2f}s)!")
                    store_in_keychain(udid, v)
                    print(f"🎉 Winning password found and saved to {host_words()['keychain']}.\n")
                    found = True
                    break
            if not found:
                print(f"[-] No match in {len(vs)} variants.")
            continue
        sys.stdout.write("[*] Probing backup keybag... ")
        sys.stdout.flush()
        ok, dur = test_backup_password(udid, candidate)
        save_attempt(udid, candidate, ok, dur, keybag_fp=kb_fp)
        if ok:
            print(f"✅ MATCH ({dur:.2f}s)!")
            store_in_keychain(udid, candidate)
            print(f"🎉 Winning password confirmed and stored in {host_words()['keychain']}.\n")
            break
        print(f"❌ fail ({dur:.2f}s)")


def main() -> int:
    parser = argparse.ArgumentParser(description="iOS Backup Password Prover & Attempt Tracker.")
    parser.add_argument("--udid", help="Target device UDID (defaults to the connected device).")
    parser.add_argument("--cli", action="store_true", help="Interactive CLI instead of web UI.")
    parser.add_argument("--port", type=int, default=8765, help="Web UI port (default: 8765).")
    parser.add_argument("--exit-on-success", action="store_true",
                        help="Web UI stops by itself once a correct password is proven (exit 0; 1 on timeout). For agents.")
    parser.add_argument("--timeout-minutes", type=float, default=30.0, help="With --exit-on-success: give up after this long.")
    parser.add_argument("--no-browser", action="store_true", help="Print the web UI link instead of opening a browser.")
    parser.add_argument("--check", help="Test a single password non-interactively (reads from stdin if '-').")
    parser.add_argument("--history", action="store_true", help="Print attempt history (hashed; no plaintext).")
    parser.add_argument("--variants", help="Generate and test variations for a root string.")
    parser.add_argument("--keychain-check", action="store_true", help="Probe macOS Keychain for stored backup passwords.")
    parser.add_argument("--open-keychain", action="store_true", help="Open macOS Keychain Access filtered for iOS Backup.")
    args = parser.parse_args()

    udid = args.udid
    if not udid:
        udids = connected_udids()
        udid = udids[0] if udids else os.environ.get("TARGET_UDID")

    if args.open_keychain:
        print("[*] Opening macOS Keychain Access...")
        open_keychain_ui("iOS Backup")
        return 0

    if not udid and not args.history:
        from common import diagnose_connection
        print(f"❌ No device UDID. {diagnose_connection()['message']} Or pass --udid / set TARGET_UDID.",
              file=sys.stderr)
        return 2

    if args.keychain_check:
        try:
            ok, pwd, msg = smart_keychain_probe(udid, interactive=True)
        except ProofToolUnavailable as e:
            print(f"🚨 {e}"); return 2
        print(("✅ " if ok and pwd else "🚨 ") + msg)
        return 0 if (ok and pwd) else 1

    if args.history:
        h = load_history()
        print(f"Total attempts recorded (hashed, no plaintext): {len(h)}")
        for item in h:
            st = "PASS" if item["success"] else "fail"
            print(f"#{item['id']:02d} [{st}] {item['timestamp']} ({item['duration_sec']}s) udid={item['udid']}")
        return 0

    if args.check is not None:
        pwd = sys.stdin.readline().rstrip("\n") if args.check == "-" else args.check
        kb_fp = keybag_fingerprint(backup_root() / udid)
        try:
            ok, dur = test_backup_password(udid, pwd)
        except ProofToolUnavailable as e:
            print(f"🚨 {e}"); return 2
        save_attempt(udid, pwd, ok, dur, keybag_fp=kb_fp)
        if ok:
            print(f"✅ MATCH ({dur:.2f}s): password is correct.")
            store_in_keychain(udid, pwd)
            return 0
        print(f"❌ fail ({dur:.2f}s): password is incorrect.")
        return 1

    if args.variants:
        kb_fp = keybag_fingerprint(backup_root() / udid)
        vs = generate_variants(args.variants)
        print(f"[*] Testing {len(vs)} variations on UDID {udid}...")
        try:
            for v in vs:
                ok, dur = test_backup_password(udid, v)
                save_attempt(udid, v, ok, dur, keybag_fp=kb_fp)
                if ok:
                    print(f"✅ MATCH ({dur:.2f}s)!")
                    store_in_keychain(udid, v)
                    return 0
        except ProofToolUnavailable as e:
            print(f"🚨 {e}"); return 2
        print("[-] No generated variant matched.")
        return 1

    if args.cli:
        launch_cli_interactive(udid)
    else:
        ok = launch_web_ui(udid, port=args.port, exit_on_success=args.exit_on_success,
                           timeout_minutes=args.timeout_minutes, open_browser=not args.no_browser)
        if args.exit_on_success:
            return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
