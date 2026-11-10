# TODO

## macOS status (2026-11-10)

The macOS path works end to end on the iPhone 8: backup (a whole new one each time, nothing overwritten) -> verify +
password proof -> safety clone -> erase -> activate (scripted real click) -> Finder UI restore -> post-restore audit
and ROI report. Verified on a real phone: the restore from a backup with apps brought all apps back; a restore from
a backup taken before the apps had re-downloaded (0 apps) does not (guarded: `restorer.py backups`, auto-source skip).

### Open

- [ ] **Try the wrong and the empty password on a real Welcome screen.** Needs the phone erased again (so a good backup
      WITH apps first: wait for the apps to finish downloading, then `backup_runner.py`, check `restorer.py backups`
      lists the apps). Harness (temporary, outside the repo): `PW_TEST_MODE=empty|wrong python3
      <scratchpad>/password_test_run.py --source clone --confirm --hello-confirmed --json`. It drives the real restore
      and, right after Continue, clears the box (empty) or types a dummy (wrong). Expected: empty -> the driver says the
      box is empty and notifies; wrong -> Finder rejects, the driver handles the alert, after two failures
      `OFFER_PASSWORD_HELPER`; the person types the real password in Finder's box and the restore continues.
      Known gap to check: a sheet that is first seen FILLED and later emptied by hand never notifies (`_password`
      keeps `sheet_prefilled=True`); the harness's empty mode clears before the first look, so it exercises the normal path.
- [ ] Live check of the new pop-up path on a Welcome screen: the driver now opens the backup pop-up with a real click
      (`FinderDriver.click`, `restorer.real_click`) and heals stale Finder lists once; both are unit-tested against the
      fake Finder, and the real-click sidebar open was verified live, but the pop-up click has not been seen live yet.
- [ ] Failure paths are out of scope for now (unplugging, locked phone, eSIM/2FA/keychain verification: decided 2026-11-10).
- [x] Retire older helpers still used only by Windows/iTunes code paths (done 2026-11-10 with WB5/WD6).

### Done 2026-11-10

- Whole-new backups: the runner renames the previous backup aside, rolls back on failure, never prunes; older safety
  clones are kept (`-prewipe-<stamp>`); `backup_manager.py` / `restorer.py backups` lists backups with stats and
  proposes the next clean-up move (Trash only after a yes, with guards). `--authoritative` removed.
- Backup stats briefing (apps, 2FA apps, Keychain, size) with a WARNING for app-less backups; `--source auto` skips them.
- Finder driver: real mouse click for the sidebar row and the backup pop-up; trailing-space sidebar names; fresh Finder
  window; one automatic heal when Finder's backup list is stale; labels for older/hand-protected copies.
- Removed the old Finder helpers (`stage-finder`, `confirm-sheet`, `monitor-restore`, `detect_finder_setup_view`,
  `auto_confirm_finder_restore_sheet`, `monitor_restore_progress`) and their tests.
- Apple ID refusal after the first restore was a CLI-restore artifact: not an issue with the Finder restore.
- Researched price estimates for every iPhone model (`market_pricing.BEST_ESTIMATES`).

### Backlog (lower priority)

- [ ] English-only UI strings in `finder_restore.py` (`Welcome`, `Restore`, `Continue`, `Restoring`...): they only work
      on an English macOS. Prefer AX roles/identifiers; where text is unavoidable use a locale table and fail closed
      to a handoff.
- [ ] `common.BACKUP_ROOT` is computed at import time and imported by value in many modules; env overrides set after
      import are ignored. Call `backup_root()` at use sites.
- [x] README badge says "Platform: macOS", but Windows is supported (now "macOS | Windows").
- [ ] Fresh Mac with no Homebrew: prove the `dep_installer.py --tools` Homebrew-missing branch.
- [ ] Click through the password helper page (`password_tracker.py --exit-on-success`) in a real browser.
- [ ] Windows: section W, everything that needs no person is done (2026-11-10, see "Progress" there). Left: install
      iTunes (UAC click), a live backup (passcode), then the iTunes recon/driver (WD1 captures, WD2, WD7).

