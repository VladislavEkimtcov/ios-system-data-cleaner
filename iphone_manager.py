"""
iPhone Revival & Device Management CLI (`iphone_manager.py`).

Unified control plane for the iPhone Revival Project. Combines:
  1. Host & USB Device Diagnostics (`storage_monitor.py`)
  2. Backup Verification & Pre-erase Gate (`backup_verifier.py`)
  3. Security & Forensic Deep Inspection (`backup_deep_inspect.py`, `run_mvt_inspection.py`)
  4. Token-Saver Output Optimization (`token_saver.py`)
  5. Credential Survival & Token Extraction (`token_extractor.py`)
  6. Automated Recovery & Restorer (`restorer.py`)

Usage Examples:
  # Check overall host & device health:
  python3 iphone_manager.py status

  # Verify connected phone's backup:
  python3 iphone_manager.py verify --connected

  # Run full automated revival pipeline:
  python3 iphone_manager.py revive

  # Run Token-Saver diagnostic on reports:
  python3 iphone_manager.py token-doctor audit README.md
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

from backup_verifier import find_backup_for_target
from token_saver import TokenCompactor, TokenDoctor, estimate_tokens, wrap_command

REPO_ROOT = Path(__file__).resolve().parent


def cmd_status(args: argparse.Namespace) -> int:
    """Displays host storage, connected device, and backup inventory."""
    print("Running Host and Device Storage Monitor...")
    res = subprocess.run([sys.executable, str(REPO_ROOT / "storage_monitor.py")])
    return res.returncode


def cmd_backup(args: argparse.Namespace) -> int:
    """Runs autonomous backup engine with live sensory feedback and verification."""
    from backup_runner import BackupRunner
    runner = BackupRunner(
        udid=args.udid,
        password=args.password,
        play_audio=not args.no_sound,
        show_notifications=not args.no_notify,
        verbose=True
    )
    success = runner.execute()
    return 0 if success else 1


def cmd_verify(args: argparse.Namespace) -> int:
    """Runs backup_verifier.py as the pre-erase gate."""
    cmd = [sys.executable, str(REPO_ROOT / "backup_verifier.py")]
    if args.connected:
        cmd.append("--connected")
    elif args.udid:
        cmd.extend(["--udid", args.udid])
    if args.max_age_hours is not None:
        cmd.extend(["--max-age-hours", str(args.max_age_hours)])
    if getattr(args, "password", None):
        cmd.extend(["--password", args.password])
    if getattr(args, "skip_password_check", False):
        cmd.append("--skip-password-check")
    if getattr(args, "steer_ui", False):
        cmd.append("--steer-ui")

    if args.compact:
        filtered, code, metric = wrap_command(cmd)
        print(filtered)
        print(f"\n[Token-Saver: Saved {metric.tokens_saved} tokens ({metric.savings_pct}%)]", file=sys.stderr)
        return code
    res = subprocess.run(cmd)
    return res.returncode


def cmd_passwords(args: argparse.Namespace) -> int:
    """Launches the iOS backup password hunter and attempt tracker."""
    cmd = [sys.executable, str(REPO_ROOT / "password_tracker.py")]
    if getattr(args, "udid", None):
        cmd.extend(["--udid", args.udid])
    if getattr(args, "cli", False):
        cmd.append("--cli")
    if getattr(args, "check", None):
        cmd.extend(["--check", args.check])
    if getattr(args, "history", False):
        cmd.append("--history")
    if getattr(args, "variants", None):
        cmd.extend(["--variants", args.variants])
    if getattr(args, "port", None):
        cmd.extend(["--port", str(args.port)])
    res = subprocess.run(cmd)
    return res.returncode


def cmd_security(args: argparse.Namespace) -> int:
    """Runs MVT spyware (Pegasus/Predator) scan and backup decryption verification."""
    cmd = [sys.executable, str(REPO_ROOT / "run_mvt_inspection.py")]

    # Resolve a concrete UDID: explicit flag, else the single connected device.
    udid = args.udid
    if not udid:
        from common import connected_udids
        devices = connected_udids()
        if len(devices) == 1:
            udid = devices[0]
    if udid:
        cmd.extend(["--udid", udid])
    if getattr(args, "skip_ioc_update", False):
        cmd.append("--skip-ioc-update")
    if getattr(args, "keep_work_copy", False):
        cmd.append("--keep-work-copy")

    # Auto-resolve the backup password from the Keychain for headless execution, but ONLY
    # for a specific device account. A None account could return another device's password.
    if "MVT_IOS_BACKUP_PASSWORD" not in os.environ and udid:
        try:
            from password_tracker import get_keychain_password
            pwd = get_keychain_password(account=udid)
            if pwd:
                os.environ["MVT_IOS_BACKUP_PASSWORD"] = pwd
        except Exception:
            pass

    if getattr(args, "compact", True):
        max_lines = getattr(args, "max_lines", 60)
        filtered, code, metric = wrap_command(cmd, max_lines=max_lines)
        print(filtered)
        print(f"\n[Token-Saver: Saved {metric.tokens_saved} tokens ({metric.savings_pct}%)]", file=sys.stderr)
        return code
    res = subprocess.run(cmd)
    return res.returncode


def cmd_token_doctor(args: argparse.Namespace) -> int:
    """Runs Token-Saver doctor audits or command output wrapping."""
    if args.audit_target:
        p = Path(args.audit_target).expanduser().resolve()
        if not p.exists():
            print(f"Error: Target path not found: {p}", file=sys.stderr)
            return 1
        content = p.read_text(errors="replace")
        report = TokenDoctor.audit(content)
        print(json.dumps(report, indent=2))
        return 0
    elif args.wrap_cmd:
        output, code, metric = wrap_command(args.wrap_cmd, max_lines=args.max_lines)
        print(output)
        print(f"\n[Token-Saver Proof: Saved {metric.tokens_saved} tokens ({metric.savings_pct}%)]", file=sys.stderr)
        return code
    else:
        print("Please provide --audit <file> or --wrap <cmd>.")
        return 1


def cmd_detect_wipe(args: argparse.Namespace) -> int:
    """Deterministically detects if the connected device is in post-erase state and ready for restore."""
    from itunes_automation import ITunesAutomation
    driver = ITunesAutomation()
    udid = getattr(args, "udid", None)
    status = driver.detect_device_wipe_state(udid)

    print("=" * 65)
    print("       POST-WIPE & RESTORE READINESS DETECTION REPORT")
    print("=" * 65)
    verdict_str = "✅ POST-WIPE CONFIRMED (Ready to Restore)" if status["is_wiped"] else "ℹ️  PRE-WIPE / NORMAL OPERATION"
    print(f"  Device Status:        {verdict_str}")
    print(f"  Connected Device:     {status['current_device_name'] or 'N/A'}")
    print(f"  Backup Device Target: {status['backup_device_name'] or 'N/A'}")
    print(f"  UDID:                 {status['udid'] or 'N/A'}")
    print(f"  Password Protected:   {'YES' if status['password_protected'] else 'not reported (unreliable; not proof of a Hello screen)'}")
    print(f"  Activation State:     {status['activation_state'] or 'N/A'}")
    if status.get("obliteration"):
        print(f"  NVRAM Obliteration:   {status['obliteration']}")
    print(f"  iTunes UI Screen:     {status['ui_state']}")

    print("\n  Detection Verdict Evidence:")
    for r in status["reasons"]:
        print(f"    • {r}")

    if status["is_wiped"]:
        print("\n  👉 RECOMMENDED ACTION:")
        print(f"     Run 'python iphone_manager.py itunes --restore' to actuate iTunes restore")
        print(f"     from backup '{status['backup_device_name']}'.")
    print("=" * 65)
    return 0 if status["is_wiped"] else 1


def cmd_itunes(args: argparse.Namespace) -> int:
    """Checks and enforces iTunes Windows environment, MobileSync paths, and drives UI automation."""
    if getattr(args, "detect_wipe", False):
        return cmd_detect_wipe(args)

    if getattr(args, "actuate", False):
        from itunes_automation import ITunesAutomation
        driver = ITunesAutomation()
        ok = driver.actuate_backup_ui()
        print(f"iTunes backup actuation: {'✅ Sent' if ok else '❌ Failed'}")
        return 0 if ok else 1

    if getattr(args, "restore", False):
        from itunes_automation import ITunesAutomation
        driver = ITunesAutomation()
        ok = driver.actuate_restore_ui()
        print(f"iTunes restore actuation: {'✅ Sent' if ok else '❌ Failed'}")
        return 0 if ok else 1

    if getattr(args, "drive", False) or getattr(args, "auto_backup", False):
        from storage_monitor import run_ideviceinfo
        info = run_ideviceinfo()
        udid = info.get("UniqueDeviceID")
        if not udid:
            from win_tools import detect_apple_pnp_devices
            pnp = detect_apple_pnp_devices()
            for p in pnp:
                if len(p.instance_id) >= 40:
                    udid = p.instance_id[-40:].lower()
                    break
        if not udid:
            from common import diagnose_connection
            print(f"❌ {diagnose_connection()['message']}", file=sys.stderr)
            return 1
        from itunes_automation import ITunesAutomation
        driver = ITunesAutomation()
        res = driver.drive_full_backup_workflow(udid)
        print(json.dumps(res, indent=2))
        return 0 if res.get("status") == "success" else 1

    if getattr(args, "monitor", False):
        from storage_monitor import run_ideviceinfo
        info = run_ideviceinfo()
        udid = info.get("UniqueDeviceID")
        if not udid:
            from win_tools import detect_apple_pnp_devices
            pnp = detect_apple_pnp_devices()
            for p in pnp:
                if len(p.instance_id) >= 40:
                    udid = p.instance_id[-40:].lower()
                    break
        if not udid:
            from common import diagnose_connection
            print(f"❌ {diagnose_connection()['message']}", file=sys.stderr)
            return 1
        from itunes_automation import ITunesAutomation
        driver = ITunesAutomation()
        prog = driver.monitor_backup(udid)
        return 0 if prog.state == "finished" else 1

    from itunes_env import print_itunes_report
    print_itunes_report(install=args.install, yes=args.yes)
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    """Smoke-tests and discovers native libimobiledevice CLI tools and USB hardware."""
    from win_tools import print_smoke_test_report
    print_smoke_test_report(install=args.install, yes=args.yes)
    return 0


def cmd_tokens(args: argparse.Namespace) -> int:
    """Standard token extraction, credential survival audit, and post-restore diff."""
    from token_extractor import (
        extract_all_tokens,
        extract_lockdown_token,
        print_token_manifest_report,
        print_verification_report,
        verify_token_survival,
    )

    action = args.token_action
    if action == "audit":
        manifest = extract_all_tokens(backup_target=getattr(args, "target", None))
        if getattr(args, "json", False):
            d = manifest.to_dict()
            if getattr(args, "compact", False):
                d = TokenCompactor.compact_json(d, max_list_items=10)
            print(json.dumps(d, indent=2))
        else:
            print_token_manifest_report(manifest)
        return 0 if manifest.verdict in ("PASS", "WARN_LOCKDOWN_ESCROW_ABSENT") else 1

    elif action == "extract":
        manifest = extract_all_tokens(backup_target=args.target)
        out_path = Path(args.out) if args.out else Path(f"{manifest.udid}-tokens.json")
        out_path.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")
        print(f"✅ Token manifest sealed and saved to: {out_path.resolve()}")
        print_token_manifest_report(manifest)
        return 0

    elif action == "verify":
        diff = verify_token_survival(
            prewipe_manifest=args.prewipe,
            post_manifest=args.post,
            backup_target=args.target,
        )
        print_verification_report(diff)
        return 0 if diff.get("status") == "PASS_100_PERCENT_SURVIVAL" else 1

    elif action == "lockdown":
        ld = extract_lockdown_token(args.udid)
        print(json.dumps(ld.to_dict(), indent=2))
        return 0 if ld.present else 1

    return 0


def cmd_revive(args: argparse.Namespace) -> int:
    """One-touch revival pipeline: inspects, runs the full pre-erase gate, clones, and stages restore."""
    import shutil
    from backup_verifier import inspect_backup
    from common import CloneError, host_mount, make_prewipe_clone, state_path
    from itunes_env import check_itunes_installation, get_apple_itunes_download_url
    from storage_monitor import run_ideviceinfo
    from win_tools import detect_apple_pnp_devices

    print("=" * 70)
    print("       PHONE REVIVAL: ONE-TOUCH RESTORATION PIPELINE")
    print("=" * 70)

    # 1. Host Storage Verification
    mount = host_mount()
    stat = shutil.disk_usage(str(mount))
    free_gb = stat.free / (1000**3)
    print(f"\n[1/4] HOST STORAGE CHECK ({mount})")
    print(f"  Free Storage: {free_gb:.1f} GB")
    if free_gb < 20.0:
        print("  ❌ INSUFFICIENT DISK SPACE: At least 20 GB free required for the safety clone.", file=sys.stderr)
        return 1
    print("  ✅ Host storage buffer confirmed adequate.")

    # 2. Driver & Daemon Subsystem
    print("\n[2/4] USB & APPLE DRIVER SUBSYSTEM")
    it_status = check_itunes_installation()
    pnp_devices = detect_apple_pnp_devices()

    if pnp_devices:
        for d in pnp_devices:
            print(f"  📱 Hardware Connected: {d.name} [{d.mode}]")
    else:
        print("  ⚠️ No Apple device detected on USB. Please plug in your iPhone.")

    info = run_ideviceinfo()
    if not info:
        print("  ⚠️ iOS service daemon handshake not established.")
        if sys.platform == "win32" and not it_status.amds_service_running:
            dl_url = get_apple_itunes_download_url()
            print("\n  🚨 ACTION REQUIRED: OFFICIAL ITUNES & APPLE USB DRIVERS MISSING")
            print("  Windows requires official iTunes and Apple Mobile Device Support to communicate with iOS.")
            print("  Please download and install official iTunes for Windows directly from Apple:")
            print(f"    👉 {dl_url}")
            print("  Once installed, re-run this command to proceed automatically!")
            return 2
        else:
            from common import diagnose_connection
            print(f"  👉 {diagnose_connection()['message']}")
            return 1

    udid = info.get("UniqueDeviceID")
    product_type = info.get("ProductType")
    ios_version = info.get("ProductVersion")
    build_version = info.get("BuildVersion")
    device_name = info.get("DeviceName", "iPhone")

    print(f"  ✅ Device Connected & Authenticated: {device_name}")
    print(f"     Identifier:   {product_type}")
    print(f"     Current iOS:  {ios_version} (Build {build_version})")
    print(f"     UDID:         {udid}")

    # 3. Full pre-erase verification gate + immutable safety clone.
    print("\n[3/4] BACKUP INTEGRITY GATE & SAFETY CLONE")
    backup = find_backup_for_target(udid)
    if not backup and getattr(args, "auto_drive", False):
        print("  🚀 AUTO-DRIVE: Launching automated iTunes backup autopilot (Windows-MCP)...")
        from itunes_automation import ITunesAutomation
        driver = ITunesAutomation()
        driver.drive_full_backup_workflow(udid)
        backup = find_backup_for_target(udid)

    if not backup:
        print(f"  🚨 No existing backup found for UDID {udid}.")
        print("  Open iTunes/Finder, check 'Encrypt local backup', and click 'Back Up Now' first.")
        print("  (Or re-run with: python iphone_manager.py revive --auto-drive to automate it!)")
        return 1

    # Run the SAME full gate as `verify` (freshness, SnapshotState, Manifest.db, encryption,
    # size/file-count, and proof of password) — not just an is_encrypted check.
    gate_ok = inspect_backup(Path(backup.backup_path), max_age_hours=24.0, interactive=False)
    if not gate_ok:
        print("\n  ❌ Pre-erase gate FAILED. Phone erase is BLOCKED. Resolve the items above and re-run.",
              file=sys.stderr)
        return 1

    try:
        prewipe_clone = make_prewipe_clone(Path(backup.backup_path))
    except CloneError as e:
        print(f"  ❌ Safety clone failed: {e}", file=sys.stderr)
        return 1
    print(f"  ✅ Immutable pre-wipe clone: {prewipe_clone.name}")
    from backup_labels import ensure_labels
    ensure_labels(backup.udid, say=lambda m: print(f"  🏷️  {m}"))   # distinct names in Finder's restore pop-up

    # 4. Token Extraction & Credential Safeguard (manifest sealed in the private state dir).
    print("\n[4/4] TOKEN EXTRACTION & CREDENTIAL SAFEGUARD")
    from token_extractor import extract_all_tokens
    token_manifest = extract_all_tokens(backup_target=backup.backup_path)

    prewipe_token_path = state_path(f"{backup.udid}-tokens-prewipe.json")
    prewipe_token_path.write_text(json.dumps(token_manifest.to_dict(), indent=2), encoding="utf-8")

    print(f"  ✅ Cryptographic Token Manifest Sealed:")
    print(f"     Artifact:       {prewipe_token_path}")
    print(f"     Keybag Posture: {token_manifest.keybag.status} ({token_manifest.keybag.class_count} classes, {token_manifest.keybag.iteration_count:,} iter)")
    print(f"     Keychain Store: {'INTACT (' + str(token_manifest.keychain.size_bytes) + ' bytes)' if token_manifest.keychain.present else 'MISSING'}")
    print(f"     Lockdown Token: {'Escrow Bag Present' if token_manifest.lockdown.has_escrow_bag else 'Pairing Record' if token_manifest.lockdown.present else 'Host USB Pairing Required'}")
    print(f"     Auth Accounts:  {len(token_manifest.accounts)} verified ({sum(1 for a in token_manifest.accounts if a.has_push)} Duo Push, {sum(1 for a in token_manifest.accounts if not a.has_push)} TOTP)")
    for acc in token_manifest.accounts:
        push_str = "[Duo Push]" if acc.has_push else "[TOTP]"
        print(f"       • {push_str} {acc.app_name}: {acc.account_name} ({acc.issuer or acc.host or 'Standard'})")

    print("\n" + "=" * 70)
    print("🎉 STAGING COMPLETE: ALL ASSETS & SAFETY SNAPSHOTS SECURED!")
    print("=================================================================")
    print("Your one-touch action to purge system cache bloat:")
    print("  1. On your iPhone, open: Settings -> General -> Transfer or Reset iPhone")
    print("  2. Tap 'Erase All Content and Settings' -> Tap 'Continue'")
    print("  3. 💡 HINT: If prompted 'Uploading Data to iCloud' / 'Finish Uploading Then Erase',")
    print("     tap 'Skip Backup' or 'Erase Now' (your full encrypted backup is already secured on this PC).")
    print("     (The phone's screen may be slow or stop responding for a minute or two while it erases: that's normal.")
    print("      Wait, don't tap repeatedly, don't unplug.)")
    print("  4. ⚠️ CRITICAL FOR eSIMs: If prompted about cellular plans, choose 'Keep eSIMs and Erase Data'.")
    print("  5. Once erased and back at the 'Hello' screen, plug back into PC:")
    print(f"     - Open iTunes -> Click 'Restore Backup...' -> Select {backup.folder_name}")
    print("     - Enter your backup password to restore Keychain, Wi-Fi, and 2FA accounts.")
    print(f"  6. After restore, take a FRESH backup, then verify token survival:")
    print(f"     python iphone_manager.py tokens verify --prewipe {prewipe_token_path}")
    print("=" * 70)
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Automated and interactive dependency setup for iTunes and native CLI tools."""
    from dep_installer import run_interactive_or_automated_setup
    run_interactive_or_automated_setup(install_all=args.install_all, yes=args.yes)
    return 0


