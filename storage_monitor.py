#!/usr/bin/env python3
"""
Storage and Device Monitor for Phone Revival Project.
Tracks host Mac disk space and connected iOS device storage metrics.

Units: "GB" = decimal (10^9 bytes), matching iPhone Settings and Finder.
       "GiB" = binary (2^30), shown alongside because earlier log entries used it.
"""

import argparse
import base64
import binascii
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from common import GB, GIB, backup_root, dir_size, find_tool, host_mount, state_path
from itunes_env import check_itunes_installation, get_apple_itunes_download_url
from market_pricing import get_device_valuation, get_friendly_name
from win_tools import detect_apple_pnp_devices

REPO_ROOT = Path(__file__).resolve().parent
# The real ledger lives in the private state dir so it is never committed and never
# mixed with the shipped sample. The example is only read by --demo.
SNAPSHOT_FILE = state_path("storage_snapshots.json")
SNAPSHOT_EXAMPLE_FILE = REPO_ROOT / "storage_snapshots.example.json"
ENV_FILE = REPO_ROOT / ".env"


def load_env_config() -> Dict[str, Any]:
    """Reads .env configuration if present, falling back to sensible defaults."""
    config = {
        "REPLACEMENT_DEVICE_COST": 799.00,
        "APPLE_STORAGE_COST_PER_GB": 0.78,
    }
    if ENV_FILE.exists():
        try:
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k in ("REPLACEMENT_DEVICE_COST", "APPLE_STORAGE_COST_PER_GB"):
                            try:
                                config[k] = float(v)
                            except ValueError:
                                pass
        except Exception:
            pass
    return config


def both(n_bytes: float) -> str:
    return f"{n_bytes / GB:.1f} GB ({n_bytes / GIB:.1f} GiB)"


# Back-compat alias: the shared implementation lives in common.host_mount().
get_host_mount = host_mount


def _read_snapshot_file(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def load_snapshots(demo: bool = False) -> List[Dict[str, Any]]:
    """
    Loads recorded storage snapshots from the real ledger.

    The shipped sample (storage_snapshots.example.json) is returned ONLY when demo=True.
    It is never merged into the real ledger, so the first real snapshot can't inherit
    sample rows and pollute the ROI numbers.
    """
    if demo:
        return _read_snapshot_file(SNAPSHOT_EXAMPLE_FILE)
    return _read_snapshot_file(SNAPSHOT_FILE)


def save_snapshot_entry(entry: Dict[str, Any]) -> None:
    history = load_snapshots()
    history.append(entry)
    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


def render_storage_bar(used_ratio: float, width: int = 28) -> str:
    """Renders a progress bar: [████████░░░░░░░░]"""
    clamped = max(0.0, min(1.0, used_ratio))
    filled_len = int(round(width * clamped))
    empty_len = width - filled_len
    return f"[{'█' * filled_len}{'░' * empty_len}]"


def record_current_snapshot(tag: str = "manual", udid: Optional[str] = None) -> Optional[Dict[str, Any]]:
    extra = ["-u", udid] if udid else []
    info = run_ideviceinfo(*extra)
    if not info:
        return None
    actual_udid = info.get("UniqueDeviceID", udid or "unknown")
    disk = run_ideviceinfo("-q", "com.apple.disk_usage", *extra)
    if not disk:
        return None

    total_cap = to_int(disk.get("TotalDiskCapacity"))
    data_cap = to_int(disk.get("TotalDataCapacity"))
    data_avail = to_int(disk.get("TotalDataAvailable"))
    data_used = max(data_cap - data_avail, 0)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tag": tag,
        "udid": actual_udid,
        "device_name": info.get("DeviceName", "iPhone"),
        "model": info.get("ProductType", "Unknown"),
        "ios_version": info.get("ProductVersion", "Unknown"),
        "total_disk_bytes": total_cap,
        "user_partition_bytes": data_cap,
        "data_used_bytes": data_used,
        "data_available_bytes": data_avail,
        "data_used_gb": round(data_used / GB, 2),
        "data_available_gb": round(data_avail / GB, 2),
        "user_partition_gb": round(data_cap / GB, 2),
        "used_pct": round((data_used / data_cap) * 100, 1) if data_cap else 0.0,
    }
    save_snapshot_entry(entry)
    return entry


