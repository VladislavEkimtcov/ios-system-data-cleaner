"""
Apple iOS Device Management & Revival MCP Server (`ios_mcp_server.py`).

Exposes Model Context Protocol (MCP) tools for iOS storage reclamation: backup
verification, keybag/token audits, host storage headroom, storage ROI, MVT security-scan
readiness, the installed-app catalog, Xcode Simulator pairing, a payload compactor, and
human-gated iTunes backup automation.

Every tool is read-only or stops at a human gate: none erases a device or restores a
backup on its own.

Note on IPSW firmwares: for Apple IPSW downloads / TSS queries, use the standalone
ipsw-mcp-server project instead.

Sample MCP client configuration:
```json
{
  "mcpServers": {
    "ios-device-cleaner": {
      "command": "python3",
      "args": ["/path/to/ios-system-data-cleaner/ios_mcp_server.py"]
    }
  }
}
```

Usage:
    python3 ios_mcp_server.py              # run the MCP server (FastMCP if installed, else stdio)
    python3 ios_mcp_server.py --selftest   # offline schema + smoke self-test
"""

from __future__ import annotations

import argparse
import contextlib
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import typing
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from backup_verifier import find_backup_for_target, inspect_backup, list_available_backups
from common import backup_root, connected_udids, dir_size, find_tool, host_mount, GB
from token_saver import TokenCompactor, TokenDoctor, compute_token_metric, estimate_tokens
from win_tools import detect_apple_pnp_devices


# ---------------------------------------------------------------------------
# USB / backup discovery
# ---------------------------------------------------------------------------

def detect_connected_apple_devices() -> Dict[str, Any]:
    """
    Inspect the host for USB-connected iPhones/iPads via libimobiledevice (idevice_id,
    ideviceinfo), Windows PnP (VID 05AC), or Apple Configurator (cfgutil), returning each
    device's UDID, ProductType, and OS build. Read-only.
    """
    detected: List[Dict[str, Any]] = []
    idevice_id_bin = find_tool("idevice_id")
    ideviceinfo_bin = find_tool("ideviceinfo")
    if idevice_id_bin:
        try:
            proc = subprocess.run([str(idevice_id_bin), "-l"], capture_output=True, text=True, timeout=5, check=False)
            for udid in [l.strip() for l in proc.stdout.splitlines() if l.strip()]:
                info: Dict[str, Any] = {"udid": udid, "source": "libimobiledevice"}
                if ideviceinfo_bin:
                    ip = subprocess.run([str(ideviceinfo_bin), "-u", udid], capture_output=True, text=True, timeout=5, check=False)
                    for line in ip.stdout.splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            if k.strip() in ("ProductType", "ProductVersion", "BuildVersion", "HardwareModel", "DeviceName"):
                                info[k.strip()] = v.strip()
                detected.append(info)
        except Exception as exc:
            return {"connected_devices": detected, "error": str(exc)}

    if not detected and sys.platform == "win32":
        for d in detect_apple_pnp_devices():
            detected.append({"name": d.name, "mode": d.mode, "source": "windows_pnp",
                             "driver_status": d.driver_status, "instance_id": d.instance_id})

    cfgutil_bin = shutil.which("cfgutil")
    if cfgutil_bin and not detected:
        try:
            proc = subprocess.run([cfgutil_bin, "--format", "JSON", "list"], capture_output=True, text=True, timeout=5, check=False)
            if proc.returncode == 0 and proc.stdout.strip():
                detected.append({"source": "cfgutil", "output": json.loads(proc.stdout)})
        except Exception:
            pass

    return {
        "connected_count": len(detected),
        "connected_devices": detected,
        "available_host_tools": {
            "idevice_id": bool(idevice_id_bin),
            "ideviceinfo": bool(ideviceinfo_bin),
            "idevicerestore": bool(find_tool("idevicerestore")),
            "cfgutil": bool(cfgutil_bin),
        },
    }


def list_local_backups(include_archives: bool = False, compact: bool = True) -> Dict[str, Any]:
    """
    List local iOS backups (device name, model, iOS version, build, encryption, size).
    Read-only.
    """
    backups = list_available_backups(include_archives=include_archives)
    data = [b.to_dict() for b in backups]
    if compact:
        data = [{
            "folder_name": b["folder_name"], "device_name": b["device_name"],
            "product_type": b["product_type"], "ios_version": b["product_version"],
            "build_version": b["build_version"], "is_encrypted": b["is_encrypted"],
            "size_gb": b["size_gb"], "last_backup": b["last_backup_date"],
        } for b in data]
    return {"backup_count": len(backups), "backups": data}


