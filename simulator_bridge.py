"""
Xcode Simulator Bridge & Data Transfer Engine (`simulator_bridge.py`).

Provides integration between local iOS MobileSync backups, device configurations,
and Xcode's iOS Simulator environment via `xcrun simctl`:

1. Simulator Discovery: Discovers available iOS runtimes, simulator models, and boot states.
2. Device Pairing / Provisioning: Automatically matches physical device models
   (e.g., iPhone 12 mini, iPhone 13, iPhone 16) to an optimal Xcode Simulator.
3. Media & Camera Roll Ingestion: Extracts photos, screenshots, and videos from the
   backup (`Manifest.db`) and injects them directly into the Simulator's native Photo Library
   via `xcrun simctl addmedia`.
4. App Data Container Staging: Exports app documents and preferences from backup domains
   (`AppDomain-<bundle_id>`) into Simulator app sandbox data containers.
5. System Posture & URL Sync: Synchronizes device name, status bar appearance, and launches
   verification URLs in Simulator Safari.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore

from backup_verifier import BackupMetadata, find_backup_for_target
from common import load_plist, state_path
from token_saver import TokenCompactor, compute_token_metric, estimate_tokens


def _ro_connect(db_path: Path) -> sqlite3.Connection:
    """Opens a backup SQLite database strictly read-only (never rewrites the real backup)."""
    return sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro&immutable=1", uri=True)


def _safe_join(base: Path, rel_path: str) -> Optional[Path]:
    """
    Joins base/rel_path but refuses any result that escapes base (e.g. via '..').
    Returns None if rel_path would write outside the destination tree.
    """
    base = Path(base).resolve()
    candidate = (base / rel_path).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return candidate


def _is_encrypted_backup(backup_path: Path) -> bool:
    """True if the backup's Manifest.plist marks it encrypted (containers unreadable)."""
    manifest, _ = load_plist(Path(backup_path) / "Manifest.plist")
    return bool(manifest and manifest.get("IsEncrypted"))


@dataclass
class SimulatorDevice:
    """Represents an Xcode Simulator instance."""

    udid: str
    name: str
    state: str  # 'Shutdown' or 'Booted'
    runtime: str
    device_type: str
    data_path: str
    is_available: bool

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SimulatorBridgeError(Exception):
    """Custom exception for Xcode Simulator operations."""


def check_simctl_available() -> bool:
    """Checks if `xcrun simctl` is available on the host Mac."""
    return shutil.which("xcrun") is not None


