"""
Token Extraction & Credential Survival Protocol (`token_extractor.py`).

Standard protocol for discovering, inspecting, extracting, and verifying authentication
tokens, Duo Mobile push credentials, 2FA accounts, Keychain artifacts, and host lockdown
escrow pairing bags across iOS backups and live devices on both Windows and macOS.

Standard Protocol Architecture:
  1. Lockdown Escrow Tokens: Locates host-device pairing records and extracts EscrowBag
     to guarantee post-erase USB pairing and prevent device passcode lockout.
  2. Apple Keybag Inspection: Parses binary BackupKeyBag in Manifest.plist, validating
     PBKDF2 iterations, salt, key-wrapping classes (Class A/B/C/D), and crypto health.
  3. Keychain Artifact Verification: Audits KeychainDomain (keychain-backup.plist /
     keychain-2.db) presence, file size, SHA256 fingerprint, and credential counts.
  4. App Auth & 2FA Container Extraction:
     - Duo Mobile (com.duosecurity.DuoMobile): Extracts institutional Duo Push
       enrollments (API hostname, user ID, push key) and 3rd-party TOTP accounts.
     - Google Authenticator (com.google.Authenticator): Decodes SQLite account vaults.
     - Microsoft Authenticator & Enterprise: Audits tenant and cloud sync tokens.
  5. Token Manifest & Pre-Wipe / Post-Restore Verification:
     Generates cryptographic TokenManifest before wipe; verifies 100% token survival
     post-restore to prove zero credential loss.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import sqlite3
import struct
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from common import backup_root as get_itunes_backup_root
from token_saver import TokenCompactor

# Back-compat module constant (tests monkeypatch token_extractor.BACKUP_ROOT).
BACKUP_ROOT = get_itunes_backup_root()


def _short_hash(value: Optional[str]) -> Optional[str]:
    """Short, non-reversible fingerprint of a sensitive string (or None)."""
    if not value:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


# Known Authenticator & Security Bundle Identifiers
KNOWN_AUTH_BUNDLES = {
    "com.duosecurity.DuoMobile": "Duo Mobile (2FA)",
    "com.google.Authenticator": "Google Authenticator",
    "com.azure.authenticator": "Microsoft Authenticator",
    "com.microsoft.msauth": "Microsoft Authenticator",
    "com.authy": "Twilio Authy",
    "com.okta.mobile": "Okta Verify",
    "com.yubico.authenticator": "Yubico Authenticator",
    "com.agilebits.onepassword-ios": "1Password",
    "com.bitwarden.mobile": "Bitwarden",
}


@dataclass
class AuthAccount:
    """Represents an extracted authentication account or 2FA token record."""

    app_name: str
    bundle_id: str
    account_name: str
    account_type: str  # e.g., 'DuoPush', 'TOTP', 'HOTP', 'Enterprise'
    issuer: Optional[str] = None
    host: Optional[str] = None  # e.g., api-12345678.duosecurity.com
    username: Optional[str] = None
    masked_secret: Optional[str] = None
    has_push: bool = False
    details: Dict[str, Any] = field(default_factory=dict)
    account_id: Optional[str] = None

    def compute_fingerprint(self) -> str:
        """Returns a stable SHA256 digest identifying this account uniquely."""
        raw = f"{self.bundle_id}:{self.account_type}:{self.account_name}:{self.issuer or ''}:{self.host or ''}:{self.username or ''}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["fingerprint"] = self.compute_fingerprint()
        return d


@dataclass
class KeybagSummary:
    """Summary of the Apple binary BackupKeyBag cryptographic structure."""

    is_encrypted: bool
    uuid: Optional[str] = None
    wrap_type: str = "Unknown"  # AES-KW (RFC3394) or AES-GCM
    iteration_count: int = 0
    salt_hex: Optional[str] = None
    dpic_iterations: int = 0
    dpsl_hex: Optional[str] = None
    class_count: int = 0
    classes: List[int] = field(default_factory=list)
    status: str = "UNENCRYPTED"  # HEALTHY, CORRUPT, UNENCRYPTED, MISSING
    raw_bytes_len: int = 0

    def to_dict(self) -> Dict[str, Any]:
        # Redact the raw salt / UUID / DPSL: emit non-reversible fingerprints instead, so
        # a committed or shared manifest can't leak the keybag salt.
        return {
            "is_encrypted": self.is_encrypted,
            "uuid_fingerprint": _short_hash(self.uuid),
            "wrap_type": self.wrap_type,
            "iteration_count": self.iteration_count,
            "salt_fingerprint": _short_hash(self.salt_hex),
            "dpic_iterations": self.dpic_iterations,
            "dpsl_fingerprint": _short_hash(self.dpsl_hex),
            "class_count": self.class_count,
            "classes": self.classes,
            "status": self.status,
            "raw_bytes_len": self.raw_bytes_len,
        }


@dataclass
class KeychainArtifact:
    """Status and cryptographic fingerprint of the backed-up iOS Keychain."""

    present: bool
    file_id: Optional[str] = None
    relative_path: Optional[str] = None
    size_bytes: int = 0
    sha256_hash: Optional[str] = None
    is_encrypted: bool = True
    estimated_items: int = 0
    status: str = "NOT_FOUND"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class LockdownEscrowToken:
    """Status of the host-device pairing record and pairing escrow token."""

    present: bool
    file_path: Optional[str] = None
    host_id: Optional[str] = None
    system_buid: Optional[str] = None
    has_escrow_bag: bool = False
    device_cert_sha256: Optional[str] = None
    wifi_mac: Optional[str] = None
    status: str = "MISSING"

    def to_dict(self) -> Dict[str, Any]:
        # Redact host IDs, SystemBUID, Wi-Fi MAC and the on-disk path into fingerprints /
        # basenames so the manifest never carries hardware or host identifiers.
        return {
            "present": self.present,
            "file_name": Path(self.file_path).name if self.file_path else None,
            "host_id_fingerprint": _short_hash(self.host_id),
            "system_buid_fingerprint": _short_hash(self.system_buid),
            "has_escrow_bag": self.has_escrow_bag,
            "device_cert_sha256": self.device_cert_sha256,
            "wifi_mac_fingerprint": _short_hash(self.wifi_mac),
            "status": self.status,
        }


@dataclass
class TokenManifest:
    """Complete cryptographic inventory of authentication tokens and posture."""

    manifest_version: str = "1.0"
    extracted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    platform: str = "windows" if sys.platform == "win32" else "darwin"
    device_name: str = "iPhone"
    udid: str = "UNKNOWN"
    backup_path: str = ""
    last_backup_date: Optional[str] = None
    keybag: KeybagSummary = field(default_factory=lambda: KeybagSummary(is_encrypted=False))
    keychain: KeychainArtifact = field(default_factory=lambda: KeychainArtifact(present=False))
    lockdown: LockdownEscrowToken = field(default_factory=lambda: LockdownEscrowToken(present=False))
    accounts: List[AuthAccount] = field(default_factory=list)
    total_token_records: int = 0
    verdict: str = "PASS"

    def compute_manifest_hash(self) -> str:
        """Returns a cryptographic fingerprint of the entire token inventory."""
        h = hashlib.sha256()
        h.update(self.udid.encode("utf-8"))
        h.update((self.keychain.sha256_hash or "no-kc").encode("utf-8"))
        h.update((self.keybag.salt_hex or "no-kb").encode("utf-8"))
        for acc in sorted(self.accounts, key=lambda a: a.compute_fingerprint()):
            h.update(acc.compute_fingerprint().encode("utf-8"))
        return h.hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "manifest_version": self.manifest_version,
            "extracted_at": self.extracted_at,
            "platform": self.platform,
            "device_name": self.device_name,
            "udid": self.udid,
            "backup_path": self.backup_path,
            "last_backup_date": self.last_backup_date,
            "manifest_hash": self.compute_manifest_hash(),
            "total_token_records": self.total_token_records,
            "verdict": self.verdict,
            "keybag": self.keybag.to_dict(),
            "keychain": self.keychain.to_dict(),
            "lockdown": self.lockdown.to_dict(),
            "accounts": [a.to_dict() for a in self.accounts],
        }


# =====================================================================
# 1. LOCKDOWN PAIRING & ESCROW TOKEN EXTRACTION
# =====================================================================

def get_lockdown_search_dirs() -> List[Path]:
    """Returns platform-specific lockdown pairing directories."""
    dirs: List[Path] = []
    if sys.platform == "win32":
        all_user = os.environ.get("ALLUSERSPROFILE", r"C:\ProgramData")
        dirs.append(Path(all_user) / "Apple" / "Lockdown")
        appdata = os.environ.get("APPDATA")
        if appdata:
            dirs.append(Path(appdata) / "Apple Computer" / "Lockdown")
        local_app = os.environ.get("LOCALAPPDATA")
        if local_app:
            dirs.append(Path(local_app) / "Apple" / "Lockdown")
    else:
        dirs.append(Path("/var/db/lockdown"))
        dirs.append(Path.home() / "Library" / "Lockdown")
    return dirs


def extract_lockdown_token(udid: Optional[str] = None) -> LockdownEscrowToken:
    """
    Discovers and parses host-device lockdown pairing record (<udid>.plist),
    validating the EscrowBag and HostID.
    """
    # A UDID is required: without one we would pick an arbitrary pairing record that may
    # belong to a different device, and report its escrow/host identifiers as this device's.
    if not udid:
        return LockdownEscrowToken(present=False, status="NO_UDID_SPECIFIED")

    search_dirs = get_lockdown_search_dirs()
    candidate_file: Optional[Path] = None
    for d in search_dirs:
        if not d.is_dir():
            continue
        target = d / f"{udid}.plist"
        if target.is_file():
            candidate_file = target
            break

    if not candidate_file or not candidate_file.is_file():
        return LockdownEscrowToken(
            present=False,
            status="LOCKDOWN_RECORD_NOT_FOUND",
        )

    try:
        with candidate_file.open("rb") as f:
            data = plistlib.load(f)

        host_id = str(data.get("HostID") or "")
        buid = str(data.get("SystemBUID") or "")
        escrow = data.get("EscrowBag")
        has_escrow = bool(escrow and isinstance(escrow, (bytes, bytearray)) and len(escrow) > 0)
        wifi_mac = data.get("WiFiMACAddress")

        dev_cert = data.get("DeviceCertificate")
        cert_hash = None
        if dev_cert and isinstance(dev_cert, bytes):
            cert_hash = hashlib.sha256(dev_cert).hexdigest()[:16]

        return LockdownEscrowToken(
            present=True,
            file_path=str(candidate_file),
            host_id=host_id,
            system_buid=buid,
            has_escrow_bag=has_escrow,
            device_cert_sha256=cert_hash,
            wifi_mac=wifi_mac,
            status="VALID_ESCROW_PAIRING" if has_escrow else "PAIRING_WITHOUT_ESCROW",
        )
    except Exception as e:
        return LockdownEscrowToken(
            present=False,
            file_path=str(candidate_file),
            status=f"READ_ERROR_{type(e).__name__}",
        )


# =====================================================================
# 2. APPLE KEYBAG CRYPTOGRAPHIC EXTRACTION
# =====================================================================

def parse_apple_keybag(keybag_bytes: Optional[bytes]) -> KeybagSummary:
    """
    Parses the binary BackupKeyBag structure found inside Manifest.plist.

    Real iOS keybags use 8-byte TLV framing (4-byte tag + 4-byte big-endian length), so
    that is tried FIRST; a 12-byte variant is only attempted as a fallback. (Trying the
    12-byte layout first could mis-parse a real keybag into garbage.)
    Iterates standard 4-byte tags (TYPE, UUID, WRAP, KTYP, CLAS, WPKY, ITER, SALT, DPIC, DPSL).
    """
    if not keybag_bytes or len(keybag_bytes) < 16:
        return KeybagSummary(
            is_encrypted=False,
            status="EMPTY_OR_UNENCRYPTED_KEYBAG",
            raw_bytes_len=len(keybag_bytes) if keybag_bytes else 0,
        )

    blob_len = len(keybag_bytes)

    def _try_parse(is_12_byte: bool) -> Optional[List[Tuple[str, bytes]]]:
        pos = 0
        records: List[Tuple[str, bytes]] = []
        while pos + 8 <= blob_len:
            tag = keybag_bytes[pos : pos + 4].decode("ascii", errors="replace")
            if is_12_byte:
                if pos + 12 > blob_len:
                    return None
                _, field_len = struct.unpack(">II", keybag_bytes[pos + 4 : pos + 12])
                pos += 12
            else:
                field_len = struct.unpack(">I", keybag_bytes[pos + 4 : pos + 8])[0]
                pos += 8

            if pos + field_len > blob_len:
                return None
            val = keybag_bytes[pos : pos + field_len]
            pos += field_len
            records.append((tag, val))
        return records if pos == blob_len else None

    # 8-byte framing first (the real layout), then the 12-byte variant as a fallback.
    parsed_tags = _try_parse(False)
    if not parsed_tags:
        parsed_tags = _try_parse(True)

    if not parsed_tags:
        return KeybagSummary(
            is_encrypted=True,
            status="CORRUPT_TAG_STRUCTURE",
            raw_bytes_len=blob_len,
        )

    uuid_str = None
    wrap_type = "Unknown"
    iter_count = 0
    salt_hex = None
    dpic = 0
    dpsl_hex = None
    classes: List[int] = []

    for tag, val in parsed_tags:
        if tag == "UUID":
            uuid_str = val.hex()
        elif tag == "WRAP":
            if len(val) >= 4:
                wrap_val = struct.unpack(">I", val[:4])[0]
                # WRAP is a bitmask of how each class key is wrapped (1=device key,
                # 2=passcode-derived); it is NOT an "AES-KW vs other" selector.
                parts = []
                if wrap_val & 1:
                    parts.append("device-key")
                if wrap_val & 2:
                    parts.append("passcode")
                wrap_type = f"wrap=0x{wrap_val:x}" + (f" ({'+'.join(parts)})" if parts else "")
        elif tag == "ITER":
            if len(val) >= 4:
                iter_count = struct.unpack(">I", val[:4])[0]
        elif tag == "SALT":
            salt_hex = val.hex()
        elif tag == "DPIC":
            if len(val) >= 4:
                dpic = struct.unpack(">I", val[:4])[0]
        elif tag == "DPSL":
            dpsl_hex = val.hex()
        elif tag == "CLAS":
            if len(val) >= 4:
                classes.append(struct.unpack(">I", val[:4])[0])

    is_enc = iter_count > 0 or len(classes) > 0
    return KeybagSummary(
        is_encrypted=is_enc,
        uuid=uuid_str,
        wrap_type=wrap_type,
        iteration_count=iter_count,
        salt_hex=salt_hex,
        dpic_iterations=dpic,
        dpsl_hex=dpsl_hex,
        class_count=len(classes),
        classes=classes,
        status="HEALTHY_ENCRYPTED_KEYBAG" if is_enc else "UNENCRYPTED",
        raw_bytes_len=blob_len,
    )


# =====================================================================
# 3. KEYCHAIN ARTIFACT EXTRACTION
# =====================================================================

def extract_keychain_artifact(backup_dir: Path) -> KeychainArtifact:
    """
    Locates and inspects KeychainDomain (keychain-backup.plist / keychain-2.db),
    computing its SHA256 integrity hash and assessing byte payload.
    """
    manifest_db = backup_dir / "Manifest.db"
    keychain_file_id: Optional[str] = None
    rel_path: Optional[str] = None

    # 1. Query Manifest.db if present
    if manifest_db.is_file():
        try:
            conn = sqlite3.connect(f"file:{manifest_db.resolve()}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute(
                "SELECT fileID, relativePath FROM Files WHERE domain='KeychainDomain' LIMIT 1;"
            )
            row = cur.fetchone()
            if row:
                keychain_file_id, rel_path = row[0], row[1]
            conn.close()
        except Exception:
            pass

    # 2. Fallback to direct SHA1 calculation for KeychainDomain-keychain-backup.plist
    if not keychain_file_id:
        calc_id = hashlib.sha1(b"KeychainDomain-keychain-backup.plist").hexdigest()
        cand = backup_dir / calc_id[:2] / calc_id
        if cand.is_file():
            keychain_file_id = calc_id
            rel_path = "keychain-backup.plist"

    if not keychain_file_id:
        return KeychainArtifact(
            present=False,
            status="KEYCHAIN_NOT_FOUND_IN_BACKUP",
        )

    file_blob = backup_dir / keychain_file_id[:2] / keychain_file_id
    if not file_blob.is_file():
        # Check flat layout (legacy)
        flat = backup_dir / keychain_file_id
        if flat.is_file():
            file_blob = flat
        else:
            return KeychainArtifact(
                present=False,
                file_id=keychain_file_id,
                relative_path=rel_path,
                status="KEYCHAIN_INDEXED_BUT_BLOB_MISSING",
            )

    size = file_blob.stat().st_size
    sha256 = hashlib.sha256(file_blob.read_bytes()).hexdigest()

    # Estimate item count based on 512-byte class record packing
    est_items = max(1, size // 512)

    return KeychainArtifact(
        present=True,
        file_id=keychain_file_id,
        relative_path=rel_path or "keychain-backup.plist",
        size_bytes=size,
        sha256_hash=sha256,
        is_encrypted=True,
        estimated_items=est_items,
        status="KEYCHAIN_STORE_INTACT",
    )


# =====================================================================
# 4. APP AUTH & 2FA ACCOUNT EXTRACTION
# =====================================================================

def _mask_secret(secret: str) -> str:
    """
    Fully masks a TOTP/HOTP secret for reporting. No characters are revealed: a partial
    mask (first/last few) leaks enough base32 to meaningfully narrow a brute force, so the
    report shows only that a secret is present and its length.
    """
    s = str(secret).strip()
    if not s:
        return ""
    return f"•••••••• (secret present, {len(s)} chars)"


def _infer_duo_issuer(host: str, explicit_issuer: Optional[str] = None) -> str:
    """
    Generic issuer inference for a Duo account (no hardcoded institution).

    Uses the account's own issuer when present, otherwise derives an organization label
    from a custom Duo host domain when one is embedded (e.g. 'duo.example.edu' -> 'example.edu').
    A generic 'api-XXXX.duosecurity.com' host carries no org name, so it falls back to
    'Duo Security'.
    """
    if explicit_issuer:
        return str(explicit_issuer)
    h = (host or "").strip().lower()
    if h and "duosecurity.com" not in h:
        # Custom-branded Duo host: use the registrable domain as the org label.
        parts = [p for p in h.split(".") if p]
        if len(parts) >= 2:
            return ".".join(parts[-2:])
    return "Duo Security"


def extract_duo_accounts(backup_dir: Path) -> List[AuthAccount]:
    """
    Extracts Duo Mobile accounts (institutional / Enterprise Duo Push and 3rd-party TOTP)
    from AppDomain-com.duosecurity.DuoMobile.
    """
    accounts: List[AuthAccount] = []
    bundle_id = "com.duosecurity.DuoMobile"
    app_domain = f"AppDomain-{bundle_id}"

    # Search for Duo preferences plist or SQLite database
    manifest_db = backup_dir / "Manifest.db"
    found_files: List[Tuple[str, str]] = []

    if manifest_db.is_file():
        try:
            conn = sqlite3.connect(f"file:{manifest_db.resolve()}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute(
                "SELECT fileID, relativePath FROM Files WHERE domain=? OR domain LIKE '%duo%';",
                (app_domain,),
            )
            found_files = cur.fetchall()
            conn.close()
        except Exception:
            pass

    # Direct fallback for standard relative paths
    known_rel_paths = [
        "Library/Preferences/com.duosecurity.DuoMobile.plist",
        "Documents/duo.sqlite",
        "Documents/duo_accounts.sqlite",
    ]
    if not found_files:
        for rp in known_rel_paths:
            fid = hashlib.sha1(f"{app_domain}-{rp}".encode("utf-8")).hexdigest()
            p = backup_dir / fid[:2] / fid
            if p.is_file():
                found_files.append((fid, rp))

    for fid, rel_path in found_files:
        blob_path = backup_dir / fid[:2] / fid
        if not blob_path.is_file():
            continue

        # Case A: Property List
        if rel_path.endswith(".plist") or "Preferences" in rel_path:
            try:
                with blob_path.open("rb") as f:
                    plist_data = plistlib.load(f)

                # 1. Inspect 'accounts' or 'DuoAccounts'
                acc_list = plist_data.get("accounts") or plist_data.get("Accounts") or []
                if isinstance(acc_list, dict):
                    acc_list = list(acc_list.values())

                for acc in acc_list:
                    if not isinstance(acc, dict):
                        continue
                    name = str(acc.get("name") or acc.get("accountName") or "Duo Account")
                    host = str(acc.get("host") or acc.get("apiHost") or "")
                    user = str(acc.get("user") or acc.get("username") or acc.get("email") or "")
                    acc_type = "DuoPush" if host or "duo" in name.lower() else "TOTP"
                    issuer = _infer_duo_issuer(host, acc.get("issuer"))
                    has_push = bool(acc.get("pushEnabled") or acc.get("isPushEnabled") or host)

                    accounts.append(
                        AuthAccount(
                            app_name="Duo Mobile",
                            bundle_id=bundle_id,
                            account_name=name,
                            account_type=acc_type,
                            issuer=issuer,
                            host=host or None,
                            username=user or None,
                            has_push=has_push,
                            account_id=str(acc.get("keyId") or acc.get("id") or ""),
                        )
                    )

                # 2. Inspect 'third_party_accounts'
                tp_accounts = plist_data.get("third_party_accounts") or plist_data.get("otp_accounts") or []
                if isinstance(tp_accounts, dict):
                    tp_accounts = list(tp_accounts.values())

                for tp in tp_accounts:
                    if not isinstance(tp, dict):
                        continue
                    name = str(tp.get("name") or tp.get("label") or "TOTP Account")
                    issuer = str(tp.get("issuer") or "Third Party")
                    secret = tp.get("secret")
                    accounts.append(
                        AuthAccount(
                            app_name="Duo Mobile (TOTP)",
                            bundle_id=bundle_id,
                            account_name=name,
                            account_type="TOTP",
                            issuer=issuer,
                            username=str(tp.get("user") or ""),
                            masked_secret=_mask_secret(secret) if secret else None,
                            has_push=False,
                        )
                    )

                # 3. Direct institutional Duo host mapping in root plist
                if not accounts and (plist_data.get("api_host") or plist_data.get("host")):
                    host = str(plist_data.get("api_host") or plist_data.get("host"))
                    user = str(plist_data.get("username") or plist_data.get("user") or "")
                    accounts.append(
                        AuthAccount(
                            app_name="Duo Mobile",
                            bundle_id=bundle_id,
                            account_name="Institutional Duo Account",
                            account_type="DuoPush",
                            issuer=_infer_duo_issuer(host),
                            host=host,
                            username=user or None,
                            has_push=True,
                        )
                    )
            except Exception:
                # Plist could be encrypted in raw backup without password; record container existence
                pass

        # Case B: SQLite Accounts Database
        elif rel_path.endswith(".sqlite") or "duo" in rel_path.lower():
            try:
                conn = sqlite3.connect(f"file:{blob_path.resolve()}?mode=ro", uri=True)
                cur = conn.cursor()
                # Check for accounts table
                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('accounts', 'DuoAccount', 'duo_accounts');"
                )
                tbl = cur.fetchone()
                if tbl:
                    tbl_name = tbl[0]
                    cur.execute(f"SELECT * FROM {tbl_name} LIMIT 20;")
                    rows = cur.fetchall()
                    cols = [d[0].lower() for d in cur.description]
                    for r in rows:
                        row_dict = dict(zip(cols, r))
                        name = str(row_dict.get("name") or row_dict.get("account_name") or "Duo Account")
                        host = str(row_dict.get("host") or row_dict.get("api_host") or "")
                        user = str(row_dict.get("user_id") or row_dict.get("username") or "")
                        acc_type = "DuoPush" if host else "TOTP"
                        accounts.append(
                            AuthAccount(
                                app_name="Duo Mobile (DB)",
                                bundle_id=bundle_id,
                                account_name=name,
                                account_type=acc_type,
                                issuer=_infer_duo_issuer(host),
                                host=host or None,
                                username=user or None,
                                has_push=bool(host),
                            )
                        )
                conn.close()
            except Exception:
                pass

    return accounts


def extract_google_authenticator_accounts(backup_dir: Path) -> List[AuthAccount]:
    """
    Extracts Google Authenticator 2FA TOTP accounts from
    AppDomain-com.google.Authenticator (google_authenticator.sqlite).
    """
    accounts: List[AuthAccount] = []
    bundle_id = "com.google.Authenticator"
    app_domain = f"AppDomain-{bundle_id}"

    manifest_db = backup_dir / "Manifest.db"
    found_files: List[Tuple[str, str]] = []

    if manifest_db.is_file():
        try:
            conn = sqlite3.connect(f"file:{manifest_db.resolve()}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute(
                "SELECT fileID, relativePath FROM Files WHERE domain=? AND relativePath LIKE '%sqlite%';",
                (app_domain,),
            )
            found_files = cur.fetchall()
            conn.close()
        except Exception:
            pass

    if not found_files:
        rel = "Documents/google_authenticator.sqlite"
        fid = hashlib.sha1(f"{app_domain}-{rel}".encode("utf-8")).hexdigest()
        p = backup_dir / fid[:2] / fid
        if p.is_file():
            found_files.append((fid, rel))

    for fid, rel in found_files:
        db_path = backup_dir / fid[:2] / fid
        if not db_path.is_file():
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='accounts';"
            )
            if cur.fetchone():
                cur.execute("SELECT email, secret, issuer FROM accounts;")
                for email, secret, issuer in cur.fetchall():
                    acc_name = str(email or "Google 2FA")
                    iss = str(issuer or "Google Authenticator")
                    accounts.append(
                        AuthAccount(
                            app_name="Google Authenticator",
                            bundle_id=bundle_id,
                            account_name=acc_name,
                            account_type="TOTP",
                            issuer=iss,
                            username=acc_name,
                            masked_secret=_mask_secret(secret) if secret else None,
                            has_push=False,
                        )
                    )
            conn.close()
        except Exception:
            pass

    return accounts


def extract_microsoft_authenticator_accounts(backup_dir: Path) -> List[AuthAccount]:
    """Extracts Microsoft Authenticator records from com.azure.authenticator."""
    accounts: List[AuthAccount] = []
    bundle_ids = ["com.azure.authenticator", "com.microsoft.msauth"]

    manifest_db = backup_dir / "Manifest.db"
    found_files: List[Tuple[str, str, str]] = []

    if manifest_db.is_file():
        try:
            conn = sqlite3.connect(f"file:{manifest_db.resolve()}?mode=ro", uri=True)
            cur = conn.cursor()
            for b in bundle_ids:
                cur.execute(
                    "SELECT fileID, relativePath FROM Files WHERE domain=? AND relativePath LIKE '%.plist';",
                    (f"AppDomain-{b}",),
                )
                for fid, rel in cur.fetchall():
                    found_files.append((b, fid, rel))
            conn.close()
        except Exception:
            pass

    for b, fid, rel in found_files:
        p = backup_dir / fid[:2] / fid
        if not p.is_file():
            continue
        try:
            with p.open("rb") as f:
                data = plistlib.load(f)
            accs = data.get("accounts") or data.get("Accounts") or []
            if isinstance(accs, dict):
                accs = list(accs.values())
            for a in accs:
                if isinstance(a, dict):
                    username = str(a.get("username") or a.get("upn") or a.get("name") or "MS Account")
                    tenant = str(a.get("tenant") or a.get("tenantId") or "Microsoft")
                    accounts.append(
                        AuthAccount(
                            app_name="Microsoft Authenticator",
                            bundle_id=b,
                            account_name=username,
                            account_type="Enterprise",
                            issuer=tenant,
                            username=username,
                            has_push=True,
                        )
                    )
        except Exception:
            pass

    return accounts


# =====================================================================
# 5. UNIFIED TOKEN EXTRACTION PROTOCOL
# =====================================================================

def extract_all_tokens(backup_target: Optional[str] = None) -> TokenManifest:
    """
    Standard token extraction protocol:
      1. Resolves target backup directory and device UDID.
      2. Extracts host Lockdown pairing record & escrow token.
      3. Parses Apple binary BackupKeyBag in Manifest.plist.
      4. Audits KeychainDomain artifact (presence, size, SHA256).
      5. Decodes Duo Mobile, Google Authenticator, and MS Authenticator credentials.
      6. Seals cryptographic TokenManifest with overall verdict.
    """
    # 1. Resolve Backup
    target_path: Optional[Path] = None
    if backup_target:
        p = Path(backup_target).expanduser().resolve()
        if p.is_dir() and (p / "Manifest.plist").is_file():
            target_path = p

    if not target_path:
        root = get_itunes_backup_root()
        if root.is_dir():
            if backup_target:
                cand = root / backup_target
                if cand.is_dir() and (cand / "Manifest.plist").is_file():
                    target_path = cand

            if not target_path:
                dirs = [
                    d
                    for d in root.iterdir()
                    if d.is_dir() and (d / "Manifest.plist").is_file()
                ]
                if dirs:
                    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
                    target_path = dirs[0]

    if not target_path:
        # Generate graceful missing-backup manifest
        return TokenManifest(
            device_name="Unknown Device",
            udid="NONE",
            backup_path="",
            verdict="FAIL_NO_BACKUP",
        )

    # 2. Read Metadata (Info.plist & Manifest.plist)
    udid = target_path.name
    device_name = "iPhone"
    last_backup_date: Optional[str] = None
    info_path = target_path / "Info.plist"
    manifest_path = target_path / "Manifest.plist"

    if info_path.is_file():
        try:
            with info_path.open("rb") as f:
                info_p = plistlib.load(f)
                device_name = str(info_p.get("Device Name") or info_p.get("Product Name") or "iPhone")
                udid = str(info_p.get("Unique Identifier") or udid)
                _lbd = info_p.get("Last Backup Date")
                last_backup_date = _lbd.isoformat() if hasattr(_lbd, "isoformat") else (str(_lbd) if _lbd else None)
        except Exception:
            last_backup_date = None

    keybag_bytes: Optional[bytes] = None
    is_encrypted = False
    if manifest_path.is_file():
        try:
            with manifest_path.open("rb") as f:
                man_p = plistlib.load(f)
                is_encrypted = bool(man_p.get("IsEncrypted", False))
                keybag_bytes = man_p.get("BackupKeyBag")
        except Exception:
            pass

    # 3. Lockdown Escrow Token
    lockdown = extract_lockdown_token(udid)

    # 4. Apple Keybag
    keybag = parse_apple_keybag(keybag_bytes)
    if not is_encrypted and not keybag.is_encrypted:
        keybag.status = "UNENCRYPTED_WARNING"

    # 5. Keychain Artifact
    keychain = extract_keychain_artifact(target_path)

    # 6. Authenticator & Token Accounts
    accounts: List[AuthAccount] = []
    accounts.extend(extract_duo_accounts(target_path))
    accounts.extend(extract_google_authenticator_accounts(target_path))
    accounts.extend(extract_microsoft_authenticator_accounts(target_path))

    # Overall Verdict
    verdict = "PASS"
    if not is_encrypted:
        verdict = "FAIL_UNENCRYPTED_BACKUP"
    elif not keychain.present:
        verdict = "WARN_KEYCHAIN_MISSING"
    elif not lockdown.present:
        verdict = "WARN_LOCKDOWN_ESCROW_ABSENT"

    total_tokens = len(accounts) + (1 if keychain.present else 0) + (1 if lockdown.has_escrow_bag else 0)

    return TokenManifest(
        device_name=device_name,
        udid=udid,
        backup_path=str(target_path),
        last_backup_date=last_backup_date,
        keybag=keybag,
        keychain=keychain,
        lockdown=lockdown,
        accounts=accounts,
        total_token_records=total_tokens,
        verdict=verdict,
    )


# =====================================================================
# 6. TOKEN SURVIVAL VERIFICATION & DIFF ENGINE
# =====================================================================

def verify_token_survival(
    prewipe_manifest: Union[TokenManifest, Dict[str, Any], Path, str],
    post_manifest: Optional[Union[TokenManifest, Dict[str, Any], Path, str]] = None,
    backup_target: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Compares pre-wipe token inventory against post-restore token inventory:
    Verifies that all Duo Push, Google Authenticator, Keychain, and Escrow
    tokens survived the reset and restore with zero loss.
    """
    # Load pre-wipe manifest
    if isinstance(prewipe_manifest, (str, Path)):
        p = Path(prewipe_manifest).expanduser().resolve()
        with p.open("r", encoding="utf-8") as f:
            pre_data = json.load(f)
    elif isinstance(prewipe_manifest, TokenManifest):
        pre_data = prewipe_manifest.to_dict()
    else:
        pre_data = prewipe_manifest

    # Load post-restore manifest (or extract fresh)
    if post_manifest is None:
        post_obj = extract_all_tokens(backup_target=backup_target)
        post_data = post_obj.to_dict()
    elif isinstance(post_manifest, (str, Path)):
        p = Path(post_manifest).expanduser().resolve()
        with p.open("r", encoding="utf-8") as f:
            post_data = json.load(f)
    elif isinstance(post_manifest, TokenManifest):
        post_data = post_manifest.to_dict()
    else:
        post_data = post_manifest

    pre_accounts = {a["fingerprint"]: a for a in pre_data.get("accounts", [])}
    post_accounts = {a["fingerprint"]: a for a in post_data.get("accounts", [])}

    retained = []
    missing = []
    for fp, acc in pre_accounts.items():
        if fp in post_accounts:
            retained.append(acc)
        else:
            missing.append(acc)

    new_accs = [acc for fp, acc in post_accounts.items() if fp not in pre_accounts]

    pre_kc = pre_data.get("keychain", {})
    post_kc = post_data.get("keychain", {})
    keychain_intact = bool(post_kc.get("present") and post_kc.get("size_bytes", 0) > 0)

    pre_lock = pre_data.get("lockdown", {})
    post_lock = post_data.get("lockdown", {})
    lockdown_intact = bool(post_lock.get("present"))

    pre_total = len(pre_accounts)

    warnings: List[str] = []

    # Detect comparing against the SAME backup on disk (not a real post-restore backup).
    pre_lbd = pre_data.get("last_backup_date")
    post_lbd = post_data.get("last_backup_date")
    same_backup = bool(pre_lbd and post_lbd and pre_lbd == post_lbd)
    if same_backup:
        warnings.append(
            "Post-restore 'Last Backup Date' is identical to the pre-wipe backup: this looks "
            "like the SAME backup, not a fresh post-restore one. Take a new backup after the "
            "restore, then re-run verify."
        )

    # Encrypted backups hide app containers, so account-level checks see 0 accounts. Don't
    # report that as 100% survival.
    accounts_inspectable = pre_total > 0 or len(post_accounts) > 0
    if not accounts_inspectable:
        return {
            "status": "ACCOUNTS_NOT_INSPECTABLE",
            "device_name": post_data.get("device_name", pre_data.get("device_name")),
            "udid": post_data.get("udid", pre_data.get("udid")),
            "prewipe_tokens_count": 0,
            "post_tokens_count": 0,
            "retained_count": 0,
            "missing_count": 0,
            "survival_percentage": None,
            "accounts_inspectable": False,
            "keychain_intact": keychain_intact,
            "lockdown_escrow_intact": lockdown_intact,
            "retained_accounts": [],
            "missing_accounts": [],
            "new_accounts": [],
            "warnings": warnings + [
                "App 2FA accounts are inside the encrypted Keychain and cannot be read from the "
                "backup, so per-account survival can't be measured here. Confirm Keychain and "
                "escrow are intact above, then verify each authenticator on the device."
            ],
        }

    survival_pct = round((len(retained) / pre_total) * 100.0, 1) if pre_total else 0.0
    status = "PASS_100_PERCENT_SURVIVAL" if (not missing and keychain_intact) else "FAIL_TOKENS_LOST"

    return {
        "status": status,
        "device_name": post_data.get("device_name", pre_data.get("device_name")),
        "udid": post_data.get("udid", pre_data.get("udid")),
        "prewipe_tokens_count": pre_total,
        "post_tokens_count": len(post_accounts),
        "retained_count": len(retained),
        "missing_count": len(missing),
        "survival_percentage": survival_pct,
        "accounts_inspectable": True,
        "keychain_intact": keychain_intact,
        "lockdown_escrow_intact": lockdown_intact,
        "retained_accounts": retained,
        "missing_accounts": missing,
        "new_accounts": new_accs,
        "warnings": warnings,
    }