# ---------------------------------------------------------------------------
# Verifier / tokens / storage / ROI / security (read-only)
# ---------------------------------------------------------------------------

def verify_backup(target: Optional[str] = None, max_age_hours: float = 24.0) -> Dict[str, Any]:
    """
    Run the pre-erase verification gate on a backup (freshness, SnapshotState, Manifest.db,
    encryption, size/file-count). Returns the pass/fail verdict and the full report text.
    Does NOT erase anything. The proof-of-password step is non-interactive here.

    Args:
        target: Backup UDID or folder path (defaults to the most recent active backup).
        max_age_hours: Fail backups older than this.
    """
    backup = find_backup_for_target(target)
    if not backup:
        return {"error": "No matching backup found."}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        passed = inspect_backup(Path(backup.backup_path), max_age_hours=max_age_hours, interactive=False)
    return {"verdict": "PASS" if passed else "FAIL", "passed": passed,
            "backup_path": backup.backup_path, "report": buf.getvalue()}


def audit_backup_tokens(target: Optional[str] = None) -> Dict[str, Any]:
    """
    Audit the credential posture of a backup: keybag health, Keychain presence, lockdown
    escrow, and any readable 2FA accounts. Sensitive values are reported as fingerprints
    only (no salts, MACs, host IDs, or secrets). Read-only.

    Args:
        target: Backup UDID or folder path (defaults to the most recent backup).
    """
    from token_extractor import extract_all_tokens
    manifest = extract_all_tokens(backup_target=target)
    return TokenCompactor.compact_json(manifest.to_dict(), max_list_items=25)


def get_host_storage_status() -> Dict[str, Any]:
    """
    Report host free/used storage on the backup volume and the total size of existing local
    backups, to budget headroom before a backup. Read-only.
    """
    mount = host_mount()
    try:
        usage = shutil.disk_usage(str(mount))
    except OSError as e:
        return {"error": f"Cannot read host storage: {e}"}
    root = backup_root()
    backup_bytes = dir_size(root) if root.exists() else 0
    return {
        "host_mount": str(mount),
        "total_gb": round(usage.total / GB, 2),
        "used_gb": round(usage.used / GB, 2),
        "free_gb": round(usage.free / GB, 2),
        "existing_backups_gb": round((backup_bytes or 0) / GB, 2),
        "adequate_buffer": usage.free / GB >= 20.0,
    }


def get_storage_roi_report(udid: Optional[str] = None, demo: bool = False) -> Dict[str, Any]:
    """
    Compute the before/after storage-recovery and ROI report from recorded snapshots.
    Read-only.

    Args:
        udid: Restrict to one device's snapshots.
        demo: Use the bundled example snapshots instead of your real ledger.
    """
    import storage_monitor
    diff = storage_monitor.compute_storage_diff(udid, demo=demo)
    if not diff:
        return {"status": "insufficient_snapshots",
                "message": "Need a before_reset and a post_restore snapshot. Try demo=true to see a sample."}
    return TokenCompactor.compact_json(diff, max_list_items=10)


def run_security_scan(target: Optional[str] = None) -> Dict[str, Any]:
    """
    Report readiness of the MVT (Mobile Verification Toolkit) security scan for a backup:
    whether mvt-ios is installed and whether indicator files are present, plus the exact
    command to run the full scan. It does NOT run the decrypt/scan itself (that writes a
    working copy and must be launched deliberately), so this tool stays read-only.

    Args:
        target: Backup UDID or folder path (defaults to the most recent backup).
    """
    import run_mvt_inspection as mvt
    backup = find_backup_for_target(target)
    mvt_bin = mvt.find_mvt_ios()
    ioc_files = mvt.ioc_file_count()
    ready = bool(mvt_bin) and ioc_files > 0
    return {
        "mvt_ios_installed": bool(mvt_bin),
        "mvt_ios_path": mvt_bin or None,
        "indicator_files_available": ioc_files,
        "scan_ready": ready,
        "target_backup": backup.folder_name if backup else None,
        "run_command": (
            f"python3 run_mvt_inspection.py --udid {backup.folder_name}" if backup
            else "python3 run_mvt_inspection.py --udid <UDID>"
        ),
        "note": "This readiness check is read-only. Run the command above to perform the actual "
                "decrypt + indicator scan (it writes a temporary working copy).",
    }


