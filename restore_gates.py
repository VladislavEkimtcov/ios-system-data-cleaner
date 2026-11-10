"""
Restore safety gates (`restore_gates.py`): everything that must be true BEFORE the Finder restore
is driven, without touching the phone. The restore itself is done through Finder's UI
(`finder_restore.py`); this module only decides whether it is safe to start.

  1. exactly one phone is attached, and it is sitting at the Hello / Setup Assistant screen
     (the phone's own SetupState, never the NVRAM erase record, which survives restores);
  2. the backup belongs to that phone, is encrypted, finished, and recent;
  3. the phone's iOS is not older than the backup's, and its battery is not nearly flat;
  4. the Mac has room to spare.

Refusals are plain-language dicts: {"ok": False, "code", "message", "options": [...]}.
"""

from __future__ import annotations

import plistlib
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common import (
    backup_device_name,
    backup_root,
    connected_udids,
    device_setup_verdict,
    diagnose_connection,
    find_tool,
    _plausible_backup,
    _backup_date,
)

PREWIPE = "-prewipe"
MIN_BATTERY_PCT = 15
MIN_HOST_FREE_GB = 5

# What to tell the person whenever they are asked to erase the phone. Agents must ALWAYS say all of it.
ERASE_STEPS = ("On the phone: Settings > General > Transfer or Reset iPhone > Erase All Content and Settings. "
               "When it offers to back up to iCloud first (\"Finish Uploading Then Erase\" or similar), choose "
               "\"Skip Backup\" / \"Erase Now\": your full backup is already on this Mac, and the cloud upload can "
               "take a very long time. If asked about cellular plans, choose \"Keep eSIMs and Erase Data\".")
ERASE_WARNING = ("Heads up: while it erases, the phone's screen can be slow or stop responding for a minute or "
                 "two. That's normal. Give it time instead of tapping repeatedly, and don't unplug it.")

STOP_OPTION = {"id": "stop", "label": "Stop here. Nothing has been changed on your phone."}


