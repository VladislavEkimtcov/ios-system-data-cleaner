"""
iTunes UI recorder for Windows (`itunes_restore.py`), TODO WD1: recon before the restore driver.

Read-only except two opt-in steps: --accessible (restart iTunes so it exposes its UI) and --open-device
(select the phone's sidebar row). Prints the window tree of a running app (iTunes by default) through Windows UI Automation, as the
same `W|||cls|||name|||value|||enabled` lines `finder_restore.parse_snapshot` reads, so a capture can become a
test fixture for the future `ITunesDriver` (WD2) exactly like the fake Finder in tests/test_finder_restore.py.

    python itunes_restore.py --dump                       # iTunes, to the screen
    python itunes_restore.py --dump --save welcome        # also save tests/fixtures/itunes/welcome.txt
    python itunes_restore.py --dump --process explorer    # any other app (handy to check the recorder itself)

Line format (fields after `enabled` are extra and ignored by parse_snapshot):
    WINDOW|||<title>
    W|||<class>|||<name>|||<value>|||<enabled>|||<automation id>|||<x,y,w,h>
    SHEET|||<title>                 (another top-level window of the app, or a dialog inside the main one)
    S|||...                         (the controls of that sheet)

Never prints a password: an Edit with IsPassword set reports an empty value. No clicks, no keystrokes.
Transport: Windows PowerShell 5.1 (`powershell.exe`, never pwsh) with .NET's UIAutomationClient, which is
always present, so the repo needs no third-party package.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from common import ensure_utf8_console

REPO_ROOT = Path(__file__).resolve().parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "itunes"

# UIA ControlType -> the class words finder_restore already understands.
CLASS_WORDS = {
    "Button": "button", "RadioButton": "radio button", "ComboBox": "pop up button", "Edit": "text field",
    "Text": "static text", "CheckBox": "checkbox", "ProgressBar": "progress indicator",
    "ListItem": "menu item", "MenuItem": "menu item",
}

_DUMP_PS = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$maxNodes = [int]$env:IOSC_MAX_NODES
$procs = @(Get-Process -Name $env:IOSC_PROC -ErrorAction SilentlyContinue)
if ($procs.Count -eq 0) { 'NO_WINDOW'; exit 0 }
$ids = @($procs | ForEach-Object { $_.Id })
$tops = @($AE::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children,
          [System.Windows.Automation.Condition]::TrueCondition) | Where-Object { $ids -contains $_.Current.ProcessId })
if ($tops.Count -eq 0) { 'NO_WINDOW'; exit 0 }
$map = @{ __MAP__ }
function Clean([string]$s) { if ($null -eq $s) { return '' }; return ($s -replace '\|\|\|', '| | |' -replace '[\r\n]+', ' ') }
function ValueOf($e, [string]$type) {
  try {
    if ($type -eq 'Edit' -and $e.Current.IsPassword) { return '' }
    $p = $null
    if ($e.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$p)) {
      if ($p.Current.ToggleState -eq 'On') { return '1' } else { return '0' } }
    if ($e.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$p)) {
      if ($p.Current.IsSelected) { return '1' } else { return '0' } }
    if ($e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$p)) { return $p.Current.Value }
    if ($e.TryGetCurrentPattern([System.Windows.Automation.RangeValuePattern]::Pattern, [ref]$p)) { return [string]$p.Current.Value }
    if ($e.TryGetCurrentPattern([System.Windows.Automation.SelectionPattern]::Pattern, [ref]$p)) {
      $sel = $p.Current.GetSelection(); if ($sel.Count -gt 0) { return $sel[0].Current.Name } }
  } catch { }
  return ''
}
$script:count = 0
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
function Walk($e, [string]$scope, [int]$depth) {
  if ($depth -gt 40 -or $script:count -ge $maxNodes) { return }
  $c = $walker.GetFirstChild($e)
  while ($null -ne $c -and $script:count -lt $maxNodes) {
    $script:count++
    $cur = $c.Current
    $type = $cur.ControlType.ProgrammaticName -replace '^ControlType\.', ''
    if ($type -eq 'Window') {
      "SHEET|||" + (Clean $cur.Name)
      Walk $c 'S' ($depth + 1)
    } else {
      $cls = $map[$type]; if (-not $cls) { $cls = $type.ToLower() }
      $r = $cur.BoundingRectangle
      $rect = if ($r.IsEmpty) { '' } else { '{0},{1},{2},{3}' -f [int]$r.X, [int]$r.Y, [int]$r.Width, [int]$r.Height }
      $en = if ($cur.IsEnabled) { 'true' } else { 'false' }
      ($scope, $cls, (Clean $cur.Name), (Clean (ValueOf $c $type)), $en, (Clean $cur.AutomationId), $rect) -join '|||'
      Walk $c $scope ($depth + 1)
    }
    $c = $walker.GetNextSibling($c)
  }
}
# The biggest top-level window is the main one; any other top-level window of the app is a dialog ("sheet").
$main = $tops | Sort-Object { $_.Current.BoundingRectangle.Width * $_.Current.BoundingRectangle.Height } -Descending | Select-Object -First 1
"WINDOW|||" + (Clean $main.Current.Name)
Walk $main 'W' 0
foreach ($t in $tops) {
  if ($t -ne $main) { "SHEET|||" + (Clean $t.Current.Name); Walk $t 'S' 0 }
}
"COUNT|||$script:count"
"""


