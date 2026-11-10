"""
Finder UI restore (`finder_restore.py`): put a verified backup back on an erased iPhone by driving
Finder's "Welcome to your new iPhone" screen. No device-restore command-line tools are involved
(the CLI restore booted empty phones; the Finder restore is the only route proven to work).

How it works: a small state machine over Finder's accessibility tree.

  snapshot()  ->  classify()  ->  one deterministic action  ->  look again

Elements are found by role and name (never fixed coordinates), and every action is verified by
the next snapshot. The state machine is idempotent: it decides what to do from what Finder shows
right now, so re-running it after a stop simply picks up where the screen is.

When a step doesn't do what it should (an element is missing, a click doesn't register, an
unexpected sheet appears), the run stops with a structured `agent_handoff` result: what was
expected, what was seen, and exact instructions for the agent to fix it up with the
computer-use MCP (screenshot, click, then resume with `--resume`). The agent does the fix; the
state machine then carries on.

Safety rules that never bend:
  * the gates in restore_gates.py pass first (one phone, erased, fresh encrypted backup, ...),
    and the person has said the phone shows 'Hello' (`--hello-confirmed`);
  * Restore is only ever clicked on the password sheet of a Welcome-view restore this run
    started. The Summary view's "Restore Backup..." button is never touched: on a phone that
    isn't erased it would overwrite live data;
  * the backup chosen in Finder must be the expected one; a mismatch stops the run;
  * the backup password is never typed or read by this code. A Keychain-prefilled sheet is
    confirmed; an empty one is left for the person to fill in (we only watch).

Exit codes: 0 restored, 1 failed, 2 refused by a gate (nothing changed), 3 finished but not
confirmed (look at the phone), 4 agent handoff (fix the screen, then `--resume`).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from common import connected_udids, ensure_utf8_console, state_path
import restore_gates as gates

STATUS_NAME = ".ui_restore_status.json"
RESUME_CMD = "python3 restorer.py ui-restore --resume --confirm --hello-confirmed"
RESUME_MAX_AGE_SEC = 4 * 3600
POLL_SEC = 3.0
RECLICK_AFTER_SEC = 20.0   # Finder keeps the sheet up while it checks the password: don't press again meanwhile
UNKNOWN_TICKS_BEFORE_HANDOFF = 5
WRONG_PASSWORDS_BEFORE_HELPER = 2   # offer the recovery server once the person has failed this many times
HELPER_PORT = 8765
CLICK_RETRIES = 3

# Phrases (lower-case) Finder shows in static text while the restore is running.
RESTORED_STATE = "RestoredFromiTunesBackup"
PLACEHOLDER_RE = re.compile(r"^Item \d+$")
RESTORING_PHRASES = ("restoring", "waiting for iphone to restart", "preparing iphone", "extracting")


# ---------------------------------------------------------------------------
# Accessibility layer (the only place that talks to System Events)
# ---------------------------------------------------------------------------

class FinderUIError(Exception):
    """Finder could not be scripted at all (permissions, Finder not running)."""


_ELEMENT_CLASSES = ('"button", "radio button", "pop up button", "text field", "static text", '
                    '"checkbox", "progress indicator", "menu item"')


def _collect_block(prefix: str, scope: str) -> str:
    return f'''
        try
            set els to entire contents of {scope}
            repeat with e in els
                try
                    set c to (class of e) as text
                    if c is in {{{_ELEMENT_CLASSES}}} then
                        set n to ""
                        try
                            set n to (name of e) as text
                        end try
                        set v to ""
                        try
                            set v to (value of e) as text
                        end try
                        set en to "true"
                        try
                            set en to (enabled of e) as text
                        end try
                        set end of out to "{prefix}|||" & c & "|||" & n & "|||" & v & "|||" & en
                    end if
                end try
            end repeat
        end try
'''


def snapshot_script(window_match: str) -> str:
    return f'''
    tell application "System Events" to tell process "Finder"
        set out to {{}}
        repeat with w in windows
            set wName to name of w
            if {window_match} then
                set end of out to "WINDOW|||" & wName
                {_collect_block("W", "w")}
                try
                    if (count of sheets of w) > 0 then
                        set end of out to "SHEET|||"
                        {_collect_block("S", "sheet 1 of w")}
                    end if
                end try
                exit repeat
            end if
        end repeat
        set AppleScript's text item delimiters to linefeed
        return out as text
    end tell
    '''


def click_script(window_match: str, scope: str, cls: str, name: str, exact: bool, nth: int = 1) -> str:
    """Clicks the first enabled element of class `cls` whose name starts with (or equals) `name`
    in the window ("W") or its sheet ("S")."""
    from restorer import _applescript_str
    target = "w" if scope == "W" else "sheet 1 of w"
    if not name:
        test = "true"  # empty name = any element of this class (the pop-up button, a menu item by position)
    else:
        test = f"n is {_applescript_str(name)}" if exact else f"n starts with {_applescript_str(name)}"
    return f'''
    tell application "System Events" to tell process "Finder"
        repeat with w in windows
            set wName to name of w
            if {window_match} then
                try
                    set hits to 0
                    set els to entire contents of {target}
                    repeat with e in els
                        try
                            set c to (class of e) as text
                            if c is {_applescript_str(cls)} then
                                set n to ""
                                try
                                    set n to (name of e) as text
                                end try
                                if {test} then
                                    set hits to hits + 1
                                    if hits is {int(nth)} then
                                        set en to true
                                        try
                                            set en to enabled of e
                                        end try
                                        if en is false then return "DISABLED"
                                        click e
                                        return "CLICKED"
                                    end if
                                end if
                            end if
                        end try
                    end repeat
                end try
                return "NOT_FOUND"
            end if
        end repeat
        return "NO_WINDOW"
    end tell
    '''


def _osa(script: str, timeout: float = 60.0) -> str:
    try:
        res = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FinderUIError("Finder took too long to answer (the window may be busy)") from exc
    except OSError as exc:
        raise FinderUIError(f"osascript is unavailable: {exc}") from exc
    if res.returncode != 0:
        err = (res.stderr or "").strip()
        if "assistive" in err.lower() or "not allowed" in err.lower() or "-1743" in err or "-25211" in err:
            raise FinderUIError("macOS blocked scripting Finder: allow Accessibility (and Automation) for the "
                                "app running this (System Settings > Privacy & Security)")
        raise FinderUIError(err or f"osascript exited {res.returncode}")
    return res.stdout.strip()


def parse_snapshot(text: str) -> Dict[str, Any]:
    """Turns the script's lines into {"window": str|None, "has_sheet": bool, "items": [...]}."""
    snap: Dict[str, Any] = {"window": None, "has_sheet": False, "items": []}
    for line in (text or "").splitlines():
        parts = line.split("|||")
        if parts[0] == "WINDOW":
            snap["window"] = parts[1] if len(parts) > 1 else ""
        elif parts[0] == "SHEET":
            snap["has_sheet"] = True
        elif parts[0] in ("W", "S") and len(parts) >= 5:
            # AppleScript prints a literal "missing value" for attributes an element doesn't have
            name, value = ("" if x == "missing value" else x for x in (parts[2], parts[3]))
            snap["items"].append({"scope": parts[0], "cls": parts[1], "name": name,
                                  "value": value, "enabled": parts[4].lower() != "false"})
    return snap


