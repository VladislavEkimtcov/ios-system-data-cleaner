"""
Backup management (`backup_manager.py`, also `restorer.py backups`).

Every backup run now creates a WHOLE NEW backup instead of overwriting the last one: the previous
backup is renamed aside (`<UDID>-<its timestamp>`) and an older safety copy is kept as
`<UDID>-prewipe-<its timestamp>`. Nothing is ever deleted automatically. That costs disk space, so this
module gives the agent what it needs to keep things tidy:

  * `list_backups(udid)`    every backup folder of this phone with its stats (when taken, iOS, app count,
                            2FA apps, Keychain size, size on disk, content fingerprint);
  * `propose_moves(...)`    the next sensible clean-up moves, in plain words, most useful first;
  * `trash_backup(...)`     move ONE backup to the Trash, only after the person has said yes, with guards.

The agent shows the list and the proposal, asks, and only then runs `--trash <folder>`.
Other phones' backups are listed for information (they take space too) but are never proposed or touched.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from common import backup_root, connected_udids, dir_size, ensure_utf8_console, host_words

_W = host_words()
_TRASH = "the Trash" if sys.platform == "darwin" else "the 'Removed Backups' folder"
import restore_gates as gates

GB = 1000 ** 3
LOW_DISK_GB = 40          # below this much free space the proposal says so
STAMP = r"\d{8}-\d{6}(?:-\d+)?"


def _kind(name: str, udid: str) -> Optional[str]:
    n, u = name.lower(), udid.lower()
    if n == u:
        return "main"
    if n == u + "-prewipe":
        return "safety"
    if re.fullmatch(re.escape(u) + "-prewipe-" + STAMP, n):
        return "older_safety"
    if re.fullmatch(re.escape(u) + "-" + STAMP, n):
        return "older"
    if re.fullmatch(re.escape(u) + r"-failed-" + STAMP, n):
        return "failed"
    if n.startswith(".protected-") and u in n:
        return "protected"
    if n.startswith(u + "-"):
        return "extra"
    return None


KIND_LABEL = {
    "main": "main backup (the live one)",
    "safety": "protected safety copy",
    "older_safety": "older safety copy",
    "older": "older backup",
    "failed": "failed backup (kept for inspection)",
    "protected": "hand-protected copy",
    "extra": "extra copy",
}


def _fingerprint(path: Path) -> Optional[str]:
    """Hash of Manifest.db: two folders with the same fingerprint hold the same backup."""
    try:
        h = hashlib.sha256()
        with (path / "Manifest.db").open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()[:16]
    except OSError:
        return None


def list_backups(udid: str) -> List[Dict[str, Any]]:
    """Every folder in the backup root that belongs to this phone, with its stats. Newest first."""
    root = backup_root(udid)
    out: List[Dict[str, Any]] = []
    try:
        folders = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return out
    for p in folders:
        kind = _kind(p.name, udid)
        if not kind:
            continue
        meta = gates.inspect_source(p, udid)
        made = meta["last_backup_date"] or meta["date"]
        facts = gates.backup_facts(p)
        out.append({
            "folder": p.name, "path": p, "kind": kind, "label": KIND_LABEL[kind],
            "made": made, "made_text": gates._when(made), "ios": meta["product_version"],
            "usable": not meta["problems"], "problems": meta["problems"],
            "facts": facts, "fingerprint": _fingerprint(p),
            "size_gb": facts.get("size_gb") or 0.0,
        })
    out.sort(key=lambda b: b["made"] or datetime.min, reverse=True)
    return out


def other_phones(udid: str) -> List[Dict[str, Any]]:
    """Backups of OTHER phones: information only (they use disk too); never proposed, never touched."""
    root = backup_root(udid)
    rows = []
    try:
        for p in sorted(root.iterdir()):
            if p.is_dir() and not p.name.lower().startswith(udid.lower()) and not p.name.startswith(".") \
                    and (p / "Manifest.plist").is_file():
                rows.append({"folder": p.name, "size_gb": round((dir_size(p) or 0) / GB, 2)})
    except OSError:
        pass
    return rows


def free_gb(udid: str) -> Optional[float]:
    try:
        return round(shutil.disk_usage(str(backup_root(udid))).free / GB, 1)
    except OSError:
        return None


# ---------------------------------------------------------------------------
# The proposal (pure: takes the list, returns moves)
# ---------------------------------------------------------------------------

def propose_moves(backups: List[Dict[str, Any]], free: Optional[float] = None) -> List[Dict[str, Any]]:
    """Clean-up moves, most useful first. Each: {folder, action: "trash", why, frees_gb}.
    Never proposed: hand-protected copies, the live main backup, the newest safety copy, the only backup
    holding the most apps, or anything that would leave the phone without a usable backup."""
    usable = [b for b in backups if b["usable"]]
    apps = lambda b: (b["facts"].get("app_count") or 0)       # noqa: E731
    best_apps = max((apps(b) for b in usable), default=0)
    keepers = set()
    if usable:
        keepers.add(usable[0]["folder"])                                          # the newest usable backup
        richest = max(usable, key=lambda b: (apps(b), b["made"] or datetime.min))  # the newest with the most apps
        keepers.add(richest["folder"])
    never = {b["folder"] for b in backups if b["kind"] in ("protected", "main", "safety")}
    moves: List[Dict[str, Any]] = []
    taken: set = set()

    def add(b: Dict[str, Any], why: str) -> None:
        if b["folder"] in taken or b["folder"] in never or b["folder"] in keepers:
            return
        taken.add(b["folder"])
        moves.append({"folder": b["folder"], "action": "trash", "why": why, "frees_gb": b["size_gb"]})

    # 1. failed backups: no use to anyone
    for b in backups:
        if b["kind"] == "failed":
            add(b, "a backup that failed verification; it can't be restored from")
        elif not b["usable"] and not (Path(b["path"]) / "Manifest.plist").is_file():
            # a run that stopped before the phone sent anything (seen 2026-11-11: 0 GB, no Manifest.plist)
            add(b, "an unfinished backup (it stopped before the phone sent its data); it can't be restored from")
    # 2. exact duplicates (same Manifest.db): keep the most protected one, offer the rest
    rank = {"protected": 0, "safety": 1, "main": 2, "older_safety": 3, "extra": 4, "older": 5, "failed": 6}
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for b in backups:
        if b["fingerprint"]:
            groups.setdefault(b["fingerprint"], []).append(b)
    for grp in groups.values():
        if len(grp) > 1:
            grp.sort(key=lambda b: rank.get(b["kind"], 9))
            for dup in grp[1:]:
                add(dup, f"an exact duplicate of the {grp[0]['label']} ({grp[0]['folder']}): same data, so nothing is lost")
    # 3. superseded: older than another usable backup that has at least as many apps
    for b in sorted(backups, key=lambda x: x["made"] or datetime.min):
        if b["kind"] in ("older", "older_safety", "extra") and b["usable"]:
            newer = [o for o in usable if o is not b and (o["made"] or datetime.min) > (b["made"] or datetime.min)
                     and apps(o) >= apps(b)]
            if newer:
                add(b, f"superseded: a newer backup ({newer[0]['made_text']}) has the same or more apps "
                       f"({apps(newer[0])} vs {apps(b)})")
    # 4. low-app backups are only ever kept for their data: say so but don't push deletion
    return moves


def describe(backups: List[Dict[str, Any]], moves: List[Dict[str, Any]], others: List[Dict[str, Any]],
             free: Optional[float]) -> List[str]:
    """Plain-language lines for the chat. The agent shows these as they are."""
    lines = [f"BACKUPS_AVAILABLE: {len(backups)} backup(s) of this phone on this {_W['computer']}"
             + (f" ({free} GB free on the disk)." if free is not None else ".")]
    best = max((b["facts"].get("app_count") or 0 for b in backups), default=0)
    for b in backups:
        state = "usable" if b["usable"] else "NOT usable: " + "; ".join(b["problems"])
        lines.append(f"  - {b['label']} [{b['folder']}]: data as of {b['made_text']}, iOS {b['ios'] or '?'}, {state}")
        lines.append("      " + gates._facts_line(b["facts"]))
        n = b["facts"].get("app_count")
        if n is not None and best >= 5 and n < best / 2:
            lines.append(f"      WARNING: only {n} apps listed (the best backup lists {best}). Restoring this one would "
                         "likely bring the data back WITHOUT most of the App Store apps.")
    if others:
        total = sum(o["size_gb"] for o in others)
        lines.append(f"  (Also on this {_W['computer']}, other phones' backups, not touched: "
                     + ", ".join(f"{o['folder']} {o['size_gb']} GB" for o in others) + f"; {round(total, 1)} GB in all.)")
    if moves:
        top = moves[0]
        lines.append(f"PROPOSED_NEXT_MOVE: move '{top['folder']}' to {_TRASH}. Why: {top['why']}. "
                     f"Up to {top['frees_gb']} GB (copies that share data free less). Ask the person first; "
                     f"run `{_W['python']} restorer.py backups --trash {top['folder']}` only after a yes.")
        for m in moves[1:3]:
            lines.append(f"  then maybe: '{m['folder']}': {m['why']} (up to {m['frees_gb']} GB)")
    elif free is not None and free < LOW_DISK_GB:
        lines.append(f"PROPOSED_NEXT_MOVE: the disk is getting full ({free} GB free) and every backup above has a reason to "
                     "stay. Tell the person, and suggest clearing space elsewhere or removing another phone's old backup "
                     "(their call; I won't touch those).")
    else:
        lines.append("PROPOSED_NEXT_MOVE: nothing to clean up: every backup above is the newest of its kind or holds "
                     "something the others don't.")
    return lines


# ---------------------------------------------------------------------------
# The only deleting: one folder, to the Trash, after a yes
# ---------------------------------------------------------------------------

REMOVED_DIR_NAME = "Removed Backups"


def trash_backup(udid: str, folder: str, backups: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Moves ONE of this phone's backups to the Trash (recoverable). Refuses protected/live/safety copies and
    anything that would leave the phone without a usable backup or lose the best app count."""
    backups = backups if backups is not None else list_backups(udid)
    by = {b["folder"]: b for b in backups}
    b = by.get(folder)
    if not b:
        return {"ok": False, "message": f"'{folder}' is not one of this phone's backups on this {_W['computer']}."}
    if b["kind"] in ("protected", "main", "safety"):
        return {"ok": False, "message": f"'{folder}' is the {b['label']}: I won't trash it. "
                                         f"(If the person really wants it gone, they can do it in {'Finder' if sys.platform == 'darwin' else 'File Explorer'}.)"}
    rest = [o for o in backups if o["folder"] != folder and o["usable"]]
    if b["usable"]:
        if not rest:
            return {"ok": False, "message": "That is the only usable backup of this phone. Not trashing it."}
        best = max((o["facts"].get("app_count") or 0) for o in backups)
        if (b["facts"].get("app_count") or 0) == best and best and \
                max((o["facts"].get("app_count") or 0) for o in rest) < best:
            return {"ok": False, "message": f"That is the only backup listing all {best} apps. Not trashing it."}
    if sys.platform == "darwin":
        trash, where, how = Path.home() / ".Trash", "the Trash", "empty the Trash to free it"
    elif sys.platform == "win32":
        # Not the Recycle Bin: for folders this large the shell may delete permanently without asking. A sibling
        # folder of the backup root is on the same volume (an instant rename), recoverable, and outside the folder
        # iTunes lists.
        trash = Path(b["path"]).parent.parent / REMOVED_DIR_NAME
        where, how = f"'{trash}'", "delete that folder to free the space"
    else:
        return {"ok": False, "message": "Moving a backup aside is only automated on macOS and Windows; remove it by hand."}
    trash.mkdir(exist_ok=True)
    dest = trash / b["folder"]
    if dest.exists():
        dest = trash / f"{b['folder']} {time.strftime('%H.%M.%S')}"
    os.replace(str(b["path"]), str(dest))
    return {"ok": True, "message": f"Moved '{folder}' to {where} (about {b['size_gb']} GB; {how}).",
            "trashed": folder, "moved_to": str(dest)}


def main(argv: Optional[List[str]] = None) -> int:
    ensure_utf8_console()
    ap = argparse.ArgumentParser(description="List this phone's backups with stats and propose the next clean-up move.")
    ap.add_argument("--udid", default=None)
    ap.add_argument("--trash", metavar="FOLDER", help="Move this one backup to the Trash (only after the person said yes).")
    args = ap.parse_args(argv)
    udid = args.udid or (connected_udids() or [None])[0]
    if not udid:
        print("[!] No phone connected (pass --udid).")
        return 2
    if args.trash:
        res = trash_backup(udid, args.trash)
        print(res["message"])
        return 0 if res["ok"] else 1
    backups = list_backups(udid)
    free = free_gb(udid)
    for ln in describe(backups, propose_moves(backups, free), other_phones(udid), free):
        print(ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())
