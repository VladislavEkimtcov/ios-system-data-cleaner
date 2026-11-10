"""
iPhone Revival Restorer & Setup State Automation (`restorer.py`).

Provides automated detection and restoration orchestration for iPhones resetting
back into setup mode ('Hello' screen):
  1. USB Reconnection & Setup State Detector (`wait_for_device_return`)
  2. Finder activation of an erased phone (`activate_via_finder`)
  3. The restore itself lives in `finder_restore.py` (`restorer.py ui-restore`): a deterministic
     state machine over Finder's UI that hands off to the agent when a step goes wrong. The
     older `stage_finder_restore_selection` / `auto_confirm_finder_restore_sheet` /
     `monitor_restore_progress` helpers below are low-level debugging aids; don't use them for a real restore.
  4. Post-Restore Verification & Storage Delta Tracker (`audit_post_restore_storage`)
"""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common import backup_root, connected_udids, find_tool, make_prewipe_clone, wait_until


def get_connected_udids() -> List[str]:
    """Returns connected iOS device UDIDs (shared implementation)."""
    return connected_udids()


def query_device_info(udid: Optional[str] = None, *extra: str) -> Dict[str, str]:
    """Queries ideviceinfo with key-value output. Extra args allow a -q <domain> query."""
    bin_path = find_tool("ideviceinfo")
    if not bin_path:
        return {}
    cmd = [str(bin_path)]
    if udid:
        cmd.extend(["-u", udid])
    cmd.extend(extra)
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True, timeout=10)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {}

    info: Dict[str, str] = {}
    for line in out.splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            info[k.strip()] = v.strip()
    return info


def is_device_in_setup_state(info: Dict[str, str]) -> bool:
    """
    Determines if device is in fresh factory setup / Hello state based on lockdownd attributes.
    Key indicators:
    - ActivationState in ('Unactivated', 'FactoryDefault')
    - PasswordProtected is 'false' or False
    - BrickState is 'true' or True
    - 'obliteration' NVRAM/status entry present
    """
    if not info:
        return False
    act = info.get("ActivationState", "")
    pwd = str(info.get("PasswordProtected", "")).lower()
    brick = str(info.get("BrickState", "")).lower()
    oblit = info.get("obliteration", "")

    if act in ("Unactivated", "FactoryDefault"):
        return True
    if act == "Activated":
        return False
    if pwd == "false" and brick == "true":
        return True
    if bool(oblit):
        return True
    return False


