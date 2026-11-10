"""
Backup labels (`backup_labels.py`): make this phone's backups show up DIFFERENTLY in Finder's "Restore from
this backup" pop-up.

Finder labels each entry with the backup's `Display Name` (Info.plist), plus the folder's own timestamp for the
timestamped copies Finder makes during restores. Several of this phone's copies share one identical label, so
the pop-up can't tell them apart. This tool rewrites the label only:

    Яблоко · main · Nov 9, 5:47 PM            the Mac's main backup folder
    Яблоко · safety copy · Nov 9, 5:47 PM     the protected -prewipe clone
    Яблоко · extra copy - Nov 9, 2026 at 8:39 PM   Finder's own working copies (Finder's own date is kept)

Guarantees:
  * only the `Display Name` string in Info.plist changes (the phone's real `Device Name` is left alone); nothing
    else in the backup is rewritten; the original is kept in an `Original Display Name` key and the previous
    Info.plist is saved under the state dir;
  * fully reversible (`--revert`), idempotent (re-run it after a restore to label Finder's new copies);
  * only this phone's folders are touched, never another phone's backups;
  * refused while a restore or backup is running.

Usage:  python3 backup_labels.py [--udid UDID]            preview (changes nothing)
        python3 backup_labels.py --apply | --revert
"""

from __future__ import annotations

import argparse
import plistlib
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from backup_picker import _DATE_SUFFIX
from common import backup_root, connected_udids, ensure_utf8_console, state_path, backup_device_name

SEP = " · "          # middle dot
ORIG_KEY = "Original Device Name"
PREWIPE = "-prewipe"


def _when(dt_utc_naive: Optional[datetime]) -> str:
    if not dt_utc_naive:
        return "unknown time"
    d = dt_utc_naive.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    return d.strftime("%b ") + str(d.day) + d.strftime(", %I:%M %p").replace(" 0", " ")


def label_for(kind: str, base: str, made: Optional[datetime]) -> str:
    base = base.strip()
    if kind == "main":
        return f"{base}{SEP}main{SEP}{_when(made)}"
    if kind == "safety":
        return f"{base}{SEP}safety copy{SEP}{_when(made)}"
    if kind == "older_safety":
        return f"{base}{SEP}older safety copy{SEP}{_when(made)}"
    if kind == "protected":
        return f"{base}{SEP}hand-protected copy{SEP}{_when(made)}"
    return f"{base}{SEP}extra copy"


def _kind(folder: str, udid: str) -> Optional[str]:
    n, u = folder.lower(), udid.lower()
    if n.startswith(".protected-") and u in n:
        return "protected"          # a copy someone protected by hand; Finder lists these too, so label them
    if n == u:
        return "main"
    if n == u + PREWIPE:
        return "safety"
    if re.fullmatch(re.escape(u) + r"-prewipe-\d{8}-\d{6}(-\d+)?", n):
        return "older_safety"      # an earlier generation of the safety copy, kept instead of overwritten
    if n.startswith(u + "-"):
        return "extra"
    return None


LABEL_KEY = "Display Name"            # what Finder's pop-up shows (verified live 2026-11-09; NOT `Device Name`)
LABEL_ORIG_KEY = "Original Display Name"
LEGACY_ORIG_KEY = ORIG_KEY            # an earlier version rewrote `Device Name`; migrate that back


def _xml_escape(s: str) -> bytes:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").encode("utf-8")


def _xml_set(raw: bytes, key: str, value: str) -> bytes:
    pat = re.compile(rb"(<key>" + re.escape(key.encode()) + rb"</key>\s*<string>)(.*?)(</string>)", re.S)
    m = pat.search(raw)
    if m:
        return raw[:m.start(2)] + _xml_escape(value) + raw[m.end(2):]
    # missing key: add it just before the closing </dict> of the top-level dictionary
    end = raw.rfind(b"</dict>")
    if end < 0:
        raise ValueError(f"no {key} in Info.plist and no dictionary to add it to")
    return raw[:end] + b"\t<key>" + key.encode() + b"</key>\n\t<string>" + _xml_escape(value) + b"</string>\n" + raw[end:]


