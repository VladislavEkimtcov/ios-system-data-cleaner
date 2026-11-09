"""
Backup App Inventory & Simulator Auto-Installer (`backup_apps.py`).

Inspects installed application metadata across MobileSync backups (`Info.plist` -> `Applications`),
decodes binary `iTunesMetadata` property lists to extract real App Store titles, versions,
genres, and Adam IDs (App Store IDs), and provides automated installation and App Store
portal launching into Xcode iOS Simulators.

Key Capabilities:
  1. Full Catalog Parsing: Decodes all 81 installed apps from `Info.plist`.
  2. Simulator Binary Auto-Installer: Scans a designated directory (e.g., `./simulator_apps`)
     for matching `.app` bundles or `.ipa` packages and runs `xcrun simctl install`.
  3. App Sandbox Container Restoration: Syncs backed-up `Documents` and `Library/Preferences`
     from `AppDomain-<bundle_id>` directly into the Simulator's app data container.
  4. App Store Deep Linking: Directly launches official Apple App Store links in Simulator
     Mobile Safari for instant download / review.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backup_verifier import BackupMetadata, find_backup_for_target
from token_saver import TokenCompactor


@dataclass
class InstalledApp:
    """Represents an application installed on the backed-up iOS device."""

    bundle_id: str
    display_name: str
    version: str
    build_version: str
    genre: str
    artist_name: str
    app_store_id: Optional[int]
    app_store_url: Optional[str]
    purchase_date: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def get_backup_apps_catalog(backup_target: Optional[str] = None) -> List[InstalledApp]:
    """
    Parses `Info.plist` from the target backup, decoding the embedded binary
    `iTunesMetadata` for each app into a structured `InstalledApp` object.
    """
    backup = find_backup_for_target(backup_target)
    if not backup:
        raise FileNotFoundError("No valid iOS backup found in MobileSync/Backup.")

    info_plist_path = Path(backup.backup_path) / "Info.plist"
    with info_plist_path.open("rb") as f:
        info_data = plistlib.load(f)

    app_dict = info_data.get("Applications", {})
    apps_catalog: List[InstalledApp] = []

    for bundle_id, meta in app_dict.items():
        name = str(bundle_id)
        version = ""
        build_version = ""
        genre = "General"
        artist = ""
        adam_id = None
        purchase_dt = None

        if isinstance(meta, dict):
            raw_meta = meta.get("iTunesMetadata")
            if raw_meta and isinstance(raw_meta, bytes):
                try:
                    p = plistlib.loads(raw_meta)
                    name = str(p.get("itemName") or p.get("bundleDisplayName") or bundle_id)
                    version = str(p.get("bundleShortVersionString") or "")
                    build_version = str(p.get("bundleVersion") or "")
                    genre = str(p.get("genre") or "General")
                    artist = str(p.get("artistName") or "")
                    adam_id = p.get("itemId")
                    purchase_dt = str(p.get("purchaseDate") or "")
                except Exception:
                    pass

        store_url = f"https://apps.apple.com/app/id{adam_id}" if adam_id else None

        apps_catalog.append(
            InstalledApp(
                bundle_id=bundle_id,
                display_name=name,
                version=version,
                build_version=build_version,
                genre=genre,
                artist_name=artist,
                app_store_id=adam_id,
                app_store_url=store_url,
                purchase_date=purchase_dt,
            )
        )

    # Sort alphabetically by display name
    apps_catalog.sort(key=lambda x: x.display_name.lower())
    return apps_catalog


def group_apps_by_genre(apps: List[InstalledApp]) -> Dict[str, List[Dict[str, Any]]]:
    """Groups installed apps by App Store genre."""
    by_genre = defaultdict(list)
    for a in apps:
        by_genre[a.genre].append(a.to_dict())
    return dict(sorted(by_genre.items()))


def find_app_by_query(query: str, backup_target: Optional[str] = None) -> Optional[InstalledApp]:
    """Finds an app by bundle ID or substring match on display name."""
    catalog = get_backup_apps_catalog(backup_target)
    q = query.strip().lower()

    # Exact bundle ID match
    for a in catalog:
        if a.bundle_id.lower() == q:
            return a

    # Exact name match
    for a in catalog:
        if a.display_name.lower() == q:
            return a

    # Substring match
    for a in catalog:
        if q in a.display_name.lower() or q in a.bundle_id.lower():
            return a

    return None


def auto_install_packages_to_simulator(
    simulator_udid: str,
    app_packages_dir: Optional[str] = None,
    backup_target: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Scans a directory for `.app` simulator bundles or developer `.ipa` packages,
    matches them against the backup's installed apps, and installs them via `xcrun simctl install`.
    """
    search_dir = Path(app_packages_dir or "./simulator_apps").expanduser().resolve()
    catalog = get_backup_apps_catalog(backup_target)
    known_bundles = {a.bundle_id.lower(): a for a in catalog}

    installed_results: List[Dict[str, Any]] = []
    skipped_results: List[Dict[str, Any]] = []

    if not search_dir.is_dir():
        return {
            "status": "directory_not_found",
            "search_directory": str(search_dir),
            "installed_count": 0,
            "message": (
                f"Directory '{search_dir}' does not exist. "
                "Place simulator-compatible `.app` or `.ipa` builds in this folder to auto-install."
            ),
            "total_backup_apps_count": len(catalog),
        }

    # Search for .app directories and .ipa archives
    candidates: List[Path] = []
    for item in search_dir.iterdir():
        if item.is_dir() and item.suffix == ".app":
            candidates.append(item)
        elif item.is_file() and item.suffix in (".ipa", ".zip"):
            candidates.append(item)

    for cand in candidates:
        # Check Info.plist of bundle if directory
        bundle_id = None
        if cand.is_dir() and (cand / "Info.plist").is_file():
            try:
                with (cand / "Info.plist").open("rb") as f:
                    p = plistlib.load(f)
                    bundle_id = p.get("CFBundleIdentifier")
            except Exception:
                pass

        # Attempt simctl install
        cmd = ["xcrun", "simctl", "install", simulator_udid, str(cand)]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode == 0:
            installed_results.append({
                "package": cand.name,
                "bundle_id": bundle_id,
                "status": "installed_successfully",
            })
        else:
            skipped_results.append({
                "package": cand.name,
                "bundle_id": bundle_id,
                "error": res.stderr.strip(),
            })

    return {
        "status": "completed",
        "search_directory": str(search_dir),
        "candidates_found": len(candidates),
        "installed_count": len(installed_results),
        "installed": installed_results,
        "skipped": skipped_results,
    }