def wait_for_device_return(
    expected_udid: str,
    timeout_sec: int = 600,
    poll_interval: float = 3.0,
    expect_reset: bool = False,
    verbose: bool = True
) -> Dict[str, Any]:
    """
    Blocks until the expected UDID appears on USB in setup/booted state.
    If expect_reset is True, checks if device is already reset, or waits for
    device to disconnect or transition to unactivated/wiped setup state.
    Returns device metadata dictionary.
    """
    start_time = time.time()

    # Pre-check: If expect_reset is True, check if device is ALREADY in factory setup state
    if expect_reset:
        initial_info = query_device_info(expected_udid)
        if is_device_in_setup_state(initial_info):
            dev_name = initial_info.get("DeviceName", "iPhone")
            act_state = initial_info.get("ActivationState", "Unknown")
            prod_ver = initial_info.get("ProductVersion", "Unknown")
            prod_type = initial_info.get("ProductType", "Unknown")
            if verbose:
                print(f"[+] Device {expected_udid} is ALREADY in factory setup ('Hello') state!")
                print(f"    - Device Name:     {dev_name}")
                print(f"    - Product:         {prod_type} (iOS {prod_ver})")
                print(f"    - ActivationState: {act_state}")
                print(f"    - Setup State:     Confirmed (Unactivated / Obliterated)")
            return {
                "status": "connected",
                "udid": expected_udid,
                "device_name": dev_name,
                "activation_state": act_state,
                "product_type": prod_type,
                "product_version": prod_ver,
                "raw_info": initial_info,
                "elapsed_seconds": 0,
                "already_reset": True,
            }

        # Phase 1: Wait for device to disconnect or transition to wiped state
        if verbose:
            print(f"[*] Device {expected_udid} is currently online.")
            print("[*] Phase 1/2: Waiting for device to disconnect or transition to unactivated/wiped state...")
        last_info_check = time.time()
        while time.time() - start_time < timeout_sec:
            connected = get_connected_udids()
            if expected_udid not in connected:
                elapsed_disc = int(time.time() - start_time)
                if verbose:
                    print(f"\n[+] Device {expected_udid} disconnected after {elapsed_disc}s! Factory reset initiated.")
                    print("[*] Phase 2/2: Monitoring USB bus for reboot into 'Hello' setup screen...")
                break

            # In-place reset check: device may complete obliteration without USB link dropping
            now = time.time()
            if now - last_info_check >= 3.0:
                cur_info = query_device_info(expected_udid)
                if is_device_in_setup_state(cur_info):
                    elapsed_reset = int(now - start_time)
                    dev_name = cur_info.get("DeviceName", "iPhone")
                    act_state = cur_info.get("ActivationState", "Unknown")
                    prod_ver = cur_info.get("ProductVersion", "Unknown")
                    prod_type = cur_info.get("ProductType", "Unknown")
                    if verbose:
                        print(f"\n[+] Device {expected_udid} detected in factory setup state (in-place wipe, {elapsed_reset}s elapsed)!")
                        print(f"    - Device Name:     {dev_name}")
                        print(f"    - Product:         {prod_type} (iOS {prod_ver})")
                        print(f"    - ActivationState: {act_state}")
                    return {
                        "status": "connected",
                        "udid": expected_udid,
                        "device_name": dev_name,
                        "activation_state": act_state,
                        "product_type": prod_type,
                        "product_version": prod_ver,
                        "raw_info": cur_info,
                        "elapsed_seconds": elapsed_reset,
                        "in_place_reset": True,
                    }
                last_info_check = now

            time.sleep(poll_interval)
        else:
            raise TimeoutError(f"Device {expected_udid} did not disconnect or reset within {timeout_sec} seconds.")

    reconnect_start = time.time()
    last_log = 0.0

    if verbose and not expect_reset:
        print(f"[*] Monitoring USB bus for return of device: {expected_udid}...")
        print(f"[*] Timeout: {timeout_sec}s, Polling interval: {poll_interval}s")

    while time.time() - reconnect_start < timeout_sec:
        udids = get_connected_udids()
        elapsed = int(time.time() - reconnect_start)

        if expected_udid in udids:
            if verbose:
                print(f"\n[+] Device {expected_udid} reconnected to USB! (Elapsed: {elapsed}s)")
                print("[*] Waiting for lockdown/mobile services to settle...")

            # Device needs a few seconds after USB enumerate before lockdownd answers
            for attempt in range(15):
                time.sleep(2)
                info = query_device_info(expected_udid)
                if info:
                    dev_name = info.get("DeviceName", "iPhone")
                    act_state = info.get("ActivationState", "Unknown")
                    prod_ver = info.get("ProductVersion", "Unknown")
                    prod_type = info.get("ProductType", "Unknown")
                    if verbose:
                        print(f"[+] Lockdownd connected!")
                        print(f"    - Device Name:     {dev_name}")
                        print(f"    - Product:         {prod_type} (iOS {prod_ver})")
                        print(f"    - ActivationState: {act_state}")
                    return {
                        "status": "connected",
                        "udid": expected_udid,
                        "device_name": dev_name,
                        "activation_state": act_state,
                        "product_type": prod_type,
                        "product_version": prod_ver,
                        "raw_info": info,
                        "elapsed_seconds": elapsed,
                    }
                if verbose:
                    sys.stdout.write(".")
                    sys.stdout.flush()

        now = time.time()
        if verbose and (now - last_log >= 15.0):
            print(f"[*] Still waiting for device reboot... ({elapsed}s elapsed)")
            last_log = now

        time.sleep(poll_interval)

    from common import diagnose_connection
    hint = diagnose_connection(expected_udid)["message"]
    raise TimeoutError(f"Device {expected_udid} did not reconnect within {timeout_sec} seconds. {hint}")


def device_window_names(udid: Optional[str] = None) -> List[str]:
    """Names the device's Finder window may carry.

    Finder titles the window with the device name. A wiped phone shows the factory name
    ("iPhone"), but a live or freshly restored one shows the user's own name, which need not
    contain "iPhone" at all. Collects the live DeviceName (ideviceinfo) and the name recorded
    in the backup's Info.plist.
    """
    names: List[str] = []
    if not udid:
        udids = connected_udids()
        udid = udids[0] if len(udids) == 1 else None
    live = query_device_info(udid).get("DeviceName") if udid else None
    if live:
        names.append(live)
    if udid:
        try:
            with open(backup_root(udid) / udid / "Info.plist", "rb") as f:
                from common import backup_device_name
                recorded = backup_device_name(plistlib.load(f))
            if recorded:
                names.append(str(recorded))
        except (OSError, plistlib.InvalidFileException, ValueError):
            pass
    return list(dict.fromkeys(n.strip() for n in names if n and n.strip()))