def _xml_del(raw: bytes, key: str) -> bytes:
    return re.sub(rb"[ \t]*<key>" + re.escape(key.encode()) + rb"</key>\s*<string>.*?</string>\n?", b"", raw, count=1, flags=re.S)


def _rewrite(raw: bytes, changes: Dict[str, Optional[str]]) -> bytes:
    """Set (or, for None, delete) top-level string keys in an Info.plist, touching nothing else."""
    if raw.startswith(b"bplist"):
        # Finder writes its own working copies as binary plists: round-trip through plistlib (every type used in
        # an Info.plist survives); apply() still proves nothing but these keys changed
        d = plistlib.loads(raw)
        for k, v in changes.items():
            if v is None:
                d.pop(k, None)
            else:
                d[k] = v
        return plistlib.dumps(d, fmt=plistlib.FMT_BINARY)
    if not raw.lstrip().startswith(b"<?xml"):
        raise ValueError("Info.plist is neither XML nor binary; refusing to rewrite")
    for k, v in changes.items():
        raw = _xml_del(raw, k) if v is None else _xml_set(raw, k, v)
    return raw


def _read(path: Path) -> Dict[str, Any]:
    with path.open("rb") as f:
        d = plistlib.load(f)
    return d if isinstance(d, dict) else {}


def plan(udid: str) -> List[Dict[str, Any]]:
    root = backup_root(udid)
    rows: List[Dict[str, Any]] = []
    for p in sorted(root.iterdir()):
        kind = _kind(p.name, udid) if p.is_dir() else None
        info_path = p / "Info.plist"
        if not kind or not info_path.is_file():
            continue
        info = _read(info_path)
        owner = str(info.get("Unique Identifier") or info.get("Target Identifier") or "")
        if owner and owner.lower() != udid.lower():
            continue                                    # never another phone's backup
        real_name = backup_device_name(info)            # the phone's own name (raw: may carry a trailing space)
        current = str(info.get(LABEL_KEY) if info.get(LABEL_KEY) is not None else real_name)
        original = str(info.get(LABEL_ORIG_KEY) if info.get(LABEL_ORIG_KEY) is not None else current)
        made = info.get("Last Backup Date")
        if isinstance(made, datetime) and made.tzinfo is not None:
            made = made.astimezone(timezone.utc).replace(tzinfo=None)
        new = label_for(kind, real_name, made if isinstance(made, datetime) else None)
        if kind == "extra":
            # Finder writes its own timestamp into these copies' Display Name ("Яблоко - Nov 9, 2026 at 8:39 PM"):
            # keep it, it is what tells the copies apart
            m = _DATE_SUFFIX.search(original)
            if m:
                new += m.group(0).rstrip()
            elif isinstance(made, datetime):
                new += f"{SEP}{_when(made)}"      # rotated-aside backups have no Finder date: say when they were made
        rows.append({"folder": p.name, "kind": kind, "path": info_path, "original": original, "current": current,
                     "real_name": real_name, "legacy": LEGACY_ORIG_KEY in info, "new": new})
    return rows