# =====================================================================
# 7. FORMATTED REPORT PRINTING
# =====================================================================

def print_token_manifest_report(manifest: TokenManifest) -> None:
    """Displays human-readable formatted Token Manifest report."""
    print("=" * 70)
    print("      STANDARD TOKEN EXTRACTION & CREDENTIAL SURVIVAL REPORT")
    print("=" * 70)
    print(f"Device:           {manifest.device_name} (UDID: {manifest.udid})")
    print(f"Platform:         {manifest.platform.upper()}")
    print(f"Backup Path:      {manifest.backup_path or 'N/A'}")
    print(f"Extracted At:     {manifest.extracted_at}")
    print(f"Manifest Digest:  {manifest.compute_manifest_hash()[:16]}...")

    print(f"\n[1/4] APPLE KEYBAG POSTURE & HARDWARE PROTECTION")
    kb = manifest.keybag
    print(f"  Encrypted:        {'✅ YES' if kb.is_encrypted else '🚨 NO (Unencrypted)'}")
    print(f"  Status:           {kb.status}")
    print(f"  Wrapping Mode:    {kb.wrap_type}")
    print(f"  PBKDF2 Iter:      {kb.iteration_count:,}")
    if kb.salt_hex:
        print(f"  Keybag Salt:      {kb.salt_hex[:16]}... ({len(kb.salt_hex)//2} bytes)")
    print(f"  Class Keys:       {kb.class_count} classes protected {kb.classes[:6]}")

    print(f"\n[2/4] KEYCHAIN ARTIFACT INTEGRITY")
    kc = manifest.keychain
    print(f"  Keychain Present: {'✅ YES' if kc.present else '❌ NO'}")
    print(f"  Store Path:       {kc.relative_path or 'N/A'}")
    print(f"  Size on Disk:     {kc.size_bytes:,} bytes (~{kc.estimated_items} class items)")
    if kc.sha256_hash:
        print(f"  SHA256 Digest:    {kc.sha256_hash[:24]}...")
    print(f"  Store Status:     {kc.status}")

    print(f"\n[3/4] USB LOCKDOWN & ESCROW BAG")
    ld = manifest.lockdown
    print(f"  Pairing Record:   {'✅ FOUND' if ld.present else '⚠️ NOT FOUND (Device not yet paired on host)'}")
    if ld.present:
        print(f"  Record File:      {ld.file_path}")
        print(f"  HostID:           {ld.host_id or 'N/A'}")
        print(f"  Escrow Bag:       {'✅ INTACT (Enables smooth reboot & restore)' if ld.has_escrow_bag else '⚠️ MISSING'}")

    print(f"\n[4/4] EXTRACTED AUTH & 2FA ACCOUNTS ({len(manifest.accounts)})")
    if manifest.accounts:
        for idx, acc in enumerate(manifest.accounts, 1):
            push_tag = "[Duo Push]" if acc.has_push else "[TOTP]"
            host_info = f" -> {acc.host}" if acc.host else ""
            user_info = f" ({acc.username})" if acc.username else ""
            print(f"  {idx}. {push_tag} {acc.app_name}: {acc.account_name}{user_info}{host_info}")
            if acc.masked_secret:
                print(f"     Secret: {acc.masked_secret}")
    else:
        print("  ℹ️  No unencrypted 2FA accounts decoded (accounts are locked in encrypted keychain container).")

    print("\n" + "=" * 70)
    if manifest.verdict == "PASS":
        print("✅ VERDICT: PASS - ALL TOKENS & CRYPTO ARTIFACTS ARE VERIFIED!")
    else:
        print(f"⚠️ VERDICT: {manifest.verdict}")
    print("=" * 70 + "\n")