def _applescript_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def device_window_match(udid: Optional[str] = None) -> str:
    """AppleScript boolean over `wName` that matches the device's Finder window: an exact
    custom device name, or the factory "iPhone"/"iPad" naming."""
    # starts-with: device names may carry trailing whitespace that Finder keeps in the title
    clauses = [f"wName starts with {_applescript_str(n)}" for n in device_window_names(udid)]
    clauses += ['wName contains "iPhone"', 'wName contains "iPad"']
    return "(" + " or ".join(clauses) + ")"


def detect_finder_setup_view(udid: Optional[str] = None) -> Dict[str, Any]:
    """
    Inspects macOS Finder for an iOS device setup window ('Welcome to your new iPhone').
    Returns UI element status.
    """
    match = device_window_match(udid)
    script = '''
    tell application "System Events"
        tell process "Finder"
            set winList to every window
            set foundWin to missing value
            repeat with w in winList
                set wName to name of w
                if __DEVICE_WINDOW_MATCH__ then
                    set foundWin to w
                    exit repeat
                end if
            end repeat
            
            if foundWin is missing value then
                return "NO_WINDOW"
            end if
            
            set winName to name of foundWin
            set allStaticTexts to {}
            try
                set txtList to value of every static text of splitter group 1 of splitter group 1 of foundWin
                set allStaticTexts to txtList
            end try
            
            set hasWelcome to false
            repeat with t in allStaticTexts
                if (t as text) contains "Welcome" or (t as text) contains "Set up" or (t as text) contains "Restore" then
                    set hasWelcome to true
                end if
            end repeat
            
            set radioTexts to {}
            try
                -- Direct children of splitter group 1 of splitter group 1
                set rList to every radio button of splitter group 1 of splitter group 1 of foundWin
                repeat with r in rList
                    set end of radioTexts to (name of r as text) & " [selected=" & (value of r as text) & "]"
                end repeat
            end try
            
            set selectedBackup to ""
            try
                set selectedBackup to value of pop up button 1 of splitter group 1 of splitter group 1 of foundWin as text
            end try
            
            return winName & "|||" & (hasWelcome as text) & "|||" & (radioTexts as string) & "|||" & selectedBackup
        end tell
    end tell
    '''.replace("__DEVICE_WINDOW_MATCH__", match)
    try:
        out = subprocess.check_output(["osascript", "-e", script], text=True).strip()
        if out == "NO_WINDOW":
            return {"found": False, "reason": "No iPhone window in Finder"}
        parts = out.split("|||")
        win_name = parts[0] if len(parts) > 0 else ""
        has_welcome = (parts[1] == "true") if len(parts) > 1 else False
        radio_info = parts[2] if len(parts) > 2 else ""
        selected_backup = parts[3] if len(parts) > 3 else ""
        return {
            "found": True,
            "window_name": win_name,
            "has_welcome": has_welcome,
            "radio_info": radio_info,
            "selected_backup": selected_backup,
        }
    except Exception as e:
        return {"found": False, "error": str(e)}


def finder_window_titles() -> List[str]:
    """Titles of the Finder windows that are actually open right now."""
    try:
        out = subprocess.check_output(
            ["osascript", "-e", 'tell application "Finder" to get name of every Finder window'],
            text=True, timeout=10).strip()
    except Exception:  # noqa: BLE001
        return []
    return [t.strip() for t in out.split(", ")] if out else []