def _busy() -> bool:
    try:
        return subprocess.run(["pgrep", "-f", "AppleMobileBackup|idevicebackup2"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def apply(udid: str, revert: bool = False, dry_run: bool = True) -> List[Dict[str, Any]]:
    rows = plan(udid)
    if not dry_run and _busy():
        raise RuntimeError("a restore or backup is running right now; try again when it has finished")
    pending: List[Dict[str, Any]] = []
    for r in rows:
        target = r["original"] if revert else r["new"]
        r["target"] = target
        r["changed"] = r["current"] != target or r["legacy"] or (revert and LABEL_ORIG_KEY in _read(r["path"]))
        if r["changed"]:
            changes: Dict[str, Optional[str]] = {LABEL_KEY: target,
                                                 LABEL_ORIG_KEY: None if revert else r["original"]}
            if r["legacy"]:       # undo the earlier version, which rewrote `Device Name`
                changes["Device Name"] = r["real_name"]
                changes[LEGACY_ORIG_KEY] = None
            raw = r["path"].read_bytes()
            new_raw = _rewrite(raw, changes)
            before, after = plistlib.loads(raw), plistlib.loads(new_raw)
            # prove nothing but these keys changes, for EVERY folder, before any write
            keys = (set(before) | set(after)) - set(changes)
            if any(before.get(k) != after.get(k) for k in keys):
                raise RuntimeError(f"refusing: {r['folder']} would change something other than the label")
            r["new_raw"] = new_raw
            pending.append(r)
    if dry_run:
        return rows
    saved = state_path("label_backups")
    saved.mkdir(parents=True, exist_ok=True)
    for r in pending:
        shutil.copy2(r["path"], saved / f"{r['folder']}.Info.plist.{datetime.now():%Y%m%d-%H%M%S}")
        tmp = r["path"].with_name("Info.plist.tmp-label")
        tmp.write_bytes(r.pop("new_raw"))
        tmp.replace(r["path"])
    return rows


def ensure_labels(udid: Optional[str], say=print, apply_changes: bool = True) -> Dict[str, Any]:
    """The hook the pipeline calls (after the safety copy, before a restore, after a restore). Idempotent, and it
    NEVER raises: a labeling problem must not stop a backup or a restore. Returns {"ok", "changed", "message"}.
    With apply_changes=False it only reports what would change."""
    try:
        if not udid:
            found = connected_udids()
            udid = found[0] if len(found) == 1 else None
        if not udid:
            return {"ok": False, "changed": 0, "message": "no single phone to label backups for"}
        rows = apply(udid, dry_run=not apply_changes)
        todo = [r for r in rows if r["changed"]]
        if not todo:
            return {"ok": True, "changed": 0, "message": "backup labels already up to date"}
        verb = "Relabelled" if apply_changes else "Would relabel"
        msg = f"{verb} {len(todo)} backup(s) so they show distinctly in Finder: " + "; ".join(r["target"] for r in todo[:3]) \
              + ("..." if len(todo) > 3 else "")
        say(f"LABELS: {msg}")
        return {"ok": True, "changed": len(todo), "message": msg}
    except Exception as exc:  # noqa: BLE001
        msg = f"couldn't label the backups for Finder ({exc}); the backup and restore themselves are unaffected"
        say(f"LABELS: {msg}")
        return {"ok": False, "changed": 0, "message": msg}


def main(argv: Optional[List[str]] = None) -> int:
    ensure_utf8_console()
    ap = argparse.ArgumentParser(description="Give this phone's backups distinct labels in Finder's pop-up.")
    ap.add_argument("--udid", default=None, help="The phone (default: the one connected)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--apply", action="store_true", help="Rewrite the labels")
    g.add_argument("--revert", action="store_true", help="Put the original names back")
    args = ap.parse_args(argv)
    udid = args.udid
    if not udid:
        found = connected_udids()
        if len(found) != 1:
            print("Plug in exactly one phone, or pass --udid.")
            return 2
        udid = found[0]
    try:
        rows = apply(udid, revert=args.revert, dry_run=not (args.apply or args.revert))
    except RuntimeError as exc:
        print(f"Not changed: {exc}")
        return 2
    verb = "reverted" if args.revert else "relabelled"
    live = args.apply or args.revert
    for r in rows:
        mark = ("(unchanged)" if not r["changed"] else (verb if live else "would change"))
        print(f"{r['folder'][-22:]:>22}  {r['current'].strip()!r}  ->  {r['target']!r}  {mark}")
    if not live:
        print("\nPreview only. Nothing was changed. Run with --apply to do it (--revert undoes it).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
