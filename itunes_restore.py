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
    args = ap.parse_args(argv)
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