def select_device_in_finder(udid: Optional[str] = None) -> Dict[str, Any]:
    """Opens a Finder window and tries to open the phone from the sidebar (row found by name through
    the accessibility tree, no hard-coded coordinates). Opening the phone in Finder is what makes
    the Mac activate an unactivated phone. Never clicks Continue or any setup choice.

    Scripted clicks only highlight a sidebar row on some macOS versions, so success is verified
    (a Finder window must really be showing the phone). If not, returns a structured `handoff`
    with the row's live position for an agent (vision click) or the person.
    """
    names = list(dict.fromkeys(n for n in device_window_names(udid) + ["iPhone", "iPad"] if n))
    wanted = "{" + ", ".join(_applescript_str(n) for n in names) + "}"
    script = f'''
    set wanted to {wanted}
    tell application "Finder"
        activate
        if (count of Finder windows) is 0 then make new Finder window
    end tell
    delay 0.5
    tell application "System Events" to tell process "Finder"
        set sb to outline 1 of scroll area 1 of splitter group 1 of window 1
        repeat with r in rows of sb
            try
                set t to value of static text 1 of UI element 1 of r
                if wanted contains (t as text) then
                    set p to position of r
                    set sz to size of r
                    click at {{(item 1 of p) + 40, (item 2 of p) + ((item 2 of sz) / 2)}}
                    return "ROW|||" & (t as text) & "|||" & (item 1 of p) & "|||" & (item 2 of p) & "|||" & (item 1 of sz) & "|||" & (item 2 of sz)
                end if
            end try
        end repeat
        return "NOT_FOUND"
    end tell
    '''
    try:
        out = subprocess.check_output(["osascript", "-e", script], text=True, stderr=subprocess.STDOUT, timeout=30).strip()
    except Exception as e:  # noqa: BLE001
        return {"success": False, "reason": f"Finder scripting failed: {e}"}
    if not out.startswith("ROW"):
        return {"success": False, "reason": "The phone isn't listed in Finder's sidebar yet"}
    _, row, x, y, w, h = out.split("|||")
    if wait_until(lambda: any(t in names for t in finder_window_titles()), timeout=4.0, interval=0.5):
        return {"success": True, "row": row}
    try:
        cx, cy = int(float(x) + float(w) / 2), int(float(y) + float(h) / 2)
    except ValueError:
        cx = cy = None
    return {"success": False, "reason": "Finder highlighted the phone but didn't open it",
            "handoff": {"reason": "agent_handoff", "action": f"click the '{row}' row under Locations in the Finder sidebar",
                        "position_points": [cx, cy], "verify": "Finder shows the phone with 'Activating…' or a Welcome view"}}


def close_device_windows(udid: Optional[str] = None) -> int:
    """Closes Finder windows showing the phone (matched by the device's names). Used once the
    phone is activated, when Finder is no longer needed. Leaves the person's other windows alone."""
    names = list(dict.fromkeys(n for n in device_window_names(udid) + ["iPhone", "iPad"] if n))
    wanted = "{" + ", ".join(_applescript_str(n) for n in names) + "}"
    script = f'''
    set wanted to {wanted}
    set closed to 0
    tell application "Finder"
        repeat with w in (reverse of (get every Finder window))
            try
                if wanted contains (name of w as text) then
                    close w
                    set closed to closed + 1
                end if
            end try
        end repeat
    end tell
    return closed
    '''
    try:
        return int(subprocess.check_output(["osascript", "-e", script], text=True, timeout=15).strip() or 0)
    except Exception:  # noqa: BLE001
        return 0


def activate_via_finder(udid: str, timeout_sec: int = 180, verbose: bool = True) -> Dict[str, Any]:
    """Consented Finder step: the command-line tools can't activate a phone, but the Mac does it
    itself once the phone is opened in Finder. Waits for ActivationState == Activated."""
    def state() -> str:
        return query_device_info(udid).get("ActivationState", "")

    if state() == "Activated":
        close_device_windows(udid)
        return {"success": True, "already_activated": True}
    sel = select_device_in_finder(udid)
    if not sel.get("success"):
        return {"success": False, "reason": sel.get("reason"), "handoff": sel.get("handoff"),
                "message": "Finder needs one click to start activating the phone: click the iPhone under "
                           "Locations in Finder's sidebar (I can do it for you). The Mac then activates it by itself."}
    if verbose:
        print("[*] Opened the phone in Finder; the Mac is activating it (about a minute)...")
    ok = wait_until(lambda: state() == "Activated", timeout=timeout_sec, interval=3.0)
    if ok:
        time.sleep(1.5)  # let Finder finish painting its Welcome view, then it is no longer needed
        close_device_windows(udid)
    return {"success": bool(ok), "message": "The phone is activated." if ok else
            "The phone is still activating. Make sure the Mac is online and give it a few more minutes."}