# Select one sidebar row (the phone) through SelectionItemPattern: no mouse, no keystrokes, works in the background.
_SELECT_ROW_PS = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$procs = @(Get-Process -Name $env:IOSC_PROC -ErrorAction SilentlyContinue)
if ($procs.Count -eq 0) { 'NO_WINDOW'; exit 0 }
$ids = @($procs | ForEach-Object { $_.Id })
$want = $env:IOSC_ROW.Trim()
$cond = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty,
          [System.Windows.Automation.ControlType]::TreeItem)
foreach ($w in @($AE::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children,
                 [System.Windows.Automation.Condition]::TrueCondition) | Where-Object { $ids -contains $_.Current.ProcessId })) {
  foreach ($row in $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)) {
    if ($row.Current.Name.Trim() -eq $want) {
      $p = $null
      if ($row.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$p)) {
        $p.Select(); 'CLICKED'; exit 0 }
      'NO_PATTERN'; exit 0
    }
  }
}
'NOT_FOUND'
"""

# Press one button by its exact name through InvokePattern (no mouse, works in the background).
_PRESS_PS = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$procs = @(Get-Process -Name $env:IOSC_PROC -ErrorAction SilentlyContinue)
if ($procs.Count -eq 0) { 'NO_WINDOW'; exit 0 }
$ids = @($procs | ForEach-Object { $_.Id })
$cond = New-Object System.Windows.Automation.AndCondition(
  (New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)),
  (New-Object System.Windows.Automation.PropertyCondition($AE::NameProperty, $env:IOSC_BUTTON)))
foreach ($w in @($AE::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children,
                 [System.Windows.Automation.Condition]::TrueCondition) | Where-Object { $ids -contains $_.Current.ProcessId })) {
  $b = $w.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $cond)
  if ($null -ne $b) {
    if (-not $b.Current.IsEnabled) { 'DISABLED'; exit 0 }
    $b.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); 'CLICKED'; exit 0
  }
}
'NOT_FOUND'
"""

# Never pressed by this code, whatever a caller asks (AGENTS.md hard no's).
FORBIDDEN_BUTTONS = {"Restore Backup", "Restore Backup…", "Restore Backup...", "Restore iPhone…", "Restore iPhone...",
                     "Set up as new iPhone", "Erase and Sync", "Sync", "Back Up Now"}