---

## W. Windows-only (hand to the agent on the Windows box)

Rewritten 2026-11-10 after studying the finished macOS path. Items are in priority order (WA first). Each says which
functions change and how behaviour changes. "Unverified" marks a guess that must be checked live before it is relied on.

**Decisions carried over from macOS (do not reopen):**
- Restore is UI-driven only. `idevicebackup2 restore` left phones booting empty; there is no CLI restore on Windows either.
- Backup and queries are CLI-first (`idevicebackup2 backup`, `ideviceinfo`, `idevice_id`), through `backup_runner.py`.
- Every backup is a whole new backup; nothing is overwritten or deleted automatically.
- The password is never typed, read or logged by code or by the agent.
- The person is alerted (sound + banner) at every point that needs them, and told the moment it is crossed.

**State of the Windows test box (probed 2026-11-10, read-only):**
- Windows 10 IoT Enterprise LTSC 2021 (19044), en-US, no Microsoft Store. NTFS C: with 63.9 GB free.
- Apple Mobile Device Support 20.0.1.47 is installed; service `Apple Mobile Device Service` is running.
- **iTunes is NOT installed** (no `C:\Program Files\iTunes\iTunes.exe`, no Appx package). The Apple Devices app cannot be
  installed here (no Store). So today this box can back up but has no way to restore.
- `%APPDATA%\Apple Computer\MobileSync\Backup` exists and is empty. The good backups of the iPhone 8 are on the Mac.
- iPhone 8 (iPhone10,4, iOS 16.7.16) is paired and trusted, `SetupState: RestoredFromiTunesBackup`, `WillEncrypt: true`,
  44.1 of 59.1 GB free, name "Персик " (trailing space). Its apps were still re-downloading: see the 0-app trap, AGENTS Rule 4c.
- libimobiledevice tools (`idevicebackup2 1.4.0-9-gfa0f791`) exist only in the OLD checkout `C:\Users\user\phone_revival\tools\bin`
  (`tools/` is gitignored). The new clone at `C:\Users\user\ios-system-data-cleaner` has none yet.
- Python 3.12 with `comtypes`, `pywin32`, `psutil`, `keyring`, `windows_mcp`; no `uiautomation`, no `pywinauto`.
- Note: `Get-AppxPackage` fails under PowerShell 7 (`pwsh`) on this box; it works under Windows PowerShell 5.1. Code must
  keep calling `powershell.exe`, never `pwsh`.

**Progress 2026-11-10 (agent on the Windows box, no person needed):** WA1-WA4, WB1, WB2, WB4-WB6, WC1, WD1 (the
recorder; captures still to do), WD3 (all but the driver itself), WD5/WD6, WE1-WE3, WF1-WF3, WG1, WG2, WG4, WH1-WH3 are
done; 278 tests pass on this box. Found and fixed live: every `ideviceinfo`/`idevice_id` read decoded with the ANSI code
page and crashed on the Cyrillic phone name (now UTF-8 everywhere); SQLite handles leaked by `token_extractor`
(broke 10 tests on Windows). Checked live: the pinned tools install and talk to the phone, backup preflight passes,
the toast API accepts a banner (visibility under Focus Assist unverified), Credential Manager round-trip, robocopy
clone, `revive.bat` Python probe, `iOSbackup` installs and imports on Windows (not yet tried on a real backup).

**Still needs the person or a live run, in this order:**
1. ~~Install iTunes~~ done 2026-11-10: iTunes 12.13.12.3 (Win32), `restore_app()` = `itunes_win32`.
2. First Windows backup, once the apps have finished downloading (the phone now reports ~26 GB used, up from 15):
   `python backup_runner.py` + `--watch` (**passcode on the phone**). This closes WB3 (is idevicebackup2's output late
   through a pipe? then derive the phase from disk growth), WC2 (each alert fires once), WF2 timing, WE3 on a real
   backup (iOSbackup proof on Windows), and checks `restorer.py backups` shows the apps.
3. WD1 captures: Summary is done; still Welcome, backup list, password dialog, wrong-password alert, restoring
   (during the first erase cycle) -> WD2 driver -> WD7.