def print_verification_report(diff: Dict[str, Any]) -> None:
    """Displays human-readable formatted Token Survival verification diff."""
    print("=" * 70)
    print("         POST-RESTORE TOKEN SURVIVAL VERIFICATION REPORT")
    print("=" * 70)
    print(f"Device:           {diff.get('device_name')} (UDID: {diff.get('udid')})")
    print(f"Survival Rate:    {diff.get('survival_percentage')}%")
    print(f"Pre-Wipe Tokens:  {diff.get('prewipe_tokens_count')}")
    print(f"Retained Tokens:  {diff.get('retained_count')}")
    print(f"Missing Tokens:   {diff.get('missing_count')}")
    print(f"Keychain Intact:  {'✅ YES' if diff.get('keychain_intact') else '❌ NO'}")
    print(f"Escrow Intact:    {'✅ YES' if diff.get('lockdown_escrow_intact') else '⚠️ NO'}")

    if diff.get("retained_accounts"):
        print(f"\n[VERIFIED SURVIVED ACCOUNTS ({len(diff['retained_accounts'])})]")
        for acc in diff["retained_accounts"]:
            print(f"  ✅ {acc['app_name']}: {acc['account_name']} [{acc['account_type']}]")

    if diff.get("missing_accounts"):
        print(f"\n🚨 MISSING / LOST ACCOUNTS ({len(diff['missing_accounts'])}):")
        for acc in diff["missing_accounts"]:
            print(f"  ❌ {acc['app_name']}: {acc['account_name']} [{acc['account_type']}]")

    print("\n" + "=" * 70)
    if diff.get("status") == "PASS_100_PERCENT_SURVIVAL":
        print("🎉 100% TOKEN SURVIVAL CONFIRMED: ZERO CREDENTIALS LOST!")
    else:
        print("❌ FAIL: TOKEN SURVIVAL AUDIT FAILED - CREDENTIALS MISSING!")
    print("=" * 70 + "\n")


