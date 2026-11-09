# TODO — de-hardcoding & cross-platform hardening

Status: backlog, captured 2026-11-09. The pipeline works end-to-end on the happy path; these
items are about robustness on machines, OS versions, and locales other than the one it was
developed on. Line numbers refer to commit `827fc6c` and will drift.

Guiding rule (matches AGENTS.md §8): **CLI first → structured UI lookup → agent handoff.**
Never fixed coordinates or blind keystrokes, and never English-only string matching as the
only signal.

---

## A. Cross-platform / macOS (this Mac)

### A1. Prefer libimobiledevice CLI over Finder/iTunes UI for the restore
- [ ] Make `idevicebackup2 -u <UDID> restore --system --settings --reboot <BACKUP_ROOT>` the
      primary restore path. Today the restore is driven entirely through Finder AppleScript
      (`restorer.py` `stage_finder_restore_selection`, `auto_confirm_finder_restore_sheet`) and
      iTunes keystrokes (`itunes_automation.py` `actuate_restore_ui`).
- [ ] Password handling: `--password` puts the secret on argv (visible in `ps`), which
      SECURITY.md forbids. Use `-i/--interactive` and feed stdin, or confirm whether the
      installed libimobiledevice version reads the password any other way. Add a test
      that the password never shows up in argv.
- [ ] Keep the Finder/iTunes UI path as a fallback only. Its output must keep the existing
      structured `handoff` dict.
- [ ] Restore against the verified `-prewipe` clone (or verify the live folder still matches
      it) so a Finder auto-sync can't swap the backup underneath us.

### A2. Finder AppleScript uses hardcoded accessibility paths
- [ ] `restorer.py` addresses controls positionally: `splitter group 1 of splitter group 1`,
      `scroll area 1 of group 1 of group 1 of …`, `pop up button 1`, `sheet 1`
      (around lines 246, 260, 268, 346–360, 556–558). Any Finder layout change between
      macOS releases breaks these.
- [ ] Replace with a role + title/identifier search over the window's AX tree (a small JXA or
      Swift `AXUIElement` walker; avoid `entire contents`, which is very slow). Return JSON
      instead of `|||`-delimited strings.
- [ ] Add a `--dump-ax` debug command that prints the device window's AX tree, so an agent
      can capture fixtures for new macOS versions.

### A3. English-only UI string matching
- [ ] `"Welcome"`, `"Set up"`, `"Restore"`, `"Restore from this backup:"`, `"Continue"`,
      `"Restoring"`, `"Syncing"`, `"Step"`, `"Available"` (restorer.py) and `"set password"` /
      `"enter password"` (itunes_automation.py) only work on English systems. Prefer AX
      roles and identifiers. Where text is unavoidable, use a locale table and fail closed to
      a handoff.

### A4. Window identification by `"iPhone"` / `"iPad"` substring
- [ ] Every Finder script picks the first window whose name contains "iPhone" or "iPad".
      That breaks for custom device names ("Bob's phone"). It also breaks in
      `monitor_restore_progress` once the restored device gets its custom name back. Match
      the window or sidebar item using the device name from `ideviceinfo` or the backup's
      `Info.plist`.

### A5. Sheet confirmation is not verified
- [ ] `auto_confirm_finder_restore_sheet` clicks `button "Restore"` on `sheet 1` of the first
      matching window. It never checks that the sheet is the backup-password sheet, or that
      the password field is filled. Verify the sheet's identity (secure text field present,
      expected title) before clicking.

### A6. Restore completion detection is heuristic
- [ ] `monitor_restore_progress` decides "complete" from Finder status text
      ("Available" or empty string after a reboot). Replace it with a state machine on
      lockdown: device drops off USB → reappears → `ideviceinfo` answers →
      `DeviceName` is no longer the factory "iPhone" (or the `idevicebackup2` exit code if A1
      lands). Keep Finder text only as a progress hint.