**Found 2026-11-10 with iTunes installed (changes the WD2 design for the better):**
- iTunes builds its UI Automation tree ONLY if Windows reports a screen reader when iTunes starts. Started normally it
  exposes 13 elements (no sidebar, no device button); started with `SPI_SETSCREENREADER` on it exposes everything
  (the Summary page: 87 elements, every button, checkbox and text by name). `itunes_restore.start_accessible()` sets
  the flag only around iTunes' start-up and puts it back; the tree stays for that iTunes session.
- The sidebar row `SelectionItemPattern.Select()` reports success but doesn't change the page; `InvokePattern` on the
  toolbar button "iPhone" opens the phone's Summary. The phone's name keeps its trailing space ("Персик ").
- `finder_restore.classify` already classifies the captured iTunes Summary as "summary" (same "Back Up Now" /
  "Check for Update" words), so `SCREEN_WORDS` may need few iTunes entries.
- On the Summary: "Automatically sync when this iPhone is connected" is off; "Encrypt local backup" is on; "Restore
  Backup" is disabled while this PC has no backup (a hard no anyway); the phone lists 45 apps / 11.08 GB.
- Fixtures: `tests/fixtures/itunes/{library_accessible,summary}.txt` (serial / phone number redacted on save).

**Suggested order of work:** WA -> WB -> install iTunes -> first Windows backup once the apps have finished downloading
(check the app count) -> WC -> WD recon -> first erase/restore cycle with the driver in observe mode -> WE onwards.
Safety net for that first cycle: the Mac's Finder restore still works, at the cost of anything newer than the Mac's backup.

---

### WA. Safety before any erase on Windows (do first, small)

- [x] **WA1. Gate the erase on a working restore route.** Today `iphone_manager.cmd_revive` only checks that AMDS answers,
      then prints the erase steps. On a box like this one the person could erase with no app able to restore.
      - `itunes_env.py`: add `restore_app() -> {"kind": "itunes_win32" | "itunes_store" | "apple_devices" | None, "exe",
        "version"}`. Win32: registry keys already read in `check_itunes_installation` AND the exe really exists. Store /
        Apple Devices: `Get-AppxPackage` via `powershell.exe`. Add the result to `ITunesStatus` (new field `restore_app`).
        Note the current code reports `itunes_installed` from a registry key alone; require the exe.
      - `restore_gates.py`: add `restore_route()` (macOS: Finder, always present; Windows: `itunes_env.restore_app()`), and
        a refusal code `no_restore_app` with options: install iTunes from Apple (`dep_installer.install_itunes`), restore
        on a Mac instead, stop.
      - `iphone_manager.cmd_revive`: call it before step 3 prints "STAGING COMPLETE"; return 2 with the options.
        `backup_runner.BackupRunner.post_flight_verification`: when the route is missing, the final message must say
        "backup is safe, but do NOT erase yet: this PC has nothing to restore with".
      - `check_preconditions` (restore side) gets the same check so the dry run reports it too.
- [x] **WA2. Remove the "click Restore Backup..." instruction.** `iphone_manager.cmd_revive` (~line 445) tells the person
      to "Open iTunes -> Click 'Restore Backup...'". AGENTS.md lists that button as a hard no (it overwrites a live phone).
      Replace the closing block with `restore_gates.ERASE_STEPS` + `ERASE_WARNING` and "tell me when it shows Hello".
      Same fix in `windows_revival_guide.md` step 6, which also says the restore radio is preselected and to press Continue blind.
- [x] **WA3. Make the shared wording platform-neutral.** `restore_gates.ERASE_STEPS`, the refusals in
      `check_preconditions` ("on this Mac", "`brew install`"), `finder_restore.handoff` and `Run` messages say "Mac" /
      "Finder" / "python3". Add `common.host_words() -> {"computer": "Mac"|"PC", "app": "Finder"|"iTunes"|"Apple Devices",
      "python": "python3"|"python"}` and format the strings with it. No behaviour change on macOS; tests compare strings,
      so update them in the same change.
      Done: `common.host_words()` ("app" is "iTunes" on Windows; Apple Devices wording waits for WG3). Remaining
      Finder-only strings live in macOS-only code paths (`finder_restore.Run` screen texts: WD3 `SCREEN_WORDS`).