def cmd_sim(args: argparse.Namespace) -> int:
    """Manages Xcode Simulator discovery, booting, and backup transfer."""
    import simulator_bridge
    action = args.sim_action

    if action == "list":
        sims = simulator_bridge.list_simulators(
            only_available=not args.all,
            booted_only=args.booted,
            compact=True,
        )
        print(json.dumps(sims, indent=2))
        return 0
    elif action == "boot":
        res = simulator_bridge.boot_simulator(args.device, open_app=not args.no_gui)
        print(json.dumps(res, indent=2))
        return 0
    elif action == "shutdown":
        res = simulator_bridge.shutdown_simulator(args.device)
        print(json.dumps(res, indent=2))
        return 0
    elif action == "send":
        res = simulator_bridge.send_backup_to_simulator(
            backup_target=args.backup,
            simulator_target=args.device,
            max_media_items=args.max_media,
            open_gui=not args.no_gui,
        )
        print(json.dumps(res, indent=2))
        return 0
    elif action == "clone":
        res = simulator_bridge.launch_backup_copy_simulator(
            backup_target=args.backup,
            max_media_items=args.max_media,
            open_photos=not args.no_photos,
        )
        print(json.dumps(res, indent=2))
        return 0
    elif action == "openurl":
        res = simulator_bridge.open_simulator_url(args.device, args.url)
        print(json.dumps(res, indent=2))
        return 0
    return 1


