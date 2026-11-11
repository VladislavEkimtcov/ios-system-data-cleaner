"""
Coverage for the Windows port (TODO.md section W) that runs on any OS: no phone, no iTunes, no GUI.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("IOS_CLEANER_STATE_DIR", tempfile.mkdtemp(prefix="ioscl_test_state_"))

import backup_runner  # noqa: E402
import common  # noqa: E402


def _runner(tmp: str) -> "backup_runner.BackupRunner":
    return backup_runner.BackupRunner(udid="U1", backup_dir=Path(tmp), status_file=Path(tmp) / "s.json",
                                      play_audio=False, show_notifications=False, verbose=False)


class TestPipeReader(unittest.TestCase):
    """WB2: the Windows pipe reader reads bytes, decodes UTF-8 and splits on CR as well as LF."""

    def _lines(self, chunks):
        tmp = tempfile.mkdtemp()
        r = _runner(tmp)
        seen = []
        r._process_line = seen.append  # type: ignore[method-assign]
        buf = ""
        for c in chunks:
            buf = r._feed(buf, c)
        r._feed(buf, b"", final=True)
        return seen

    def test_carriage_return_progress_bars_become_separate_lines(self):
        out = self._lines([b"[=====     ]  45% Finished\r[======    ]  52% Finished\rBackup Successful.\n"])
        self.assertEqual(out, ["[=====     ]  45% Finished", "[======    ]  52% Finished", "Backup Successful."])

    def test_cyrillic_split_across_reads_survives(self):
        name = "Персик".encode("utf-8")
        out = self._lines([b"Device: " + name[:3], name[3:] + b"\n"])
        self.assertEqual(out, ["Device: Персик"])

    def test_last_line_without_newline_is_flushed(self):
        self.assertEqual(self._lines([b"ErrorCode 105: boom"]), ["ErrorCode 105: boom"])

    def test_run_pipe_with_a_real_child_process(self):
        script = textwrap.dedent("""
            import sys, time
            out = sys.stdout.buffer
            out.write("Waiting for passcode to be entered\\n".encode()); out.flush(); time.sleep(0.05)
            out.write("Receiving files\\n[==  ]  25% Finished\\r".encode()); out.flush()
            out.write("Устройство Персик\\n".encode("utf-8")); out.flush()
            out.write(b"Backup Successful.")
        """)
        tmp = tempfile.mkdtemp()
        r = _runner(tmp)
        seen = []
        orig = r._process_line

        def record(line):
            seen.append(line)
            orig(line)
        r._process_line = record  # type: ignore[method-assign]
        rc = r._run_pipe([sys.executable, "-c", script], os.environ.copy())
        self.assertEqual(rc, 0)
        self.assertIn("Устройство Персик", seen)
        self.assertEqual(seen[-1], "Backup Successful.")
        self.assertTrue(r.passcode_requested)
        self.assertTrue(r.handshake_confirmed)
        self.assertEqual(r.overall_percent, 25.0)


class TestEngineResolution(unittest.TestCase):
    """WB1: the engine is resolved with find_tool, and its absence is refused up front."""

    def test_missing_engine_is_refused_in_preflight(self):
        with mock.patch.object(backup_runner, "find_tool", return_value=None):
            r = _runner(tempfile.mkdtemp())
        ok, msg = r.preflight()
        self.assertFalse(ok)
        self.assertEqual(msg, common.CONNECTION_MESSAGES["tools_missing"])

    def test_found_engine_is_used_for_the_backup_command(self):
        fake = Path(tempfile.mkdtemp()) / "idevicebackup2.exe"
        with mock.patch.object(backup_runner, "find_tool", return_value=fake):
            r = _runner(tempfile.mkdtemp())
        self.assertEqual(r.tool, str(fake))
        captured = {}

        def fake_pipe(cmd, env):
            captured["cmd"] = cmd
            return 0
        with mock.patch.object(backup_runner, "rotate_backup_aside", return_value=None), \
                mock.patch.dict(sys.modules, {"pty": None}), \
                mock.patch.object(r, "_run_pipe", side_effect=fake_pipe):
            ok, _ = r.run_backup()
        self.assertTrue(ok)
        self.assertEqual(captured["cmd"][:3], [str(fake), "backup", "--full"])


class TestAlerts(unittest.TestCase):
    """WC1: one alert function, dispatched by platform; never interpolates the text into a script."""

    def test_windows_banner_passes_text_through_the_environment(self):
        with mock.patch.object(common.sys, "platform", "win32"), \
                mock.patch.object(common.subprocess, "Popen") as popen:
            how = common.send_notification('Phone "Персик" needs you', title="T", subtitle="S")
        self.assertEqual(how, "windows")
        args, kwargs = popen.call_args
        self.assertEqual(args[0][0], "powershell.exe")  # Windows PowerShell 5.1, never pwsh
        self.assertNotIn("Персик", " ".join(args[0]))
        self.assertEqual(kwargs["env"]["IOSC_ALERT_BODY"], 'Phone "Персик" needs you')
        self.assertEqual(kwargs["env"]["IOSC_ALERT_TITLE"], "T: S")

    def test_windows_sound_uses_message_beep(self):
        fake = mock.MagicMock()
        with mock.patch.object(common.sys, "platform", "win32"), mock.patch.dict(sys.modules, {"winsound": fake}), \
                mock.patch("threading.Thread") as thread:
            common.play_sound("Basso")
            target = thread.call_args.kwargs["target"]
            target()
        fake.MessageBeep.assert_called_once_with(0x10)

    def test_mac_banner_still_uses_osascript(self):
        with mock.patch.object(common.sys, "platform", "darwin"), \
                mock.patch.object(common.subprocess, "run") as run:
            self.assertEqual(common.send_notification("hi", title="T", subtitle="S"), "osascript")
        self.assertEqual(run.call_args.args[0][0], "osascript")

    def test_failures_are_swallowed(self):
        with mock.patch.object(common.sys, "platform", "win32"), \
                mock.patch.object(common.subprocess, "Popen", side_effect=OSError("no powershell")):
            self.assertIsNone(common.send_notification("x"))

    def test_backup_runner_wrappers_delegate(self):
        with mock.patch.object(common, "send_notification") as sn, mock.patch.object(common, "play_sound") as ps:
            backup_runner.send_notification("m", title="t", subtitle="s")
            backup_runner.play_sound("Ping")
        sn.assert_called_once_with("m", title="t", subtitle="s", sound=None)
        ps.assert_called_once_with("Ping")


@unittest.skipUnless(sys.platform == "win32", "real Windows only")
class TestWindowsPowerShellToast(unittest.TestCase):
    def test_toast_script_parses(self):
        # Parse only (no banner): catches a broken script without showing anything.
        ps = ("$e=$null; [System.Management.Automation.Language.Parser]::ParseInput($env:IOSC_SCRIPT,"
              "[ref]$null,[ref]$e) | Out-Null; $e.Count")
        env = dict(os.environ, IOSC_SCRIPT=common._WIN_TOAST_PS)
        out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(out.stdout.strip(), "0", out.stderr)


class TestCredentialManagerDispatch(unittest.TestCase):
    """WE1: on Windows the 'Keychain' functions use Credential Manager, never `security`."""

    def setUp(self):
        import password_tracker as pt
        self.pt = pt
        pt._KEYCHAIN_CACHE.clear()
        self.addCleanup(pt._KEYCHAIN_CACHE.clear)
        p = mock.patch.object(pt.sys, "platform", "win32")
        p.start()
        self.addCleanup(p.stop)

    def test_store_writes_one_generic_credential_per_phone(self):
        with mock.patch.object(self.pt, "wincred_write", return_value=(True, "ok")) as w, \
                mock.patch.object(self.pt.subprocess, "check_output") as co:
            ok, msg = self.pt.store_in_keychain("UDID1", "secret")
        self.assertTrue(ok)
        self.assertIn("Credential Manager", msg)
        w.assert_called_once_with("iOS Backup:UDID1", "UDID1", "secret")
        co.assert_not_called()

    def test_read_by_account_and_first_found(self):
        with mock.patch.object(self.pt, "wincred_targets", return_value=["iOS Backup:A", "iOS Backup:B"]), \
                mock.patch.object(self.pt, "wincred_read", side_effect=lambda t: "pw-" + t[-1]), \
                mock.patch.object(self.pt.subprocess, "run") as run:
            self.assertEqual(self.pt.get_keychain_password(account="B"), "pw-B")
            self.assertEqual(self.pt.get_keychain_password(), "pw-A")
            self.assertEqual(self.pt._keychain_item_account("B"), "B")
            self.assertIsNone(self.pt._keychain_item_account("C"))
        run.assert_not_called()

    def test_no_keychain_ui_on_windows(self):
        with mock.patch.object(self.pt.subprocess, "run") as run:
            self.assertFalse(self.pt.open_keychain_ui())
        run.assert_not_called()


class TestSpaceBudget(unittest.TestCase):
    """WF1: off APFS the safety copy is a full copy, so the budget scales with the backup."""

    def test_mac_keeps_the_flat_minimum(self):
        self.assertEqual(common.space_needed_gb(50 * common.GB, "darwin")[0], 20.0)

    def test_windows_needs_backup_plus_headroom_no_copy(self):
        need, why = common.space_needed_gb(25 * common.GB, "win32")
        self.assertEqual(need, 30.0)
        self.assertNotIn("safety copy", why)

    def test_safety_copy_only_on_macos(self):
        with mock.patch.object(common.sys, "platform", "win32"):
            self.assertFalse(common.safety_copy_enabled())
        with mock.patch.object(common.sys, "platform", "darwin"):
            self.assertTrue(common.safety_copy_enabled())

    def test_small_backup_still_needs_the_minimum(self):
        self.assertEqual(common.space_needed_gb(2 * common.GB, "win32")[0], 20.0)
        self.assertEqual(common.space_needed_gb(None, "win32")[0], 20.0)


class TestProcessRunning(unittest.TestCase):
    def test_windows_matches_image_names_case_insensitively(self):
        out = '"System Idle Process","0","Services","0","8 K"\n"AppleMobileBackup.exe","42","Console","1","9 K"\n'
        fake = mock.Mock(stdout=out)
        with mock.patch.object(common.sys, "platform", "win32"), \
                mock.patch.object(common.subprocess, "run", return_value=fake):
            self.assertTrue(common.process_running("applemobilebackup"))
            self.assertFalse(common.process_running("idevicebackup2"))

    def test_mac_uses_pgrep_alternation(self):
        with mock.patch.object(common.sys, "platform", "darwin"), \
                mock.patch.object(common.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(common.process_running("AppleMobileBackup", "idevicebackup2"))
        self.assertEqual(run.call_args.args[0], ["pgrep", "-f", "AppleMobileBackup|idevicebackup2"])

    def test_errors_mean_not_running(self):
        with mock.patch.object(common.subprocess, "run", side_effect=OSError("x")):
            self.assertFalse(common.process_running("x"))


class TestHostWordsAndHelperPage(unittest.TestCase):
    """WA3 / WE2: one string serves both platforms; Windows gets no 'Open Keychain Access' button."""

    def test_words(self):
        self.assertEqual(common.host_words("darwin")["computer"], "Mac")
        self.assertEqual(common.host_words("win32")["computer"], "PC")
        self.assertEqual(common.host_words("win32")["python"], "python")

    def test_helper_page_per_platform(self):
        import password_tracker as pt
        win = pt.render_page("U", "T", "win32")
        mac = pt.render_page("U", "T", "darwin")
        self.assertNotIn("Open Keychain Access", win)
        self.assertIn("Credential Manager", win)
        self.assertIn("Open Keychain Access", mac)
        for page in (win, mac):
            self.assertNotRegex(page, r"__[A-Z_]+__")


class TestRestoreAppDetection(unittest.TestCase):
    """WA1: what can restore on this computer; a registry key alone doesn't count."""

    def test_mac_is_always_finder(self):
        import itunes_env
        with mock.patch.object(itunes_env.sys, "platform", "darwin"):
            self.assertEqual(itunes_env.restore_app()["kind"], "finder")

    def test_windows_without_anything_is_none(self):
        import itunes_env
        with mock.patch.object(itunes_env.sys, "platform", "win32"), \
                mock.patch.object(itunes_env, "_itunes_exe_candidates", return_value=[Path("C:/nope/iTunes.exe")]), \
                mock.patch.object(itunes_env, "_appx", return_value=None):
            self.assertIsNone(itunes_env.restore_app()["kind"])

    def test_windows_store_and_apple_devices(self):
        import itunes_env
        pkgs = {"AppleInc.AppleDevices": {"version": "1.2", "location": "C:/WindowsApps/x"}}
        with mock.patch.object(itunes_env.sys, "platform", "win32"), \
                mock.patch.object(itunes_env, "_itunes_exe_candidates", return_value=[]), \
                mock.patch.object(itunes_env, "_appx", side_effect=pkgs.get):
            r = itunes_env.restore_app()
        self.assertEqual((r["kind"], r["version"]), ("apple_devices", "1.2"))

    def test_windows_win32_itunes_needs_the_exe(self):
        import itunes_env
        exe = Path(tempfile.mkdtemp()) / "iTunes.exe"
        exe.write_bytes(b"")
        with mock.patch.object(itunes_env.sys, "platform", "win32"), \
                mock.patch.object(itunes_env, "_itunes_exe_candidates", return_value=[exe]), \
                mock.patch.object(itunes_env, "winreg", None):
            r = itunes_env.restore_app()
        self.assertEqual((r["kind"], r["exe"]), ("itunes_win32", str(exe)))