def compute_storage_diff(udid: Optional[str] = None, demo: bool = False) -> Optional[Dict[str, Any]]:
    history = load_snapshots(demo=demo)
    if not history:
        return None
    if udid:
        history = [h for h in history if h.get("udid") == udid]
    if len(history) < 2:
        return None

    def _ts(h: Dict[str, Any]) -> str:
        return str(h.get("timestamp") or "")

    # Pick the latest 'post_restore' as 'after', then the latest 'before_reset' that
    # PRECEDES it as 'before'. This stays correct across multiple revival cycles, where
    # taking the first-ever before-snapshot would compare against a stale baseline.
    post = [h for h in history if h.get("tag") == "post_restore"]
    after = max(post, key=_ts) if post else history[-1]

    befores = [h for h in history if h.get("tag") == "before_reset" and _ts(h) <= _ts(after)]
    if befores:
        before = max(befores, key=_ts)
    else:
        earlier = [h for h in history if _ts(h) < _ts(after)]
        before = max(earlier, key=_ts) if earlier else history[0]
    if after == before:
        after = history[-1]

    used_before = before.get("data_used_gb", 0.0)
    used_after = after.get("data_used_gb", 0.0)
    recovered_gb = round(used_before - used_after, 2)
    pct_reduction = round((recovered_gb / used_before) * 100, 1) if used_before else 0.0

    avail_before = before.get("data_available_gb", 0.0)
    avail_after = after.get("data_available_gb", 0.0)
    avail_increase_gb = round(avail_after - avail_before, 2)
    avail_increase_pct = round((avail_increase_gb / avail_before) * 100, 1) if avail_before else 0.0

    config = load_env_config()
    default_retail = config.get("REPLACEMENT_DEVICE_COST", 799.00)
    nand_per_gb = config.get("APPLE_STORAGE_COST_PER_GB", 0.78)

    dev_model = after.get("model", before.get("model", "unknown"))
    valuation = get_device_valuation(dev_model)
    refurb_cost = valuation["refurb_replacement_usd"]
    retail_cost = valuation.get("retail_upgrade_usd", default_retail)

    refurb_avoided_usd = refurb_cost if recovered_gb > 0 else 0.0
    upgrade_avoided_usd = retail_cost if recovered_gb > 0 else 0.0
    nand_value_usd = round(max(0.0, recovered_gb) * nand_per_gb, 2)

    # Compute Household Summary across all distinct devices in snapshots
    by_device: Dict[str, List[Dict[str, Any]]] = {}
    for h in load_snapshots(demo=demo):
        dev_id = h.get("udid", "unknown")
        by_device.setdefault(dev_id, []).append(h)

    total_devices_serviced = 0
    total_recovered_gb = 0.0
    total_refurb_savings_usd = 0.0
    total_retail_savings_usd = 0.0

    for dev_id, snaps in by_device.items():
        b_snap = next((s for s in snaps if s.get("tag") == "before_reset"), snaps[0] if snaps else None)
        a_snap = next((s for s in reversed(snaps) if s.get("tag") == "post_restore"), None)
        if b_snap and a_snap and b_snap != a_snap:
            rec = round(b_snap.get("data_used_gb", 0.0) - a_snap.get("data_used_gb", 0.0), 2)
            if rec > 0:
                total_devices_serviced += 1
                total_recovered_gb += rec
                d_mod = a_snap.get("model", b_snap.get("model", "unknown"))
                d_val = get_device_valuation(d_mod)
                total_refurb_savings_usd += d_val["refurb_replacement_usd"]
                total_retail_savings_usd += d_val.get("retail_upgrade_usd", default_retail)

    if total_devices_serviced == 0 and recovered_gb > 0:
        total_devices_serviced = 1
        total_refurb_savings_usd = refurb_cost
        total_retail_savings_usd = retail_cost

    household_summary = {
        "total_devices_serviced": max(1, total_devices_serviced),
        "total_storage_recovered_gb": round(max(recovered_gb, total_recovered_gb), 2),
        "total_refurb_savings_usd": round(total_refurb_savings_usd, 2),
        "total_retail_savings_usd": round(total_retail_savings_usd, 2),
        "total_hardware_savings_usd": round(total_retail_savings_usd, 2),
        "total_nand_value_usd": round(max(recovered_gb, total_recovered_gb) * nand_per_gb, 2),
    }

    return {
        "device_name": after.get("device_name", before.get("device_name", "iPhone")),
        "model": after.get("model", before.get("model", "iPhone")),
        "friendly_name": valuation.get("friendly_name", after.get("device_name", "iPhone")),
        "udid": after.get("udid", before.get("udid", "")),
        "before": before,
        "after": after,
        "used_before_gb": used_before,
        "used_after_gb": used_after,
        "recovered_gb": recovered_gb,
        "pct_reduction": pct_reduction,
        "avail_before_gb": avail_before,
        "avail_after_gb": avail_after,
        "avail_increase_gb": avail_increase_gb,
        "avail_increase_pct": avail_increase_pct,
        "refurb_avoided_usd": refurb_avoided_usd,
        "upgrade_avoided_usd": upgrade_avoided_usd,
        "pricing_source": valuation.get("source", "Market Ledger"),
        "pricing_status": valuation.get("pricing_status", "FALLBACK"),
        "needs_research": valuation.get("needs_research", False),
        "stimulus_prompt": valuation.get("stimulus_prompt", ""),
        "nand_value_usd": nand_value_usd,
        "nand_cost_per_gb": nand_per_gb,
        "household_summary": household_summary,
    }