def compact_payload(payload: str, max_list_items: int = 10, max_str_len: int = 300) -> Dict[str, Any]:
    """
    Compact a JSON (or text) payload with the Token-Saver strategy and report the token
    savings. Operates ONLY on the provided payload argument — it never executes commands.

    Args:
        payload: A JSON string, or any text, to compact.
        max_list_items: Truncate lists longer than this.
        max_str_len: Truncate strings longer than this.
    """
    try:
        parsed = json.loads(payload)
        compacted = TokenCompactor.compact_json(parsed, max_list_items=max_list_items, max_str_len=max_str_len)
        compact_text = json.dumps(compacted)
        kind = "json"
    except json.JSONDecodeError:
        compact_text = TokenCompactor.filter_cli_output(payload, max_lines=max_list_items * 4)
        compacted = compact_text
        kind = "text"
    metric = compute_token_metric(payload, compact_text)
    return {"kind": kind, "compacted": compacted, "audit": TokenDoctor.audit(payload),
            "token_metric": metric.to_dict()}


# ---------------------------------------------------------------------------
# Xcode Simulator (local, non-destructive to the phone)
# ---------------------------------------------------------------------------

def list_xcode_simulators(only_available: bool = True, booted_only: bool = False, compact: bool = True) -> Dict[str, Any]:
    """List Xcode iOS Simulators via `xcrun simctl`. Read-only."""
    try:
        import simulator_bridge
        sims = simulator_bridge.list_simulators(only_available=only_available, booted_only=booted_only, compact=compact)
        return {"total_simulators": len(sims), "simulators": sims}
    except Exception as exc:
        return {"error": str(exc)}


def boot_xcode_simulator(device_target: Optional[str] = None, open_app: bool = True) -> Dict[str, Any]:
    """Boot an Xcode Simulator (and optionally open the Simulator app). Local only.

    Args:
        device_target: Simulator UDID or name (defaults to the first available iPhone).
        open_app: Open the macOS Simulator window.
    """
    try:
        import simulator_bridge
        return simulator_bridge.boot_simulator(target=device_target, open_app=open_app)
    except Exception as exc:
        return {"error": str(exc)}


def send_backup_to_simulator(backup_target: Optional[str] = None, simulator_target: Optional[str] = None,
                             max_media: int = 25, open_gui: bool = True) -> Dict[str, Any]:
    """
    Copy media/appearance from a DECRYPTED local backup into an Xcode Simulator. Encrypted
    backups are refused with a clear message. Affects only the simulator, never the phone.

    Args:
        backup_target: UDID/folder/path of the backup (defaults to the active backup).
        simulator_target: Target simulator (defaults to an optimal iPhone simulator).
        max_media: Max photos/videos to import.
        open_gui: Open the Simulator window.
    """
    try:
        import simulator_bridge
        return simulator_bridge.send_backup_to_simulator(
            backup_target=backup_target, simulator_target=simulator_target,
            max_media_items=max_media, open_gui=open_gui)
    except Exception as exc:
        return {"error": str(exc)}


def list_backup_apps(backup_target: Optional[str] = None, genre_filter: Optional[str] = None,
                     search_query: Optional[str] = None, compact: bool = True) -> Dict[str, Any]:
    """
    List installed apps from a backup (names, versions, genres, App Store IDs). Read-only.

    Args:
        backup_target: Backup UDID or folder path.
        genre_filter: Optional genre (e.g. 'Finance').
        search_query: Optional case-insensitive name/bundle match.
        compact: Compress results to conserve tokens.
    """
    try:
        import backup_apps
        catalog = backup_apps.get_backup_apps_catalog(backup_target)
        if genre_filter:
            catalog = [a for a in catalog if a.genre.lower() == genre_filter.strip().lower()]
        if search_query:
            q = search_query.strip().lower()
            catalog = [a for a in catalog if q in a.display_name.lower() or q in a.bundle_id.lower()]
        data = [a.to_dict() for a in catalog]
        if compact:
            pruned = [{"name": a["display_name"], "bundle_id": a["bundle_id"], "version": a["version"],
                       "genre": a["genre"], "app_store_id": a["app_store_id"]} for a in data]
            return {"total_apps": len(catalog), "apps": TokenCompactor.compact_json(pruned, max_list_items=15)}
        return {"total_apps": len(catalog), "apps": data}
    except Exception as exc:
        return {"error": str(exc)}