- [x] **WA4. Bring the tools into the new clone.** `common.find_tool` looks in `<repo>/tools/bin`, PATH, Scoop. Either run
      `dep_installer.install_cli_tools` (after WG1 pins the hash) or copy `tools\bin` from the old checkout. Do not add the
      old checkout to PATH.

### WB. Backup through `backup_runner.py`; retire the SendKeys backup

Makes the macOS engine the Windows engine. Closes old W2, the backup half of W3 and W8-focus by deleting the code.

- [x] **WB1. Use `find_tool` for the engine.** `BackupRunner.preflight` (~line 316, `encryption on`) and
      `BackupRunner.run_backup` (~line 358) call the bare name `"idevicebackup2"`. With the tools in `tools\bin` that raises
      FileNotFoundError on Windows. Resolve once in `__init__` (`self.tool = find_tool("idevicebackup2")`), refuse in
      `preflight` with `CONNECTION_MESSAGES["tools_missing"]` when None.
- [x] **WB2. Fix the pipe reader used on Windows.** `BackupRunner._run_pipe` opens the child with `text=True`, which
      decodes with the ANSI code page: the device name "Персик" and any non-ASCII path get mangled or raise. Read bytes,
      decode UTF-8 with `errors="replace"`, and split on both `\r` and `\n` the way `_run_pty` does (share one
      `_feed(buffer)` helper between the two). Add `creationflags=CREATE_NO_WINDOW`.
- [x] **WB3. Check child buffering live.** Closed 2026-11-10 on the first Windows backup: the "enter your passcode"
      alert came "just about right" (operator's words; good enough: it only has to be roughly concurrent, with the agent
      telling the person what to expect). The `--watch` stream showed only "accepted": the passcode was typed within
      one poll, so the agent must announce the passcode step itself when it starts a backup. No disk-growth fallback needed.
      Original note: Without a console the MinGW build probably block-buffers stdout, so
      "Waiting for passcode to be entered" and the percent lines may arrive minutes late. Measure with a real backup.
      If late: in `BackupRunner._heartbeat` derive the phase from the disk instead (no bytes written N seconds after
      start -> `waiting_passcode` + alert; first growth -> `streaming` + "passcode accepted" alert), keeping the text
      triggers in `_process_line` as the fast path. `ProgressEstimator` already measures bytes, so check-ins keep working.
      Do not add `pywinpty` unless this fails.
- [x] **WB4. Delete the false "encrypted devices hang" branch.** `ITunesAutomation.trigger_backup` (~lines 417-447)
      skips `idevicebackup2` when `WillEncrypt` is true. That is wrong: an already-encrypted phone backs up without the
      password (the macOS path does it every run). Only `encryption on` needs it, and `preflight` already handles that.
- [x] **WB5. Retire the iTunes backup UI code.** Remove `send_backup_shortcut`, `actuate_backup_ui`, `trigger_backup`,
      `detect_and_handle_dialogs`, `monitor_backup`, `drive_full_backup_workflow` and the `windows_mcp` `Desktop` import
      from `itunes_automation.py` (this is the "retire older helpers" item in the macOS list). Rewire the callers:
      - `iphone_manager.cmd_revive --auto-drive` and `cmd_itunes --drive/--actuate/--monitor` -> `BackupRunner(...).execute()`
        / `backup_runner.py --watch`.
      - `ios_mcp_server.drive_itunes_backup` -> start the runner; progress stays `get_backup_progress`.
      - Remove the matching tests in `tests/test_hardening_backlog.py`, `test_improvements.py`, `test_suite.py`.
      Keep `detect_device_wipe_state` only if something still calls it; `common.device_setup_verdict` is the newer
      equivalent used by the gates, so prefer deleting it and `cmd_detect_wipe` pointing at the verdict.