SPI_GETSCREENREADER, SPI_SETSCREENREADER, SPIF_SENDCHANGE = 0x0046, 0x0047, 0x0002


class UIError(Exception):
    """The app's UI could not be read at all (PowerShell missing, UI Automation failed)."""


def dump_script() -> str:
    pairs = "; ".join(f"'{k}' = '{v}'" for k, v in CLASS_WORDS.items())
    return _DUMP_PS.replace("__MAP__", pairs)


def dump(process: str = "iTunes", max_nodes: int = 4000, timeout: float = 120.0) -> str:
    """The raw snapshot lines of `process`'s windows ("NO_WINDOW" when it has none). Windows only."""
    if sys.platform != "win32":
        raise UIError("The UI recorder uses Windows UI Automation; on a Mac, Finder is read by finder_restore.py.")
    env = dict(os.environ, IOSC_PROC=process, IOSC_MAX_NODES=str(max_nodes))
    try:
        res = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", dump_script()],
            capture_output=True, env=env, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as e:
        raise UIError(f"couldn't run Windows PowerShell: {e}") from e
    out = res.stdout.decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
    if res.returncode != 0 or not out:
        raise UIError(res.stderr.decode("utf-8", errors="replace").strip()[-400:] or f"exit {res.returncode}")
    return out


def _ps(script: str, env_extra: Dict[str, str], timeout: float = 60.0) -> str:
    env = dict(os.environ, **env_extra)
    try:
        res = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, env=env, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as e:
        raise UIError(f"couldn't run Windows PowerShell: {e}") from e
    return res.stdout.decode("utf-8", errors="replace").strip()


def select_row(name: str, process: str = "iTunes") -> str:
    """Selects the sidebar row named `name` (spaces around it ignored: the phone's name can end in a space).
    Returns "CLICKED", "NOT_FOUND", "NO_PATTERN" or "NO_WINDOW". Only selects; never presses a button."""
    if sys.platform != "win32":
        raise UIError("Windows only.")
    return _ps(_SELECT_ROW_PS, {"IOSC_PROC": process, "IOSC_ROW": name}) or "NOT_FOUND"


def press(button: str, process: str = "iTunes") -> str:
    """Presses the button with exactly this name. Returns "CLICKED", "DISABLED", "NOT_FOUND" or "NO_WINDOW".
    Refuses the buttons that overwrite or wipe a phone (FORBIDDEN_BUTTONS)."""
    if button in FORBIDDEN_BUTTONS:
        raise UIError(f"refusing to press '{button}' (it can overwrite or wipe a phone)")
    if sys.platform != "win32":
        raise UIError("Windows only.")
    return _ps(_PRESS_PS, {"IOSC_PROC": process, "IOSC_BUTTON": button}) or "NOT_FOUND"


def itunes_windows() -> list:
    """Visible top-level windows of iTunes as (hwnd, title, class). Plain Win32: never hangs, even while a modal
    box (the "Enter Password" dialog) blocks UI Automation calls into iTunes (seen 2026-11-10)."""
    import psutil
    import win32gui
    import win32process
    pids = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == "itunes.exe"}
    out = []

    def cb(h, _):
        if win32process.GetWindowThreadProcessId(h)[1] in pids and win32gui.IsWindowVisible(h):
            out.append((h, win32gui.GetWindowText(h), win32gui.GetClassName(h)))
    win32gui.EnumWindows(cb, None)
    return out


PASSWORD_BOX = ("Enter Password", "iTunesCustomModalDialog")
WRONG_PASSWORD_PHRASE = "because the password was incorrect"


