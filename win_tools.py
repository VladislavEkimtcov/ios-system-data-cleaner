"""
Native CLI Tools Discovery & Smoke-Tester for Windows (`win_tools.py`).

Discovers and verifies libimobiledevice executables (`ideviceinfo.exe`, `idevice_id.exe`,
`idevicerestore.exe`, `idevicebackup2.exe`), tests daemon / driver communication,
and provides Windows PnP USB fallback detection for Apple devices (VID 05AC).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from common import LOCAL_TOOLS_BIN, REPO_ROOT, find_tool  # noqa: F401 (shared, re-exported)

# Standard Apple USB Product IDs (PIDs)
APPLE_USB_MODES = {
    "1281": "Recovery Mode (iBoot)",
    "1227": "DFU Mode (BootROM)",
    "12a8": "Normal Mode (iOS)",
    "12aa": "Normal Mode (iPad)",
}


@dataclass
class ToolStatus:
    name: str
    installed: bool
    path: Optional[str]
    version: Optional[str]
    smoke_test_pass: bool
    details: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ConnectedUSBDevice:
    name: str
    mode: str
    instance_id: str
    hardware_id: str
    driver_status: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def detect_apple_pnp_devices() -> List[ConnectedUSBDevice]:
    """
    Queries Windows PnP subsystem for physically connected Apple devices (VID 05AC).
    Detects Normal, Recovery, and DFU modes directly from USB descriptors.
    """
    if sys.platform != "win32":
        return []

    cmd = (
        'Get-PnpDevice -PresentOnly | Where-Object { '
        '$_.FriendlyName -match "Apple|iPhone|iPad" -or $_.HardwareID -match "05AC" '
        '} | Select-Object FriendlyName, InstanceId, Status, HardwareID | ConvertTo-Json'
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []

        import json
        raw = json.loads(proc.stdout)
        if isinstance(raw, dict):
            raw = [raw]

        devices: List[ConnectedUSBDevice] = []
        for item in raw:
            name = item.get("FriendlyName") or "Apple Mobile Device"
            inst = item.get("InstanceId") or ""
            status = item.get("Status") or "OK"
            hw_ids = item.get("HardwareID") or []
            hw_str = " ".join(hw_ids) if isinstance(hw_ids, list) else str(hw_ids)

            # Extract PID from HardwareID e.g. USB\VID_05AC&PID_12A8
            mode = "Normal Mode (iOS)"
            m = re.search(r"VID_05AC&PID_([0-9a-fA-F]{4})", hw_str)
            if m:
                pid = m.group(1).lower()
                mode = APPLE_USB_MODES.get(pid, f"Apple USB Device (PID {pid})")

            devices.append(
                ConnectedUSBDevice(
                    name=name,
                    mode=mode,
                    instance_id=inst,
                    hardware_id=hw_str,
                    driver_status=status,
                )
            )
        return devices
    except Exception:
        return []


def smoke_test_tool(tool_name: str) -> ToolStatus:
    """Probes a specific native CLI tool for execution, DLL integrity, and help output."""
    tool_path = find_tool(tool_name)
    if not tool_path:
        return ToolStatus(
            name=tool_name,
            installed=False,
            path=None,
            version=None,
            smoke_test_pass=False,
            details="Binary not found in PATH or ./tools/bin/.",
        )

    # Smoke test execution
    try:
        proc = subprocess.run(
            [str(tool_path), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        out = (proc.stdout + " " + proc.stderr).strip()
        if proc.returncode == 0 or "version" in out.lower() or "libimobiledevice" in out.lower():
            ver = out.splitlines()[0] if out else "OK"
            return ToolStatus(
                name=tool_name,
                installed=True,
                path=str(tool_path),
                version=ver[:60],
                smoke_test_pass=True,
                details="Smoke test passed (--version returned successfully).",
            )
    except Exception:
        pass

    # Try --help
    try:
        proc = subprocess.run(
            [str(tool_path), "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        out = (proc.stdout + " " + proc.stderr).strip()
        if proc.returncode in (0, 1) and ("usage" in out.lower() or "options" in out.lower()):
            return ToolStatus(
                name=tool_name,
                installed=True,
                path=str(tool_path),
                version="Available (responds to --help)",
                smoke_test_pass=True,
                details="Smoke test passed (--help verified execution).",
            )
    except Exception as exc:
        return ToolStatus(
            name=tool_name,
            installed=True,
            path=str(tool_path),
            version=None,
            smoke_test_pass=False,
            details=f"Execution failed (possible missing DLLs): {exc}",
        )

    return ToolStatus(
        name=tool_name,
        installed=True,
        path=str(tool_path),
        version=None,
        smoke_test_pass=False,
        details="Tool responded with unexpected status or empty output.",
    )


def smoke_test_all_tools() -> Dict[str, Any]:
    """Runs a full smoke test across all libimobiledevice tools and connected USB devices."""
    core_tools = ["ideviceinfo", "idevice_id", "idevicebackup2"]
    optional_tools = ["idevicerestore", "irecovery"]

    results: List[ToolStatus] = []
    for t in core_tools:
        results.append(smoke_test_tool(t))

    for t in optional_tools:
        st = smoke_test_tool(t)
        if not st.installed and sys.platform == "win32":
            st.details = "Optional on Windows (Firmware flashing natively supported via iTunes Shift-click Restore)."
        results.append(st)

    core_results = results[: len(core_tools)]
    pnp_devices = detect_apple_pnp_devices()

    # Try idevice_id -l to check usbmuxd communication
    usbmux_reachable = False
    connected_udids: List[str] = []
    idevice_id_path = find_tool("idevice_id")
    if idevice_id_path:
        try:
            proc = subprocess.run(
                [str(idevice_id_path), "-l"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            if proc.returncode == 0:
                usbmux_reachable = True
                connected_udids = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        except Exception:
            pass

    return {
        "tools": [r.to_dict() for r in results],
        "core_tools_installed": all(r.installed for r in core_results),
        "core_smoke_tests_passed": all(r.smoke_test_pass for r in core_results),
        "all_tools_installed": all(r.installed for r in results),
        "all_smoke_tests_passed": all(r.smoke_test_pass for r in results),
        "usbmux_reachable": usbmux_reachable,
        "connected_udids_via_cli": connected_udids,
        "connected_pnp_devices": [d.to_dict() for d in pnp_devices],
        "local_bin_directory": str(LOCAL_TOOLS_BIN),
    }


def print_smoke_test_report(
    install: bool = False,
    yes: bool = False,
    interactive: bool = True,
) -> None:
    """Prints a formatted report of the native CLI tools smoke test and offers installation."""
    print("=" * 70)
    print("       PHONE REVIVAL: NATIVE CLI TOOLS & USB SMOKE TEST")
    print("=" * 70)

    report = smoke_test_all_tools()

    print("\n[NATIVE CLI TOOLS (libimobiledevice)]")
    for t in report["tools"]:
        icon = "✅" if t["smoke_test_pass"] else ("⚠️" if t["installed"] else "❌")
        print(f"  {icon} {t['name']:<16} : {t['details']}")
        if t["path"]:
            print(f"     Path: {t['path']}")

    print("\n[USB & DAEMON SUBSYSTEM]")
    if report["usbmux_reachable"]:
        print("  ✅ Apple usbmuxd / AMDS daemon communication is ACTIVE")
    else:
        print("  ⚠️ Apple usbmuxd daemon not responding via CLI (requires Apple Mobile Device Support)")

    print("\n[PHYSICALLY DETECTED APPLE USB DEVICES]")
    pnp = report["connected_pnp_devices"]
    if pnp:
        for d in pnp:
            print(f"  📱 {d['name']}")
            print(f"     State:  {d['mode']}")
            print(f"     Driver: {d['driver_status']}")
            print(f"     ID:     {d['instance_id']}")
    else:
        print("  No Apple USB devices currently detected via Windows PnP.")

    udids = report["connected_udids_via_cli"]
    if udids:
        print(f"\n  Active Device UDIDs (via libimobiledevice): {', '.join(udids)}")

    if report["core_tools_installed"]:
        print("\n" + "-" * 70)
        print("  ✅ Core diagnostic & backup CLI suite is installed and operational!")
        print(f"  Location: {report['local_bin_directory']}")
    else:
        print("\n" + "-" * 70)
        from dep_installer import install_cli_tools, prompt_yes_no
        should_install = install or yes
        if not should_install and interactive and sys.stdin.isatty():
            should_install = prompt_yes_no("  -> Download and install native libimobiledevice suite into ./tools/bin?", default=False)
        if should_install:
            res = install_cli_tools()
            print(f"\n  Installation outcome: {res.get('message')}")
        else:
            print("  TIP: Run 'python iphone_manager.py tools --install' to auto-download")
            print(f"  or drop Win32 libimobiledevice binaries into: {report['local_bin_directory']}")
    print("=" * 70)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Native CLI Tools Discovery & Smoke-Tester.")
    parser.add_argument("--install", action="store_true", help="Download and install CLI tools into ./tools/bin/.")
    parser.add_argument("--yes", "-y", action="store_true", help="Non-interactive mode.")
    parser.add_argument("--no-prompt", action="store_true", help="Do not prompt interactively.")
    args = parser.parse_args()
    print_smoke_test_report(install=args.install, yes=args.yes, interactive=not args.no_prompt)