### A7. Fixed sleeps instead of condition waits
- [~] `common.wait_until` added; `restart_finder_services` now waits for a new Finder PID and
      `launch_or_focus_itunes` waits for the iTunes process. Still fixed: the 2 s iTunes
      settle (needs W3/UIA to observe readiness), menu-key delays, the 1 s USB-stability gap.
- [ ] Replace fixed `time.sleep(N)` waits with "poll until condition or timeout":
      restorer.py (`restart_finder_services` sleeps the full timeout, ~185, ~438),
      itunes_automation.py (`launch_or_focus_itunes` 4 s + 5 s + 2 s, menu-key delays,
      `trigger_backup` 2 s / 2.5 s). Add one shared `wait_until(predicate, timeout, interval)`
      helper in `common.py`.

### A8. Wipe detection
- [ ] `ITunesAutomation.detect_device_wipe_state` *infers* `ui_state` from `is_wiped` and never
      observes the UI (~line 257). Either really observe it (UIA/AX) or drop the field.
- [ ] Move wipe detection into `common.py` so Finder and iTunes paths share one
      implementation: NVRAM `obliteration`, factory `DeviceName`, and `PasswordProtected`.

### A9. Keychain enumeration is too broad
- [x] `password_tracker.list_keychain_backup_accounts` runs `security dump-keychain`
      (~line 328), which reads metadata for every item in the login keychain and can trigger
      prompts. Query only `-s "iOS Backup"`, or keep a list of known UDIDs and look each one
      up.

### A10. Duplicated / import-time config
- [x] `itunes_env.get_itunes_backup_root` duplicates `common.backup_root` (itunes_env.py
      ~53–100). Delete it and import the shared one.
- [ ] `common.BACKUP_ROOT` is computed at import time and imported by value in many modules.
      Env overrides set after import (MCP server, tests) are silently ignored. Call
      `backup_root()` at use sites.

### A11. Docs drift
- [ ] README badge says "Platform: macOS", but Windows is supported. AGENTS.md and
      SECURITY.md describe behaviour that A1–A6 will change. Update them when those land.

---

## W. Windows-only (hand to the agent on the Windows box)

Verify every item on a real Windows 10 and Windows 11 machine with an iPhone attached. Note
which Apple software is installed for each test: iTunes Win32 installer, iTunes from the
Microsoft Store, or the **Apple Devices** app.

### W1. `revive.bat` Python detection is broken
- [ ] The inner `if %errorlevel% neq 0` (lines ~13) sits inside the parenthesised block that
      starts at line 11. `%errorlevel%` there is expanded when the block is parsed, so it
      still holds the failing result of `where python`. If `python` is missing but `py` is
      installed, the script still says "Python 3 is not detected" and exits. Fix with
      `if errorlevel 1` or delayed expansion `!errorlevel!`.
- [ ] `where python` also matches the Microsoft Store *App Execution Alias* stub in
      `%LOCALAPPDATA%\Microsoft\WindowsApps`, which opens the Store instead of running
      Python. Try `py -3` first, then check that `python --version` really runs.
- [ ] Add `cd /d "%~dp0"` so the script works when launched from another directory.

### W2. Duplicate keystrokes / duplicate backup triggers
- [x] In `send_backup_shortcut`, the PowerShell `SendKeys` fallback runs even when the
      Windows-MCP shortcut already succeeded, so Alt+F → D → B is sent twice.
- [x] `trigger_backup` calls `actuate_backup_ui()` twice (~lines 351 and 393), and
      `actuate_backup_ui` calls `launch_or_focus_itunes` again on top. This can queue two
      backups or leave a menu open.

### W3. Replace SendKeys with UI Automation (or the CLI)
- [ ] Menu accelerators (`%f`, `d`, `b`) depend on locale and iTunes version. Find
      "Back Up Now", "Continue", "Restore Backup…" and the password dialog by
      ControlType + Name/AutomationId through UI Automation (`uiautomation`, `pywinauto`
      UIA backend, or `comtypes`). Use invoke patterns, not keystrokes.