class FinderDriver:
    """The real thing: System Events scripting against the device's Finder window."""

    def __init__(self, udid: str):
        from restorer import device_window_match
        self.udid = udid
        self._match = device_window_match(udid)

    def snapshot(self) -> Dict[str, Any]:
        return parse_snapshot(_osa(snapshot_script(self._match), timeout=90))

    def click(self, scope: str, cls: str, name: str, exact: bool = False, nth: int = 1) -> str:
        return _osa(click_script(self._match, scope, cls, name, exact, nth))

    def escape(self) -> None:
        _osa('tell application "System Events" to key code 53', timeout=10)

    def open_device(self) -> Dict[str, Any]:
        from restorer import select_device_in_finder
        return select_device_in_finder(self.udid)


# ---------------------------------------------------------------------------
# Classifying what Finder shows (pure, unit-tested)
# ---------------------------------------------------------------------------

def _texts(snap: Dict[str, Any], scope: Optional[str] = None) -> List[str]:
    out = []
    for it in snap["items"]:
        if it["cls"] == "static text" and (scope is None or it["scope"] == scope):
            out.append((it["value"] or it["name"]).strip())
    return [t for t in out if t]


def _find(snap: Dict[str, Any], cls: str, prefix: str, scope: Optional[str] = None) -> Optional[Dict[str, Any]]:
    for it in snap["items"]:
        if it["cls"] == cls and it["name"].lower().startswith(prefix.lower()) and (scope is None or it["scope"] == scope):
            return it
    return None


def classify(snap: Dict[str, Any]) -> str:
    """One of: no_window, password_sheet, other_sheet, welcome, restoring, activating, summary, unknown."""
    if not snap.get("window"):
        return "no_window"
    if snap.get("has_sheet"):
        if any(it["scope"] == "S" and it["cls"] == "text field" for it in snap["items"]):
            return "password_sheet"
        return "other_sheet"
    if _find(snap, "radio button", "Restore from this backup", "W"):
        return "welcome"
    blob = " ".join(_texts(snap, "W")).lower()
    if any(p in blob for p in RESTORING_PHRASES):
        return "restoring"
    if "activating" in blob:
        return "activating"
    if _find(snap, "button", "Back Up Now", "W") or _find(snap, "button", "Check for Update", "W"):
        return "summary"
    return "unknown"