def dismiss_wrong_password_alert() -> str:
    """Presses OK on iTunes' "could not restore ... because the password was incorrect" alert, and only on that
    alert (its text is read through UI Automation first; the alert does not block it). Returns "CLICKED",
    "NOT_FOUND" or "OTHER_ALERT" (an alert with any other text is left for the agent / person)."""
    import win32con
    import win32gui
    alerts = [w for w in itunes_windows() if w[2] == "iTunesCustomModalDialog" and w[1] == "iTunes"]
    if not alerts:
        return "NOT_FOUND"
    text = dump(timeout=20)
    if WRONG_PASSWORD_PHRASE not in text:
        return "OTHER_ALERT"
    hwnd = alerts[0][0]
    found = []
    win32gui.EnumChildWindows(hwnd, lambda c, _: found.append(c) if (win32gui.GetClassName(c) == "Button"
                                                                     and win32gui.GetWindowText(c) == "OK") else None, None)
    if not found:
        return "NOT_FOUND"
    # BM_CLICK posted to the button is ignored while the dialog isn't the active window (seen 2026-11-10); the
    # dialog's own "button OK was clicked" command works in the background.
    ctrl_id = win32gui.GetDlgCtrlID(found[0])
    win32gui.PostMessage(hwnd, win32con.WM_COMMAND, (win32con.BN_CLICKED << 16) | ctrl_id, found[0])
    return "CLICKED"


def wait_for(pred, timeout: float, step: float = 1.0) -> bool:
    import time
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return False


def is_accessible(text: str) -> bool:
    """iTunes started WITHOUT a screen reader present exposes ~13 elements and no sidebar (seen 2026-11-10);
    started with one, it exposes the sidebar tree, the device button and the tabs."""
    return "|||tree|||" in text or "|||treeitem|||" in text


def start_accessible(itunes_exe: Optional[str] = None, wait_sec: float = 60.0) -> str:
    """(Re)starts iTunes so its full UI is readable: iTunes builds its accessibility tree only when Windows reports
    a screen reader at its start-up (found 2026-11-10). The system flag is set only around the start and put back
    as it was. Refuses while a backup or restore is running. Returns the first readable snapshot."""
    if sys.platform != "win32":
        raise UIError("Windows only.")
    import ctypes
    import time

    from common import process_running
    from itunes_automation import ITunesAutomation
    if process_running("AppleMobileBackup", "idevicebackup2"):
        raise UIError("a backup or restore is running; I won't restart iTunes now")
    current = dump("iTunes")
    if current != "NO_WINDOW" and is_accessible(current):
        return current
    it = ITunesAutomation(itunes_exe)
    if it.is_itunes_running():
        subprocess.run(["taskkill", "/IM", "iTunes.exe"], capture_output=True)
        deadline = time.time() + 30
        while it.is_itunes_running() and time.time() < deadline:
            time.sleep(1)
        if it.is_itunes_running():
            raise UIError("iTunes didn't close (a dialog may be open in it)")
    u32 = ctypes.windll.user32
    before = ctypes.c_bool()
    u32.SystemParametersInfoW(SPI_GETSCREENREADER, 0, ctypes.byref(before), 0)
    try:
        u32.SystemParametersInfoW(SPI_SETSCREENREADER, 1, None, SPIF_SENDCHANGE)
        if not it.launch_or_focus_itunes():
            raise UIError(f"couldn't start iTunes ({it.itunes_exe})")
        deadline = time.time() + wait_sec
        text = "NO_WINDOW"
        while time.time() < deadline:
            time.sleep(3)
            text = dump("iTunes")
            if text != "NO_WINDOW" and is_accessible(text):
                return text
        raise UIError("iTunes started but its controls still aren't readable")
    finally:
        u32.SystemParametersInfoW(SPI_SETSCREENREADER, int(before.value), None, SPIF_SENDCHANGE)


_REDACT = re.compile(r"(Serial Number: |Phone Number: |IMEI: |MEID: |ICCID: )[^|]+")


def redact(text: str) -> str:
    """Fixtures are committed: drop the phone's serial / phone number / IMEI from the Summary page."""
    return _REDACT.sub(lambda m: m.group(1) + "REDACTED", text)