- [ ] Better still, make `idevicebackup2` the primary engine on Windows too (see A1). Today it
      is only a fallback, and it is skipped when encryption is on because it "will hang
      waiting for a CLI password prompt" (~lines 405–433). Check whether that claim holds:
      backing up an already-encrypted device should not need the password.

### W4. Restore presses Enter blindly
- [ ] `actuate_restore_ui` sends `{ENTER}` to whatever control has focus and assumes the
      default backup is the right one (~lines 296–306). Before Continue, read the backup
      combo box through UIA and match the device name and date against the target backup.
      Otherwise return a handoff.

### W5. Apple Devices app (Windows 11) is not handled
- [ ] Apple's **Apple Devices** app (Microsoft Store) replaces iTunes for device management.
      Once it is installed, iTunes stops showing the device. Detect it, automate it (UIA), and
      report it in `itunes_env.check_itunes_installation`.
- [ ] iTunes detection only checks `Program Files\iTunes\iTunes.exe` (itunes_env.py ~176–177
      and `ITunesAutomation.__init__`). It misses the Store build under `WindowsApps`
      (resolve it via `Get-AppxPackage`).
- [ ] Confirm the AMDS service name for each install type (`iphone_manager.py` ~357 checks
      `amds_service_running`).

### W6. Backup root selection
- [x] `common.backup_root()` returns the *first* existing candidate (Win32 `%APPDATA%`, Store
      `%USERPROFILE%\Apple`, `Packages\AppleInc.iTunes*`). If an old iTunes left one folder
      and Apple Devices writes to another, we pick the wrong one. Prefer the candidate that
      contains the target UDID, or the one with the newest `Status.plist`.
- [ ] Verify the exact path the Apple Devices app writes to.

### W7. Console encoding
- [ ] Emoji and box-drawing output can raise `UnicodeEncodeError` on legacy cp1252/cp437
      consoles. In every entry point, call
      `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`, or set
      `PYTHONIOENCODING=utf-8` and `chcp 65001` in `revive.bat`.

### W8. Small bugs
- [x] `itunes_automation.main()` uses `json`, but `json` is only imported under
      `if __name__ == "__main__"` (~lines 633/638). That raises NameError when `main()` is
      called through an import. Move the import to the top of the file.
- [ ] Focusing windows by title substring `"itunes"` plus `SetForegroundWindow` is English-only
      and often blocked by the foreground lock. Focus through UIA instead (or with
      `AttachThreadInput`).

### W9. Pre-wipe clone on NTFS
- [ ] Windows has no CoW, so the `robocopy` clone doubles disk usage. Check the free-space
      check (common.py ~336) and the host-buffer budgeting in `backup_runner` /
      `iphone_manager status` with real numbers. Confirm the robocopy flags and the
      exit-code handling (<8 = success).

### W10. macOS-only conveniences without a Windows equivalent
- [ ] Password storage and proof re-use: the Keychain calls in `password_tracker.py` silently
      do nothing on Windows. Use Windows Credential Manager (`keyring` or `cmdkey`), or
      document the gap clearly.
- [ ] Notifications and sounds (`backup_runner.play_sound` uses `afplay`, `send_notification`
      uses `osascript`): add `winsound` and toast equivalents, or a clean no-op.
- [ ] `backup_runner` falls back from PTY to a pipe on Windows. Check that idevicebackup2's
      `\r`-based progress still parses into the 10% milestones.

### W11. Supply chain for native tools
- [ ] `dep_installer.py` downloads a third-party libimobiledevice Windows build and only
      *prints* its SHA-256 (~line 171). Pin a version and an expected hash, and fail on
      mismatch.

### W12. CI coverage
- [ ] `windows-latest` CI runs, but check that the tests actually exercise the paths above
      with mocks: bat parsing, backup-root selection, UIA handoff dicts, console encoding.
