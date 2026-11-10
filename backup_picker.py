"""
Backup picker (`backup_picker.py`): choose the right entry in Finder's "Restore from this backup"
pop-up, and prove it is right before anything is clicked.

What the real pop-up looks like (captured 2026-11-09, Finder on macOS 15):
  * it lists EVERY backup on this Mac, for every phone: other people's phones included;
  * entries are labelled "<device name>" or "<device name> - <Mon d, yyyy at h:mm AM>". The date
    suffix is the folder's own timestamp (Finder's working copies of a restore), NOT when the phone
    was backed up, and several entries share the exact same label, so a label can't identify one;
  * the real backup time is the line under the pop-up, "Last Backed Up: Today, 5:47 PM", and it
    only appears once an entry is selected.

So the picker never trusts a label alone. It (1) keeps only entries whose device name matches the
phone we validated, (2) selects them one at a time, (3) reads "Last Backed Up" for each, and
(4) accepts the first whose time equals the validated backup's own capture time (Info.plist
`Last Backup Date`). Another phone's backup can't match the name; a stale or empty backup of the
same phone can't match the time. If nothing matches, it stops and reports every reading so the
agent (or person) decides: it never guesses.

The driver is the one from finder_restore.py (`snapshot()` and `click(scope, cls, name, exact, nth)`).
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

TOLERANCE = timedelta(minutes=3)          # Finder shows minutes; allow clock rounding
MAX_CANDIDATES = 12                        # never walk an unbounded menu

_DATE_SUFFIX = re.compile(r"(\s-\s[A-Z][a-z]{2,8}\.?\s\d{1,2},\s\d{4}\sat\s\d{1,2}:\d{2}\s?[AP]M)+\s*$")
_WHEN = re.compile(r"^(?P<day>today|yesterday|[A-Za-z]{3,9}\.? \d{1,2}, \d{4}|\d{1,2}/\d{1,2}/\d{2,4})"
                   r"(?:,| at)?\s+(?P<h>\d{1,2}):(?P<m>\d{2})\s*(?P<ap>[AP]M)$", re.I)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFC", s or "").replace("’", "'").replace(" ", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def entry_name(label: str) -> str:
    """The device-name part of a menu label (date suffixes removed, whitespace normalised)."""
    return _norm(_DATE_SUFFIX.sub("", label or ""))


def names_equal(a: str, b: str) -> bool:
    """True when entry label `a` belongs to the phone named `b`: its name, or `b` followed by a backup_labels.py
    description ("Яблоко · safety copy · Nov 9, 5:47 PM")."""
    x, y = entry_name(a).casefold(), entry_name(b).casefold()
    return x == y or x.startswith(y + " \u00b7 ")


def parse_menu(snap: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The pop-up's entries in menu order: {"index": 0-based position among menu items, "label", "name", "dated"}."""
    out = []
    pos = 0
    for it in snap["items"]:
        if it["cls"] != "menu item":
            continue
        label = it["name"] or it["value"]
        out.append({"index": pos, "label": label, "name": entry_name(label),
                    "dated": bool(_DATE_SUFFIX.search(label or ""))})
        pos += 1
    return out


def find_last_backed_up(snap: Dict[str, Any]) -> Optional[str]:
    for it in snap["items"]:
        if it["cls"] == "static text":
            t = _norm(it["value"] or it["name"])
            if t.lower().startswith("last backed up"):
                return t
    return None


def parse_when(raw: Optional[str], now: Optional[datetime] = None) -> Optional[datetime]:
    """'Last Backed Up: Today, 5:47 PM' -> local naive datetime. None when it can't be read (other
    languages, new formats): the caller must then NOT guess."""
    if not raw:
        return None
    text = _norm(raw.split(":", 1)[1] if raw.lower().startswith("last backed up") else raw)
    m = _WHEN.match(text)
    if not m:
        return None
    now = now or datetime.now()
    day = m["day"].lower()
    try:
        if day == "today":
            d = now.date()
        elif day == "yesterday":
            d = (now - timedelta(days=1)).date()
        elif "/" in day:
            mo, da, yr = (int(x) for x in day.split("/"))
            d = datetime(yr + (2000 if yr < 100 else 0), mo, da).date()
        else:
            d = datetime.strptime(m["day"].replace(".", ""), "%b %d, %Y").date()
        h = int(m["h"]) % 12 + (12 if m["ap"].upper() == "PM" else 0)
        return datetime(d.year, d.month, d.day, h, int(m["m"]))
    except ValueError:
        return None