class TestUIRecorder(unittest.TestCase):
    """WD1: the iTunes recorder speaks parse_snapshot's language and never reads a password box."""

    def test_script_maps_control_types_and_hides_passwords(self):
        import itunes_restore
        script = itunes_restore.dump_script()
        self.assertIn("'ComboBox' = 'pop up button'", script)
        self.assertIn("IsPassword", script)
        self.assertNotIn("__MAP__", script)

    def test_capture_parses_like_a_finder_snapshot(self):
        import finder_restore
        import itunes_restore
        text = ("WINDOW|||iTunes\nW|||radio button|||Restore from this backup:|||1|||true|||rb1|||1,2,3,4\n"
                "W|||pop up button|||Персик|||Персик · main · Nov 10|||true|||cb|||5,6,7,8\n"
                "SHEET|||Enter Password\nS|||text field||||||true|||pw|||\nS|||button|||Restore||||||false||||||\n")
        snap = finder_restore.parse_snapshot(text)
        self.assertEqual(snap["window"], "iTunes")
        self.assertTrue(snap["has_sheet"])
        self.assertEqual(snap["items"][1]["value"], "Персик · main · Nov 10")
        self.assertFalse(snap["items"][-1]["enabled"])
        self.assertEqual(itunes_restore.summarize(text)["elements"], 4)

    def test_refuses_off_windows(self):
        import itunes_restore
        with mock.patch.object(itunes_restore.sys, "platform", "darwin"):
            with self.assertRaises(itunes_restore.UIError):
                itunes_restore.dump("iTunes")

    def _fixture(self, name):
        return (REPO_ROOT / "tests" / "fixtures" / "itunes" / f"{name}.txt").read_text(encoding="utf-8")

    def test_real_itunes_summary_capture(self):
        # Captured live 2026-11-10 (iTunes 12.13.12.3, Windows 10 LTSC, iPhone 8 on iOS 16.7.16).
        import finder_restore
        import itunes_restore
        text = self._fixture("summary")
        self.assertTrue(itunes_restore.is_accessible(text))
        snap = finder_restore.parse_snapshot(text)
        self.assertEqual(finder_restore.classify(snap), "summary")  # Finder's classifier already reads iTunes' Summary
        names = {it["name"] for it in snap["items"]}
        self.assertTrue({"Back Up Now", "Restore Backup", "Restore iPhone…", "Encrypt local backup"} <= names)
        self.assertIn("Персик ", names)  # the trailing space is part of the phone's name
        self.assertNotRegex(text, r"Serial Number: (?!REDACTED)")

    def test_itunes_started_without_a_screen_reader_hides_its_ui(self):
        import itunes_restore
        self.assertTrue(itunes_restore.is_accessible(self._fixture("library_accessible")))
        self.assertFalse(itunes_restore.is_accessible("WINDOW|||iTunes\nW|||button|||Go to the iTunes Store|||||||||true\n"))

    def test_real_itunes_restore_screens(self):
        # Captured live 2026-11-10 during the first Windows restore.
        import finder_restore
        import itunes_restore
        welcome = finder_restore.parse_snapshot(self._fixture("welcome"))
        self.assertEqual(finder_restore.classify(welcome), "welcome")
        entry = next(i["value"] for i in welcome["items"] if i["cls"] == "pop up button")
        self.assertTrue(entry.startswith("Персик · main ·"))
        alert = self._fixture("wrong_password_alert")
        self.assertIn(itunes_restore.WRONG_PASSWORD_PHRASE, alert)
        self.assertTrue(finder_restore.parse_snapshot(alert)["has_sheet"])

    def test_never_presses_buttons_that_overwrite_or_wipe(self):
        import itunes_restore
        for b in ("Restore Backup", "Restore iPhone…", "Set up as new iPhone", "Back Up Now", "Sync"):
            with self.assertRaises(itunes_restore.UIError):
                itunes_restore.press(b)

    def test_redact(self):
        import itunes_restore
        self.assertEqual(itunes_restore.redact("W|||button|||Serial Number: F00BA4|||x"),
                         "W|||button|||Serial Number: REDACTED|||x")

    @unittest.skipUnless(sys.platform == "win32", "real Windows only")
    def test_live_dump_of_explorer(self):
        import itunes_restore
        text = itunes_restore.dump("explorer", max_nodes=50)
        if text == "NO_WINDOW":
            self.skipTest("no desktop session")
        self.assertTrue(text.startswith("WINDOW|||"))
        self.assertTrue(any(ln.startswith(("W|||", "S|||")) for ln in text.splitlines()))