- [x] **WB6. Make `revive` on Windows follow the macOS sequence.** `cmd_revive` still uses the old chain
      (`find_backup_for_target` -> `inspect_backup` -> clone -> tokens). Change it to: runner (whole-new backup, rollback)
      -> verify + password proof -> safety clone -> `restorer.py backups` briefing with the 0-app WARNING -> WA1 gate ->
      erase wording. An existing backup older than the freshness limit is no longer accepted silently.
      Done: `--use-existing` keeps the old "use the current backup" path but through the full 24 h gate.
- [~] **WB7. Windows first-time encryption.** `BackupRunner._prompt_for_password` uses `getpass` and stores the result
      with `store_in_keychain`. On Windows route the store through WE1; when no TTY, start the helper page instead of failing.
      Partly: the store goes to Credential Manager and the prompt says so. Without a TTY it still refuses (a NEW
      password must be typed by the person; the helper page only proves an existing one): needs a "set password" page.

### WC. Alerts on Windows (sound + banner)

- [x] **WC1. One alert function for both platforms.** Move `play_sound` and `send_notification` from `backup_runner.py`
      into `common.py` as `alert(message, title, subtitle, sound)`; keep thin wrappers in `backup_runner` so imports and
      tests keep working. `BackupRunner.notify` and `finder_restore._alert` then need no change.
      - macOS: unchanged (`afplay`, `osascript display notification`).
      - Windows sound: `winsound.MessageBeep` with a map Ping -> MB_ICONASTERISK, Glass -> MB_OK, Basso -> MB_ICONHAND.
        Run in a thread; never block.
      - Windows banner: toast through `powershell.exe` and WinRT `ToastNotificationManager` (no install needed; use the
        PowerShell AppUserModelID). Fallback when that throws: `System.Windows.Forms.NotifyIcon.ShowBalloonTip`. Pass
        the text through an environment variable, not string interpolation, so quotes in a device name cannot break the
        script. Timeout 5 s, all failures swallowed.
      - Unverified: toasts on LTSC with Focus Assist on. If they are suppressed, the sound still plays; say so in the log.
- [ ] **WC2. Same alert points as macOS.** After WC1 and WB3 confirm on a real run that each fires once: passcode needed,
      passcode accepted, 25/50/75 %, verification failed, backup done, restore needs the password, password rejected,
      helper page opened, restore needs a hand (handoff), restore finished.

### WD. Restore by driving iTunes (the large item; replaces old W3-restore and W4)

Design: reuse `finder_restore.Run` unchanged as the state machine and give it a Windows driver with the same five
methods (`snapshot`, `click`, `escape`, `heal`, `open_device`). `backup_picker.choose_backup`, `restore_gates` and the
exit codes / handoff protocol stay shared.

- [~] **WD1. Recon before code.** Add `itunes_restore.py --dump` (read-only): prints the iTunes window's UI Automation tree
      as the same `W|||cls|||name|||value|||enabled` lines `finder_restore.parse_snapshot` reads. Capture and save as test
      fixtures: Summary (possible now), then Welcome, backup list open, password dialog, wrong-password alert, restoring,
      during the first erase cycle. Everything below marked unverified depends on these captures.
      Unverified: how much of iTunes' custom-drawn UI is exposed to UIA; the exact English strings; whether the Welcome
      screen shows a "Last Backed Up" line or puts the date in the combo entry; whether the password box is a separate
      top-level dialog.
      Recorder done: `itunes_restore.py --dump [--save NAME]` (UI Automation via powershell.exe, password values
      never read, extra fields: automation id + rectangle). Checked on Explorer. The iTunes captures need iTunes.
