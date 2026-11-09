"""
Synthetic Test Backup Fixture Generator (`fixtures.py`).

Generates an isolated, fully valid synthetic iOS MobileSync backup directory with
`Info.plist`, `Manifest.plist`, `Status.plist`, `Manifest.db`, and synthetic wallpaper assets.
Ensures unit tests, MCP selftests, and Windows environments can verify the full restoration
and inspection pipeline without requiring a physical iPhone or pre-existing local backup.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

GENRES = ["Business", "Finance", "Shopping", "Productivity", "Utilities", "Social Networking", "Entertainment"]


def make_synthetic_keybag() -> bytes:
    """Generates a valid Apple BackupKeyBag binary payload with 4 classes and 10,000 iterations."""
    buf = bytearray()
    buf += b"TYPE" + struct.pack(">I", 4) + struct.pack(">I", 1)
    buf += b"UUID" + struct.pack(">I", 16) + b"0123456789abcdef"
    buf += b"WRAP" + struct.pack(">I", 4) + struct.pack(">I", 1)
    buf += b"ITER" + struct.pack(">I", 4) + struct.pack(">I", 10000)
    buf += b"SALT" + struct.pack(">I", 32) + (b"\xaa" * 32)
    for c in [1, 2, 3, 4]:
        buf += b"CLAS" + struct.pack(">I", 4) + struct.pack(">I", c)
        buf += b"WPKY" + struct.pack(">I", 40) + (b"\xbb" * 40)
    return bytes(buf)


def make_synthetic_cpbitmap(width: int = 32, height: int = 32) -> bytes:
    """Generates valid raw cpbitmap bytes."""
    stride = (width * 4 + 63) // 64 * 64
    pixel_bytes = bytearray(stride * height)
    for y in range(height):
        for x in range(width):
            offset = y * stride + x * 4
            pixel_bytes[offset : offset + 4] = b"\x00\x80\xff\xff"
    trailer = struct.pack("<7I", 0, 0, width, height, 1, 1, 0xdcb543a2)
    return bytes(pixel_bytes) + trailer


def create_synthetic_backup(target_dir: Path) -> Path:
    """Creates a complete synthetic iPhone 12 mini backup with 81 apps and wallpaper."""
    backup_path = target_dir / "00008101-001234567890ABCD"
    backup_path.mkdir(parents=True, exist_ok=True)

    # 1. Applications Dictionary (81 apps)
    apps_dict = {}

    # Crucial known apps
    duo_meta = {
        "itemName": "Duo Mobile",
        "bundleDisplayName": "Duo Mobile",
        "bundleShortVersionString": "4.65.0",
        "bundleVersion": "46500",
        "genre": "Business",
        "artistName": "Cisco Duo",
        "itemId": 422663827,
        "purchaseDate": "2024-01-01T00:00:00Z",
    }
    apps_dict["com.duosecurity.DuoMobile"] = {"iTunesMetadata": plistlib.dumps(duo_meta)}

    chase_meta = {
        "itemName": "Chase Mobile",
        "bundleDisplayName": "Chase",
        "bundleShortVersionString": "10.0",
        "genre": "Finance",
        "itemId": 298867224,
    }
    apps_dict["com.chase"] = {"iTunesMetadata": plistlib.dumps(chase_meta)}

    amazon_meta = {
        "itemName": "Amazon Shopping",
        "bundleDisplayName": "Amazon",
        "bundleShortVersionString": "22.0",
        "genre": "Shopping",
        "itemId": 297606951,
    }
    apps_dict["com.amazon.Amazon"] = {"iTunesMetadata": plistlib.dumps(amazon_meta)}

    # Generate remaining apps to reach exactly 81 apps
    for i in range(len(apps_dict), 81):
        bundle_id = f"com.example.synthetic.app{i}"
        genre = GENRES[i % len(GENRES)]
        meta = {
            "itemName": f"App {i}",
            "bundleDisplayName": f"App {i}",
            "bundleShortVersionString": "1.0",
            "genre": genre,
            "itemId": 1000000 + i,
        }
        apps_dict[bundle_id] = {"iTunesMetadata": plistlib.dumps(meta)}

    # 2. Info.plist
    info_data = {
        "Device Name": "User’s iPhone 12 mini",
        "Product Name": "iPhone 12 mini",
        "Product Type": "iPhone13,1",
        "Product Version": "18.1",
        "Build Version": "22B83",
        "Serial Number": "SYNTHSERIAL01",
        "Unique Identifier": "00008101-001234567890ABCD",
        "Target Identifier": "00008101-001234567890ABCD",
        "Last Backup Date": datetime.now(timezone.utc),
        "Applications": apps_dict,
        "Installed Applications": list(apps_dict.keys()),
    }
    with (backup_path / "Info.plist").open("wb") as f:
        plistlib.dump(info_data, f)

    # 3. Manifest.plist
    manifest_data = {
        "IsEncrypted": True,
        "WasPasscodeSet": True,
        "BackupKeyBag": make_synthetic_keybag(),
    }
    with (backup_path / "Manifest.plist").open("wb") as f:
        plistlib.dump(manifest_data, f)

    # 4. Status.plist
    status_data = {
        "SnapshotState": "finished",
        "IsFullBackup": True,
        "Version": "10.0",
        "Date": datetime.now(timezone.utc),
    }
    with (backup_path / "Status.plist").open("wb") as f:
        plistlib.dump(status_data, f)

    # 5. Manifest.db, Wallpaper, Keychain, and Auth App Containers
    manifest_db_path = backup_path / "Manifest.db"
    conn = sqlite3.connect(str(manifest_db_path))
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB);"
    )

    lock_file_id = "abcd1234ef567890abcd1234ef567890abcd1234"
    cur.execute(
        "INSERT INTO Files (fileID, domain, relativePath, flags) VALUES (?, ?, ?, ?);",
        (lock_file_id, "HomeDomain", "Library/SpringBoard/LockBackground.cpbitmap", 1),
    )

    # Keychain artifact
    keychain_file_id = hashlib.sha1(b"KeychainDomain-keychain-backup.plist").hexdigest()
    cur.execute(
        "INSERT INTO Files (fileID, domain, relativePath, flags) VALUES (?, ?, ?, ?);",
        (keychain_file_id, "KeychainDomain", "keychain-backup.plist", 1),
    )

    # Duo Mobile preferences
    duo_rel = "Library/Preferences/com.duosecurity.DuoMobile.plist"
    duo_file_id = hashlib.sha1(f"AppDomain-com.duosecurity.DuoMobile-{duo_rel}".encode("utf-8")).hexdigest()
    cur.execute(
        "INSERT INTO Files (fileID, domain, relativePath, flags) VALUES (?, ?, ?, ?);",
        (duo_file_id, "AppDomain-com.duosecurity.DuoMobile", duo_rel, 1),
    )

    # Google Authenticator sqlite
    google_rel = "Documents/google_authenticator.sqlite"
    google_file_id = hashlib.sha1(f"AppDomain-com.google.Authenticator-{google_rel}".encode("utf-8")).hexdigest()
    cur.execute(
        "INSERT INTO Files (fileID, domain, relativePath, flags) VALUES (?, ?, ?, ?);",
        (google_file_id, "AppDomain-com.google.Authenticator", google_rel, 1),
    )

    conn.commit()
    conn.close()

    # Stage the synthetic cpbitmap file at <fileID[:2]>/<fileID>
    blob_dir = backup_path / lock_file_id[:2]
    blob_dir.mkdir(parents=True, exist_ok=True)
    (blob_dir / lock_file_id).write_bytes(make_synthetic_cpbitmap(64, 64))

    # Stage synthetic Keychain blob (2048 bytes)
    kc_dir = backup_path / keychain_file_id[:2]
    kc_dir.mkdir(parents=True, exist_ok=True)
    (kc_dir / keychain_file_id).write_bytes(b"\x00" * 512 + b"\xff" * 512 + b"KEYCHAIN-SEALED-STORE" * 48)

    # Stage synthetic Duo Mobile plist
    duo_plist = {
        "accounts": [
            {
                "name": "Example Organization",
                "host": "api-00000000.duosecurity.com",
                "user": "testuser",
                "pushEnabled": True,
                "keyId": "SYNTHKEY0000",
            }
        ],
        "third_party_accounts": [
            {
                "name": "Example Service (2FA)",
                "issuer": "Example Service",
                "user": "testuser",
                # RFC 6238 canonical test vector (not a real secret).
                "secret": "JBSWY3DPEHPK3PXP",
            }
        ],
    }
    duo_dir = backup_path / duo_file_id[:2]
    duo_dir.mkdir(parents=True, exist_ok=True)
    with (duo_dir / duo_file_id).open("wb") as f:
        plistlib.dump(duo_plist, f)

    # Stage synthetic Google Authenticator sqlite database
    google_dir = backup_path / google_file_id[:2]
    google_dir.mkdir(parents=True, exist_ok=True)
    g_db_path = google_dir / google_file_id
    g_conn = sqlite3.connect(str(g_db_path))
    g_cur = g_conn.cursor()
    g_cur.execute(
        "CREATE TABLE accounts (id INTEGER PRIMARY KEY, email TEXT, secret TEXT, counter INTEGER, type INTEGER, provider TEXT, issuer TEXT, original_name TEXT);"
    )
    g_cur.execute(
        "INSERT INTO accounts (email, secret, counter, type, provider, issuer, original_name) VALUES (?, ?, ?, ?, ?, ?, ?);",
        ("testuser@example.com", "GEZDGNBVGY3TQOJQ", 0, 1, "Google", "Google", "testuser@example.com"),
    )
    g_conn.commit()
    g_conn.close()

    return backup_path