class TestPasswordAdvice(unittest.TestCase):
    """AGENTS Rule 1: the remediation text never suggests a password on a command line."""

    def test_remediation_points_at_the_helper_page(self):
        import contextlib
        import io
        import backup_verifier
        for plat in ("darwin", "win32"):
            buf = io.StringIO()
            with mock.patch.object(backup_verifier.sys, "platform", plat), contextlib.redirect_stdout(buf),                     mock.patch("password_tracker.open_keychain_ui", return_value=False):
                backup_verifier.print_remediation_guide(["PROOF OF PASSWORD REQUIRED: x"], "UDID1", interactive=False)
            out = buf.getvalue()
            self.assertNotIn("--password", out)
            self.assertIn("password_tracker.py --udid UDID1 --exit-on-success", out)


@unittest.skipUnless(sys.platform == "win32", "real Windows only")
class TestCredentialManagerLive(unittest.TestCase):
    def test_round_trip_with_a_dummy_secret(self):
        import password_tracker as pt
        target = f"iOS Backup:UNITTEST-{os.getpid()}"
        ok, why = pt.wincred_write(target, "UNITTEST", "pässwörd-Пароль")
        if not ok:
            self.skipTest(f"no Credential Manager session here: {why}")
        self.addCleanup(pt.wincred_delete, target)
        self.assertEqual(pt.wincred_read(target), "pässwörd-Пароль")
        self.assertIn(target, pt.wincred_targets())
        self.assertTrue(pt.wincred_delete(target))
        self.assertIsNone(pt.wincred_read(target))