- [ ] **WD2. `ITunesDriver` (new file `itunes_restore.py`).**
      - Transport: `powershell.exe -NoProfile` running a script that loads `UIAutomationClient` / `UIAutomationTypes`
        (.NET, always present). This mirrors `_osa` and keeps the repo free of third-party runtime packages. One helper
        `_ps_uia(script, timeout)` raising a `UIError` (rename `FinderUIError` to a shared base so `Run._drive` catches both).
      - `snapshot()`: find the main window by process name `iTunes` (not by title: W8), walk descendants, map ControlType
        to the existing class words: Button -> "button", RadioButton -> "radio button", ComboBox -> "pop up button",
        Edit -> "text field", Text -> "static text", CheckBox -> "checkbox", ProgressBar -> "progress indicator",
        ListItem/MenuItem -> "menu item". A modal child window or owned dialog sets `has_sheet` and its controls get scope "S".
        Never return the value of a password Edit: report only "" or "filled" (`IsPassword` is true and UIA hides the value
        anyway; `_box_filled` then needs a non-empty marker, so check length via `ValuePattern` only if readable, else
        treat the box as empty and let the person press the button).
      - `click(scope, cls, name, exact, nth)`: InvokePattern for buttons, SelectionItemPattern for radio and list items,
        ExpandCollapsePattern for the combo. Fallback only when a pattern is missing: a real click at the element's
        `BoundingRectangle` centre with `SendInput` (ctypes), the Windows twin of `restorer.real_click`. Return the same
        strings: "CLICKED", "DISABLED", "NOT_FOUND", "NO_WINDOW".
      - `open_device()`: start iTunes if needed (path from `restore_app()`), wait for the window, then select the device
        button (unverified name). On failure return the same `handoff` shape with `position_points`.
      - `escape()`: `SendInput` VK_ESCAPE to the iTunes window. `heal()`: close iTunes, wait for exit, start it again,
        `open_device()`. Do not restart the AMDS service (needs admin).
      - No keystrokes for navigation, no `SetForegroundWindow` dependence: UIA patterns work on background windows.
- [~] **WD3. Let the shared code run on Windows.**
      - `finder_restore.restore`: replace the `sys.platform != "darwin"` refusal (~line 903) with a driver factory:
        darwin -> `FinderDriver`, win32 -> `ITunesDriver` when `restore_app()` is iTunes, otherwise refusal `no_restore_app`.
      - `finder_restore.classify`: move the literal strings ("Restore from this backup", "Back Up Now", "Check for Update",
        `RESTORING_PHRASES`, the confirm labels in `sheet_confirm_button`, `_password_rejected`) into a per-app table
        `SCREEN_WORDS[app]` chosen by the driver (`driver.app`). This is also the hook for the macOS "English-only" backlog item.
      - `_restore_process_running` and `backup_labels._busy`: on Windows look for `AppleMobileBackup.exe` (and
        `idevicebackup2.exe` for `_busy`) with `tasklist /FI`. Unverified: that iTunes on Windows runs the transfer in
        `AppleMobileBackup.exe` (it ships in `Common Files\Apple\Mobile Device Support`); confirm during the first restore.
        If it does not, `Run._finish` falls back to the phone's own `SetupState` plus a quiet period.
      - `open_in_system_browser`: `os.startfile(url)` on Windows.
      - `RESUME_CMD`: build from `sys.executable` name instead of the literal `python3`.
      - `restorer.main`: `activate` and `heal-finder` become no-ops with a clear message on Windows, or map to
        `ITunesDriver.open_device` / `heal` (iTunes activates an unactivated phone once it shows it; unverified).
      - `restorer.device_window_names`, `close_device_windows`, `select_device_in_finder`: macOS only; guard them.
      Done: `common.process_running` (tasklist) for `_restore_process_running` and `backup_labels._busy`,
      `os.startfile` browser, `RESUME_CMD` from `host_words`, `restore()` refuses with `no_restore_app` or
      `itunes_driver_not_ready` (the agent drives iTunes by vision until WD2). Left: the driver factory and
      `SCREEN_WORDS[app]` (need WD1 captures), `restorer.main` activate/heal-finder on Windows.
- [ ] **WD4. Backup choice must be proven (old W4).** `backup_picker.choose_backup` already refuses to guess.
      For iTunes: feed it the combo's entries and the date text. `parse_when` and `_DATE_SUFFIX` only know Finder's English
      formats; add the iTunes format once WD1 shows it (unverified). If iTunes shows no capture time at all, accept only
      when exactly one entry carries this phone's name after `backup_labels.ensure_labels` made the labels distinct
      (`expected_label` path), otherwise stop with `step: choose_backup`. Never press Continue on an unproven entry.
      Check live that iTunes shows `Display Name` (what `backup_labels` rewrites) and not `Device Name` (unverified).