def sheet_confirm_button(snap: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for label in ("Restore", "Continue", "OK"):
        it = _find(snap, "button", label, "S")
        if it:
            return it
    return None


def sheet_text(snap: Dict[str, Any]) -> str:
    return " ".join(_texts(snap, "S"))


def _describe(snap: Dict[str, Any]) -> Dict[str, Any]:
    """A compact, password-free summary of the screen for logs and handoffs."""
    return {
        "window": snap.get("window"),
        "has_sheet": snap.get("has_sheet"),
        "state": classify(snap),
        "texts": _texts(snap)[:12],
        "buttons": [{"name": it["name"], "scope": it["scope"], "enabled": it["enabled"]}
                    for it in snap["items"] if it["cls"] == "button" and it["name"]][:12],
        "radios": [{"name": it["name"], "value": it["value"]} for it in snap["items"] if it["cls"] == "radio button"],
        "popups": [it["value"] or it["name"] for it in snap["items"] if it["cls"] == "pop up button"],
    }


# ---------------------------------------------------------------------------
# Handoff to the agent
# ---------------------------------------------------------------------------

def handoff(step: str, why: str, snap: Optional[Dict[str, Any]], do: str, **extra: Any) -> Dict[str, Any]:
    """The run stops here and the agent takes over with the computer-use MCP."""
    return {
        "ok": False, "code": "agent_handoff", "step": step, "message": why,
        "seen": _describe(snap) if snap else None,
        "agent_instructions": [
            "Use the computer-use MCP: request_access for Finder, then take a screenshot of the iPhone's Finder window.",
            do,
            "Never type or read the backup password; if a password box is empty, ask the person to type it (and tell "
            "them a box is waiting for them).",
            "Never click 'Restore Backup...' in the Summary view, and never click anything on a phone that is not erased.",
            f"When the screen is fixed, resume the deterministic run: {RESUME_CMD}",
        ],
        "resume": RESUME_CMD,
        **extra,
    }


def _password_rejected(text: str) -> bool:
    t = text.lower()
    return "password" in t and ("incorrect" in t or "wrong" in t or "try again" in t)


# ---------------------------------------------------------------------------
# The password recovery server (offered when the person keeps getting the password wrong)
# ---------------------------------------------------------------------------

def launch_password_helper(udid: str, wait_sec: float = 20.0) -> Optional[Tuple[str, Any]]:
    """Starts password_tracker.py's local web page in the background (no browser pop-up: the person is
    OFFERED the link, not surprised by it). Returns (url, process), or None if it didn't come up."""
    import threading
    cmd = [sys.executable, str(Path(__file__).with_name("password_tracker.py")), "--udid", udid,
           "--port", str(HELPER_PORT), "--exit-on-success", "--no-browser"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except OSError:
        return None
    found: List[str] = []

    def reader() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            if "http://" in line and not found:
                found.append("http://" + line.split("http://", 1)[1].strip())
    threading.Thread(target=reader, daemon=True).start()
    deadline = time.monotonic() + wait_sec
    while not found and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    if not found:
        proc.terminate()
        return None
    return found[0], proc


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------

class Run:
    """Holds one restore run. `driver` and the timing hooks are injectable so the whole flow is
    testable against a scripted Finder."""

    def __init__(self, udid: str, plan: Dict[str, Any], driver: Any, *,
                 sleep: Callable[[float], None] = time.sleep,
                 now: Callable[[], float] = time.monotonic,
                 notify: Optional[Callable[[str], None]] = None,
                 say: Callable[[str], None] = gates.say,
                 status: Optional[Callable[..., None]] = None,
                 devices: Callable[[], List[str]] = connected_udids,
                 phone_name: Optional[Callable[[], str]] = None,
                 poll: float = POLL_SEC, finish_timeout: float = 1500.0,
                 launch_helper: Optional[Callable[[], Optional[Tuple[str, Any]]]] = None,
                 setup_state: Optional[Callable[[], str]] = None,
                 restore_active: Optional[Callable[[], bool]] = None):
        self.udid, self.plan, self.driver = udid, plan, driver
        self.sleep, self.now, self.poll = sleep, now, poll
        self.finish_timeout = finish_timeout
        self.launch_helper = launch_helper or (lambda: launch_password_helper(udid))
        self.wrong_passwords = 0
        self.rejected_showing = False
        self.last_restore_click: Optional[float] = None
        self.helper_url: Optional[str] = None
        self.helper_proc: Any = None
        self.helper_reported = False
        self.notify = notify or (lambda msg: None)
        self.say = say
        self.status = status or (lambda **kw: None)
        self.devices = devices
        self.phone_name = phone_name or (lambda: "")
        self.setup_state = setup_state or (lambda: RESTORED_STATE)
        self.restore_active = restore_active or (lambda: False)
        self.visited: List[str] = []
        self.restore_started = False
        self.sheet_first_seen: Optional[float] = None
        self.sheet_prefilled = False
        self.sheet_notified = False
        self.continue_clicks = 0
        self.welcome_unsettled = 0
        self.backup_verified = False
        self.restore_clicks = 0
        self.last_text = ""

    # -- steps --------------------------------------------------------------

    def _welcome(self, snap: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        expected = (self.plan.get("backup_name") or "").strip()
        popup = next((it for it in snap["items"] if it["cls"] == "pop up button"), None)
        chosen = ((popup or {}).get("value") or (popup or {}).get("name") or "").strip()
        if not chosen or PLACEHOLDER_RE.match(chosen):
            # Seen live: right after the Welcome view appears the pop-up reads "Item 1" (a
            # placeholder) before Finder fills in the backup's name. Judge it on a later look.
            self.welcome_unsettled += 1
            if self.welcome_unsettled >= UNKNOWN_TICKS_BEFORE_HANDOFF:
                return handoff("choose_backup", "The backup pop-up never showed a backup name.", snap,
                               f"Look at the pop-up next to 'Restore from this backup' and make sure it shows '{expected}'.")
            return None
        if not self.backup_verified:
            out = self._pick_backup(expected, snap)
            if out:
                return out
        radio = _find(snap, "radio button", "Restore from this backup", "W")
        if radio and radio["value"] not in ("1", "true"):
            if self.driver.click("W", "radio button", "Restore from this backup") != "CLICKED":
                return handoff("choose_restore_option", "I couldn't select 'Restore from this backup'.",
                               snap, "Click the 'Restore from this backup' option, then click Continue.")
            self.sleep(1.0)
        res = self.driver.click("W", "button", "Continue", exact=True)
        self.continue_clicks += 1
        if res != "CLICKED":
            return handoff("continue", f"The Continue button wasn't clickable ({res}).", snap,
                           "Make sure 'Restore from this backup' is selected with the right backup, then click Continue.")
        self.say("Chose the backup in Finder and pressed Continue.")
        self.sleep(2.0)
        return None

    def _pick_backup(self, expected: str, snap: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Proves Finder has THIS phone's validated backup selected (name + capture time), selecting it
        if needed. Returns a handoff when it can't be proven; the run then stops before Continue."""
        import backup_picker
        last = self.plan.get("last_backup_date")
        expected_dt = datetime.fromisoformat(last) if last else None
        res = backup_picker.choose_backup(self.driver, expected, expected_dt, self.sleep, self.say, snap=snap,
                                          expected_label=self.plan.get("backup_label") if (self.plan.get("backup_label") or "").strip() != expected else None)
        if res["ok"]:
            self.backup_verified = True
            how = "already selected" if res.get("kept") else "selected it"
            self.say(f"RESTORING_FROM: Finder's entry '{res['label'].strip()}' ({how}); it shows "
                     f"'{res['shown']}'. That matches the backup I checked ({self.plan.get('backup_made_text') or last}).")
            if res.get("name_only"):
                self.say("Heads up: no capture time was recorded for this backup, so only the phone's name could be matched.")
            return None
        readings = res.get("readings") or []
        seen = "; ".join(f"'{r.get('label', '').strip()}' shows '{r.get('shown')}'" for r in readings)
        why = {"cannot_open_menu": "I couldn't open the backup pop-up.",
               "menu_not_readable": "Finder's backup pop-up opens, but a script can't read its entries. The entry Finder "
                                    "preselected doesn't show the expected time, so I need to look at the entries.",
               "no_entry_for_phone": f"Finder's pop-up has no backup for '{expected}'.",
               "no_matching_time": f"None of this phone's backups in Finder shows the expected time ({self.plan.get('backup_made_text') or last}). {seen}"}.get(
            res["reason"], "I couldn't confirm which backup is selected.")
        label = (self.plan.get("backup_label") or "").strip()
        exact = (f"Select the entry named exactly '{label}'. " if label and label != expected else "")
        return handoff("choose_backup", why + " I stopped before pressing Continue so the wrong backup is never restored.", snap,
                       f"Open the backup pop-up (press Escape to close any open menu) and look at the entries named '{expected}'. {exact}"
                       f"Select an entry with NO date after its name (the plain '{expected}'; dated ones are Finder's own copies, which have no saved "
                       f"password), and check its 'Last Backed Up' line reads {self.plan.get('backup_made_text') or last}. "
                       "If none does, stop and tell the person what Finder lists.", readings=readings)

    def _password(self, snap: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if _password_rejected(sheet_text(snap)):
            # Finder re-shows the sheet with "...was incorrect. Please try again." Seen live 2026-11-09: its OK
            # button stays enabled on an EMPTY box, so pressing it is pointless. Never press anything here:
            # the box belongs to the person now. Count the rejection once per appearance of this message.
            if not self.rejected_showing:
                self.rejected_showing = True
                self.wrong_passwords += 1
                self.sheet_notified = True
                self.say("Finder says that password was incorrect. The box is waiting for another try; I won't press anything.")
                self.notify("The backup password was rejected. Please type it in the box in Finder, or ask the assistant for help.")
                if self.restore_clicks > 0 or self.wrong_passwords >= WRONG_PASSWORDS_BEFORE_HELPER:
                    self._offer_helper()  # the Keychain's password failed, or the person has now missed twice
            return None
        self.rejected_showing = False
        btn = sheet_confirm_button(snap)
        if not btn:
            if self.restore_clicks >= CLICK_RETRIES:
                return handoff("password_sheet", "The password sheet has no Restore button I can find.", snap,
                               "Look at the sheet. If a password is filled in, click Restore; if not, ask the person to type it.")
            return None
        t = self.now()
        if self.sheet_first_seen is None:
            self.sheet_first_seen = t
            self.sheet_prefilled = btn["enabled"]
            return None  # the sheet slides in: judge it on the next look, not during the animation
        stable = (t - self.sheet_first_seen) >= 1.5
        waited = self.last_restore_click is None or (t - self.last_restore_click) >= RECLICK_AFTER_SEC
        if self.sheet_prefilled and btn["enabled"] and stable and waited and self.restore_clicks < CLICK_RETRIES:
            res = self.driver.click("S", "button", btn["name"], exact=True)
            self.restore_clicks += 1
            self.last_restore_click = t
            if res == "CLICKED":
                self.restore_started = True
                self.say("The password was already filled in from your Keychain, so I pressed Restore.")
                self.status(phase="restoring", summary="Restore started in Finder")
                self.sleep(2.0)
                return None
            return handoff("password_sheet", f"The Restore button didn't accept the click ({res}).", snap,
                           "Click Restore on the password sheet.")
        if self.sheet_prefilled and self.restore_clicks >= CLICK_RETRIES:
            return handoff("password_sheet", "I pressed Restore on the password sheet several times and it's still showing.",
                           snap, "Look at the sheet for an error, then click Restore yourself.")
        if not self.sheet_prefilled and not self.sheet_notified:
            self.sheet_notified = True
            self.notify("Please type your iPhone backup password in the box in the Finder window on your Mac, "
                        "then press Restore.")
            self.say("Finder is asking for the backup password. Please type it into the box on your Mac "
                     "(I can't see or type it) and press Restore. I'll carry on by myself afterwards.")
        # An empty sheet is the person's: they may be mid-password, so we never click for them.
        return None

    def _other_sheet(self, snap: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        text = sheet_text(snap).lower()
        if _password_rejected(text) or ("password" in text and "couldn" in text):
            # The person typed a wrong password. Dismiss Finder's alert (the only click we make on it),
            # keep watching, and after repeated failures offer the recovery server.
            if self.driver.click("S", "button", "OK") != "CLICKED":
                return handoff("wrong_password_alert", "Finder says the password is wrong and I couldn't dismiss the alert.",
                               snap, "Click OK on the alert, so the person can try the password again.")
            self.wrong_passwords += 1
            self.sheet_first_seen = None  # the password sheet comes back: judge it afresh
            self.say(f"Finder says that password was wrong (attempt {self.wrong_passwords}). The box is waiting for another try.")
            if self.wrong_passwords >= WRONG_PASSWORDS_BEFORE_HELPER:
                self._offer_helper()
            return None
        return handoff("unexpected_sheet", "Finder is showing a message I don't recognise.", snap,
                       "Read the message on the sheet. If it only needs dismissing (OK), click OK. If it reports a "
                       "problem, stop and tell the person in plain words.")

    def _offer_helper(self) -> None:
        if self.helper_url:
            return
        started = self.launch_helper()
        if not started:
            self.say("The password still isn't working. I couldn't start the password helper page; you can look in "
                     "Keychain Access (search 'iOS Backup') or keep trying.")
            return
        self.helper_url, self.helper_proc = started
        self.status(password_helper_url=self.helper_url)
        self.notify("The backup password isn't working. A password helper page is ready; ask the assistant for the link.")
        self.say("OFFER_PASSWORD_HELPER: The backup password hasn't worked a couple of times. I started a password helper "
                 f"page on this Mac that tries what you remember and common variations: {self.helper_url} . Offer it to "
                 "the person (open it for them if they say yes). The Finder box keeps waiting meanwhile.")

    def _check_helper(self) -> None:
        proc = self.helper_proc
        if proc is None or self.helper_reported or proc.poll() is None:
            return
        self.helper_reported = True
        if proc.returncode == 0:
            self.say("PASSWORD_FOUND: The helper page found the right password and saved it to the Keychain. Tell the person "
                     "to type it into the Finder box (Keychain Access, search 'iOS Backup', shows it). The restore continues by itself.")

    def _stop_helper(self) -> None:
        if self.helper_proc is not None and self.helper_proc.poll() is None:
            self.helper_proc.terminate()

    # -- main loop ----------------------------------------------------------

    def drive(self, timeout_sec: float) -> Tuple[int, Dict[str, Any]]:
        try:
            return self._drive(timeout_sec)
        finally:
            self._stop_helper()

    def _drive(self, timeout_sec: float) -> Tuple[int, Dict[str, Any]]:
        start = self.now()
        unknown = 0
        open_attempts = 0
        last_state = ""
        while self.now() - start < timeout_sec:
            try:
                snap = self.driver.snapshot()
            except FinderUIError as exc:
                return 4, handoff("finder_unreachable", str(exc), None,
                                  "Bring Finder to the front (open_application) and make sure it is responding.")
            st = classify(snap)
            if st != last_state:
                self.say(f"Finder shows: {st.replace('_', ' ')}")
                last_state = st
            if st not in self.visited:
                self.visited.append(st)
            if st != "unknown":
                unknown = 0

            if st == "no_window":
                if self.restore_started:
                    # Seen live: Finder closes the phone's window when the restore hands over, while the
                    # phone is still on USB. Never reopen it; wait for the phone to return with its name.
                    self.status(phase="rebooting", summary="The phone is restarting")
                    return self._finish()
                if open_attempts >= CLICK_RETRIES:
                    return 4, handoff("open_device", "I couldn't open the phone in Finder.", snap,
                                      "Open a Finder window and click the iPhone under Locations in the sidebar.")
                open_attempts += 1
                sel = self.driver.open_device()
                if not sel.get("success"):
                    return 4, handoff("open_device", sel.get("reason") or "I couldn't open the phone in Finder.",
                                      snap, (sel.get("handoff") or {}).get("action")
                                      or "Click the iPhone under Locations in the Finder sidebar.")
                self.sleep(2.0)
            elif st == "activating":
                self.status(phase="activating", summary="The Mac is activating the phone")
            elif st == "welcome":
                if self.restore_started:
                    return 4, handoff("back_at_welcome",
                                      "Finder is showing the Welcome screen again after the restore began.", snap,
                                      "Look at the Finder window for an error and tell the person what it says.")
                if self.continue_clicks >= CLICK_RETRIES:
                    return 4, handoff("continue", "I pressed Continue several times but the Welcome screen is still showing.",
                                      snap, "Check the window for an error, make sure the right backup is selected, click Continue.")
                out = self._welcome(snap)
                if out:
                    return 4, out
            elif st == "password_sheet":
                out = self._password(snap)
                if out:
                    return 4, out
            elif st == "other_sheet":
                out = self._other_sheet(snap)
                if out:
                    return 4, out
            elif st == "restoring":
                self.restore_started = True
                # Finder's sidebar is part of the window's text too: report only the progress lines
                txt = " ".join(t for t in _texts(snap, "W")
                               if any(p in t.lower() for p in RESTORING_PHRASES))[:200]
                if txt != self.last_text:
                    self.last_text = txt
                    self.say(f"Restore progress: {txt}")
                    self.status(phase="restoring", summary=txt)
            elif st == "summary":
                if self.restore_started:
                    return self._finish()
                if "welcome" in self.visited or "password_sheet" in self.visited:
                    return 4, handoff("unexpected_summary",
                                      "Finder jumped to the phone's Summary page mid-restore.", snap,
                                      "Look at the window. If a restore is progressing, leave it alone; if it was cancelled, say so.")
                return 2, gates.refusal(
                    "phone_not_erased",
                    "Finder is showing a normal, set-up phone (the Summary page), not the Welcome screen. "
                    "I won't restore onto it: that would replace everything on it.",
                    [{"id": "erase_first", "label": "Erase the phone first (Settings > General > Transfer or Reset iPhone > "
                                                    "Erase All Content and Settings) and tell me when it shows 'Hello'."},
                     gates.STOP_OPTION])
            else:  # unknown
                unknown += 1
                if unknown >= UNKNOWN_TICKS_BEFORE_HANDOFF:
                    return 4, handoff("unrecognised_screen", "Finder is showing something I don't recognise.", snap,
                                      "Work out which step the Finder window is on (Welcome, password box, progress, "
                                      "Summary) and bring it to the Welcome screen or the next step.")
            self._check_helper()
            self.sleep(self.poll)

        return 1, {"ok": False, "code": "timeout",
                   "message": "The restore didn't finish in the time I was given. The phone may still be working: "
                              "look at it, and resume if it's still going.",
                   "resume": RESUME_CMD, "options": [gates.STOP_OPTION]}

    def _finish(self) -> Tuple[int, Dict[str, Any]]:
        expected = (self.plan.get("backup_name") or "").strip()
        self.say("Finder has handed the restore to the phone. The transfer takes about 10 minutes; waiting for it "
                 "to finish, restart and come back. Please leave the phone alone until then.")

        def back() -> bool:
            # The phone takes the backup's name EARLY (a minute in), and Finder's window closes at the
            # same time, so neither means "done". Done = the transfer process has exited AND the phone
            # itself reports it was restored, after its restart.
            if self.restore_active() or self.udid not in self.devices():
                return False
            name = self.phone_name().strip()
            return (bool(name) and (not expected or name == expected)
                    and self.setup_state() == RESTORED_STATE)

        deadline = self.now() + self.finish_timeout
        ok = back()
        while not ok and self.now() < deadline:
            self.sleep(5.0)
            ok = back()
        if ok:
            msg = ("Your iPhone is restored and has restarted. Next: unlock it with your passcode. "
                   "Your apps will re-download over Wi-Fi in the background; keep it on Wi-Fi and plugged in "
                   "for a while. Don't judge storage until they've finished.")
            self.status(phase="completed", summary=msg)
            return 0, {"ok": True, "restored": True, "message": msg, "plan": self.plan}
        msg = ("Finder finished, but I couldn't confirm the phone came back with its own name. Please look at the "
               "phone: if it shows an Apple logo or progress bar, give it 10 more minutes; if it shows 'Hello' "
               "again, tell me.")
        self.status(phase="unconfirmed", summary=msg)
        return 3, {"ok": False, "code": "unconfirmed", "message": msg,
                   "options": [{"id": "wait", "label": "Wait 10 minutes and I'll check again."}, gates.STOP_OPTION]}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _write_status(**kw: Any) -> None:
    path = state_path(STATUS_NAME)
    data = _read_json(path)
    data.update(kw)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    except OSError:
        pass


def _alert(message: str) -> None:
    try:
        from backup_runner import play_sound, send_notification
        play_sound("Ping")
        send_notification(message, subtitle="Restore")
    except Exception:  # noqa: BLE001
        pass


def _phone_name(udid: str) -> str:
    from restorer import query_device_info
    return query_device_info(udid).get("DeviceName", "")


def _setup_state(udid: str) -> str:
    """The phone's own record of how it was set up. After a finished restore it reads
    RestoredFromiTunesBackup. Observed live 2026-11-09: it stays empty throughout the restore."""
    from common import _ideviceinfo
    return _ideviceinfo(udid, "-q", "com.apple.purplebuddy").get("SetupState", "")


def _restore_process_running() -> bool:
    """macOS runs the transfer in the AppleMobileBackup helper; it is busy for the whole restore
    (about 11 minutes for a 9 GB backup) and exits at the end. Observed live 2026-11-09."""
    if sys.platform != "darwin":
        return False
    try:
        return subprocess.run(["pgrep", "-f", "AppleMobileBackup"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def announce_backups(plan: Dict[str, Any], say: Callable[[str], None] = gates.say) -> List[str]:
    """ALWAYS shown before a restore (dry run and real run): every backup of this phone on this Mac and
    which one will be restored. The agent must relay these lines to the person in chat and get their go-ahead."""
    lines = gates.describe_backups(plan.get("available_backups") or [])
    say(f"BACKUPS_AVAILABLE: I found {sum(1 for ln in lines if not ln.startswith(' '))} backup(s) of this phone on this Mac.")
    for ln in lines:
        say("    " + ln.strip() if ln.startswith(" ") else "  - " + ln)
    say(f"RESTORING_FROM: the {plan.get('backup_kind') == 'clone' and 'protected safety copy' or 'main backup'}, your phone's data as of "
        f"{plan.get('backup_made_text') or plan.get('backup_made')}. Finder lists copies by name, so I will make sure it selects "
        "one of this phone's copies showing exactly that time (never another phone's).")
    return lines


def restore(udid: Optional[str], source: str, max_age_hours: float, confirm: bool, hello_confirmed: bool,
            resume: bool, timeout_sec: int, driver: Any = None) -> Tuple[int, Dict[str, Any]]:
    prior = _read_json(state_path(STATUS_NAME)) if resume else {}
    fresh = False
    if prior.get("plan") and prior.get("gates_passed_at"):
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(prior["gates_passed_at"])).total_seconds()
            fresh = age < RESUME_MAX_AGE_SEC
        except ValueError:
            fresh = False

    midflow = bool(resume and fresh and prior.get("phase") in ("restoring", "rebooting", "handoff"))
    if midflow:
        # The phone is no longer at 'Hello' (the restore has begun), so the "is it erased" gate would
        # wrongly refuse. Trust the gates this same run already passed, but re-check it's the same lone phone.
        plan = prior["plan"]
        now = connected_udids()
        if len(now) > 1 or (now and now[0].lower() != plan["udid"].lower()):
            r = gates.refusal("device_changed", "The plugged-in phone isn't the one this restore started on.")
            gates.say(r["message"])
            return 2, r
    else:
        # Hook 2: make this phone's backups show distinctly in Finder's pop-up (idempotent). A real run applies it
        # BEFORE the gates so the plan carries the exact Finder label; a dry run only says what would change.
        from backup_labels import ensure_labels
        ensure_labels(udid, say=gates.say, apply_changes=bool(confirm and hello_confirmed))
        gate = gates.check_preconditions(udid, source, max_age_hours, allow_unactivated=True)
        if not gate["ok"]:
            gates.say(gate["message"])
            for o in gate["options"]:
                gates.say(f"  - {o['label']}")
            return 2, gate
        plan = gate["plan"]

        age = gates._human_age(plan["backup_age_hours"]) if plan["backup_age_hours"] is not None else "at an unknown time"
        gates.say(f"Everything checks out. Your phone answers like an erased phone, and I found the backup "
                  f"'{plan['backup_name'].strip()}' from {age}.")
        gates.say("I can't see the phone's screen, and an erased phone looks the same to me while it is still "
                  "finishing (progress bar) as when it shows 'Hello'. Is the word 'Hello' on the screen now?")
        for w in plan["warnings"]:
            gates.say(f"Heads up: {w}")
        announce_backups(plan)
        if not confirm:
            gates.say("This was a practice run, and nothing was changed. When you say go, I'll drive Finder to restore "
                      "(about 10-30 minutes; the phone will restart once; a password box may appear on the Mac).")
            return 0, {"ok": True, "dry_run": True, "plan": plan}
        if not hello_confirmed:
            r = gates.refusal(
                "needs_hello_confirmation",
                "I can't tell from here whether the phone has finished erasing, so I need you to tell me. "
                "Please look at the phone: when it shows 'Hello' (no progress bar), tell me and I'll start.",
                [{"id": "hello_showing", "label": "'Hello' is on the screen: I'll start the restore."},
                 {"id": "wait", "label": "Still a progress bar or Apple logo: wait a few minutes and tell me again."},
                 gates.STOP_OPTION])
            gates.say(r["message"])
            return 2, r
        _write_status(plan=plan, gates_passed_at=datetime.now(timezone.utc).isoformat(), phase="starting")

    if not confirm or not hello_confirmed:
        r = gates.refusal("needs_confirmation", "Resuming needs --confirm and --hello-confirmed, same as starting.")
        return 2, r

    if sys.platform != "darwin" and driver is None:
        r = gates.refusal("unsupported_platform", "Driving Finder only works on a Mac.")
        return 2, r

    drv = driver or FinderDriver(plan["udid"])
    run = Run(plan["udid"], plan, drv, notify=_alert, status=_write_status,
              phone_name=lambda: _phone_name(plan["udid"]),
              setup_state=lambda: _setup_state(plan["udid"]), restore_active=_restore_process_running)
    run.restore_started = bool(prior.get("phase") in ("restoring", "rebooting")) if midflow else False
    code, result = run.drive(timeout_sec)
    if code == 4:
        _write_status(phase="handoff", summary=result.get("message"), handoff_step=result.get("step"))
        _alert("The restore needs a hand. Please check the assistant.")
    elif code == 0:
        _alert("Your iPhone is restored. Please unlock it.")
        # Hook 3: Finder just left a new timestamped working copy; label it so the pop-up stays tidy.
        from backup_labels import ensure_labels
        ensure_labels(plan["udid"], say=gates.say)
    return code, result


def main(argv: Optional[List[str]] = None) -> int:
    ensure_utf8_console()
    ap = argparse.ArgumentParser(description="Restore a verified backup onto an erased iPhone by driving Finder.")
    ap.add_argument("--udid", default=os.environ.get("TARGET_UDID"), help="Expected phone UDID (default: the one connected)")
    ap.add_argument("--source", choices=["auto", "clone", "live"], default="auto", help="Which backup to check")
    ap.add_argument("--max-age-hours", type=float, default=48.0, help="Refuse backups older than this")
    ap.add_argument("--confirm", action="store_true",
                    help="Really restore. Only after the person has said go. Without it this is a dry run.")
    ap.add_argument("--hello-confirmed", action="store_true",
                    help="The person has said the phone's screen shows 'Hello'. Required with --confirm.")
    ap.add_argument("--resume", action="store_true",
                    help="Continue a run that stopped with an agent handoff (after the agent fixed the screen)")
    ap.add_argument("--timeout-minutes", type=int, default=60, help="Give up waiting after this long")
    ap.add_argument("--json", action="store_true", help="Print the final result as JSON (for the agent)")
    args = ap.parse_args(argv)

    code, result = restore(args.udid, args.source, args.max_age_hours, args.confirm, args.hello_confirmed,
                           args.resume, args.timeout_minutes * 60)
    if args.json or code == 4:
        print(json.dumps(result, indent=2, default=str))
    elif result.get("message"):
        gates.say(result["message"])
    return code


if __name__ == "__main__":
    sys.exit(main())