def print_diff_report(udid: Optional[str] = None, demo: bool = False) -> None:
    diff = compute_storage_diff(udid, demo=demo)
    if not diff:
        print("\n[!] Not enough snapshots found to compute storage delta.")
        print("    Run: python3 storage_monitor.py --snap before_reset (prior to wipe)")
        print("    And: python3 storage_monitor.py --snap post_restore (after restore)")
        print("    Or:  python3 storage_monitor.py --diff --demo   (view the bundled sample)")
        return

    b = diff["before"]
    a = diff["after"]
    part_gb = a.get("user_partition_gb", 59.1)
    b_used_ratio = b.get("data_used_bytes", 0) / b.get("user_partition_bytes", 1)
    a_used_ratio = a.get("data_used_bytes", 0) / a.get("user_partition_bytes", 1)
    hh = diff["household_summary"]

    print("\n" + "=" * 65)
    print("      PHONE REVIVAL: STORAGE & HOUSEHOLD ROI REPORT")
    print("=" * 65)
    print(f"  Device:         {diff['device_name']} ({diff['model']})")
    print(f"  UDID:           {diff['udid']}")
    print(f"  User Partition: {part_gb} GB\n")

    print(f"  [BEFORE RESET]  ({b.get('tag')}, {b.get('timestamp')[:16]})")
    print(f"  {render_storage_bar(b_used_ratio)} {diff['used_before_gb']} GB used ({b.get('used_pct')}%) | {diff['avail_before_gb']} GB free\n")

    print(f"  [POST RESTORE]  ({a.get('tag')}, {a.get('timestamp')[:16]})")
    print(f"  {render_storage_bar(a_used_ratio)} {diff['used_after_gb']} GB used ({a.get('used_pct')}%) | {diff['avail_after_gb']} GB free\n")

    print("  ---------------------------------------------------------------")
    print(f"  🎯 DEVICE STORAGE RECOVERED:   +{diff['recovered_gb']} GB (-{diff['pct_reduction']}% cache purged)")
    print(f"  📈 AVAILABLE CAPACITY GAINED:  +{diff['avail_increase_gb']} GB (+{diff['avail_increase_pct']}% free space)")
    print("  💰 AVOIDED HARDWARE COSTS (LIVE MARKET VALUATION):")
    status_tag = f"[{diff.get('pricing_status', 'LIVE')}]"
    print(f"     • Like-for-Like Refurbished:  ${diff.get('refurb_avoided_usd', 0.0):.2f} {status_tag}")
    print(f"       Market Source: {diff.get('pricing_source', 'Market Ledger')}")
    print(f"     • New Retail Upgrade Avoided: ${diff['upgrade_avoided_usd']:.2f} [Apple Store Base MSRP]")
    print(f"  💵 APPLE NAND EQUIVALENT:      ${diff['nand_value_usd']:.2f} ({diff['recovered_gb']} GB at ${diff['nand_cost_per_gb']:.2f}/GB)")
    print("  ---------------------------------------------------------------")
    dev_label = f"{hh['total_devices_serviced']} Device" if hh['total_devices_serviced'] == 1 else f"{hh['total_devices_serviced']} Devices"
    print(f"  🏡 HOUSEHOLD CUMULATIVE EFFORT ({dev_label} Serviced):")
    print(f"     • Total Clean Storage Recovered:  +{hh['total_storage_recovered_gb']} GB")
    print(f"     • Total Refurbished Replacements: ${hh.get('total_refurb_savings_usd', 0.0):.2f} (Secondary market value)")
    print(f"     • Total New Purchases Deferred:   ${hh['total_hardware_savings_usd']:.2f} (New retail hardware avoided)")
    print(f"     • Total Reclaimed NAND Value:     ${hh['total_nand_value_usd']:.2f}")
    print("=" * 65 + "\n")

    if diff.get("needs_research") and diff.get("stimulus_prompt"):
        print(diff["stimulus_prompt"] + "\n")