- [x] **WD5. Password box on Windows.** iTunes for Windows has no saved-password prefill (unverified, believed true), so
      the normal case is the empty box: `Run._password` already notifies and only watches. Check the known macOS gap
      (box first seen filled, later emptied) cannot occur here. Wording in `_offer_helper` / `_check_helper` mentions
      "Keychain Access": switch by platform (see WE2).
      Done for the wording; whether iTunes ever prefills the box is still to be seen on the first restore.
- [x] **WD6. Remove the blind restore.** Delete `ITunesAutomation.actuate_restore_ui` (sends `{ENTER}` to whatever has
      focus) and `iphone_manager.cmd_itunes --restore`; point the CLI at `restorer.py ui-restore`.
- [ ] **WD7. First live cycle in observe mode.** Run `ui-restore` with a flag that makes `ITunesDriver.click` refuse
      (returns "NOT_FOUND") so every step ends in a handoff; the agent performs each click with the OS-control MCP while
      `--dump` records the trees. Turn the recordings into a fake-iTunes test like `tests/test_finder_restore.py`, then
      enable real clicks.

### WE. Password proof and storage on Windows (old W10)

- [x] **WE1. Credential Manager through ctypes.** In `password_tracker.py` make `store_in_keychain`,
      `get_keychain_password`, `_keychain_item_account` and `list_keychain_backup_accounts` dispatch by platform. Windows:
      `advapi32.CredWriteW` / `CredReadW` / `CredEnumerateW`, type GENERIC, target `iOS Backup:<UDID>`, persist
      LOCAL_MACHINE, blob UTF-16. `cmdkey` cannot read a secret back, and `keyring` is a third-party package. Behaviour
      change: a proven password is remembered per Windows user, so `backup_runner.preflight` and
      `backup_verifier` stop asking again. Update `test_non_mac_returns_empty_without_calling_security`.
- [x] **WE2. No "look it up" UI on Windows.** Credential Manager does not reveal generic secrets, so `open_keychain_ui`
      has no equivalent. On Windows: hide the "Open Keychain Access" button in the helper page
      (`PasswordServerHandler.do_GET`, the HTML near line 568) and change `PASSWORD_FOUND` in `finder_restore._check_helper`
      to "the password you just typed in the helper page is the right one; type the same into the iTunes box".
      `smart_keychain_probe` and `backup_verifier` `--steer-ui` print the Windows wording instead of steering.
- [~] **WE3. Proof tool on Windows.** `password_tracker._find_iosbackup_python` / `_find_mvt_ios` decide how a password is
      tested. Check `proof_tool_available()` on this box and that `dep_installer` can install what it needs; the pre-erase
      gate is blocked without it.
      `pip install iOSbackup` works on this box and `proof_tool_available()` is now True; `dep_installer.py
      --proof-tool` and the setup routine install it. Still to prove a real Windows backup with it (needs one).

### WF. Disk space and clean-up on NTFS (old W9)

- [x] **WF1. Honest space budget.** NTFS has no copy-on-write: a cycle holds the previous backup (rotated aside), the new
      one and the `robocopy` clone, about 3x the backup at peak. `BackupRunner.preflight` and `cmd_revive` only test a
      flat 20 GB. Change to: needed = estimated backup size x 2.2 (new backup + clone) on non-APFS, compared with free
      space; refuse with the numbers and point at `restorer.py backups` for a clean-up proposal. `make_prewipe_clone`
      already checks 1.1x before copying; keep it.
      Done: `common.space_needed_gb`; the backup status shows a `cloning` phase that is never reported as a stall.
- [~] **WF2. `robocopy` details.** In `common.make_prewipe_clone`: add `/DCOPY:T /XJ /MT:8 /NJH /NJS`, keep "exit < 8 is
      success" but log the code, and time it on a real 10+ GB backup. Update `update_status("cloning")` so check-ins say
      the copy is running (on macOS it is instant, on Windows it takes minutes).
      Flags, exit-code log and the `cloning` status are in; checked on a synthetic backup. Timing on 10+ GB: first real backup.