def summarize(text: str) -> Dict[str, Any]:
    """A few counts for the screen, so a capture can be judged at a glance."""
    from finder_restore import parse_snapshot
    snap = parse_snapshot(text)
    by_cls: Dict[str, int] = {}
    for it in snap["items"]:
        by_cls[it["cls"]] = by_cls.get(it["cls"], 0) + 1
    named = sum(1 for it in snap["items"] if it["name"])
    return {"window": snap["window"], "has_sheet": snap["has_sheet"], "elements": len(snap["items"]),
            "named": named, "classes": dict(sorted(by_cls.items(), key=lambda kv: -kv[1]))}


def check_welcome(expect_name: str, expect_time: str) -> Dict[str, Any]:
    """Reads the Welcome screen and says whether it is safe to press Continue: 'Restore from this backup' selected
    and the pop-up showing this phone's backup at the expected time (e.g. "2:27 PM"). Never changes anything."""
    from finder_restore import classify, parse_snapshot
    snap = parse_snapshot(dump(timeout=30))
    radio = next((i["value"] for i in snap["items"]
                  if i["cls"] == "radio button" and i["name"] == "Restore from this backup:"), None)
    entry = next((i["value"] for i in snap["items"] if i["cls"] == "pop up button"), "")
    ok = (classify(snap) == "welcome" and radio == "1" and entry.strip().startswith(expect_name.strip())
          and expect_time in entry)
    return {"ok": ok, "screen": classify(snap), "restore_selected": radio == "1", "entry": entry}


def press_continue_and_wait_for_password_box(timeout: float = 30.0) -> bool:
    """Presses Continue on the Welcome screen and waits for the 'Enter Password' window. InvokePattern blocks while
    that modal box is open, so the press runs in a background thread and the windows are watched instead."""
    import threading
    threading.Thread(target=lambda: press("Continue"), daemon=True).start()
    return wait_for(lambda: any((w[1], w[2]) == PASSWORD_BOX for w in itunes_windows()), timeout)


def retry_after_password_found(expect_name: str, expect_time: str) -> str:
    """After the password helper page proved the password (the person then types it in iTunes): OK on the
    wrong-password alert, check the Welcome selection, Continue, wait for the password box. Returns a status word:
    PASSWORD_BOX_UP, NOT_WELCOME (stopped before Continue; hand off), OTHER_ALERT, NO_PASSWORD_BOX."""
    r = dismiss_wrong_password_alert()
    if r == "OTHER_ALERT":
        return "OTHER_ALERT"
    wait_for(lambda: not any(w[2] == "iTunesCustomModalDialog" for w in itunes_windows()), 15)
    if not check_welcome(expect_name, expect_time)["ok"]:
        return "NOT_WELCOME"
    return "PASSWORD_BOX_UP" if press_continue_and_wait_for_password_box() else "NO_PASSWORD_BOX"


def watch(fixture_prefix: Optional[str] = None, max_minutes: float = 60.0) -> int:
    """One line per change during a restore: iTunes' windows first (never hangs), the UI tree only when no modal
    box is open, and whether AppleMobileBackup.exe (the transfer) is running. Optionally saves each new screen as
    a fixture. Read-only. For an agent's event monitor."""
    import time

    from common import process_running
    from finder_restore import classify, parse_snapshot
    last, n, t0 = None, 0, time.time()
    while time.time() - t0 < max_minutes * 60:
        wins = itunes_windows()
        modal = [(w[1], w[2]) for w in wins if w[2] != "iTunes"]
        busy = process_running("AppleMobileBackup")
        text = None
        if modal:
            desc = f"modal window(s): {modal}"
        else:
            try:
                text = dump(timeout=20)
            except UIError as e:
                desc = f"UI not readable ({e.__class__.__name__})"
            else:
                if text == "NO_WINDOW":
                    desc = "iTunes has no window"
                else:
                    s = parse_snapshot(text)
                    texts = [i["name"] for i in s["items"] if i["cls"] in ("static text", "pane") and i["name"]][:4]
                    desc = f"screen={classify(s)} sheet={s['has_sheet']} texts={texts}"
        key = (desc, busy)
        if key != last:
            last, n = key, n + 1
            if fixture_prefix and text and text != "NO_WINDOW":
                FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
                (FIXTURE_DIR / f"{fixture_prefix}_{n:02d}.txt").write_text(redact(text) + "\n", encoding="utf-8")
            print(f"[{time.strftime('%H:%M:%S')}] #{n} transfer={'running' if busy else 'no'} {desc}", flush=True)
        time.sleep(3)
    return 0


