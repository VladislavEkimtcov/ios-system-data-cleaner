# TODO

## Next, immediate: prove the UI restore on a real Welcome screen

**Done 2026-11-09/10:** the CLI restore is gone (`cli_restore.py` and its tests deleted; `restorer.py
cli-restore` removed). The restore is now `restorer.py ui-restore` (`finder_restore.py`): a deterministic
state machine over Finder's accessibility tree (role/name lookup, verify-after-every-action) that stops
with an `agent_handoff` when a step fails, so the agent fixes the screen with the computer-use MCP and
resumes. The safety gates live on in `restore_gates.py`. Covered by `tests/test_finder_restore.py`
(scripted fake Finder) and `tests/test_restore_gates.py`.

- [x] First real run 2026-11-09 (iPhone 8): welcome -> Continue -> Keychain-prefilled sheet -> Restore -> restore completed, phone back as its own name. Fixed live: popup shows placeholder "Item 1" before it settles; AppleScript prints "missing value"; click script must materialise `entire contents` first; window closes mid-restore (don't reopen). Still to tune: snapshot takes ~5 s; progress text.
- [x] 2026-11-09 second run: Finder skipped the password sheet; script wrongly reported "restored" after 1 min (name flips early). Fixed: done = AppleMobileBackup exited + phone restarted + SetupState RestoredFromiTunesBackup (~11 min). Re-run end to end to see the fixed completion path live.
- [x] Backup picker (`backup_picker.py`): name + "Last Backed Up" time must match the validated backup; verified live on the Welcome screen (keep-current path). A script-opened pop-up isn't readable through AX (a real click is), so a non-matching preselection hands off to the agent. Untested live: the walk-the-menu path (needs a readable menu).
- [x] `backup_labels.py`: distinct, dated labels for this phone's backups (applied 2026-11-09 to 5 folders; reversible). Finder reads `Display Name`, not `Device Name` (fixed 2026-11-09 after a live check). The corrected labels have not yet been seen in a real pop-up: check at the next Welcome screen.
- [x] Label hooks wired (after the safety copy, before a real restore, after a successful restore); covered by tests with the real backups isolated. Not exercised live: a full backup -> revive -> restore cycle through all three.
- [ ] Find a script-side way to open the pop-up so its items are exposed (real-click equivalent via CGEvent?), so the agent isn't needed for a non-matching preselection.
- [ ] **Run it for real again** on an erased phone (`ui-restore` dry run, then `--confirm --hello-confirmed`)
      and fix what the real tree looks like: the AX class names/values in `snapshot_script` (radio
      value `1`/`0`, the backup pop-up's menu items, the sheet's text field and "Restore" button, the
      progress text phrases in `RESTORING_PHRASES`) were written from earlier hand-driven runs and
      have NOT been exercised against a live Welcome screen. Expect to tune them.
- [ ] Check `entire contents of window` speed on this Mac's Finder (snapshot each 3 s; if slow, scope
      it to the splitter group).
- [ ] Confirm the sheet-settle rule (1.5 s) is enough: an earlier hand click during the slide missed.
- [ ] Retire the older low-level helpers (`stage-finder`, `confirm-sheet`, `monitor-restore`; fixed AX
      paths) once `ui-restore` is proven. They remain only as debugging aids; `iphone_manager.py` and a
      few tests still call them.
- [x] Empty password box is watched; wrong-password alerts are dismissed and the run continues; after 2 failures the password helper is launched and offered (`OFFER_PASSWORD_HELPER`). Tested with the fake Finder and the real helper launcher only; try it live with a deliberately wrong password.
- [ ] Open question (low priority): why the CLI restore booted empty (`--system` untested).

Status: backlog, captured 2026-11-09. The pipeline works end-to-end on the happy path; these
items are about robustness on machines, OS versions, and locales other than the one it was
developed on. Line numbers refer to commit `827fc6c` and will drift.

Guiding rule (matches AGENTS.md §8): **CLI first → structured UI lookup → agent handoff.**
Never fixed coordinates or blind keystrokes, and never English-only string matching as the
only signal.

---

## A. Cross-platform / macOS (this Mac)

### A1. Prefer libimobiledevice CLI over Finder/iTunes UI for the restore
_Implemented 2026-11-09 as `cli_restore.py` (`restorer.py cli-restore`); needs the real-phone run in section M._
- [x] Make `idevicebackup2 -u <UDID> restore --system --settings --reboot <BACKUP_ROOT>` the
      primary restore path. Today the restore is driven entirely through Finder AppleScript
      (`restorer.py` `stage_finder_restore_selection`, `auto_confirm_finder_restore_sheet`) and
      iTunes keystrokes (`itunes_automation.py` `actuate_restore_ui`).