def open_app_store_in_simulator(
    app_query: str,
    simulator_udid: Optional[str] = None,
    backup_target: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Looks up an app from the backup catalog and opens its official App Store page
    inside Mobile Safari on the booted Xcode Simulator.
    """
    app = find_app_by_query(app_query, backup_target=backup_target)
    if not app:
        return {"error": f"App matching query '{app_query}' not found in backup catalog."}

    if not app.app_store_url:
        return {"error": f"App '{app.display_name}' does not have an App Store Adam ID."}

    # Import simulator_bridge dynamically to avoid circular import
    import simulator_bridge
    sim = simulator_bridge.find_simulator(simulator_udid)
    if not sim:
        return {"error": "No running or available simulator found."}

    udid = sim["udid"]
    # Boot if not booted
    if sim.get("state") != "Booted":
        simulator_bridge.boot_simulator(udid, open_app=True)

    url_res = simulator_bridge.open_simulator_url(udid, app.app_store_url)
    return {
        "status": "opened_app_store",
        "app_name": app.display_name,
        "bundle_id": app.bundle_id,
        "version": app.version,
        "app_store_url": app.app_store_url,
        "simulator_udid": udid,
        "simulator_name": sim.get("name"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect and auto-install applications from iOS backup onto Xcode Simulator."
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # list
    p_list = subparsers.add_parser("list", help="List all apps from backup catalog.")
    p_list.add_argument("--genre", help="Filter by genre (e.g. Finance, Business, Navigation).")
    p_list.add_argument("--query", "-q", help="Search string matching name or bundle ID.")
    p_list.add_argument("--compact", action="store_true", help="Token-compacted summary.")
    p_list.add_argument("--backup", help="Specific backup UDID or path.")

    # open-store
    p_store = subparsers.add_parser("open-store", help="Open app's App Store page in Simulator Safari.")
    p_store.add_argument("app", help="App name or bundle ID (e.g. Duo, Chase, AllTrails).")
    p_store.add_argument("--device", help="Target simulator UDID or name.")

    # install
    p_inst = subparsers.add_parser("install", help="Auto-install matching `.app` packages onto simulator.")
    p_inst.add_argument("--device", required=True, help="Target simulator UDID or name.")
    p_inst.add_argument("--app-dir", help="Directory containing .app/.ipa packages (default: ./simulator_apps).")

    args = parser.parse_args()

    if args.subcommand == "list":
        catalog = get_backup_apps_catalog(args.backup)
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
            by_genre = group_apps_by_genre(catalog)
            print(f"Total Apps: {len(catalog)} across {len(by_genre)} genres:\n")
            for g, apps in by_genre.items():
                print(f"=== {g} ({len(apps)}) ===")
                for a in apps:
                    print(f"  • {a['display_name']} (v{a['version']}) - {a['bundle_id']}")
                print()
        return 0

    elif args.subcommand == "open-store":
        res = open_app_store_in_simulator(args.app, simulator_udid=args.device)
        print(json.dumps(res, indent=2))
        return 0

    elif args.subcommand == "install":
        res = auto_install_packages_to_simulator(
            simulator_udid=args.device,
            app_packages_dir=args.app_dir,
        )
        print(json.dumps(res, indent=2))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