def open_app_store_in_simulator(app_query: str, simulator_target: Optional[str] = None,
                                backup_target: Optional[str] = None) -> Dict[str, Any]:
    """
    Open an installed app's App Store page in Simulator Safari. Local only.

    Args:
        app_query: App display name or bundle ID.
        simulator_target: Simulator UDID or name.
        backup_target: Backup folder or UDID.
    """
    try:
        import backup_apps
        return backup_apps.open_app_store_in_simulator(
            app_query=app_query, simulator_udid=simulator_target, backup_target=backup_target)
    except Exception as exc:
        return {"error": str(exc)}


def drive_itunes_backup(udid: Optional[str] = None) -> Dict[str, Any]:
    """
    Drive iTunes on Windows to take a backup, monitoring until Status.plist reports
    'finished', then verify it. A backup is non-destructive and stops at the human
    passcode/encryption gate.

    Args:
        udid: Device UDID (defaults to the single connected device).
    """
    if sys.platform != "win32":
        return {"error": "iTunes desktop automation is only available on Windows."}
    target_udid = udid or (connected_udids()[0] if connected_udids() else None)
    if not target_udid:
        return {"error": "No connected device and no --udid provided."}
    from itunes_automation import ITunesAutomation
    return ITunesAutomation().drive_full_backup_workflow(target_udid)


# ---------------------------------------------------------------------------
# Tool registry (exactly 14; all read-only or human-gated)
# ---------------------------------------------------------------------------

ALL_TOOLS: List[Callable[..., Any]] = [
    detect_connected_apple_devices,
    list_local_backups,
    verify_backup,
    audit_backup_tokens,
    get_host_storage_status,
    get_storage_roi_report,
    run_security_scan,
    compact_payload,
    list_xcode_simulators,
    boot_xcode_simulator,
    send_backup_to_simulator,
    list_backup_apps,
    open_app_store_in_simulator,
    drive_itunes_backup,
]


def _create_fastmcp_server() -> Optional[Any]:
    """Initializes FastMCP from `mcp.server.fastmcp` or `fastmcp` if available."""
    FastMCPClass = None
    try:
        from mcp.server.fastmcp import FastMCP as _MCPFastMCP  # type: ignore
        FastMCPClass = _MCPFastMCP
    except ImportError:
        try:
            from fastmcp import FastMCP as _StandaloneFastMCP  # type: ignore
            FastMCPClass = _StandaloneFastMCP
        except ImportError:
            return None
    mcp = FastMCPClass("ios-system-data-cleaner")
    for fn in ALL_TOOLS:
        mcp.tool()(fn)
    return mcp


# ---------------------------------------------------------------------------
# Zero-dependency JSON-RPC 2.0 stdio fallback
# ---------------------------------------------------------------------------

def _python_type_to_json_schema(py_type: Any) -> Dict[str, Any]:
    origin = typing.get_origin(py_type)
    if origin is not None:
        args = [a for a in typing.get_args(py_type) if a is not type(None)]
        if args:
            return _python_type_to_json_schema(args[0])
    if py_type is int:
        return {"type": "integer"}
    if py_type is bool:
        return {"type": "boolean"}
    if py_type is float:
        return {"type": "number"}
    return {"type": "string"}


def _build_tool_schema(fn: Callable[..., Any]) -> Dict[str, Any]:
    sig = inspect.signature(fn)
    doc = inspect.getdoc(fn) or ""
    # With `from __future__ import annotations`, raw annotations are strings. Resolve them
    # to real types so Optional[int] is advertised as integer, not string.
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}
    properties: Dict[str, Any] = {}
    required: List[str] = []
    for name, param in sig.parameters.items():
        annotation = hints.get(name, param.annotation)
        prop_schema = _python_type_to_json_schema(annotation)
        if param.default is not inspect.Parameter.empty and param.default is not None:
            prop_schema["default"] = param.default
        properties[name] = prop_schema
        if param.default is inspect.Parameter.empty:
            required.append(name)
    return {"name": fn.__name__, "description": doc,
            "inputSchema": {"type": "object", "properties": properties, "required": required}}


