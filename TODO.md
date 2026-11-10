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
- [ ] Retire older helpers still used only by Windows/iTunes code paths when Windows is next touched.

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
- [ ] README badge says "Platform: macOS", but Windows is supported.
- [ ] Fresh Mac with no Homebrew: prove the `dep_installer.py --tools` Homebrew-missing branch.
- [ ] Click through the password helper page (`password_tracker.py --exit-on-success`) in a real browser.
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