def cmd_apps(args: argparse.Namespace) -> int:
    """Manages backup installed applications and simulator deployment."""
    import backup_apps
    action = args.app_action

    if action == "list":
        catalog = backup_apps.get_backup_apps_catalog(args.backup)
        if args.genre:
            catalog = [a for a in catalog if a.genre.lower() == args.genre.strip().lower()]
        if args.query:
            q = args.query.strip().lower()
            catalog = [a for a in catalog if q in a.display_name.lower() or q in a.bundle_id.lower()]

        if args.compact:
            data = [
                {
                    "name": a.display_name,
                    "bundle_id": a.bundle_id,
                    "version": a.version,
                    "genre": a.genre,
                    "app_store_id": a.app_store_id,
                }
                for a in catalog
            ]
            print(json.dumps(TokenCompactor.compact_json(data, max_list_items=15), indent=2))
        else:
            by_genre = backup_apps.group_apps_by_genre(catalog)
            print(f"Total Apps: {len(catalog)} across {len(by_genre)} genres:\n")
            for g, apps in by_genre.items():
                print(f"=== {g} ({len(apps)}) ===")
                for a in apps:
                    print(f"  • {a['display_name']} (v{a['version']}) - {a['bundle_id']}")
                print()
        return 0

    elif action == "open-store":
        res = backup_apps.open_app_store_in_simulator(args.app, simulator_udid=args.device)
        print(json.dumps(res, indent=2))
        return 0

    elif action == "install":
        res = backup_apps.auto_install_packages_to_simulator(
            simulator_udid=args.device,
            app_packages_dir=args.app_dir,
        )
        print(json.dumps(res, indent=2))
        return 0
    return 1