def list_simulators(
    only_available: bool = True,
    booted_only: bool = False,
    compact: bool = True,
) -> List[Dict[str, Any]]:
    """
    Lists iOS Simulators on the host Mac using `xcrun simctl list --json devices`.
    """
    if not check_simctl_available():
        raise SimulatorBridgeError("Xcode CLI `xcrun` is not installed or not in PATH.")

    try:
        proc = subprocess.run(
            ["xcrun", "simctl", "list", "--json", "devices"],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        data = json.loads(proc.stdout)
    except Exception as exc:
        raise SimulatorBridgeError(f"Failed to query simulators: {exc}") from exc

    devices_dict = data.get("devices", {})
    results: List[Dict[str, Any]] = []

    for runtime_id, dev_list in devices_dict.items():
        # Only parse iOS runtimes unless asked otherwise
        runtime_short = runtime_id.split(".")[-1] if "." in runtime_id else runtime_id

        for d in dev_list:
            is_avail = bool(d.get("isAvailable", True))
            state = str(d.get("state", "Shutdown"))

            if only_available and not is_avail:
                continue
            if booted_only and state.lower() != "booted":
                continue

            entry = {
                "udid": d.get("udid"),
                "name": d.get("name"),
                "state": state,
                "runtime": runtime_short,
                "device_type": d.get("deviceTypeIdentifier", "").split(".")[-1],
                "data_path": d.get("dataPath"),
                "is_available": is_avail,
            }
            if compact:
                # Strip raw data_path to conserve tokens
                entry.pop("data_path", None)
            results.append(entry)

    return results


def find_simulator(target: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Finds a simulator by UDID or case-insensitive name match.
    If target is None, returns the first Booted simulator, or the first available iPhone.
    """
    sims = list_simulators(only_available=True, compact=False)
    if not sims:
        return None

    if not target:
        # Prefer already booted simulator
        for s in sims:
            if s.get("state") == "Booted":
                return s
        # Otherwise prefer an iPhone
        for s in sims:
            if "iphone" in s.get("name", "").lower():
                return s
        return sims[0]

    target_clean = target.strip().lower()
    # Exact UDID match
    for s in sims:
        if s.get("udid", "").lower() == target_clean:
            return s
    # Name match
    for s in sims:
        if s.get("name", "").lower() == target_clean:
            return s
    # Substring match
    for s in sims:
        if target_clean in s.get("name", "").lower():
            return s

    return None


def boot_simulator(
    target: Optional[str] = None,
    open_app: bool = True,
    wait_seconds: int = 5,
) -> Dict[str, Any]:
    """
    Boots an Xcode Simulator and optionally opens the macOS Simulator graphical application.
    """
    sim = find_simulator(target)
    if not sim:
        raise SimulatorBridgeError(f"No matching simulator found for '{target}'.")

    udid = sim["udid"]
    state = sim["state"]

    if state.lower() != "booted":
        boot_res = subprocess.run(
            ["xcrun", "simctl", "boot", udid],
            capture_output=True,
            text=True,
            check=False,
        )
        if boot_res.returncode != 0 and "current state: Booted" not in boot_res.stderr:
            raise SimulatorBridgeError(f"Failed to boot simulator {udid}: {boot_res.stderr.strip()}")

        # Wait briefly for CoreSimulator services to initialize
        if wait_seconds > 0:
            time.sleep(wait_seconds)

    if open_app:
        subprocess.run(["open", "-a", "Simulator"], check=False)

    return {
        "status": "booted",
        "udid": udid,
        "name": sim["name"],
        "runtime": sim["runtime"],
        "gui_opened": open_app,
    }


def shutdown_simulator(target: Optional[str] = None) -> Dict[str, Any]:
    """Shuts down a booted Xcode Simulator."""
    sim = find_simulator(target)
    if not sim:
        raise SimulatorBridgeError(f"No matching simulator found for '{target}'.")

    udid = sim["udid"]
    res = subprocess.run(
        ["xcrun", "simctl", "shutdown", udid],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "status": "shutdown",
        "udid": udid,
        "name": sim["name"],
        "success": res.returncode == 0,
    }


# Hardware product type mapping to Xcode CoreSimulator device types
PRODUCT_TYPE_TO_DEVICE_TYPE = {
    "iPhone10,1": "com.apple.CoreSimulator.SimDeviceType.iPhone-8",
    "iPhone10,4": "com.apple.CoreSimulator.SimDeviceType.iPhone-8",
    "iPhone10,2": "com.apple.CoreSimulator.SimDeviceType.iPhone-8-Plus",
    "iPhone10,5": "com.apple.CoreSimulator.SimDeviceType.iPhone-8-Plus",
    "iPhone12,8": "com.apple.CoreSimulator.SimDeviceType.iPhone-SE--2nd-generation-",
    "iPhone13,1": "com.apple.CoreSimulator.SimDeviceType.iPhone-12-mini",
    "iPhone13,2": "com.apple.CoreSimulator.SimDeviceType.iPhone-12",
    "iPhone13,3": "com.apple.CoreSimulator.SimDeviceType.iPhone-12-Pro",
    "iPhone13,4": "com.apple.CoreSimulator.SimDeviceType.iPhone-12-Pro-Max",
    "iPhone14,2": "com.apple.CoreSimulator.SimDeviceType.iPhone-13-Pro",
    "iPhone14,3": "com.apple.CoreSimulator.SimDeviceType.iPhone-13-Pro-Max",
    "iPhone14,4": "com.apple.CoreSimulator.SimDeviceType.iPhone-13-mini",
    "iPhone14,5": "com.apple.CoreSimulator.SimDeviceType.iPhone-13",
    "iPhone14,6": "com.apple.CoreSimulator.SimDeviceType.iPhone-SE-3rd-generation",
    "iPhone14,7": "com.apple.CoreSimulator.SimDeviceType.iPhone-14",
    "iPhone14,8": "com.apple.CoreSimulator.SimDeviceType.iPhone-14-Plus",
    "iPhone15,2": "com.apple.CoreSimulator.SimDeviceType.iPhone-14-Pro",
    "iPhone15,3": "com.apple.CoreSimulator.SimDeviceType.iPhone-14-Pro-Max",
    "iPhone15,4": "com.apple.CoreSimulator.SimDeviceType.iPhone-15",
    "iPhone15,5": "com.apple.CoreSimulator.SimDeviceType.iPhone-15-Plus",
    "iPhone16,1": "com.apple.CoreSimulator.SimDeviceType.iPhone-15-Pro",
    "iPhone16,2": "com.apple.CoreSimulator.SimDeviceType.iPhone-15-Pro-Max",
    "iPhone17,1": "com.apple.CoreSimulator.SimDeviceType.iPhone-16-Pro",
    "iPhone17,2": "com.apple.CoreSimulator.SimDeviceType.iPhone-16-Pro-Max",
    "iPhone17,3": "com.apple.CoreSimulator.SimDeviceType.iPhone-16",
    "iPhone17,4": "com.apple.CoreSimulator.SimDeviceType.iPhone-16-Plus",
}


def get_active_ios_runtime() -> str:
    """Finds the first available iOS runtime identifier."""
    proc = subprocess.run(
        ["xcrun", "simctl", "list", "--json", "runtimes"],
        capture_output=True,
        text=True,
        check=True,
    )
    runtimes = json.loads(proc.stdout).get("runtimes", [])
    for r in runtimes:
        if r.get("isAvailable") and "iOS" in r.get("name", ""):
            return r["identifier"]
    return "com.apple.CoreSimulator.SimRuntime.iOS-26-3"


def provision_simulator_copy_from_backup(
    backup_target: Optional[str] = None,
    name_suffix: str = "(Backup Copy)",
) -> Dict[str, Any]:
    """
    Creates an exact simulated device model corresponding to the backup's physical hardware.
    """
    backup = find_backup_for_target(backup_target)
    if not backup:
        raise SimulatorBridgeError("No valid iOS backup found in MobileSync/Backup.")

    sim_name = f"{backup.device_name} {backup.product_name} {name_suffix}".strip()
    sim = find_simulator(sim_name)
    if sim and sim.get("is_available"):
        return sim

    device_type = PRODUCT_TYPE_TO_DEVICE_TYPE.get(
        backup.product_type,
        "com.apple.CoreSimulator.SimDeviceType.iPhone-12-mini",
    )
    runtime = get_active_ios_runtime()

    create_proc = subprocess.run(
        ["xcrun", "simctl", "create", sim_name, device_type, runtime],
        capture_output=True,
        text=True,
        check=True,
    )
    new_udid = create_proc.stdout.strip()
    return {
        "udid": new_udid,
        "name": sim_name,
        "state": "Shutdown",
        "runtime": runtime.split(".")[-1],
        "device_type": device_type.split(".")[-1],
        "is_available": True,
        "created": True,
    }


def launch_backup_copy_simulator(
    backup_target: Optional[str] = None,
    max_media_items: int = 25,
    open_photos: bool = True,
    include_wallpaper: bool = True,
) -> Dict[str, Any]:
    """
    Full High-Level Workflow:
      1. Provisions exact hardware replica (e.g. iPhone 12 mini) for the backup.
      2. Boots the simulator.
      3. Synchronizes status bar posture and device presentation.
      4. Ingests backup camera roll photos into Photo Library.
      5. Extracts and synchronizes wallpaper from backup to match physical device.
      6. Launches Simulator application window and opens Photos app.
    """
    sim = provision_simulator_copy_from_backup(backup_target=backup_target)
    udid = sim["udid"]

    res = send_backup_to_simulator(
        backup_target=backup_target,
        simulator_target=udid,
        include_media=True,
        include_wallpaper=include_wallpaper,
        max_media_items=max_media_items,
        open_gui=True,
    )

    if open_photos:
        try:
            subprocess.run(
                ["xcrun", "simctl", "launch", udid, "com.apple.mobileslideshow"],
                capture_output=True,
                check=False,
            )
            res["photos_app_launched"] = True
        except Exception:
            res["photos_app_launched"] = False

    return res


def extract_media_from_backup(
    backup_path: Path | str,
    max_items: int = 30,
) -> List[Tuple[Path, str]]:
    """
    Scans a local backup's `Manifest.db` for photos and videos, returning
    pairs of (actual_file_on_disk, original_filename).
    """
    b_path = Path(backup_path).expanduser().resolve()
    manifest_db = b_path / "Manifest.db"
    if not manifest_db.is_file():
        return []

    conn = _ro_connect(manifest_db)
    cur = conn.cursor()

    # Query image and video files marked as regular files (flags=1)
    query = """
        SELECT fileID, relativePath
        FROM Files
        WHERE (
            relativePath LIKE '%.jpg' OR
            relativePath LIKE '%.jpeg' OR
            relativePath LIKE '%.png' OR
            relativePath LIKE '%.heic' OR
            relativePath LIKE '%.mov'
        ) AND flags = 1
        LIMIT ?
    """
    try:
        cur.execute(query, (max_items * 3,))
        rows = cur.fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()

    discovered: List[Tuple[Path, str]] = []
    for file_id, rel_path in rows:
        disk_path = b_path / file_id[:2] / file_id
        if disk_path.is_file() and disk_path.stat().st_size > 0:
            filename = Path(rel_path).name
            discovered.append((disk_path, filename))
            if len(discovered) >= max_items:
                break

    return discovered


def import_media_to_simulator(
    simulator_udid: str,
    media_items: List[Tuple[Path, str]],
) -> Dict[str, Any]:
    """
    Stages raw hashed backup files into a temporary directory with proper file
    extensions and imports them into the Simulator's Photo Library via `simctl addmedia`.
    """
    if not media_items:
        return {"status": "no_media", "imported_count": 0}

    with tempfile.TemporaryDirectory(prefix="sim_media_staging_") as tmp_dir:
        staged_paths: List[str] = []
        for idx, (src_path, orig_name) in enumerate(media_items):
            ext = Path(orig_name).suffix or ".png"
            dest_name = f"import_{idx:03d}_{orig_name}"
            dest_path = Path(tmp_dir) / dest_name
            try:
                dest_path.write_bytes(src_path.read_bytes())
                staged_paths.append(str(dest_path))
            except Exception:
                continue

        if not staged_paths:
            return {"status": "staging_failed", "imported_count": 0}

        # Execute xcrun simctl addmedia
        cmd = ["xcrun", "simctl", "addmedia", simulator_udid] + staged_paths
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)

        if res.returncode != 0:
            return {
                "status": "error",
                "imported_count": 0,
                "error": res.stderr.strip() or "addmedia command failed",
            }

        return {
            "status": "success",
            "imported_count": len(staged_paths),
            "staged_filenames": [Path(p).name for p in staged_paths[:5]],
        }


def decode_cpbitmap(path_or_bytes: Union[str, Path, bytes]) -> Any:
    """
    Decodes an Apple CoreDisplay/CoreGraphics `.cpbitmap` raw image file into a PIL RGBA Image.

    Format details:
      - Trailing metadata is located in the final 28 bytes:
        (meta_len, plist_len, width, height, flag1, flag2, magic) = struct.unpack("<7I", trailer)
      - Row stride is 64-byte aligned: `stride = (width * 4 + 63) // 64 * 64`
      - Pixels are BGRA 32-bit.
    """
    if Image is None:
        raise SimulatorBridgeError("Pillow (`PIL`) is required to decode `.cpbitmap` files.")

    if isinstance(path_or_bytes, (str, Path)):
        p = Path(path_or_bytes).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"cpbitmap file not found: {p}")
        data = p.read_bytes()
    else:
        data = path_or_bytes

    if len(data) < 28:
        raise ValueError(f"Data too small for cpbitmap ({len(data)} bytes).")

    trailer = data[-28:]
    meta_len, plist_len, width, height, flag1, flag2, magic = struct.unpack("<7I", trailer)
    if width <= 0 or height <= 0 or width > 10000 or height > 10000:
        raise ValueError(f"Invalid cpbitmap dimensions extracted from trailer: {width}x{height}")

    stride = (width * 4 + 63) // 64 * 64
    needed_bytes = stride * height
    if len(data) < needed_bytes:
        raise ValueError(f"Buffer underflow: expected {needed_bytes} bytes for {width}x{height}, found {len(data)}")

    raw_pixels = bytearray(width * height * 4)
    for y in range(height):
        row_start = y * stride
        raw_pixels[y * width * 4 : (y + 1) * width * 4] = data[row_start : row_start + width * 4]

    return Image.frombytes("RGBA", (width, height), bytes(raw_pixels), "raw", "BGRA")


def convert_heic_to_png(heic_path: Union[str, Path], png_path: Union[str, Path]) -> bool:
    """
    Converts a HEIC/HEIF image to PNG. Uses macOS native `/usr/bin/sips` if available,
    falling back to Pillow.
    """
    heic_p = Path(heic_path).expanduser().resolve()
    png_p = Path(png_path).expanduser().resolve()
    if not heic_p.is_file():
        return False

    png_p.parent.mkdir(parents=True, exist_ok=True)

    if shutil.which("sips"):
        try:
            res = subprocess.run(
                ["sips", "-s", "format", "png", str(heic_p), "--out", str(png_p)],
                capture_output=True,
                check=False,
                timeout=10,
            )
            if res.returncode == 0 and png_p.is_file() and png_p.stat().st_size > 0:
                return True
        except Exception:
            pass

    if Image is not None:
        try:
            im = Image.open(str(heic_p))
            im.save(str(png_p), "PNG")
            return png_p.is_file() and png_p.stat().st_size > 0
        except Exception:
            pass

    return False


def extract_wallpaper_from_backup(
    backup_target: Optional[str] = None,
    output_dir: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """
    Scans a local MobileSync backup and extracts all wallpaper assets:
      - SpringBoard legacy bitmaps: LockBackground.cpbitmap, HomeBackground.cpbitmap,
        OriginalLockBackground.cpbitmap
      - SpringBoard thumbnails: LockBackgroundThumbnail.jpg, HomeBackgroundThumbnail.jpg
      - PosterBoard layer assets: portrait-layer_background.HEIC, Adjusted.PNG
      - SpringBoard layout & appearance preferences: com.apple.springboard.plist

    Decodes Apple's proprietary `.cpbitmap` and HEIC files into full-resolution PNG images.
    """
    backup = find_backup_for_target(backup_target)
    if not backup:
        raise SimulatorBridgeError("No valid iOS backup found in MobileSync/Backup.")

    b_path = Path(backup.backup_path).expanduser().resolve()
    manifest_db = b_path / "Manifest.db"
    if not manifest_db.is_file():
        raise SimulatorBridgeError(f"Manifest.db not found in backup: {b_path}")

    if output_dir:
        out_path = Path(output_dir).expanduser().resolve()
    else:
        # Default to the private state dir (overridable by the caller / IOS_CLEANER_STATE_DIR),
        # never a hardcoded third-party session folder.
        out_path = state_path("wallpapers")

    out_path.mkdir(parents=True, exist_ok=True)

    try:
        conn = _ro_connect(manifest_db)
        cur = conn.cursor()
        query = """
            SELECT fileID, domain, relativePath, flags
            FROM Files
            WHERE (domain = 'HomeDomain' AND (relativePath LIKE '%SpringBoard/%Background%' OR relativePath LIKE '%SpringBoard/%Thumbnail%' OR relativePath = 'Library/Preferences/com.apple.springboard.plist'))
               OR (domain = 'AppDomain-com.apple.PosterBoard' AND (relativePath LIKE '%portrait-layer_background.HEIC%' OR relativePath LIKE '%Adjusted.PNG%'))
            ORDER BY relativePath;
        """
        cur.execute(query)
        rows = cur.fetchall()
        conn.close()
    except sqlite3.DatabaseError as e:
        return {
            "status": "error",
            "message": f"Manifest database cannot be read directly (backup is encrypted): {e}",
            "backup_dir": str(b_path),
            "wallpapers_extracted_count": 0,
            "wallpapers": [],
        }

    wallpapers: List[Dict[str, Any]] = []
    springboard_settings: Dict[str, Any] = {}

    for file_id, domain, rel_path, flags in rows:
        disk_file = b_path / file_id[:2] / file_id
        if not disk_file.is_file() or disk_file.stat().st_size == 0:
            continue

        base_name = Path(rel_path).name

        # 1. SpringBoard Preferences
        if base_name == "com.apple.springboard.plist":
            try:
                with open(disk_file, "rb") as f:
                    sb_plist = plistlib.load(f)
                springboard_settings = {
                    "dimming_enabled": sb_plist.get("SBEnableWallpaperDimming", False),
                    "lock_wallpapers": sb_plist.get("SBLockScreenWallpapers", {}),
                    "home_wallpapers": sb_plist.get("SBHomeScreenWallpapers", {}),
                    "migrated_to_posterboard": sb_plist.get("SBMigratedHomeScreenDefaultsTintToPosterBoard", False),
                    "has_lock_poster": "SBLockPoster" in sb_plist,
                }
            except Exception:
                pass
            continue

        # 2. Legacy CPBitmaps
        if base_name.endswith(".cpbitmap"):
            target_name = ""
            if "OriginalLockBackground" in base_name or "OriginalHomeBackground" in base_name:
                target_name = "Original_Wallpaper.png"
            elif "LockBackground" in base_name:
                target_name = "LockScreen_Wallpaper.png"
            elif "HomeBackground" in base_name:
                target_name = "HomeScreen_Wallpaper.png"

            if target_name and Image is not None:
                dest_png = out_path / target_name
                try:
                    img = decode_cpbitmap(disk_file)
                    img.save(str(dest_png), "PNG")
                    wallpapers.append({
                        "name": target_name,
                        "type": "bitmap",
                        "source_relative_path": rel_path,
                        "output_path": str(dest_png),
                        "width": img.width,
                        "height": img.height,
                        "size_bytes": dest_png.stat().st_size,
                    })
                except Exception as e:
                    pass

        # 3. SpringBoard Thumbnails
        elif base_name.endswith(".jpg"):
            target_name = "LockScreen_Thumbnail.jpg" if "Lock" in base_name else "HomeScreen_Thumbnail.jpg"
            dest_jpg = out_path / target_name
            try:
                shutil.copyfile(disk_file, dest_jpg)
                dims = [0, 0]
                if Image is not None:
                    try:
                        with Image.open(str(dest_jpg)) as im:
                            dims = [im.width, im.height]
                    except Exception:
                        pass
                wallpapers.append({
                    "name": target_name,
                    "type": "thumbnail",
                    "source_relative_path": rel_path,
                    "output_path": str(dest_jpg),
                    "width": dims[0],
                    "height": dims[1],
                    "size_bytes": dest_jpg.stat().st_size,
                })
            except Exception:
                pass

        # 4. PosterBoard Layer Assets (HEIC / PNG)
        elif base_name.endswith(".HEIC"):
            target_name = "PosterBoard_LayerBackground.png"
            dest_png = out_path / target_name
            if convert_heic_to_png(disk_file, dest_png):
                dims = [0, 0]
                if Image is not None:
                    try:
                        with Image.open(str(dest_png)) as im:
                            dims = [im.width, im.height]
                    except Exception:
                        pass
                wallpapers.append({
                    "name": target_name,
                    "type": "poster_heic",
                    "source_relative_path": rel_path,
                    "output_path": str(dest_png),
                    "width": dims[0],
                    "height": dims[1],
                    "size_bytes": dest_png.stat().st_size,
                })

        elif base_name == "Adjusted.PNG":
            target_name = "PosterBoard_Adjusted.png"
            dest_png = out_path / target_name
            try:
                shutil.copyfile(disk_file, dest_png)
                dims = [0, 0]
                if Image is not None:
                    try:
                        with Image.open(str(dest_png)) as im:
                            dims = [im.width, im.height]
                    except Exception:
                        pass
                wallpapers.append({
                    "name": target_name,
                    "type": "poster_adjusted",
                    "source_relative_path": rel_path,
                    "output_path": str(dest_png),
                    "width": dims[0],
                    "height": dims[1],
                    "size_bytes": dest_png.stat().st_size,
                })
            except Exception:
                pass

    return {
        "status": "success",
        "backup_udid": backup.udid,
        "device_name": backup.device_name,
        "product_type": backup.product_type,
        "output_directory": str(out_path),
        "wallpapers_extracted_count": len(wallpapers),
        "wallpapers": wallpapers,
        "springboard_settings": springboard_settings,
    }


def apply_wallpaper_to_simulator(
    simulator_udid: str,
    backup_target: Optional[str] = None,
    import_to_photos: bool = True,
    inject_springboard: bool = True,
    inject_posterboard: bool = True,
) -> Dict[str, Any]:
    """
    Applies extracted wallpaper assets and configuration directly to the target Xcode Simulator:
      1. Ingests full-resolution decoded wallpapers into the Simulator Photo Library (`addmedia`).
      2. Injects SpringBoard wallpaper bitmaps and thumbnails into `<sim_data>/Library/SpringBoard/`.
      3. Synchronizes PosterBoard data store into `<sim_data>/Library/Application Support/PRBPosterExtensionDataStore/61/`.
      4. Rewrites `com.apple.springboard.plist` (SBLockPoster, dimming, crop rects) to activate backup appearance.
    """
    backup = find_backup_for_target(backup_target)
    if not backup:
        raise SimulatorBridgeError("No valid iOS backup found in MobileSync/Backup.")

    # Step 1: Extract and decode all wallpaper assets
    extract_res = extract_wallpaper_from_backup(backup_target=backup_target)
    wallpapers = extract_res.get("wallpapers", [])

    sim_data_dir = (
        Path.home()
        / "Library"
        / "Developer"
        / "CoreSimulator"
        / "Devices"
        / simulator_udid
        / "data"
    )
    if not sim_data_dir.is_dir():
        raise SimulatorBridgeError(f"Simulator data directory not found at: {sim_data_dir}")

    # Step 2: Import decoded full wallpapers to Simulator Photo Library
    photos_imported: List[str] = []
    if import_to_photos:
        media_candidates = [
            w["output_path"]
            for w in wallpapers
            if w.get("type") in ("bitmap", "poster_heic", "poster_adjusted")
            and Path(w["output_path"]).is_file()
        ]
        if media_candidates:
            cmd = ["xcrun", "simctl", "addmedia", simulator_udid] + media_candidates
            sub_res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if sub_res.returncode == 0:
                photos_imported = [Path(p).name for p in media_candidates]

    # Step 3: Inject SpringBoard container bitmaps
    springboard_injected = False
    if inject_springboard:
        sb_dir = sim_data_dir / "Library" / "SpringBoard"
        sb_dir.mkdir(parents=True, exist_ok=True)
        b_path = Path(backup.backup_path).expanduser().resolve()
        manifest_db = b_path / "Manifest.db"

        if manifest_db.is_file():
            conn = _ro_connect(manifest_db)
            cur = conn.cursor()
            cur.execute("""
                SELECT fileID, relativePath
                FROM Files
                WHERE domain = 'HomeDomain' AND relativePath LIKE 'Library/SpringBoard/%';
            """)
            sb_files = cur.fetchall()
            conn.close()

            for file_id, rel_path in sb_files:
                src_file = b_path / file_id[:2] / file_id
                if src_file.is_file() and src_file.stat().st_size > 0:
                    dest_file = _safe_join(sim_data_dir, rel_path)
                    if dest_file is None:
                        continue  # refuse paths that escape the simulator data dir
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src_file, dest_file)
            springboard_injected = True

    # Step 4: Inject PosterBoard data store & rewrite springboard.plist
    posterboard_injected = False
    if inject_posterboard:
        b_path = Path(backup.backup_path).expanduser().resolve()
        manifest_db = b_path / "Manifest.db"

        if manifest_db.is_file():
            conn = _ro_connect(manifest_db)
            cur = conn.cursor()
            cur.execute("""
                SELECT fileID, relativePath, flags
                FROM Files
                WHERE domain = 'AppDomain-com.apple.PosterBoard';
            """)
            pb_files = cur.fetchall()

            for file_id, rel_path, flags in pb_files:
                if not rel_path:
                    continue
                target = _safe_join(sim_data_dir, rel_path)
                if target is None:
                    continue  # refuse paths that escape the simulator data dir
                if flags == 2:
                    target.mkdir(parents=True, exist_ok=True)
                elif flags == 1:
                    src_file = b_path / file_id[:2] / file_id
                    if src_file.is_file():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(src_file, target)

            # Rewrite com.apple.springboard.plist
            cur.execute("""
                SELECT fileID
                FROM Files
                WHERE domain = 'HomeDomain' AND relativePath = 'Library/Preferences/com.apple.springboard.plist';
            """)
            sb_plist_row = cur.fetchone()
            conn.close()

            if sb_plist_row:
                sb_src = b_path / sb_plist_row[0][:2] / sb_plist_row[0]
                if sb_src.is_file():
                    try:
                        with open(sb_src, "rb") as f:
                            backup_sb = plistlib.load(f)

                        sim_sb_path = sim_data_dir / "Library" / "Preferences" / "com.apple.springboard.plist"
                        sim_sb: Dict[str, Any] = {}
                        if sim_sb_path.is_file():
                            try:
                                with open(sim_sb_path, "rb") as f:
                                    sim_sb = plistlib.load(f)
                            except Exception:
                                sim_sb = {}

                        # Synchronize wallpaper properties
                        if "SBLockScreenWallpapers" in backup_sb:
                            sim_sb["SBLockScreenWallpapers"] = backup_sb["SBLockScreenWallpapers"]
                        if "SBHomeScreenWallpapers" in backup_sb:
                            sim_sb["SBHomeScreenWallpapers"] = backup_sb["SBHomeScreenWallpapers"]
                        if "SBEnableWallpaperDimming" in backup_sb:
                            sim_sb["SBEnableWallpaperDimming"] = backup_sb["SBEnableWallpaperDimming"]
                        if "SBMigratedHomeScreenDefaultsTintToPosterBoard" in backup_sb:
                            sim_sb["SBMigratedHomeScreenDefaultsTintToPosterBoard"] = backup_sb["SBMigratedHomeScreenDefaultsTintToPosterBoard"]

                        # Rewrite SBLockPoster URL to simulator path
                        if "SBLockPoster" in backup_sb:
                            try:
                                poster_bytes = backup_sb["SBLockPoster"]
                                decoded = plistlib.loads(poster_bytes)
                                objs = decoded.get("$objects", [])
                                for idx, obj in enumerate(objs):
                                    if isinstance(obj, str) and "file://" in obj and "PRBPosterExtensionDataStore" in obj:
                                        # Replace iOS container path with simulator data path
                                        rel_subpath = obj.split("PRBPosterExtensionDataStore", 1)[-1]
                                        new_url = f"file://{sim_data_dir}/Library/Application%20Support/PRBPosterExtensionDataStore{rel_subpath}"
                                        objs[idx] = new_url
                                sim_sb["SBLockPoster"] = plistlib.dumps(decoded, fmt=plistlib.FMT_BINARY)
                            except Exception:
                                pass

                        sim_sb_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(sim_sb_path, "wb") as f:
                            plistlib.dump(sim_sb, f, fmt=plistlib.FMT_BINARY)

                        posterboard_injected = True
                    except Exception:
                        pass

    return {
        "status": "success",
        "simulator_udid": simulator_udid,
        "backup_udid": backup.udid,
        "wallpapers_extracted_count": len(wallpapers),
        "photos_imported_count": len(photos_imported),
        "photos_imported": photos_imported,
        "springboard_injected": springboard_injected,
        "posterboard_injected": posterboard_injected,
        "output_directory": extract_res.get("output_directory"),
    }


def send_backup_to_simulator(
    backup_target: Optional[str] = None,
    simulator_target: Optional[str] = None,
    include_media: bool = True,
    include_wallpaper: bool = True,
    max_media_items: int = 25,
    open_gui: bool = True,
    sync_posture: bool = True,
) -> Dict[str, Any]:
    """
    End-to-End Orchestrator: Sends local backup assets and configuration into Xcode Simulator.
      1. Resolves MobileSync backup metadata.
      2. Boots target Simulator (or matching device type).
      3. Synchronizes device name and status bar.
      4. Ingests backup photos/screenshots into Simulator Photo Library.
      5. Extracts, decodes, and synchronizes backup wallpaper into Simulator SpringBoard/Photos.
      6. Launches Simulator application window on macOS desktop.
    """
    backup = find_backup_for_target(backup_target)
    if not backup:
        return {"error": "No valid iOS backup found in MobileSync/Backup."}

    # Encrypted backups store Manifest.db and app containers encrypted, so nothing can be
    # read out of them directly. Say so instead of silently importing nothing.
    if _is_encrypted_backup(Path(backup.backup_path)):
        return {
            "status": "encrypted_backup",
            "error": "This backup is encrypted; its databases and app containers cannot be read "
                     "directly. Point --backup at a decrypted copy (e.g. `mvt-ios decrypt-backup`).",
            "backup_udid": backup.udid,
        }

    # Step 1: Boot or find simulator
    boot_info = boot_simulator(target=simulator_target, open_app=open_gui)
    sim_udid = boot_info["udid"]
    sim_name = boot_info["name"]

    # Step 2: Posture and appearance sync
    if sync_posture:
        try:
            # Override status bar to clean presentation state (100% battery, 9:41 AM)
            subprocess.run(
                [
                    "xcrun", "simctl", "status_bar", sim_udid, "override",
                    "--time", "9:41",
                    "--batteryState", "charged",
                    "--batteryLevel", "100",
                    "--cellularMode", "active",
                    "--cellularBars", "4",
                ],
                capture_output=True,
                check=False,
            )
        except Exception:
            pass

    # Step 3: Extract and import media from backup
    media_res: Dict[str, Any] = {"imported_count": 0, "status": "skipped"}
    if include_media:
        items = extract_media_from_backup(backup.backup_path, max_items=max_media_items)
        media_res = import_media_to_simulator(sim_udid, items)

    # Step 4: Extract and apply wallpaper from backup
    wallpaper_res: Dict[str, Any] = {"status": "skipped"}
    if include_wallpaper:
        try:
            wallpaper_res = apply_wallpaper_to_simulator(
                simulator_udid=sim_udid,
                backup_target=backup_target,
                import_to_photos=True,
                inject_springboard=True,
                inject_posterboard=True,
            )
        except Exception as e:
            wallpaper_res = {"status": "error", "error": str(e)}

    report = {
        "status": "completed",
        "action": "send_backup_to_xcode_simulator",
        "backup_source": {
            "device_name": backup.device_name,
            "product_name": backup.product_name,
            "product_type": backup.product_type,
            "ios_version": backup.product_version,
            "udid": backup.udid,
            "size_gb": backup.size_gb,
        },
        "target_simulator": {
            "udid": sim_udid,
            "name": sim_name,
            "runtime": boot_info.get("runtime"),
            "gui_opened": open_gui,
        },
        "media_transfer": media_res,
        "wallpaper_sync": wallpaper_res,
        "instructions": (
            "The Xcode Simulator is now active with assets and wallpaper from your iPhone backup. "
            "You can view photos and wallpapers in Photos app, or verify SpringBoard appearance."
        ),
    }

    return report


def open_simulator_url(
    simulator_target: Optional[str] = None,
    url: str = "https://example.com",
) -> Dict[str, Any]:
    """Opens a URL in Mobile Safari inside the Simulator."""
    boot_res = boot_simulator(simulator_target, open_app=True)
    sim_udid = boot_res["udid"]

    res = subprocess.run(
        ["xcrun", "simctl", "openurl", sim_udid, url],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "status": "opened" if res.returncode == 0 else "error",
        "simulator": sim_udid,
        "url": url,
        "error": res.stderr.strip() if res.returncode != 0 else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Xcode Simulator Bridge for iPhone Revival & Backup Transfer"
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # list
    p_list = subparsers.add_parser("list", help="List available Xcode iOS Simulators.")
    p_list.add_argument("--booted", action="store_true", help="List only booted simulators.")
    p_list.add_argument("--all", action="store_true", help="Include unavailable devices.")

    # boot
    p_boot = subparsers.add_parser("boot", help="Boot a simulator and open Simulator app.")
    p_boot.add_argument("device", nargs="?", help="UDID or simulator name.")
    p_boot.add_argument("--no-gui", action="store_true", help="Do not launch Simulator macOS window.")

    # shutdown
    p_shut = subparsers.add_parser("shutdown", help="Shut down a simulator.")
    p_shut.add_argument("device", nargs="?", help="UDID or simulator name.")

    # send
    p_send = subparsers.add_parser("send", help="Send backup assets & posture into simulator.")
    p_send.add_argument("--backup", help="Specific backup UDID or folder path.")
    p_send.add_argument("--device", help="Target simulator UDID or name.")
    p_send.add_argument("--max-media", type=int, default=25, help="Max photos/media to import.")
    p_send.add_argument("--no-gui", action="store_true", help="Run headless without opening window.")

    # clone
    p_clone = subparsers.add_parser("clone", help="Provision and launch an exact simulator replica of the backup.")
    p_clone.add_argument("--backup", help="Specific backup UDID or folder path.")
    p_clone.add_argument("--max-media", type=int, default=25, help="Max photos/media to import.")
    p_clone.add_argument("--no-photos", action="store_true", help="Do not automatically launch Photos app.")

    # openurl
    p_url = subparsers.add_parser("openurl", help="Open a URL inside Simulator Safari.")
    p_url.add_argument("url", help="URL to open.")
    p_url.add_argument("--device", help="Simulator UDID or name.")

    # wallpaper
    p_wall = subparsers.add_parser("wallpaper", help="Extract and apply backup wallpaper to simulator.")
    p_wall.add_argument("--backup", help="Specific backup UDID or folder path.")
    p_wall.add_argument("--device", help="Simulator UDID or name.")
    p_wall.add_argument("--extract-only", help="Extract decoded wallpapers to directory without simulator injection.")
    p_wall.add_argument("--no-photos", action="store_true", help="Do not import wallpapers into Simulator Photo Library.")
    p_wall.add_argument("--no-inject", action="store_true", help="Do not inject SpringBoard/PosterBoard containers.")

    args = parser.parse_args()

    if args.subcommand == "list":
        sims = list_simulators(
            only_available=not args.all,
            booted_only=args.booted,
            compact=True,
        )
        print(json.dumps(sims, indent=2))
        return 0

    elif args.subcommand == "boot":
        res = boot_simulator(args.device, open_app=not args.no_gui)
        print(json.dumps(res, indent=2))
        return 0

    elif args.subcommand == "shutdown":
        res = shutdown_simulator(args.device)
        print(json.dumps(res, indent=2))
        return 0

    elif args.subcommand == "send":
        res = send_backup_to_simulator(
            backup_target=args.backup,
            simulator_target=args.device,
            max_media_items=args.max_media,
            open_gui=not args.no_gui,
        )
        print(json.dumps(res, indent=2))
        return 0

    elif args.subcommand == "clone":
        res = launch_backup_copy_simulator(
            backup_target=args.backup,
            max_media_items=args.max_media,
            open_photos=not args.no_photos,
        )
        print(json.dumps(res, indent=2))
        return 0

    elif args.subcommand == "openurl":
        res = open_simulator_url(args.device, args.url)
        print(json.dumps(res, indent=2))
        return 0

    elif args.subcommand == "wallpaper":
        if args.extract_only:
            res = extract_wallpaper_from_backup(
                backup_target=args.backup,
                output_dir=args.extract_only,
            )
        else:
            boot_res = boot_simulator(args.device, open_app=True)
            res = apply_wallpaper_to_simulator(
                simulator_udid=boot_res["udid"],
                backup_target=args.backup,
                import_to_photos=not args.no_photos,
                inject_springboard=not args.no_inject,
                inject_posterboard=not args.no_inject,
            )
        print(json.dumps(res, indent=2))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