def main(argv: Optional[list] = None) -> int:
    ensure_utf8_console()
    ap = argparse.ArgumentParser(description="Read-only UI recorder for iTunes on Windows (TODO WD1).")
    ap.add_argument("--dump", action="store_true", help="Print the app's UI tree as snapshot lines.")
    ap.add_argument("--process", default="iTunes", help="Process name without .exe (default: iTunes).")
    ap.add_argument("--save", metavar="NAME", help="Also save to tests/fixtures/itunes/NAME.txt (a test fixture).")
    ap.add_argument("--max-nodes", type=int, default=4000)
    ap.add_argument("--accessible", action="store_true",
                    help="First (re)start iTunes so its full UI is readable (screen-reader flag around start-up only).")
    ap.add_argument("--open-device", metavar="NAME", help="First select this phone's row in the sidebar (no buttons).")
    ap.add_argument("--watch", metavar="PREFIX", nargs="?", const="", default=None,
                    help="Follow a restore, one line per change (for an event monitor); with PREFIX also save fixtures.")
    ap.add_argument("--continue-if", metavar=("NAME", "TIME"), nargs=2,
                    help="Press Continue only if Welcome shows 'Restore from this backup' with NAME's backup at TIME.")
    ap.add_argument("--retry-password", metavar=("NAME", "TIME"), nargs=2,
                    help="After the helper page proved the password: OK on the wrong-password alert, check, Continue.")
    args = ap.parse_args(argv)
    if args.watch is not None:
        return watch(args.watch or None)
    if args.continue_if:
        chk = check_welcome(*args.continue_if)
        print(f"WELCOME_CHECK: {chk}")
        if not chk["ok"]:
            print("NOT pressing Continue.")
            return 4
        print("PASSWORD_BOX_UP" if press_continue_and_wait_for_password_box() else "NO_PASSWORD_BOX")
        return 0
    if args.retry_password:
        r = retry_after_password_found(*args.retry_password)
        print(r)
        return 0 if r == "PASSWORD_BOX_UP" else 4
    try:
        if args.accessible:
            start_accessible()
        if args.open_device:
            print(f"# select '{args.open_device}': {select_row(args.open_device)}", file=sys.stderr)
            import time
            time.sleep(3)
    except UIError as e:
        print(f"[!] {e}", file=sys.stderr)
        return 1
    if not args.dump:
        ap.print_help()
        return 0
    try:
        text = dump(args.process, max_nodes=args.max_nodes)
    except UIError as e:
        print(f"[!] {e}", file=sys.stderr)
        return 1
    if text == "NO_WINDOW":
        print(f"[!] {args.process} has no open window. Start it (and open the phone) first.", file=sys.stderr)
        return 1
    print(redact(text))
    s = summarize(text)
    print(f"\n# {s['elements']} elements ({s['named']} named) in '{s['window']}'"
          f"{' + dialog' if s['has_sheet'] else ''}: {s['classes']}", file=sys.stderr)
    if args.save:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        path = FIXTURE_DIR / f"{Path(args.save).name}.txt"
        path.write_text(redact(text) + "\n", encoding="utf-8")
        print(f"# saved {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