def cmd_restorer(args: argparse.Namespace) -> int:
    """Manages iPhone reconnection after reset, Finder restore automation, and storage audits."""
    import restorer

    action = args.restorer_action
    if action == "wait":
        try:
            expect_reset = getattr(args, "expect_reset", False)
            res = restorer.wait_for_device_return(args.udid, timeout_sec=args.timeout, expect_reset=expect_reset)
            print(json.dumps(res, indent=2))
            return 0
        except TimeoutError as e:
            print(f"❌ {e}", file=sys.stderr)
            return 1

    elif action == "audit":
        data = restorer.audit_post_restore_storage(args.udid)
        print(json.dumps(data, indent=2))
        return 0

    elif action == "heal-finder":
        success = restorer.restart_finder_services(timeout_sec=args.timeout)
        return 0 if success else 1

    return 1


def cmd_market(args: argparse.Namespace) -> int:
    """Dynamic secondary market valuation & live economic ROI engine."""
    import market_pricing
    action = getattr(args, "market_action", None)
    if not action:
        if getattr(args, "subcommand", "") == "roi":
            action = "roi"
        else:
            action = "list"

    if action == "list":
        market_pricing.print_ledger_list()
        return 0

    if action == "get":
        market_pricing.print_valuation(args.model, capacity_gb=args.capacity)
        return 0

    if action == "set":
        entry = market_pricing.record_live_quote(
            model=args.model,
            refurb_usd=args.refurb,
            retail_usd=args.retail,
            source=args.source,
            capacity_gb=args.capacity,
        )
        print(f"\n✅ Live quote recorded for {entry['friendly_name']} ({args.model}):")
        print(f"   Refurb Replacement: ${entry['refurb_replacement_usd']:.2f}")
        print(f"   Retail Avoidance:   ${entry['retail_upgrade_usd']:.2f}")
        print(f"   Source:             {entry['source']}\n")
        return 0

    if action == "check":
        market_pricing.print_staleness_audit(days=args.days)
        return 0

    if action == "roi":
        import storage_monitor
        storage_monitor.print_diff_report(getattr(args, "udid", None))
        return 0

    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="iPhone Revival Project: Autonomous iOS Storage Cleaner & Zero-Lockout Device Revival Suite"
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # status
    p_stat = subparsers.add_parser("status", help="Inspect host storage & USB-connected iPhone.")
    p_stat.set_defaults(func=cmd_status)

    # backup (Autonomous backup engine with desktop chimes & verification)
    p_bup = subparsers.add_parser("backup", help="Autonomous iPhone encrypted backup with desktop chimes & verification.")
    p_bup.add_argument("--udid", help="Device UDID (defaults to connected device).")
    p_bup.add_argument("--password", help="Candidate backup decryption password.")
    p_bup.add_argument("--no-sound", action="store_true", help="Disable audio chimes.")
    p_bup.add_argument("--no-notify", action="store_true", help="Disable macOS notification banners.")
    p_bup.set_defaults(func=cmd_backup)

    # verify
    p_ver = subparsers.add_parser("verify", help="Verify local backup integrity (pre-erase gate).")
    p_ver.add_argument("--connected", action="store_true", help="Check device plugged in via USB.")
    p_ver.add_argument("--udid", help="Check specific device UDID.")
    p_ver.add_argument("--max-age-hours", type=float, default=24.0, help="Max backup age in hours.")
    p_ver.add_argument("--password", help="Candidate backup password to cryptographically prove.")
    p_ver.add_argument("--skip-password-check", action="store_true", help="DANGEROUS: Bypass mandatory proof-of-password gate before erase.")
    p_ver.add_argument("--steer-ui", action="store_true", help="Actively steer macOS UI (Keychain Access / Finder) to the required gate location.")
    p_ver.add_argument("--compact", action="store_true", help="Filter output with Token-Saver.")
    p_ver.set_defaults(func=cmd_verify)

    # passwords (Interactive backup password hunter & tracker)
    p_pwd = subparsers.add_parser("passwords", help="Interactive iOS backup password hunter & attempt tracker (Web UI / CLI).")
    p_pwd.add_argument("--udid", help="Device UDID (defaults to target phone).")
    p_pwd.add_argument("--cli", action="store_true", help="Run in interactive CLI mode instead of web UI.")
    p_pwd.add_argument("--port", type=int, default=8765, help="Port for web pop-up (default: 8765).")
    p_pwd.add_argument("--check", help="Test a single candidate password non-interactively.")
    p_pwd.add_argument("--history", action="store_true", help="Show all previously attempted passwords.")
    p_pwd.add_argument("--variants", help="Generate and test variations for a root string.")
    p_pwd.set_defaults(func=cmd_passwords)

    # security (MVT Anti-Pegasus audit & password check)
    p_sec = subparsers.add_parser("security", help="Anti-Pegasus spyware audit & backup decryption verification (MVT).")
    p_sec.add_argument("--udid", help="Device UDID to scan.")
    p_sec.add_argument("--skip-ioc-update", action="store_true", help="Skip refreshing STIX2 IOC indicators.")
    p_sec.add_argument("--keep-work-copy", action="store_true", help="Retain decrypted plaintext copy.")
    p_sec.add_argument("--no-compact", dest="compact", action="store_false", help="Disable Token-Saver compaction.")
    p_sec.add_argument("--max-lines", type=int, default=60, help="Maximum lines for compact report.")
    p_sec.set_defaults(func=cmd_security)

    # token-doctor
    p_tok = subparsers.add_parser("token-doctor", help="Diagnose token waste and filter command outputs.")
    p_tok.add_argument("--audit", dest="audit_target", help="Audit file or report for token waste.")
    p_tok.add_argument("--wrap", dest="wrap_cmd", nargs=argparse.REMAINDER, help="Execute command with RTK filter.")
    p_tok.add_argument("--max-lines", type=int, default=50, help="Max lines for wrapped output.")
    p_tok.set_defaults(func=cmd_token_doctor)

    # sim (Xcode Simulator bridge)
    p_sim = subparsers.add_parser("sim", help="Xcode iOS Simulator discovery, booting, and backup transfer.")
    sim_sub = p_sim.add_subparsers(dest="sim_action", required=True)

    sim_list = sim_sub.add_parser("list", help="List available Xcode iOS Simulators.")
    sim_list.add_argument("--booted", action="store_true", help="List only booted simulators.")
    sim_list.add_argument("--all", action="store_true", help="Include unavailable devices.")

    sim_boot = sim_sub.add_parser("boot", help="Boot a simulator and open Simulator app.")
    sim_boot.add_argument("device", nargs="?", help="UDID or simulator name.")
    sim_boot.add_argument("--no-gui", action="store_true", help="Do not launch Simulator macOS window.")

    sim_shut = sim_sub.add_parser("shutdown", help="Shut down a simulator.")
    sim_shut.add_argument("device", nargs="?", help="UDID or simulator name.")

    sim_send = sim_sub.add_parser("send", help="Send backup assets & posture into simulator.")
    sim_send.add_argument("--backup", help="Specific backup UDID or folder path.")
    sim_send.add_argument("--device", help="Target simulator UDID or name.")
    sim_send.add_argument("--max-media", type=int, default=25, help="Max photos/media to import.")
    sim_send.add_argument("--no-gui", action="store_true", help="Run headless without opening window.")

    sim_clone = sim_sub.add_parser("clone", help="Provision and launch an exact simulator replica of the backup.")
    sim_clone.add_argument("--backup", help="Specific backup UDID or folder path.")
    sim_clone.add_argument("--max-media", type=int, default=25, help="Max photos/media to import.")
    sim_clone.add_argument("--no-photos", action="store_true", help="Do not automatically launch Photos app.")

    sim_url = sim_sub.add_parser("openurl", help="Open a URL inside Simulator Safari.")
    sim_url.add_argument("url", help="URL to open.")
    sim_url.add_argument("--device", help="Simulator UDID or name.")

    p_sim.set_defaults(func=cmd_sim)

    # apps (Backup application catalog & simulator deployment)
    p_apps = subparsers.add_parser("apps", help="Backup installed apps catalog, App Store links & simulator deployment.")
    app_sub = p_apps.add_subparsers(dest="app_action", required=True)

    app_list = app_sub.add_parser("list", help="List all installed apps from backup.")
    app_list.add_argument("--genre", help="Filter by genre (e.g. Finance, Games, Shopping).")
    app_list.add_argument("--query", "-q", help="Search string matching name or bundle ID.")
    app_list.add_argument("--compact", action="store_true", help="Token-compacted summary.")
    app_list.add_argument("--backup", help="Specific backup UDID or path.")

    app_store = app_sub.add_parser("open-store", help="Open app's official App Store page in Simulator Safari.")
    app_store.add_argument("app", help="App name or bundle ID (e.g. Duo, Chase, AllTrails).")
    app_store.add_argument("--device", help="Target simulator UDID or name.")

    app_inst = app_sub.add_parser("install", help="Auto-install matching .app packages onto simulator.")
    app_inst.add_argument("--device", required=True, help="Target simulator UDID or name.")
    app_inst.add_argument("--app-dir", help="Directory containing .app/.ipa packages (default: ./simulator_apps).")

    p_apps.set_defaults(func=cmd_apps)

    # itunes (iTunes environment enforcer)
    p_itunes = subparsers.add_parser("itunes", help="Inspect and enforce iTunes Windows installation & backup paths.")
    p_itunes.add_argument("--install", action="store_true", help="Download and install official iTunes from Apple website.")
    p_itunes.add_argument("--drive", action="store_true", help="Automate focusing iTunes and triggering backup via Windows-MCP.")
    p_itunes.add_argument("--actuate", action="store_true", help="Actuate backup directly via iTunes UI.")
    p_itunes.add_argument("--restore", action="store_true", help="Actuate restore directly via iTunes UI.")
    p_itunes.add_argument("--detect-wipe", action="store_true", help="Deterministically detect if connected device is post-wipe.")
    p_itunes.add_argument("--monitor", action="store_true", help="Monitor live backup file-write progress for connected device.")
    p_itunes.add_argument("--yes", "-y", action="store_true", help="Non-interactive mode.")
    p_itunes.set_defaults(func=cmd_itunes)

    # detect-wipe (Deterministic post-erase state detector)
    p_detect = subparsers.add_parser("detect-wipe", help="Deterministically detects post-wipe setup state and restore readiness.")
    p_detect.add_argument("--udid", help="Target device UDID.")
    p_detect.set_defaults(func=cmd_detect_wipe)

    # tools (Native CLI tools & USB smoke-test)
    p_tools = subparsers.add_parser("tools", help="Discover and smoke-test native libimobiledevice CLI tools.")
    p_tools.add_argument("--install", action="store_true", help="Download and extract native CLI suite into ./tools/bin/.")
    p_tools.add_argument("--yes", "-y", action="store_true", help="Non-interactive mode.")
    p_tools.set_defaults(func=cmd_tools)

    # setup (Automated dependency installer & environment setup)
    p_setup = subparsers.add_parser("setup", help="Verify and install all missing dependencies.")
    p_setup.add_argument("--install-all", "-a", action="store_true", help="Install all missing dependencies.")
    p_setup.add_argument("--yes", "-y", action="store_true", help="Non-interactive mode.")
    p_setup.set_defaults(func=cmd_setup)

    # revive / auto-revive (One-touch automatic revival pipeline)
    p_revive = subparsers.add_parser("revive", aliases=["auto-revive"], help="One-touch automagic revival pipeline (inspect, gate, snapshot, stage restore).")
    p_revive.add_argument("--auto-drive", action="store_true", help="Automatically drive iTunes backup via Windows-MCP and monitor progress.")
    p_revive.set_defaults(func=cmd_revive)

    # restorer (Reset return detection & restoration automation)
    p_rst = subparsers.add_parser("restorer", help="iPhone reset reconnection, Finder restore automation & storage audit.")
    rst_sub = p_rst.add_subparsers(dest="restorer_action", required=True)

    rst_wait = rst_sub.add_parser("wait", help="Wait for reset iPhone to return on USB bus.")
    rst_wait.add_argument("--udid", default=os.environ.get("TARGET_UDID"), help="Device UDID (defaults to TARGET_UDID env or single connected device).")
    rst_wait.add_argument("--timeout", type=int, default=600, help="Timeout in seconds.")
    rst_wait.add_argument("--expect-reset", action="store_true", help="First wait for currently-connected device to disconnect, then wait for reboot.")

    rst_audit = rst_sub.add_parser("audit", help="Audit post-restore device storage.")
    rst_audit.add_argument("--udid", default=os.environ.get("TARGET_UDID"), help="Device UDID.")

    rst_heal = rst_sub.add_parser("heal-finder", help="Auto-heal stuck Finder / AMPDevicesAgent UI by restarting services.")
    rst_heal.add_argument("--timeout", type=float, default=10.0, help="Max seconds to wait for Finder to respawn.")

    p_rst.set_defaults(func=cmd_restorer)

    # tokens (Standard Token Extraction & Credential Survival Protocol)
    p_tokens = subparsers.add_parser(
        "tokens", help="Standard token extraction, keybag inspection, and credential survival verification."
    )
    tok_sub = p_tokens.add_subparsers(dest="token_action", required=True)

    tok_audit = tok_sub.add_parser("audit", help="Inspect and audit token posture.")
    tok_audit.add_argument("--target", help="Specific backup UDID or directory path.")
    tok_audit.add_argument("--json", action="store_true", help="Output full JSON manifest.")
    tok_audit.add_argument("--compact", action="store_true", help="Token-saver compacted summary.")

    tok_ext = tok_sub.add_parser("extract", help="Extract and save cryptographic token manifest.")
    tok_ext.add_argument("--target", help="Specific backup UDID or directory path.")
    tok_ext.add_argument("--out", "-o", help="Output JSON path (default: <UDID>-tokens.json).")

    tok_ver = tok_sub.add_parser("verify", help="Verify token survival against a pre-wipe manifest.")
    tok_ver.add_argument("--prewipe", required=True, help="Path to pre-wipe token manifest JSON.")
    tok_ver.add_argument("--post", help="Path to post-restore manifest JSON (or re-extracts live).")
    tok_ver.add_argument("--target", help="Target backup UDID or directory path.")

    tok_lock = tok_sub.add_parser("lockdown", help="Query host lockdown pairing record and escrow token.")
    tok_lock.add_argument("--udid", help="Device UDID to query.")

    p_tokens.set_defaults(func=cmd_tokens)

    # market / roi
    p_mkt = subparsers.add_parser(
        "market",
        aliases=["roi"],
        help="Dynamic secondary market valuation & live economic ROI engine.",
    )
    mkt_sub = p_mkt.add_subparsers(dest="market_action")

    mkt_list = mkt_sub.add_parser("list", help="List registered device market quotes.")
    mkt_get = mkt_sub.add_parser("get", help="Get valuation and agent research directive for a model.")
    mkt_get.add_argument("model", help="Hardware model identifier (e.g. iPhone12,8)")
    mkt_get.add_argument("--capacity", type=int, default=64, help="Capacity in GB")

    mkt_set = mkt_sub.add_parser("set", help="Record researched live market quotation.")
    mkt_set.add_argument("--model", required=True, help="Hardware model identifier")
    mkt_set.add_argument("--refurb", required=True, type=float, help="Like-for-like refurbished cost USD")
    mkt_set.add_argument("--retail", type=float, default=799.0, help="New retail base upgrade avoided USD")
    mkt_set.add_argument("--source", required=True, help="Provenance / source summary")
    mkt_set.add_argument("--capacity", type=int, default=64, help="Capacity in GB")

    mkt_check = mkt_sub.add_parser("check", help="Audit quotes against stale threshold and stimulate agent.")
    mkt_check.add_argument("--days", type=int, default=14, help="Stale threshold in days")

    mkt_roi = mkt_sub.add_parser("roi", help="Display before vs after storage recovery & live ROI report.")
    mkt_roi.add_argument("--udid", help="Device UDID to filter report.")

    p_mkt.set_defaults(func=cmd_market)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