- [x] **WF3. Clean-up without the Recycle Bin.** `backup_manager.trash_backup` refuses off macOS. Do not use the Recycle
      Bin: for folders this large the shell can delete permanently without a visible prompt. Move the folder with
      `os.replace` to `<backup root parent>\Removed Backups\<folder>` (same volume, instant, recoverable, and outside the
      folder iTunes lists). Keep all existing guards. Message: "moved to Removed Backups; delete that folder to free the space".
- [ ] **WF4. Later, optional: hard-link clone.** Hard-linking the hashed payload files (copying only the plists and
      `Manifest.db`) would make the clone near-free. Risk: a manual "Back Up Now" in iTunes rewrites payload files in
      place and would damage both copies. Only consider it together with a read-only attribute on the clone, and test first.

### WG. Environment detection and supply chain (old W5, W6, W11, W13)

- [x] **WG1. Pin the libimobiledevice download.** `dep_installer.install_cli_tools` follows the GitHub "latest" release
      and only prints the SHA-256. Pin one release (start from `LIBIMOBILE_FALLBACK_URL`), add `LIBIMOBILE_SHA256`, fail
      and delete the file on mismatch, and extract only after the check. A new release needs a deliberate constant bump.
- [x] **WG2. Treat "the phone answers" as the truth.** `cmd_revive` uses `amds_service_running` to decide what is wrong.
      The Apple Devices app has no such service, so that check is a false alarm there. Use `common.diagnose_connection()`
      first; consult the service only to explain a failure. Confirmed on this box: service name
      `Apple Mobile Device Service` with the Win32 AMDS 20.0.1.47.
- [ ] **WG3. Detect the Apple Devices app; fail closed.** `restore_app()` (WA1) reports it. Until someone has a Windows 11
      box, `ITunesDriver` is not used for it: the restore stops with a handoff that names the app and the steps.
      Still open from the old list: the exact backup path it writes to (candidate `%USERPROFILE%\Apple\MobileSync\Backup`
      is already in `common.windows_backup_root_candidates`), and whether iTunes stops showing the phone once it is installed.
- [x] **WG4. Apple's USB driver without iTunes.** This box proves AMDS alone is enough for backup. Document that
      iTunes is needed for the restore only, and let `dep_installer.install_itunes` say so.

### WH. Tests, CI and docs (old W12)

- [~] **WH1.** (all but the fake-iTunes test, which waits for WD7) Fake-iTunes driver test (WD7); pipe reader with `\r` progress and Cyrillic bytes (WB2); `alert()` dispatch
      with `sys.platform` patched (WC1); Credential Manager functions with `ctypes` mocked (WE1); `restore_route` gate
      (WA1); `trash_backup` on Windows (WF3); budget maths (WF1).
- [x] **WH2.** `windows-latest` CI already runs the suite: add a job step that runs `revive.bat` far enough to prove the
      Python detection (old W1 is fixed but was never run on a clean Windows).
- [x] **WH3.** `windows_revival_guide.md` rewritten to the agent-driven flow; README badge "Platform: macOS" -> both;
      AGENTS.md "CLI-first rule" and section 8 already say restore is UI-only: add the iTunes driver beside the Finder one.

### Already done (kept for the record)

- [x] W1 `revive.bat`: run-time `if errorlevel`, real Python probe (`py -3` first, Store alias avoided), `cd /d "%~dp0"`.
      Still wants one run on a clean Windows (WH2).
- [x] W2 duplicate keystrokes / duplicate backup triggers (the code goes away entirely with WB5).
- [x] W6 `common.backup_root()` prefers the candidate holding the UDID, then the newest.
- [x] W7 UTF-8 console (`common.ensure_utf8_console`, `chcp 65001` in `revive.bat`).
- [x] W8 `json` import in `itunes_automation.main()`.
- Superseded: old W13 "CLI-first for backup AND restore". Backup is CLI-first (WB); restore is UI-only (WD).