- [x] Password handling (hidden dialog; password only in the child's env, test-covered): `--password` puts the secret on argv (visible in `ps`), which
      SECURITY.md forbids. Use `-i/--interactive` and feed stdin, or confirm whether the
      installed libimobiledevice version reads the password any other way. Add a test
      that the password never shows up in argv.
- [x] Keep the Finder/iTunes UI path as a fallback only. Its output must keep the existing
      structured `handoff` dict.
- [x] Restore against the verified `-prewipe` clone (or verify the live folder still matches
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
- [x] Every Finder script picks the first window whose name contains "iPhone" or "iPad".
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
_CLI path done in `cli_restore.wait_for_finish`; the Finder path still uses the heuristic._
- [~] `monitor_restore_progress` decides "complete" from Finder status text
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

## M. macOS full-stack validation (hand to the agent on this Mac)

Do this before calling macOS "ready for a normie to point an agent at the repo URL".
Needs a spare phone you can afford to wipe (never the daily driver) and the human at the
physical gates. Follow the CLI-first rule in AGENTS.md: Finder is driven only if the tools are
missing, the install was declined, and the user accepted Finder driving.

Already exercised on 2026-11-09 (phone was found already restored, so only the front half ran):
connection diagnosis, host buffer check, `backup_verifier.py` PASS with proof of password,
safe refusal in `stage-finder` when the phone was not wiped. Fixed that day: Finder window match
with a trailing space in the device name, and `status` misreporting a restored phone as wiped.

- [~] Fresh-user start: no libimobiledevice installed. Detection and the macOS installer
      (`dep_installer.py --tools`: choices without consent, `--yes` to install, Homebrew-missing
      branch) are done and unit-tested. Still to prove on a Mac that really has no Homebrew.
- [x] Baseline: `storage_monitor.py --snap before_reset`; `status` shows the real setup state.
- [x] Backup via `backup_runner.py` (incremental, 6 min; passcode gate not exercised because the phone was unlocked): encryption forced on, progress check-ins
      (`.backup_status.json`), `Ping`/`Glass` sounds and banners at the passcode gate.
- [x] `backup_verifier.py` PASS with proof of password, then the `-prewipe` APFS clone.
      AGENTS.md `--compact` mention fixed. Found and fixed: `make_prewipe_clone` reused a stale
      clone (4 h older than the verified backup); it now refreshes it, swapping only after the
      new one verifies.
- [ ] Gate 1: human erases on the phone ("Keep eSIMs and Erase Data").
      `restorer.py wait` detects the return to the Hello screen.
- [ ] Restore via CLI (A1; `restorer.py cli-restore`, dry-run already refuses correctly on a set-up phone). Then separately test the Finder fallback: `stage-finder` against a
      real Welcome screen, window match with a device name that has trailing spaces or
      non-Latin characters, password sheet, backup choice.
- [ ] Password helper page for the proof (`password_tracker.py --exit-on-success`, also `cli-restore --password-source helper`): click through it once in a real browser.
- [x] Activation (found 2026-11-09): erased phone is Unactivated and refuses CLI restore; `restorer.py activate` opens it in Finder, waits for Activated, closes the window. Scripted sidebar click only highlights on this Mac; the handoff (vision click) works.
- [ ] Gate 2: password entry. `monitor-restore` takes over and follows the restore to reboot.
- [ ] Post-restore: `storage_monitor.py --diff`, ROI report, MVT scan, app catalog. Wait until
      app re-downloads settle before reading the "recovered GB" number.
- [ ] Failure paths: unplug mid-backup, locked phone, wrong backup password, Finder hung
      (`heal-finder`), disk buffer too small. Each must end in a clear one-line human action.
- [ ] End state: the phone is usable, eSIM/2FA/keychain intact, nothing left half-done.

### M-next. Open items after the first real CLI restore (2026-11-09, do later)

The first end-to-end run (erase -> activate via Finder -> `cli-restore` -> restarted as
"Яблоко", activated, SetupState RestoredFromiTunesBackup) worked. Still to do:

- [ ] **Apps missing after restore (reported by the user, investigate first).** Facts so far: the
      backup holds 45 installed apps in Info.plist and ~866 app domains in Manifest.plist; the
      phone's data used (~8 GB) matches the backup's ~9.8 GB, so the app *data* came back. App
      binaries are not in a backup: iOS re-downloads them after the person signs in to their
      Apple ID, so apps stay absent until that happens (and the phone must be on Wi-Fi). Find
      out whether the user saw empty home screen vs greyed download icons, whether the phone is
      still at Setup Assistant, and whether the Apple ID step was skipped. Decide whether
      `cli-restore` should pass/avoid `--skip-apps`, and add plain-language guidance to
      AGENTS.md: "sign in with your Apple ID on the phone so your apps can re-download".
- [ ] **RESOLVED by Finder; CLI restore root cause still open (2026-11-09).** A Finder restore of the
      same backup gave the full onboarding (Apple ID, Touch ID) and all apps. The CLI restores
      (`--system --settings`, default reboot) booted to the lock screen with no setup and no
      apps. Research found corroboration, not a documented bug: a 2023 write-up
      (naut.ca, "Restoring iPhones with idevicebackup2") reports `--system --settings` installed
      no apps and that `--system --no-reboot` (no `--settings`) followed by a manual reboot fixed
      it; libimobiledevice issue #1548 (iOS 17.4.1) reports a restore that reaches 100% yet the
      phone boots as new; #414 asks about the setup screens (opposite symptom) and has no fix.
      `--settings` restoring the phone's "setup done" state is my UNCONFIRMED hypothesis.
      SOURCE CONFIRMED 2026-11-09 (idevicebackup2.c): `--settings` => `RestorePreserveSettings=false`
      (the backup's settings overwrite the phone's fresh ones); without it => `true`. Default
      reboot = `RestoreShouldReboot` omitted. `cli-restore` now omits `--settings` by default
      (`--restore-settings` opts in) and keeps the auto reboot.
      TO PROVE: erase the phone, `activate`, `cli-restore --confirm --hello-confirmed`, and check
      that Setup Assistant + Apple ID appear and apps come back. Then drop "experimental".
      (Older plan, now superseded: CLI restore with `--system --no-reboot` only, then reboot by hand; compare. Until
      then `cli-restore` is marked experimental in AGENTS.md and the agent should default to Finder.)
- [ ] **Apple ID sign-in failed on first boot after the restore (2026-11-09):** phone went straight to the
      Home screen, signed out, and refused the Apple ID password, so no apps re-downloaded and the
      layout was not rebuilt. Backup identity was verified (right phone, 17:47, 45 apps listed).
      Add a PRE-erase question to AGENTS.md: "Do you know your Apple ID password, and can you
      sign in on this phone (2FA device nearby)?", and a post-restore step that checks sign-in.
      Also guard `make_prewipe_clone`: refreshing the clone replaces the older good copy; a new
      backup of a degraded phone must never replace the only good backup (keep one previous
      generation). A protected copy was made by hand: `.protected-<udid>-golden-20261109`.
- [ ] Post-restore: wait for app re-downloads to settle, then `storage_monitor.py --snap
      post_restore` and `--diff`, ROI report, MVT scan, app catalog.
- [ ] Verify eSIM, 2FA apps and keychain are intact (end-state item).
- [ ] Failure paths: unplug mid-restore, wrong backup password (covered by tests, not on a
      real phone), locked phone, Finder hung (`heal-finder`), disk buffer too small.
- [ ] Test the Finder-fallback restore (`stage-finder`) on a real Welcome screen.
- [ ] Click through the password helper page in a real browser (`password_tracker.py
      --exit-on-success`, and `cli-restore --password-source helper`).
- [ ] Fresh Mac with no Homebrew: prove the `dep_installer.py --tools` Homebrew-missing branch.
- [ ] Activation: the scripted Finder sidebar click only highlights the row on this Mac; the
      handoff (vision click) works. Find a reliable scripted way, or keep the handoff.
- [ ] `restorer.py wait`: reword its "Hello" claim; it cannot see the screen (see AGENTS.md).
- [ ] Windows: everything in section W still needs a real machine.

---

## W. Windows-only (hand to the agent on the Windows box)

Verify every item on a real Windows 10 and Windows 11 machine with an iPhone attached. Note
which Apple software is installed for each test: iTunes Win32 installer, iTunes from the
Microsoft Store, or the **Apple Devices** app.

### W1. `revive.bat` Python detection is broken
_Fixed on `hardening/batch-1`; still needs a run on real Windows 10/11 (with and without the Store alias)._
- [x] The inner `if %errorlevel% neq 0` (lines ~13) sits inside the parenthesised block that
      starts at line 11. `%errorlevel%` there is expanded when the block is parsed, so it
      still holds the failing result of `where python`. If `python` is missing but `py` is
      installed, the script still says "Python 3 is not detected" and exits. Fix with
      `if errorlevel 1` or delayed expansion `!errorlevel!`.
- [x] `where python` also matches the Microsoft Store *App Execution Alias* stub in
      `%LOCALAPPDATA%\Microsoft\WindowsApps`, which opens the Store instead of running
      Python. Try `py -3` first, then check that `python --version` really runs.
- [x] Add `cd /d "%~dp0"` so the script works when launched from another directory.

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
- [x] Emoji and box-drawing output can raise `UnicodeEncodeError` on legacy cp1252/cp437
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

### W13. CLI-first for backup AND restore, Finder/iTunes only as consented fallback
Policy is in AGENTS.md ("CLI-first rule"). macOS is being tested first; the Windows agent
works this out on the box.
- [ ] Find out what actually installs `idevicebackup2.exe` today (`winget search`,
      `scoop search`, `choco search libimobiledevice`; prebuilt
      github.com/jrjr/libimobiledevice-windows; MSYS2 clang64 package). Check what
      `dep_installer.py` already does and reuse it.
- [ ] Apple's USB driver (iTunes or Apple Devices app) is still required; detect it.
- [ ] Detect tools via a shared `find_cli_tools()` (`shutil.which` plus known install dirs).
      If missing: offer install; fall back to iTunes/Apple Devices UI automation only if the
      user declines AND accepts UI driving.
- [ ] Restore over CLI (`idevicebackup2 restore`) has no code path on either platform yet;
      mirror whatever the macOS side ends up with.