if __name__ == "__main__":
    unittest.main()


class TestUnprovenPasswordIsNotAFailure(unittest.TestCase):
    """A first backup on a new computer, good except for proof of the password: open the helper page and carry on,
    never report "Backup FAILED" and never roll the good backup back (TODO W, second-run handoff)."""

    def _fake_inspect(self, results):
        calls = iter(results)

        def inspect(**kw):
            ok, fails = next(calls)
            if kw.get("failures_out") is not None:
                kw["failures_out"].extend(fails)
            return ok
        return inspect

    def _setup(self, tmp):
        r = _runner(tmp)
        (Path(tmp) / "U1").mkdir()
        return r

    def test_only_password_unproven(self):
        import backup_verifier as bv
        self.assertTrue(bv.only_password_unproven([bv.PASSWORD_UNPROVEN_PREFIX + ": x"]))
        self.assertFalse(bv.only_password_unproven([bv.PASSWORD_UNPROVEN_PREFIX + ": x", "backup is NOT encrypted"]))
        self.assertFalse(bv.only_password_unproven([]))

    def test_proven_on_the_helper_page_carries_on_to_the_safety_copy(self):
        import backup_verifier as bv
        with tempfile.TemporaryDirectory() as tmp:
            r = self._setup(tmp)
            pwd_fail = (False, [bv.PASSWORD_UNPROVEN_PREFIX + ": no proof"])
            with mock.patch.object(bv, "inspect_backup", side_effect=self._fake_inspect([pwd_fail, (True, [])])), \
                 mock.patch.object(r, "_prove_password_with_helper", return_value=True) as helper, \
                 mock.patch.object(backup_runner, "safety_copy_enabled", return_value=True), \
                 mock.patch.object(backup_runner, "make_prewipe_clone",
                                   side_effect=backup_runner.CloneError("stop here")) as clone:
                ok, msg = r.post_flight_verification()
            helper.assert_called_once()
            clone.assert_called_once()   # got past verification to the safety copy
            self.assertIn("clone", msg)

    def test_not_proven_keeps_the_backup_and_says_so(self):
        import backup_verifier as bv
        with tempfile.TemporaryDirectory() as tmp:
            r = self._setup(tmp)
            pwd_fail = (False, [bv.PASSWORD_UNPROVEN_PREFIX + ": no proof"])
            with mock.patch.object(r, "preflight", return_value=(True, "")), \
                 mock.patch.object(r, "run_backup", return_value=(True, "")), \
                 mock.patch.object(bv, "inspect_backup", side_effect=self._fake_inspect([pwd_fail])), \
                 mock.patch.object(r, "_prove_password_with_helper", return_value=False), \
                 mock.patch.object(r, "_roll_back") as roll_back:
                self.assertFalse(r.execute())
            roll_back.assert_not_called()
            import backup_progress
            snap = backup_progress.describe(backup_progress.read_status(Path(tmp) / "s.json"))
            self.assertEqual(snap["phase"], "password_unproven")
            self.assertNotIn("FAILED", snap["summary"])
            self.assertIn("kept", snap["summary"])

    def test_a_real_failure_still_fails(self):
        import backup_verifier as bv
        with tempfile.TemporaryDirectory() as tmp:
            r = self._setup(tmp)
            bad = (False, ["backup is NOT encrypted", bv.PASSWORD_UNPROVEN_PREFIX + ": x"])
            with mock.patch.object(bv, "inspect_backup", side_effect=self._fake_inspect([bad])), \
                 mock.patch.object(r, "_prove_password_with_helper") as helper:
                ok, _ = r.post_flight_verification()
            self.assertFalse(ok)
            helper.assert_not_called()

    def test_windows_skips_the_safety_copy(self):
        import backup_verifier as bv
        with tempfile.TemporaryDirectory() as tmp:
            r = self._setup(tmp)
            with mock.patch.object(bv, "inspect_backup", return_value=True), \
                 mock.patch.object(backup_runner, "safety_copy_enabled", return_value=False), \
                 mock.patch.object(backup_runner, "make_prewipe_clone") as clone:
                ok, _ = r.post_flight_verification()
            self.assertTrue(ok)
            clone.assert_not_called()