def stage_finder_restore_selection(
    expected_backup_name: Optional[str] = None,
    require_welcome: bool = True,
    udid: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Selects 'Restore from this backup' in macOS Finder and clicks Continue — but only on a
    device sitting in the post-wipe Welcome/Set-Up view, and only when the backup chosen in
    the pop-up matches the expected device.

    SAFETY:
      - Refuses unless Finder shows the Welcome / Set Up / Restore view (require_welcome).
        The old fallback that clicked 'Restore Backup…' in the Summary view is removed: on
        a device that was NOT wiped, that overwrites live data.
      - Aborts on a backup/device-name mismatch so we never restore the wrong backup.
      - On any missing element it returns a structured 'agent handoff' result instead of
        clicking something unverified.
    """
    match = device_window_match(udid)
    view = detect_finder_setup_view(udid)
    if not view.get("found"):
        return {"success": False, "reason": "no_window",
                "handoff": "No Finder iPhone/iPad window found. Bring the device window forward and retry."}

    if require_welcome and not view.get("has_welcome"):
        return {"success": False, "reason": "not_welcome_view",
                "handoff": "Finder is not showing the Welcome/Set Up/Restore view. The device may not be "
                           "wiped; refusing to click to avoid overwriting live data."}

    selected = (view.get("selected_backup") or "").strip()
    if expected_backup_name and selected and expected_backup_name.lower() not in selected.lower():
        return {"success": False, "reason": "backup_mismatch", "selected_backup": selected,
                "expected": expected_backup_name,
                "handoff": f"Selected backup '{selected}' does not match expected device "
                           f"'{expected_backup_name}'. Aborting; pick the correct backup manually."}

    # Welcome view confirmed and backup matches: select the restore radio and Continue only.
    script = '''
    tell application "System Events"
        tell process "Finder"
            set winList to every window
            set foundWin to missing value
            repeat with w in winList
                set wName to name of w
                if __DEVICE_WINDOW_MATCH__ then
                    set foundWin to w
                    exit repeat
                end if
            end repeat
            if foundWin is missing value then
                return "AGENT_HANDOFF: no device window"
            end if
            try
                set targetRadio to radio button "Restore from this backup:" of splitter group 1 of splitter group 1 of foundWin
                click targetRadio
            on error
                try
                    set targetRadio to radio button "Restore from this backup" of scroll area 1 of group 1 of group 1 of splitter group 1 of splitter group 1 of foundWin
                    click targetRadio
                end try
            end try
            set curBackup to ""
            try
                set curBackup to value of pop up button 1 of splitter group 1 of splitter group 1 of foundWin as text
            end try
            try
                set btnContinue to button "Continue" of splitter group 1 of splitter group 1 of foundWin
                click btnContinue
                return "CLICKED_CONTINUE: " & curBackup
            on error
                return "AGENT_HANDOFF: Continue button not found for backup " & curBackup
            end try
        end tell
    end tell
    '''.replace("__DEVICE_WINDOW_MATCH__", match)
    try:
        res = subprocess.check_output(["osascript", "-e", script], text=True).strip()
    except Exception as e:
        return {"success": False, "reason": "osascript_error", "error": str(e),
                "handoff": "AppleScript could not drive Finder; an agent should click "
                           "'Restore from this backup' then Continue."}
    if res.startswith("AGENT_HANDOFF"):
        return {"success": False, "reason": "agent_handoff", "result": res, "handoff": res}
    return {"success": True, "result": res, "selected_backup": selected}


def _finder_pid() -> Optional[str]:
    """PID of the running Finder process, or None."""
    try:
        res = subprocess.run(["pgrep", "-x", "Finder"], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    pids = res.stdout.split()
    return pids[0] if res.returncode == 0 and pids else None


def restart_finder_services(timeout_sec: float = 10.0, verbose: bool = True) -> bool:
    """
    Auto-healing utility for stuck Finder / AMPDevicesAgent UI.
    Terminates Finder and the Apple Mobile Device agent to force clean respawn, then waits
    (up to timeout_sec) until launchd has brought a *new* Finder process back.
    """
    if verbose:
        print("[*] Auto-Healing: Restarting Finder and AMPDevicesAgent services...")
    old_pid = _finder_pid()
    try:
        subprocess.run(["killall", "-HUP", "Finder", "AMPDevicesAgent"], capture_output=True, text=True)
    except Exception as e:
        if verbose:
            print(f"[!] Warning during process termination: {e}")
    new_pid = wait_until(lambda: (p := _finder_pid()) and p != old_pid and p, timeout_sec, 0.25)
    if verbose and not new_pid:
        print(f"[!] Finder did not respawn within {timeout_sec:.0f}s; activating it explicitly.")
    try:
        subprocess.run(["osascript", "-e", 'tell application "Finder" to activate'], capture_output=True, text=True)
        if verbose:
            print("[+] Finder restarted and reactivated successfully.")
        return True
    except Exception as e:
        if verbose:
            print(f"[!] Failed to reactivate Finder: {e}")
        return False


class RestorerSafetyError(Exception):
    """Raised when hardware safety invariants or preconditions fail during automated restoration."""
    pass


def auto_confirm_finder_restore_sheet(
    expected_udid: Optional[str] = None,
    max_backup_age_hours: float = 2.0,
    verbose: bool = True
) -> Dict[str, Any]:
    """
    Safely confirms and clicks 'Restore' on Finder's backup password sheet if and only if:
      1. Strictly ONE iOS device is connected to USB (preventing cross-device ambiguity).
      2. The connected device UDID matches expected_udid (if specified).
      3. No recent disconnect/reconnect flapping occurred (connection stability verified).
      4. A verified, encrypted backup for this device completed within max_backup_age_hours.
    """
    # 1. Single phone invariant
    connected = get_connected_udids()
    if len(connected) == 0:
        raise RestorerSafetyError("Safety halt: No iOS devices connected to USB.")
    if len(connected) > 1:
        raise RestorerSafetyError(
            f"Safety halt: Multiple iOS devices connected ({connected}). Aborting to prevent cross-device restore ambiguity."
        )

    actual_udid = connected[0]
    target_udid = expected_udid or os.environ.get("TARGET_UDID")
    if target_udid and actual_udid != target_udid:
        raise RestorerSafetyError(
            f"Safety halt: Connected device ({actual_udid}) does not match expected target ({target_udid})."
        )

    # 2. Connection stability check (verify no flapping in last few seconds)
    info1 = query_device_info(actual_udid)
    time.sleep(1.0)
    info2 = query_device_info(actual_udid)
    if not info1 or not info2:
        raise RestorerSafetyError("Safety halt: USB connection is unstable or device is still booting.")

    # 3. Freshness & Encryption check on backup
    b_path = backup_root() / actual_udid
    status_plist = b_path / "Status.plist"
    manifest_plist = b_path / "Manifest.plist"
    info_plist = b_path / "Info.plist"

    if not status_plist.exists() or not manifest_plist.exists() or not info_plist.exists():
        raise RestorerSafetyError(f"Safety halt: No verified backup structure found at {b_path}.")

    # 3b. The immutable pre-wipe safety clone must exist before we confirm a restore.
    prewipe = b_path.parent / f"{actual_udid}-prewipe"
    if not prewipe.is_dir():
        raise RestorerSafetyError(
            f"Safety halt: pre-wipe safety clone '{prewipe.name}' not found. "
            "Run the backup/revive step that creates it before auto-confirming a restore."
        )

    with open(status_plist, "rb") as f:
        st = plistlib.load(f)
    if st.get("SnapshotState") != "finished":
        raise RestorerSafetyError(f"Safety halt: Backup snapshot state is '{st.get('SnapshotState')}', not 'finished'.")

    with open(manifest_plist, "rb") as f:
        mf = plistlib.load(f)
    if not mf.get("IsEncrypted"):
        raise RestorerSafetyError("Safety halt: Backup is not encrypted (Keychain/2FA tokens missing).")

    with open(info_plist, "rb") as f:
        inf = plistlib.load(f)
    last_b_date = inf.get("Last Backup Date")
    age_hours = 0.0
    if last_b_date:
        from datetime import datetime, timezone
        if last_b_date.tzinfo is None:
            last_b_date = last_b_date.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        age_hours = (now - last_b_date).total_seconds() / 3600.0
        if age_hours > max_backup_age_hours:
            raise RestorerSafetyError(
                f"Safety halt: Backup is too old ({age_hours:.1f} hours > max {max_backup_age_hours} hours)."
            )

    if verbose:
        print(f"[+] Safety Invariants Verified: Exactly 1 device ({actual_udid}), recent encrypted backup ({age_hours:.2f}h old), stable USB link.")

    # 4. Trigger AppleScript on Finder sheet
    match = device_window_match(actual_udid)
    script = '''
    tell application "System Events"
        tell process "Finder"
            set winList to every window
            set targetSheet to missing value
            repeat with w in winList
                set wName to name of w
                if __DEVICE_WINDOW_MATCH__ then
                    try
                        set sList to every sheet of w
                        if (count of sList) > 0 then
                            set targetSheet to sheet 1 of w
                            exit repeat
                        end if
                    end try
                end if
            end repeat
            
            if targetSheet is missing value then
                return "NO_SHEET"
            end if
            
            try
                set btnRestore to button "Restore" of targetSheet
                click btnRestore
                return "CLICKED_RESTORE"
            on error err
                return "ERROR: " & err
            end try
        end tell
    end tell
    '''.replace("__DEVICE_WINDOW_MATCH__", match)
    res = subprocess.check_output(["osascript", "-e", script], text=True).strip()
    return {
        "success": (res == "CLICKED_RESTORE"),
        "result": res,
        "udid": actual_udid,
        "backup_age_hours": round(age_hours, 2),
    }


def monitor_restore_progress(
    expected_udid: str,
    timeout_sec: int = 2400,
    poll_interval: float = 4.0,
    verbose: bool = True
) -> Dict[str, Any]:
    """
    Monitors Finder sync/restore status text and device reboot cycle until complete.
    """
    if verbose:
        print("[*] Monitoring Finder restoration progress...")

    start = time.time()
    last_status = ""
    reboot_detected = False
    # Resolved once: the device drops off USB mid-restore, but its backup's Info.plist still
    # names the window it will come back as.
    match = device_window_match(expected_udid)

    while time.time() - start < timeout_sec:
        # Check Finder status text
        script = '''
        tell application "System Events"
            tell process "Finder"
                set winList to every window
                repeat with w in winList
                    set wName to name of w
                    if __DEVICE_WINDOW_MATCH__ then
                        try
                            set tList to value of every static text of splitter group 1 of splitter group 1 of w
                            try
                                set tList2 to value of every static text of group 1 of splitter group 1 of splitter group 1 of w
                                set tList to tList & tList2
                            end try
                            return tList as string
                        end try
                    end if
                end repeat
                return ""
            end tell
        end tell
        '''.replace("__DEVICE_WINDOW_MATCH__", match)
        try:
            status_text = subprocess.check_output(["osascript", "-e", script], text=True).strip()
            if status_text and status_text != last_status:
                if verbose:
                    print(f"[*] Finder Status: {status_text}")
                last_status = status_text

            if "Restoring" in status_text:
                pass
            elif "Syncing" in status_text and "Step" in status_text:
                pass
            elif reboot_detected and ("Available" in status_text or status_text == ""):
                # Finished reboot and back to normal
                if verbose:
                    print("[+] Device completed restore and post-restore reboot!")
                return {"status": "complete", "last_status": status_text}

        except Exception:
            pass

        # Also watch USB connection for the post-restore reboot
        udids = get_connected_udids()
        if expected_udid not in udids and not reboot_detected:
            reboot_detected = True
            if verbose:
                print("[*] Device disconnected for post-restore reboot...")

        time.sleep(poll_interval)

    return {"status": "timeout", "last_status": last_status}


def audit_post_restore_storage(udid: str, verbose: bool = True) -> Dict[str, Any]:
    """
    Queries connected device storage post-restore and computes delta.
    """
    info = query_device_info(udid)
    if not info:
        return {"status": "error", "message": "Device not reachable via ideviceinfo"}

    # Storage figures live in the disk_usage domain; without -q they come back as 0.
    disk = query_device_info(udid, "-q", "com.apple.disk_usage")

    def _int(d: Dict[str, str], key: str) -> int:
        try:
            return int(d.get(key, 0) or 0)
        except ValueError:
            return 0

    total_disk = _int(disk, "TotalDiskCapacity")
    user_cap = _int(disk, "TotalDataCapacity")
    user_avail = _int(disk, "TotalDataAvailable")
    user_used = user_cap - user_avail if (user_cap and user_avail) else 0

    to_gb = lambda b: round(b / 1e9, 2)

    data = {
        "device_name": info.get("DeviceName", "iPhone"),
        "product_type": info.get("ProductType", ""),
        "total_disk_gb": to_gb(total_disk),
        "user_partition_gb": to_gb(user_cap),
        "data_used_gb": to_gb(user_used),
        "data_available_gb": to_gb(user_avail),
    }

    if verbose:
        print("\n" + "=" * 60)
        print("         POST-RESTORE STORAGE AUDIT")
        print("=" * 60)
        print(f"  Device Name:      {data['device_name']}")
        print(f"  User Partition:   {data['user_partition_gb']} GB")
        print(f"  Data in Use:      {data['data_used_gb']} GB")
        print(f"  Data Available:   {data['data_available_gb']} GB")
        print("=" * 60)

    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="iPhone Revival Restorer CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    default_udid = os.environ.get("TARGET_UDID")

    # Subcommand: wait
    p_wait = subparsers.add_parser("wait", help="Wait for reset iPhone to return on USB")
    p_wait.add_argument("--udid", default=default_udid, help="Target device UDID")
    p_wait.add_argument("--timeout", type=int, default=600, help="Timeout in seconds")
    p_wait.add_argument("--expect-reset", action="store_true", help="First wait for currently-connected device to disconnect, then wait for reboot")

    # Subcommand: stage-finder
    p_stg = subparsers.add_parser("stage-finder", help="Detect & stage Finder restore UI")
    p_stg.add_argument("--auto-confirm", action="store_true", help="Auto-confirm restore password sheet with safety invariants")
    p_stg.add_argument("--udid", default=default_udid, help="Target device UDID")

    # Subcommand: confirm-sheet
    p_cnf = subparsers.add_parser("confirm-sheet", help="Safely auto-confirm Finder restore password sheet")
    p_cnf.add_argument("--udid", default=default_udid, help="Target device UDID")
    p_cnf.add_argument("--max-age-hours", type=float, default=2.0, help="Maximum allowed backup age in hours")

    # Subcommand: monitor-restore
    p_mon = subparsers.add_parser("monitor-restore", help="Monitor restore progress")
    p_mon.add_argument("--udid", default=default_udid, help="Target device UDID")

    # Subcommand: audit
    p_audit = subparsers.add_parser("audit", help="Audit post-restore device storage")
    p_audit.add_argument("--udid", default=default_udid, help="Target device UDID")

    # Subcommand: heal-finder
    # Subcommand: activate (consented Finder step that makes the Mac activate an unactivated phone)
    p_act = subparsers.add_parser("activate", help="Open the phone in Finder so the Mac activates it (needs the person's OK)")
    p_act.add_argument("--udid", default=default_udid, help="Target device UDID")
    p_act.add_argument("--timeout", type=int, default=180, help="Seconds to wait for activation")

    p_bk = subparsers.add_parser("backups", help="List every backup of this phone on this Mac with its stats "
                                                  "(when taken, app count, 2FA apps, Keychain, size). Read-only.")
    p_bk.add_argument("--udid", default=default_udid, help="Target device UDID")

    # Subcommand: ui-restore (THE restore: deterministic Finder UI driving, agent handoff on trouble)
    subparsers.add_parser("ui-restore", help="Restore by driving Finder deterministically (hands off to the agent when stuck). "
                                             "Same flags as finder_restore.py; run it with --help", add_help=False)

    p_heal = subparsers.add_parser("heal-finder", help="Auto-heal stuck Finder / AMPDevicesAgent UI by restarting services")
    p_heal.add_argument("--timeout", type=float, default=10.0, help="Max seconds to wait for Finder to respawn")

    if len(sys.argv) > 1 and sys.argv[1] == "ui-restore":
        import finder_restore
        return finder_restore.main(sys.argv[2:])

    args = parser.parse_args()

    if args.command == "wait":
        try:
            wait_for_device_return(args.udid, timeout_sec=args.timeout, expect_reset=args.expect_reset)
            return 0
        except TimeoutError as e:
            print(f"[!] {e}")
            return 1
    elif args.command == "backups":
        import restore_gates
        u = args.udid or (get_connected_udids() or [None])[0]
        if not u:
            print("[!] No phone connected.")
            return 2
        lines = restore_gates.describe_backups(restore_gates.available_backups(u))
        print(f"BACKUPS_AVAILABLE: {sum(1 for l in lines if not l.startswith(' '))} backup(s) of this phone on this Mac.")
        for ln in lines:
            print("  - " + ln if not ln.startswith(" ") else "    " + ln.strip())
        return 0
    elif args.command == "activate":
        u = args.udid or (get_connected_udids() or [None])[0]
        if not u:
            print("[!] No phone connected.")
            return 2
        res = activate_via_finder(u, timeout_sec=args.timeout)
        print(res.get("message") or res)
        return 0 if res.get("success") else 1
    elif args.command == "stage-finder":
        ui_info = detect_finder_setup_view()
        print(f"Finder Setup View: {ui_info}")
        res = stage_finder_restore_selection()
        print(f"Stage Result: {res}")
        if not res.get("success"):
            return 1
        if args.auto_confirm:
            time.sleep(1.0)
            try:
                c_res = auto_confirm_finder_restore_sheet(expected_udid=args.udid)
                print(f"Auto-Confirm Result: {c_res}")
            except RestorerSafetyError as err:
                print(f"[!] Auto-confirm aborted for safety: {err}")
                return 1
        return 0
    elif args.command == "confirm-sheet":
        try:
            c_res = auto_confirm_finder_restore_sheet(expected_udid=args.udid, max_backup_age_hours=args.max_age_hours)
            print(f"Auto-Confirm Result: {c_res}")
            return 0
        except RestorerSafetyError as err:
            print(f"[!] Safety check rejected restore confirmation: {err}")
            return 1
    elif args.command == "monitor-restore":
        monitor_restore_progress(args.udid)
        return 0
    elif args.command == "audit":
        audit_post_restore_storage(args.udid)
        return 0
    elif args.command == "heal-finder":
        success = restart_finder_services(timeout_sec=args.timeout)
        return 0 if success else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