def time_matches(shown_local: Optional[datetime], expected_utc_naive: Optional[datetime]) -> bool:
    if shown_local is None or expected_utc_naive is None:
        return False
    expected = expected_utc_naive.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)
    return abs(shown_local - expected) <= TOLERANCE


def _close_menu(driver: Any) -> None:
    """Closes an open pop-up menu (Escape) if the driver can; harmless when nothing is open."""
    esc = getattr(driver, "escape", None)
    if esc:
        try:
            esc()
        except Exception:  # noqa: BLE001
            pass


def _open_menu(driver: Any, sleep: Callable[[float], None]) -> bool:
    if driver.click("W", "pop up button", "") != "CLICKED":
        return False
    sleep(1.0)
    return True


def choose_backup(driver: Any, device_name: str, expected_utc_naive: Optional[datetime],
                  sleep: Callable[[float], None], say: Callable[[str], None] = lambda m: None,
                  snap: Optional[Dict[str, Any]] = None, expected_label: Optional[str] = None) -> Dict[str, Any]:
    """Selects the entry that is this phone's validated backup. Returns
    {"ok": True, "label", "shown", "kept": bool} or {"ok": False, "reason", "readings": [...]}.
    `expected_utc_naive` None means no capture time is known: then the name alone decides, and the result says so."""
    readings: List[Dict[str, Any]] = []
    snap = snap or driver.snapshot()
    popup = next((it for it in snap["items"] if it["cls"] == "pop up button"), None)
    current = (popup or {}).get("value") or (popup or {}).get("name") or ""
    shown = find_last_backed_up(snap)

    def acceptable(label: str, shown_raw: Optional[str]) -> bool:
        if not names_equal(label, device_name):
            return False
        if expected_label and entry_name(label).casefold() != entry_name(expected_label).casefold():
            return False                      # labels were made distinct (backup_labels.py): the exact entry is known
        if _DATE_SUFFIX.search(label or ""):
            # A dated entry is one of Finder's own working copies of an earlier restore. Observed live 2026-11-09:
            # Finder has no saved password for it, so the restore died with "password was incorrect"; the plain
            # entry (the Mac's own backup folder) restored silently. Prefer the plain entries.
            return False
        if expected_utc_naive is None:
            return True
        return time_matches(parse_when(shown_raw), expected_utc_naive)

    if acceptable(current, shown):
        return {"ok": True, "label": current, "shown": shown, "kept": True, "name_only": expected_utc_naive is None}
    readings.append({"label": current, "shown": shown, "selected": True})

    if not _open_menu(driver, sleep):
        return {"ok": False, "reason": "cannot_open_menu", "readings": readings}
    entries = parse_menu(driver.snapshot())
    if not entries:
        # Seen live 2026-11-09: a menu opened by a script is visible on screen but its entries are not exposed to
        # the accessibility tree (a real mouse click exposes them). Not a failure to guess around: hand it to the agent.
        _close_menu(driver)
        return {"ok": False, "reason": "menu_not_readable", "readings": readings}
    mine = [e for e in entries if names_equal(e["name"], device_name)]
    mine.sort(key=lambda e: (e["dated"], e["index"]))     # plain entries first, Finder's dated copies last
    mine = [e for e in mine if not e["dated"]] or mine
    if expected_label:
        exact = [e for e in mine if entry_name(e["label"]).casefold() == entry_name(expected_label).casefold()]
        mine = exact or mine
    others = len(entries) - len(mine)
    say(f"Finder lists {len(entries)} backups on this Mac: {len(mine)} for this phone, {others} for other phones "
        "(those are never touched).")
    if not mine:
        _close_menu(driver)
        return {"ok": False, "reason": "no_entry_for_phone", "readings": readings,
                "entries": [e["label"] for e in entries]}

    for n, e in enumerate(mine[:MAX_CANDIDATES]):
        if n:
            if not _open_menu(driver, sleep):
                return {"ok": False, "reason": "cannot_open_menu", "readings": readings}
        if driver.click("W", "menu item", "", exact=False, nth=e["index"] + 1) != "CLICKED":
            readings.append({"label": e["label"], "shown": None, "error": "could not select"})
            continue
        sleep(1.0)
        after = driver.snapshot()
        raw = find_last_backed_up(after)
        readings.append({"label": e["label"], "shown": raw})
        if expected_utc_naive is None or time_matches(parse_when(raw), expected_utc_naive):
            return {"ok": True, "label": e["label"], "shown": raw, "kept": False, "name_only": expected_utc_naive is None}
    _close_menu(driver)
    return {"ok": False, "reason": "no_matching_time", "readings": readings}