def _run_stdio_fallback_server() -> None:
    """JSON-RPC 2.0 MCP server over stdin/stdout, no external deps."""
    tool_map = {fn.__name__: fn for fn in ALL_TOOLS}
    tool_schemas = [_build_tool_schema(fn) for fn in ALL_TOOLS]

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}
        if msg_id is None:
            continue

        if method == "initialize":
            response = {"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "ios-system-data-cleaner", "version": "2.0.0"}}}
        elif method == "ping":
            response = {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        elif method == "tools/list":
            response = {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tool_schemas}}
        elif method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments") or {}
            if tool_name not in tool_map:
                response = {"jsonrpc": "2.0", "id": msg_id,
                            "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}}
            else:
                try:
                    # Tools (and the code they call) may print; capture that so it can never
                    # corrupt the JSON-RPC stream on stdout. Redirect to stderr instead.
                    with contextlib.redirect_stdout(sys.stderr):
                        result_obj = tool_map[tool_name](**tool_args)
                    is_err = isinstance(result_obj, dict) and "error" in result_obj
                    response = {"jsonrpc": "2.0", "id": msg_id, "result": {
                        "content": [{"type": "text", "text": json.dumps(result_obj, indent=2)}],
                        "isError": is_err}}
                except Exception as exc:
                    response = {"jsonrpc": "2.0", "id": msg_id, "result": {
                        "content": [{"type": "text", "text": json.dumps({"error": str(exc)}, indent=2)}],
                        "isError": True}}
        else:
            response = {"jsonrpc": "2.0", "id": msg_id,
                        "error": {"code": -32601, "message": f"Method not found: {method}"}}

        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


def _run_selftest() -> int:
    """Verifies the 14-tool registry, schemas, and a few read-only tools against a fixture."""
    import tempfile
    schemas = [_build_tool_schema(fn) for fn in ALL_TOOLS]
    assert len(schemas) == 14, f"Expected 14 tools, found {len(schemas)}"
    names = {s["name"] for s in schemas}
    for required in ("verify_backup", "audit_backup_tokens", "get_host_storage_status",
                     "get_storage_roi_report", "run_security_scan", "compact_payload"):
        assert required in names, f"missing new tool {required}"
    for dropped in ("audit_token_efficiency", "monitor_itunes_backup_progress",
                    "clone_backup_to_simulator", "shutdown_xcode_simulator", "open_simulator_url"):
        assert dropped not in names, f"dropped tool still present: {dropped}"

    temp_dir = None
    import backup_verifier
    if not backup_verifier.BACKUP_ROOT.exists() or not list(backup_verifier.BACKUP_ROOT.glob("*/Info.plist")):
        temp_dir = tempfile.TemporaryDirectory()
        from tests.fixtures import create_synthetic_backup
        create_synthetic_backup(Path(temp_dir.name))
        os.environ["IPHONE_BACKUP_DIR"] = temp_dir.name
        backup_verifier.BACKUP_ROOT = Path(temp_dir.name)

    try:
        backups_res = list_local_backups(compact=True)
        assert "backups" in backups_res
        storage = get_host_storage_status()
        assert "free_gb" in storage or "error" in storage
        comp = compact_payload(json.dumps({"a": None, "b": list(range(50))}), max_list_items=5)
        assert comp["token_metric"]["raw_tokens_est"] >= comp["token_metric"]["compact_tokens_est"]
        tokens = audit_backup_tokens()
        assert isinstance(tokens, dict)
    finally:
        if temp_dir:
            temp_dir.cleanup()
            os.environ.pop("IPHONE_BACKUP_DIR", None)

    print(json.dumps({
        "selftest": "passed",
        "registered_tools_count": len(schemas),
        "registered_tools": sorted(names),
        "sample_backups_found": backups_res.get("backup_count", 0),
        "fastmcp_available": _create_fastmcp_server() is not None,
    }, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Apple iOS Device Management & Revival MCP Server")
    parser.add_argument("--selftest", action="store_true", help="Run offline schema/verification self-test and exit.")
    parser.add_argument("--stdio-fallback", action="store_true", help="Force built-in JSON-RPC 2.0 stdio transport.")
    args = parser.parse_args()

    if args.selftest:
        return _run_selftest()

    mcp_instance = None if args.stdio_fallback else _create_fastmcp_server()
    if mcp_instance is not None:
        mcp_instance.run()
    else:
        _run_stdio_fallback_server()
    return 0


if __name__ == "__main__":
    sys.exit(main())