def say(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M')}] {msg}", flush=True)


def refusal(code: str, message: str, options: Optional[List[Dict[str, str]]] = None, **extra: Any) -> Dict[str, Any]:
    return {"ok": False, "code": code, "message": message,
            "options": options if options is not None else [STOP_OPTION], **extra}


def _read_plist(path: Path) -> Dict[str, Any]:
    try:
        with path.open("rb") as f:
            data = plistlib.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _version_tuple(v: str) -> Tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", v or "")[:3]) or (0,)


def _human_age(hours: float) -> str:
    if hours < 1:
        return f"{max(1, int(hours * 60))} minutes ago"
    if hours < 48:
        return f"{int(round(hours))} hours ago"
    return f"{int(hours // 24)} days ago"


def _battery(udid: str) -> Optional[int]:
    from common import _ideviceinfo
    raw = _ideviceinfo(udid, "-q", "com.apple.mobile.battery").get("BatteryCurrentCapacity")
    try:
        return int(raw) if raw is not None else None
    except ValueError:
        return None


def _now_utc_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Choosing and inspecting the backup
# ---------------------------------------------------------------------------

def inspect_source(path: Path, udid: str) -> Dict[str, Any]:
    """Facts about one backup folder, plus `problems` (plain-language reasons not to use it)."""
    info = _read_plist(path / "Info.plist")
    manifest = _read_plist(path / "Manifest.plist")
    status = _read_plist(path / "Status.plist")
    date = _backup_date(path)
    problems: List[str] = []
    ok, why = _plausible_backup(path)
    if not ok:
        problems.append(f"the backup looks incomplete ({why})")
    owner = str(info.get("Unique Identifier") or info.get("Target Identifier") or "")
    if owner and owner.lower() != udid.lower():
        problems.append("this backup belongs to a different phone")
    if not manifest.get("IsEncrypted"):
        problems.append("the backup is not encrypted, so passwords, Wi-Fi and Health data would not come back")
    if str(status.get("SnapshotState") or "") not in ("finished", ""):
        problems.append(f"the backup did not finish (state: {status.get('SnapshotState')})")
    age_h = ((_now_utc_naive() - date).total_seconds() / 3600) if date else None
    # Finder's "Last Backed Up" line shows Info.plist's `Last Backup Date` (when the phone's data was
    # captured), a few minutes before Status.plist's `Date` (when the backup finished).
    last = info.get("Last Backup Date")
    if isinstance(last, datetime) and last.tzinfo is not None:
        last = last.astimezone(timezone.utc).replace(tzinfo=None)
    return {
        "path": path, "name": path.name, "date": date, "age_hours": age_h,
        "last_backup_date": last if isinstance(last, datetime) else None,
        # the phone's REAL name (backup_labels.py may have rewritten `Device Name` into a Finder label)
        "device_name": backup_device_name(info),
        "label": str(info.get("Display Name") or info.get("Device Name") or ""),   # what Finder's pop-up shows
        "product_type": str(info.get("Product Type") or ""),
        "product_version": str(info.get("Product Version") or ""),
        "problems": problems,
    }


def _local(dt_utc_naive: datetime) -> datetime:
    return dt_utc_naive.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def _when(dt_utc_naive: Optional[datetime]) -> str:
    if not dt_utc_naive:
        return "at an unknown time"
    d = _local(dt_utc_naive)
    return d.strftime("%b ") + str(d.day) + d.strftime(", %I:%M %p").replace(" 0", " ")


def available_backups(udid: str, chosen: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Every backup folder on this Mac that belongs to this phone, in plain terms, with the one that
    will be restored marked. Finder's own timestamped working copies are listed too."""
    root = backup_root(udid)
    out: List[Dict[str, Any]] = []
    try:
        folders = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return out
    base = udid.lower()
    for p in folders:
        n = p.name.lower()
        if n == base:
            kind, label = "main", "main backup"
        elif n == base + PREWIPE:
            kind, label = "safety", "protected safety copy"
        elif n.startswith(base + "-"):
            kind, label = "other", f"extra copy ({p.name[len(udid) + 1:]})"
        else:
            continue
        meta = inspect_source(p, udid)
        out.append({
            "folder": p.name, "kind": kind, "label": label,
            "made": (meta["last_backup_date"] or meta["date"]).isoformat() if (meta["last_backup_date"] or meta["date"]) else None,
            "made_text": _when(meta["last_backup_date"] or meta["date"]),
            "ios": meta["product_version"], "device_name": meta["device_name"].strip(),
            "usable": not meta["problems"], "problems": meta["problems"],
            "chosen": bool(chosen and p.resolve() == Path(chosen).resolve()),
        })
    return out


def describe_backups(backups: List[Dict[str, Any]]) -> List[str]:
    """Plain-language lines for the chat: what exists, and which one is being restored."""
    lines = []
    for b in backups:
        state = "usable" if b["usable"] else "not usable: " + "; ".join(b["problems"])
        mark = "  <-- RESTORING THIS ONE" if b["chosen"] else ""
        lines.append(f"{b['label']}: your phone's data as of {b['made_text']}, iOS {b['ios'] or '?'}, {state}{mark}")
    return lines


def choose_source(udid: str, which: str = "auto") -> Optional[Dict[str, Any]]:
    """`auto` = the newest usable backup, preferring the immutable -prewipe clone on a tie."""
    root = backup_root(udid)
    cands = []
    for kind, p in (("clone", root / f"{udid}{PREWIPE}"), ("live", root / udid)):
        if p.is_dir() and which in ("auto", kind):
            meta = inspect_source(p, udid)
            meta["kind"] = kind
            cands.append(meta)
    if not cands:
        return None
    usable = [c for c in cands if not c["problems"]] or cands
    usable.sort(key=lambda c: (c["date"] or datetime.min, c["kind"] == "clone"), reverse=True)
    return usable[0]


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------

def check_preconditions(udid: Optional[str] = None, source: str = "auto",
                        max_age_hours: float = 48.0, allow_unactivated: bool = False) -> Dict[str, Any]:
    """Runs every safety gate without touching the phone. Returns {"ok": True, "plan": ...}
    or a refusal carrying a plain-language message and the choices to offer the user."""
    if not find_tool("ideviceinfo"):
        return refusal(
            "tools_missing",
            "The small helper tools that talk to the iPhone aren't installed on this Mac yet.",
            [{"id": "install_tools", "label": "Let me install them (run `python3 dep_installer.py --tools --yes`), "
                                              "or run `brew install libimobiledevice` yourself."},
             STOP_OPTION],
        )

    udids = connected_udids()
    if len(udids) > 1:
        return refusal("multiple_devices",
                       "More than one iPhone/iPad is plugged in. To be safe, unplug all but the one "
                       "you want to restore, then tell me.")
    if not udids:
        diag = diagnose_connection(udid)
        return refusal("no_device", diag["message"], [{"id": "retry", "label": "Fix that, then tell me and I'll check again."}, STOP_OPTION])
    target = udids[0]
    if udid and udid.lower() != target.lower():
        return refusal("wrong_device",
                       "The phone that's plugged in isn't the one we backed up. Plug in the right "
                       "phone, or tell me you want to restore onto a different one.")

    verdict = device_setup_verdict(target)
    if verdict["verdict"] == "set_up":
        return refusal(
            "phone_not_erased",
            "This phone still has your data on it, so I won't restore onto it. A restore replaces "
            "everything on the phone, and your backup is already safe.",
            [{"id": "erase_first",
              "label": "Erase it first. " + ERASE_STEPS + " " + ERASE_WARNING + " When it shows 'Hello', tell me."},
             STOP_OPTION],
            verdict=verdict,
        )
    if verdict["verdict"] == "unknown":
        return refusal(
            "phone_state_unclear",
            "I can't tell whether the phone is at the 'Hello' screen yet. " + "; ".join(verdict["reasons"]).capitalize() + ". "
            "Unlock or wake it, make sure it shows 'Hello', and check the cable.",
            [{"id": "retry", "label": "Fix that, then tell me and I'll check again."}, STOP_OPTION],
            verdict=verdict,
        )

    if not allow_unactivated and verdict.get("activation_state") and verdict["activation_state"] != "Activated":
        # Observed 2026-11-09: an unactivated phone refuses the backup service
        # ("Could not start service com.apple.mobilebackup2: Invalid service").
        return refusal(
            "phone_not_activated",
            "The phone isn't activated yet. Your Mac activates it by itself once the phone is opened in Finder.",
            [{"id": "activate_via_finder",
              "label": "Let me open the phone in Finder for a minute so your Mac activates it "
                       "(I run `python3 restorer.py activate`; the restore does this step itself)."},
             {"id": "activate_on_phone",
              "label": "Activate it on the phone: choose your language and region, join Wi-Fi, and wait for "
                       "'Activating iPhone' to finish. Stop at the 'Apps & Data' screen and tell me."},
             STOP_OPTION],
            verdict=verdict,
        )

    src = choose_source(target, source)
    if not src:
        return refusal("no_backup",
                       "I can't find a backup for this phone on this Mac.",
                       [{"id": "other_mac", "label": "The backup is on another computer: connect that one and run this there."}, STOP_OPTION])
    if src["problems"]:
        return refusal("backup_unusable",
                       "I found a backup, but I can't safely use it: " + "; ".join(src["problems"]) + ".",
                       [{"id": "stop", "label": "Stop here. Nothing has been changed on your phone."}], backup=src["name"])
    if src["age_hours"] is not None and src["age_hours"] > max_age_hours:
        return refusal(
            "backup_old",
            f"The newest backup is from {_human_age(src['age_hours'])}. Restoring it would lose "
            "anything newer.",
            [{"id": "use_anyway", "label": f"Restore it anyway (re-run with --max-age-hours {int(src['age_hours']) + 1})."},
             STOP_OPTION],
        )

    dev_ver = verdict.get("product_version", "")
    if src["product_version"] and _version_tuple(src["product_version"]) > _version_tuple(dev_ver):
        return refusal(
            "phone_ios_older",
            f"The backup was made on iOS {src['product_version']} but the phone is on iOS {dev_ver}. "
            "A phone can't restore a backup from a newer iOS.",
            [{"id": "update_phone", "label": "Update the phone to the latest iOS first (it may need Wi-Fi and the Hello setup), then tell me."},
             STOP_OPTION],
        )

    battery = _battery(target)
    if battery is not None and battery < MIN_BATTERY_PCT:
        return refusal(
            "battery_low",
            f"The phone's battery is at {battery}%. If it dies mid-restore it can need extra repair "
            "steps. Leave it plugged in for about 10 minutes first.",
            [{"id": "retry", "label": "Tell me when it has charged a bit and I'll continue."}, STOP_OPTION],
        )

    free_gb = shutil.disk_usage(str(backup_root(target))).free / 1e9
    if free_gb < MIN_HOST_FREE_GB:
        return refusal("mac_disk_full",
                       f"This Mac has only {free_gb:.1f} GB free. Free up a few GB first.",
                       [{"id": "retry", "label": "Tell me when there's more space."}, STOP_OPTION])

    warnings: List[str] = []
    if src["product_type"] and verdict.get("product_type") and src["product_type"] != verdict["product_type"]:
        warnings.append(f"The backup came from a different model ({src['product_type']}); the phone is "
                        f"{verdict['product_type']}. Most things restore fine, but check afterwards.")
    return {
        "ok": True,
        "plan": {
            "udid": target,
            "phone_name_now": verdict.get("device_name"),
            "backup_name": src["device_name"],
            "backup_kind": src["kind"],
            "backup_made": src["date"].isoformat() if src["date"] else None,
            "last_backup_date": src["last_backup_date"].isoformat() if src["last_backup_date"] else None,
            "backup_made_text": _when(src["last_backup_date"] or src["date"]),
            "backup_label": src["label"],
            "available_backups": available_backups(target, src["path"]),
            "backup_age_hours": round(src["age_hours"], 1) if src["age_hours"] is not None else None,
            "backup_ios": src["product_version"],
            "phone_ios": dev_ver,
            "battery_pct": battery,
            "source_path": str(src["path"]),
            "setup_evidence": verdict["reasons"],
            "warnings": warnings,
        },
    }


# ---------------------------------------------------------------------------
# Password handling (never argv, never shown to the agent)