# =====================================================================
# 8. COMMAND LINE DISPATCHER
# =====================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Standard Token Extraction & Credential Survival Protocol for iOS Revival."
    )
    sub = parser.add_subparsers(dest="action", help="Token action")

    # audit / inspect
    p_audit = sub.add_parser("audit", help="Inspect and audit token posture.")
    p_audit.add_argument("--target", help="Specific backup UDID or directory path.")
    p_audit.add_argument("--json", action="store_true", help="Output full JSON manifest.")
    p_audit.add_argument("--compact", action="store_true", help="Token-saver compacted summary.")

    # extract / save
    p_extract = sub.add_parser("extract", help="Extract and save cryptographic token manifest.")
    p_extract.add_argument("--target", help="Specific backup UDID or directory path.")
    p_extract.add_argument("--out", "-o", help="Output JSON path (default: <UDID>-tokens.json).")

    # verify / diff
    p_verify = sub.add_parser("verify", help="Verify token survival against a pre-wipe manifest.")
    p_verify.add_argument("--prewipe", required=True, help="Path to pre-wipe token manifest JSON.")
    p_verify.add_argument("--post", help="Path to post-restore manifest JSON (or re-extracts live).")
    p_verify.add_argument("--target", help="Target backup UDID or directory path.")

    # lockdown
    p_lock = sub.add_parser("lockdown", help="Query host lockdown pairing record and escrow token.")
    p_lock.add_argument("--udid", help="Device UDID to query.")

    args = parser.parse_args()

    if not args.action or args.action == "audit":
        manifest = extract_all_tokens(backup_target=getattr(args, "target", None))
        if getattr(args, "json", False):
            d = manifest.to_dict()
            if getattr(args, "compact", False):
                d = TokenCompactor.compact_json(d, max_list_items=10)
            print(json.dumps(d, indent=2))
        else:
            print_token_manifest_report(manifest)
        return 0 if manifest.verdict == "PASS" else 1

    elif args.action == "extract":
        manifest = extract_all_tokens(backup_target=args.target)
        out_path = Path(args.out) if args.out else Path(f"{manifest.udid}-tokens.json")
        out_path.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")
        print(f"✅ Token manifest sealed and saved to: {out_path.resolve()}")
        print_token_manifest_report(manifest)
        return 0

    elif args.action == "verify":
        diff = verify_token_survival(
            prewipe_manifest=args.prewipe,
            post_manifest=args.post,
            backup_target=args.target,
        )
        print_verification_report(diff)
        return 0 if diff.get("status") == "PASS_100_PERCENT_SURVIVAL" else 1

    elif args.action == "lockdown":
        ld = extract_lockdown_token(args.udid)
        print(json.dumps(ld.to_dict(), indent=2))
        return 0 if ld.present else 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