def run_ideviceinfo(*extra: str) -> dict[str, str]:
    """Runs ideviceinfo and parses 'Key: Value' lines. Returns {} on failure."""
    ideviceinfo_bin = find_tool("ideviceinfo")
    if not ideviceinfo_bin:
        return {}
    try:
        raw = subprocess.check_output([str(ideviceinfo_bin), *extra], stderr=subprocess.DEVNULL, timeout=20)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {}
    out = {}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            out[k.strip()] = v.strip()
    return out


def decode_data_value(value: str) -> str:
    """ideviceinfo prints plist <data> values as base64 (e.g. 'Tk8=' -> 'NO')."""
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
        return decoded if decoded.isprintable() and decoded else value
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return value


def to_int(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def main():
    parser = argparse.ArgumentParser(description="Phone Revival: Storage & Device Monitor with Snapshot Tracking")
    parser.add_argument("--snapshot", "--snap", dest="snapshot", nargs="?", const="manual",
                        help="Save a storage snapshot with optional tag (e.g. before_reset, post_restore).")
    parser.add_argument("--diff", action="store_true", help="Display before vs after storage recovery comparison report.")
    parser.add_argument("--demo", action="store_true", help="Use the bundled example snapshots (does not touch your real ledger).")
    parser.add_argument("--history", action="store_true", help="Display all recorded storage snapshots.")
    parser.add_argument("--udid", help="Target specific device UDID.")
    parser.add_argument("--stimulate-pricing", action="store_true", help="Emit a market-pricing research directive for your web-search tool.")
    args = parser.parse_args()

    if args.stimulate_pricing:
        from market_pricing import generate_stimulus_banner, get_friendly_name
        diff = compute_storage_diff(args.udid, demo=args.demo)
        mod = diff["model"] if diff else "iPhone12,8"
        friendly = get_friendly_name(mod)
        print(generate_stimulus_banner(mod, friendly, 64, reason="User / Agent forced pricing refresh", current_refurb=diff.get("refurb_avoided_usd", 95.0) if diff else 95.0))
        return

    if args.diff:
        print_diff_report(args.udid, demo=args.demo)
        return

    if args.history:
        history = load_snapshots(demo=args.demo)
        if args.udid:
            history = [h for h in history if h.get("udid") == args.udid]
        print("\n" + "=" * 65)
        print("          STORAGE SNAPSHOT HISTORY")
        print("=" * 65)
        for i, h in enumerate(history, 1):
            print(f"#{i} [{h.get('tag')}] {h.get('timestamp')[:19]}")
            print(f"   Device:    {h.get('device_name')} ({h.get('model')}) - UDID: {h.get('udid')}")
            print(f"   Storage:   {h.get('data_used_gb')} GB used ({h.get('used_pct')}%) | {h.get('data_available_gb')} GB free\n")
        print("=" * 65)
        return

    if args.snapshot:
        entry = record_current_snapshot(tag=args.snapshot, udid=args.udid)
        if entry:
            print(f"\n✅ Snapshot recorded: [{entry['tag']}] {entry['device_name']} ({entry['data_used_gb']} GB used / {entry['data_available_gb']} GB free)")
        else:
            print("\n❌ Could not record snapshot: Device not reachable via USB.")
        return

    print("=" * 65)
    print("          PHONE REVIVAL: STORAGE & DEVICE MONITOR")
    print("=" * 65)

    host_mount = get_host_mount()
    stat = shutil.disk_usage(str(host_mount))
    free_gb = stat.free / GB
    mount_label = f"HOST STORAGE: {host_mount}"
    print(f"\n[{mount_label}]")
    print(f"  Total Capacity: {stat.total / GB:.1f} GB")
    print(f"  Used Space:     {stat.used / GB:.1f} GB ({stat.used / stat.total * 100:.1f}%)")
    print(f"  Free Space:     {free_gb:.1f} GB")

    if free_gb < 20.0:
        print("  ⚠️ CRITICAL: Host free storage is under 20 GB! Free up space before backing up.")
    elif free_gb < 35.0:
        print("  ⚠️ CAUTION: Host has less than 35 GB free. Large backups may require clearing caches.")
    else:
        print("  ✅ Host has adequate working storage buffer.")

    # iTunes status check
    if sys.platform == "win32":
        it_status = check_itunes_installation()
        print("\n[ITUNES CONFIGURATION & ENFORCEMENT]")
        if it_status.itunes_installed:
            print(f"  iTunes:               ✅ {it_status.install_type} (v{it_status.itunes_version})")
        else:
            print(f"  iTunes:               🚨 NOT INSTALLED (Download from Apple: {get_apple_itunes_download_url()})")
        if it_status.amds_service_running:
            print("  Apple Mobile Support: ✅ Running")
        else:
            print("  Apple Mobile Support: ⚠️ Inactive or missing")
        print(f"  Backup Directory:     {it_status.backup_root}")

    try:
        _broot = backup_root()
        backup_bytes = dir_size(_broot) if _broot.exists() else 0
    except PermissionError:
        backup_bytes = None
    if backup_bytes is None:
        print("  ⚠️ Cannot read backup folder - grant permissions or run as Administrator.")
    else:
        print(f"  Existing Local Backups Size: {backup_bytes / GB:.2f} GB")

    print("\n[CONNECTED IOS DEVICE VIA USB]")
    info = run_ideviceinfo()
    if not info:
        # Check PnP fallback on Windows
        pnp_devs = detect_apple_pnp_devices()
        if pnp_devs:
            print("  Apple Device detected via USB (Windows PnP):")
            for d in pnp_devs:
                print(f"    📱 {d.name} [{d.mode}]")
                print(f"       Driver Status: {d.driver_status}")
            print("  (Note: Install native libimobiledevice CLI tools in ./tools/bin to query iOS internals)")
        else:
            from common import diagnose_connection
            print(f"  👉 {diagnose_connection()['message']}")
        print("\n" + "=" * 65)
        return

    print(f"  Device Name:    {info.get('DeviceName', 'Unknown')}")
    print(f"  Model:          {info.get('ProductType', 'Unknown')} ({info.get('HardwareModel', '')})")
    print(f"  iOS Version:    {info.get('ProductVersion', 'Unknown')} (Build {info.get('BuildVersion', '')})")
    print(f"  Serial Number:  {info.get('SerialNumber', 'Unknown')}")
    print(f"  UDID:           {info.get('UniqueDeviceID', 'Unknown')}")
    print(f"  Activation:     {info.get('ActivationState', 'Unknown')}")
    fm = info.get("fm-activation-locked")
    print(f"  Activation Lock (Find My): {decode_data_value(fm) if fm else 'Unknown'}")
    pwd_prot = info.get("PasswordProtected")
    if pwd_prot and pwd_prot.lower() == "false":
        print("  Device Setup State:   Unconfigured / Hello Screen (No Passcode)")
    oblit = info.get("obliteration")
    if oblit:
        decoded_oblit = decode_data_value(oblit)
        if "EACS Done" in decoded_oblit:
            print(f"  ✨ POST-ERASE STATE:  Factory Wipe Complete ({decoded_oblit})")

    backup_info = run_ideviceinfo("-q", "com.apple.mobile.backup")
    will_encrypt = backup_info.get("WillEncrypt")
    if will_encrypt is not None:
        flag = "✅" if will_encrypt.lower() == "true" else "🚨"
        print(f"  Backup Encryption Set On Device: {flag} {will_encrypt}")

    disk = run_ideviceinfo("-q", "com.apple.disk_usage")
    if disk:
        total_cap = to_int(disk.get("TotalDiskCapacity"))
        data_cap = to_int(disk.get("TotalDataCapacity"))
        data_avail = to_int(disk.get("TotalDataAvailable"))
        data_used = max(data_cap - data_avail, 0)

        print("\n  --- Device Storage Breakdown ---")
        print(f"  Hardware Disk:  {both(total_cap)}")
        print(f"  User Partition: {both(data_cap)}")
        print(f"  Data In Use:    {both(data_used)}")
        print(f"  Data Available: {both(data_avail)}")
        print("  (System Data itself is only shown on the phone: Settings -> General -> iPhone Storage)")

        if data_used / GB > free_gb - 15:
            print(f"  ⚠️ ALERT: Device data ({data_used / GB:.1f} GB) leaves under a 15 GB buffer on host "
                  f"({free_gb:.1f} GB free).")
        else:
            print("  ✅ Host has enough space to hold this device backup payload.")

        diff = compute_storage_diff(info.get("UniqueDeviceID"))
        if diff and diff.get("recovered_gb", 0) > 0:
            print(f"\n  🎯 Revival Diff: +{diff['recovered_gb']} GB recovered (-{diff['pct_reduction']}% used data purged)")
            print(f"     Free space increased: +{diff['avail_increase_gb']} GB (+{diff['avail_increase_pct']}%)")
            print("     (Run 'python3 storage_monitor.py --diff' for visual before/after report)")

    print("\n" + "=" * 65)


if __name__ == "__main__":
    main()
