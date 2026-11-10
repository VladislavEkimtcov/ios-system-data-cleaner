"""
iPhone Revival Restorer & Setup State Automation (`restorer.py`).

Provides automated detection and restoration orchestration for iPhones resetting
back into setup mode ('Hello' screen):
  1. USB Reconnection & Setup State Detector (`wait_for_device_return`)
  2. Finder activation of an erased phone (`activate_via_finder`)
  3. The restore itself lives in `finder_restore.py` (`restorer.py ui-restore`): a deterministic
     state machine over Finder's UI that hands off to the agent when a step goes wrong.
  4. Post-Restore Verification & Storage Delta Tracker (`audit_post_restore_storage`)
"""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from common import backup_root, connected_udids, find_tool, wait_until


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
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", timeout=10)
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




def finder_window_titles() -> List[str]:
    """Titles of the Finder windows that are actually open right now."""
    try:
        out = subprocess.check_output(
            ["osascript", "-e", 'tell application "Finder" to get name of every Finder window'],
            text=True, encoding="utf-8", errors="replace", timeout=10).strip()
    except Exception:  # noqa: BLE001
        return []
    return [t.strip() for t in out.split(", ")] if out else []


def real_click(x: float, y: float, timeout: float = 15.0) -> bool:
    """A genuine mouse click at screen point (x, y), posted as a hardware-level event. Needed because System
    Events' scripted `click` only highlights Finder's sidebar row and opens a pop-up in a way a script can't
    read afterwards (both seen live 2026-11-10). A real click does both. macOS only; needs the same
    Accessibility permission the rest of the Finder scripting already needs."""
    if sys.platform != "darwin":
        return False
    jxa = (
        "ObjC.import('CoreGraphics'); ObjC.import('Foundation');"
        f"var p = $.CGPointMake({float(x)}, {float(y)});"
        "function ev(t){ return $.CGEventCreateMouseEvent(null, t, p, $.kCGMouseButtonLeft); }"
        "$.CGEventPost($.kCGHIDEventTap, $.CGEventCreateMouseEvent(null, $.kCGEventMouseMoved, p, $.kCGMouseButtonLeft));"
        "delay(0.15);"
        "$.CGEventPost($.kCGHIDEventTap, ev($.kCGEventLeftMouseDown)); delay(0.08);"
        "$.CGEventPost($.kCGHIDEventTap, ev($.kCGEventLeftMouseUp));"
    )
    try:
        subprocess.run(["osascript", "-l", "JavaScript", "-e", jxa], capture_output=True, timeout=timeout, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def select_device_in_finder(udid: Optional[str] = None) -> Dict[str, Any]:
    """Opens a Finder window and tries to open the phone from the sidebar (row found by name through
    the accessibility tree, no hard-coded coordinates). Opening the phone in Finder is what makes
    the Mac activate an unactivated phone. Never clicks Continue or any setup choice.

    Scripted clicks only highlight a sidebar row on some macOS versions, so success is verified
    (a Finder window must really be showing the phone). If not, returns a structured `handoff`
    with the row's live position for an agent (vision click) or the person.
    """
    names = list(dict.fromkeys(n for n in device_window_names(udid) + ["iPhone", "iPad"] if n))
    # Finder's sidebar row can carry a trailing space ("Персик "): match both spellings
    wanted = "{" + ", ".join(_applescript_str(n) for n in names + [n + " " for n in names]) + "}"
    script = f'''
    set wanted to {wanted}
    tell application "Finder"
        activate
        make new Finder window   -- a fresh window: its sidebar layout is predictable, the person's own windows stay as they are
    end tell
    delay 0.7
    tell application "System Events" to tell process "Finder"
        set sb to outline 1 of scroll area 1 of splitter group 1 of window 1
        repeat with r in rows of sb
            try
                set t to value of static text 1 of UI element 1 of r
                if wanted contains (t as text) then
                    set p to position of r
                    set sz to size of r
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
    try:
        cx, cy = int(float(x) + min(float(w) / 2, 60)), int(float(y) + float(h) / 2)
    except ValueError:
        cx = cy = None
    if cx is not None and real_click(cx, cy) and \
            wait_until(lambda: any(t in names for t in finder_window_titles()), timeout=6.0, interval=0.5):
        return {"success": True, "row": row}
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

    # Subcommand: audit
    p_audit = subparsers.add_parser("audit", help="Audit post-restore device storage")
    p_audit.add_argument("--udid", default=default_udid, help="Target device UDID")

    # Subcommand: activate (consented Finder step that makes the Mac activate an unactivated phone)
    p_act = subparsers.add_parser("activate", help="Open the phone in Finder so the Mac activates it (needs the person's OK)")
    p_act.add_argument("--udid", default=default_udid, help="Target device UDID")
    p_act.add_argument("--timeout", type=int, default=180, help="Seconds to wait for activation")

    p_bk = subparsers.add_parser("backups", help="List every backup of this phone on this Mac with its stats (when taken, "
                                                  "app count, 2FA apps, Keychain, size) and propose the next clean-up move.")
    p_bk.add_argument("--udid", default=default_udid, help="Target device UDID")
    p_bk.add_argument("--trash", metavar="FOLDER", help="Move this one backup to the Trash (only after the person said yes)")

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
        import backup_manager
        u = args.udid or (get_connected_udids() or [None])[0]
        if not u:
            print("[!] No phone connected.")
            return 2
        return backup_manager.main(["--udid", u] + (["--trash", args.trash] if args.trash else []))
    elif args.command == "activate":
        u = args.udid or (get_connected_udids() or [None])[0]
        if not u:
            print("[!] No phone connected.")
            return 2
        res = activate_via_finder(u, timeout_sec=args.timeout)
        print(res.get("message") or res)
        return 0 if res.get("success") else 1
    elif args.command == "audit":
        audit_post_restore_storage(args.udid)
        return 0
    elif args.command == "heal-finder":
        success = restart_finder_services(timeout_sec=args.timeout)
        return 0 if success else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
