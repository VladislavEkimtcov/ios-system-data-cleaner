"""
Coverage for TODO.md backlog items that can be checked without a device or a GUI:
W2 (no duplicate backup keystrokes), W6 (Windows backup-root selection), W8 (json import),
A10 (single backup-root implementation).
"""

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("IOS_CLEANER_STATE_DIR", tempfile.mkdtemp(prefix="ioscl_test_state_"))

import common  # noqa: E402
import itunes_automation  # noqa: E402
import itunes_env  # noqa: E402


class _FakeDesktop:
    def __init__(self, fail=False):
        self.fail = fail
        self.keys = []

    def shortcut(self, key):
        if self.fail:
            raise RuntimeError("no desktop")
        self.keys.append(key)


def _driver(desktop):
    d = itunes_automation.ITunesAutomation.__new__(itunes_automation.ITunesAutomation)
    d.desktop = desktop
    d.launch_or_focus_itunes = mock.Mock(return_value=True)
    return d


@mock.patch.object(itunes_automation.time, "sleep", lambda *_: None)
class TestBackupKeystrokes(unittest.TestCase):
    def test_sendkeys_fallback_skipped_when_mcp_succeeded(self):
        drv = _driver(_FakeDesktop())
        with mock.patch.object(itunes_automation.subprocess, "run") as run:
            self.assertTrue(drv.send_backup_shortcut())
        run.assert_not_called()
        self.assertEqual(drv.desktop.keys, ["alt+f", "d", "b"])

    def test_sendkeys_fallback_used_when_mcp_fails(self):
        drv = _driver(_FakeDesktop(fail=True))
        with mock.patch.object(itunes_automation.subprocess, "run") as run:
            self.assertTrue(drv.send_backup_shortcut())
        run.assert_called_once()

    def test_sendkeys_fallback_used_without_mcp(self):
        drv = _driver(None)
        with mock.patch.object(itunes_automation.subprocess, "run") as run:
            self.assertTrue(drv.send_backup_shortcut())
        run.assert_called_once()

    def test_actuate_focuses_itunes_once(self):
        drv = _driver(_FakeDesktop())
        drv.actuate_backup_ui()
        self.assertEqual(drv.launch_or_focus_itunes.call_count, 1)

    def test_no_resend_while_dialog_open(self):
        drv = _driver(_FakeDesktop())
        drv.actuate_backup_ui = mock.Mock(return_value=True)
        drv.detect_and_handle_dialogs = mock.Mock(return_value="set_password_dialog")
        clock = iter(range(0, 10_000, 100))  # each time.time() call jumps 100 s
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"IPHONE_BACKUP_DIR": tmp}), \
                mock.patch.object(itunes_automation.time, "time", lambda: next(clock)), \
                mock.patch.object(itunes_automation, "find_tool", return_value=None):
            res = drv.trigger_backup("UDID-X", settling_window_sec=1)
        self.assertEqual(drv.actuate_backup_ui.call_count, 1)
        self.assertEqual(res["engine"], "itunes_ui")

    def test_resend_once_when_no_dialog(self):
        drv = _driver(_FakeDesktop())
        drv.actuate_backup_ui = mock.Mock(return_value=True)
        drv.detect_and_handle_dialogs = mock.Mock(return_value=None)
        clock = iter(range(0, 10_000, 100))
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"IPHONE_BACKUP_DIR": tmp}), \
                mock.patch.object(itunes_automation.time, "time", lambda: next(clock)), \
                mock.patch.object(itunes_automation, "find_tool", return_value=None):
            drv.trigger_backup("UDID-X", settling_window_sec=1)
        self.assertEqual(drv.actuate_backup_ui.call_count, 2)


class TestItunesAutomationImports(unittest.TestCase):
    def test_json_available_at_module_level(self):
        # main() prints json.dumps(...); it must work when main() is called via import.
        self.assertTrue(hasattr(itunes_automation, "json"))


class TestSingleBackupRoot(unittest.TestCase):
    def test_itunes_env_uses_common_implementation(self):
        self.assertIs(itunes_env.get_itunes_backup_root, common.backup_root)

    def test_mobilesync_override_honoured_everywhere(self):
        env = {"MOBILESYNC_BACKUP_DIR": "/tmp/ms-override"}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("IPHONE_BACKUP_DIR", None)
            self.assertEqual(itunes_env.get_itunes_backup_root(), Path("/tmp/ms-override").resolve())

    def test_itunes_automation_reads_root_live(self):
        self.assertFalse(hasattr(itunes_automation, "BACKUP_ROOT"))


class TestSelectBackupRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.a = base / "a"  # e.g. stale Win32 iTunes folder
        self.b = base / "b"  # e.g. Apple Devices folder
        self.missing = base / "missing"

    def tearDown(self):
        self.tmp.cleanup()

    def _backup(self, root, udid, age_s):
        d = root / udid
        d.mkdir(parents=True)
        f = d / "Status.plist"
        f.write_bytes(b"")
        t = time.time() - age_s
        os.utime(f, (t, t))

    def test_none_when_nothing_exists(self):
        self.assertIsNone(common.select_backup_root([self.missing]))

    def test_first_existing_when_empty(self):
        self.a.mkdir()
        self.b.mkdir()
        self.assertEqual(common.select_backup_root([self.missing, self.a, self.b]), self.a)

    def test_prefers_newest_backup(self):
        self._backup(self.a, "OLD", age_s=86400 * 300)
        self._backup(self.b, "NEW", age_s=60)
        self.assertEqual(common.select_backup_root([self.a, self.b]), self.b)

    def test_prefers_root_holding_udid(self):
        self._backup(self.a, "TARGET", age_s=86400 * 30)
        self._backup(self.b, "OTHER", age_s=60)
        self.assertEqual(common.select_backup_root([self.a, self.b], udid="TARGET"), self.a)

    def test_udid_in_both_picks_newest(self):
        self._backup(self.a, "TARGET", age_s=86400 * 30)
        self._backup(self.b, "TARGET", age_s=60)
        self.assertEqual(common.select_backup_root([self.a, self.b], udid="TARGET"), self.b)

    def test_unknown_udid_falls_back_to_newest(self):
        self._backup(self.a, "OLD", age_s=86400 * 300)
        self._backup(self.b, "NEW", age_s=60)
        self.assertEqual(common.select_backup_root([self.a, self.b], udid="NOPE"), self.b)


class TestKeychainEnumeration(unittest.TestCase):
    """A9: targeted metadata lookups only; never `security dump-keychain`, never -w."""

    def _fake_security(self, present):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            acct = cmd[cmd.index("-a") + 1] if "-a" in cmd else (present[0] if present else None)
            ok = acct in present
            out = f'keychain: "x"\n    "acct"<blob>="{acct}"\n    "svce"<blob>="iOS Backup"\n' if ok else ""
            return mock.Mock(returncode=0 if ok else 44, stdout=out)
        return calls, run

    def test_only_targeted_metadata_queries(self):
        import password_tracker
        calls, run = self._fake_security(["AAA", "CCC"])
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(password_tracker.sys, "platform", "darwin"), \
                mock.patch.object(password_tracker, "connected_udids", return_value=["BBB"]), \
                mock.patch.object(password_tracker.subprocess, "run", side_effect=run), \
                mock.patch.object(password_tracker.subprocess, "check_output") as co:
            root = Path(tmp)
            for name in ("CCC", "CCC-prewipe"):
                (root / name).mkdir()
                (root / name / "Info.plist").write_bytes(b"")
            accounts = password_tracker.list_keychain_backup_accounts(["AAA"], root=root)
        co.assert_not_called()
        self.assertEqual(sorted(accounts), ["AAA", "CCC"])
        for cmd in calls:
            self.assertNotIn("dump-keychain", cmd)
            self.assertNotIn("-w", cmd)
        looked_up = [c[c.index("-a") + 1] for c in calls if "-a" in c]
        self.assertEqual(looked_up, ["AAA", "BBB", "CCC"])  # deduped, archive suffix stripped

    def test_non_mac_returns_empty_without_calling_security(self):
        import password_tracker
        with mock.patch.object(password_tracker.sys, "platform", "win32"), \
                mock.patch.object(password_tracker.subprocess, "run") as run:
            self.assertEqual(password_tracker.list_keychain_backup_accounts(["AAA"]), [])
        run.assert_not_called()


class _FakeClock:
    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


class TestWaitUntil(unittest.TestCase):
    """A7: one condition-wait helper instead of fixed sleeps."""

    def test_immediate_true_does_not_sleep(self):
        c = _FakeClock()
        self.assertEqual(common.wait_until(lambda: "ok", 10, clock=c.now, sleep=c.sleep), "ok")
        self.assertEqual(c.sleeps, [])

    def test_returns_when_condition_becomes_true(self):
        c = _FakeClock()
        res = common.wait_until(lambda: c.t >= 2.0 and "ready", 10, 0.5, clock=c.now, sleep=c.sleep)
        self.assertEqual(res, "ready")
        self.assertAlmostEqual(c.t, 2.0)

    def test_times_out_without_overshooting(self):
        c = _FakeClock()
        self.assertIsNone(common.wait_until(lambda: False, 1.2, 0.5, clock=c.now, sleep=c.sleep))
        self.assertAlmostEqual(c.t, 1.2)

    def test_predicate_exception_counts_as_not_yet(self):
        c = _FakeClock()
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise OSError("lockdownd not up")
            return True
        self.assertTrue(common.wait_until(flaky, 10, 1, clock=c.now, sleep=c.sleep))
        self.assertEqual(len(calls), 3)


class TestFinderRespawnWait(unittest.TestCase):
    def test_waits_for_new_pid_not_fixed_sleep(self):
        import restorer
        pids = iter(["100", "100", None, "200"])
        with mock.patch.object(restorer, "_finder_pid", side_effect=lambda: next(pids)), \
                mock.patch.object(restorer.subprocess, "run") as run, \
                mock.patch.object(common.time, "sleep") as slp:
            self.assertTrue(restorer.restart_finder_services(timeout_sec=10, verbose=False))
        self.assertEqual(slp.call_count, 2)  # two polls, then new PID "200" seen
        self.assertEqual(run.call_count, 2)  # killall + activate


class TestUtf8Console(unittest.TestCase):
    """W7: emoji output must not crash a legacy cp1252/cp437 console."""

    def test_reconfigures_legacy_stream(self):
        import io
        buf = io.BytesIO()
        legacy = io.TextIOWrapper(buf, encoding="cp1252")
        with mock.patch.object(common.sys, "stdout", legacy), mock.patch.object(common.sys, "stderr", legacy):
            common.ensure_utf8_console()
            print("✅ backup verified ━━━", file=legacy)
            legacy.flush()
        self.assertIn("✅".encode("utf-8"), buf.getvalue())

    def test_utf8_stream_left_alone(self):
        stream = mock.Mock(encoding="UTF-8")
        with mock.patch.object(common.sys, "stdout", stream), mock.patch.object(common.sys, "stderr", stream):
            common.ensure_utf8_console()
        stream.reconfigure.assert_not_called()


class TestReviveBat(unittest.TestCase):
    """W1: static checks; the script itself must be exercised on a real Windows box."""

    def setUp(self):
        self.text = (REPO_ROOT / "revive.bat").read_text()

    def test_no_parse_time_errorlevel_in_blocks(self):
        code = [ln for ln in self.text.splitlines() if not ln.strip().upper().startswith("REM")]
        self.assertNotIn("%errorlevel% neq", "\n".join(code).lower())

    def test_runs_from_script_dir_and_probes_python(self):
        code = "\n".join(ln for ln in self.text.splitlines() if not ln.strip().upper().startswith("REM"))
        self.assertIn('cd /d "%~dp0"', self.text)
        self.assertNotIn("where python", code)
        self.assertIn("py -3 -c", self.text)
        self.assertIn("chcp 65001", self.text)


class TestDiagnoseConnection(unittest.TestCase):
    """Tell the user *why* the phone isn't usable: cable, lock, Trust prompt, recovery."""

    def _diag(self, udids, usb, pair_out=None, tools=True, udid=None):
        pair_out = pair_out or {}
        calls = []

        def run(cmd, **kw):
            calls.append(cmd[-1])
            out = pair_out.get(cmd[-1], "")
            return mock.Mock(stdout=out, stderr="", returncode=0 if "SUCCESS" in out else 1)
        tool = (lambda name: Path("/x/" + name)) if tools else (lambda name: None)
        with mock.patch.object(common, "connected_udids", return_value=udids), \
                mock.patch.object(common, "usb_ios_devices", return_value=usb), \
                mock.patch.object(common, "find_tool", side_effect=tool), \
                mock.patch.object(common.subprocess, "run", side_effect=run):
            return common.diagnose_connection(udid), calls

    def test_ready(self):
        d, _ = self._diag(["U1"], ["normal"], {"validate": "SUCCESS: Validated pairing with device U1"})
        self.assertTrue(d["ready"])
        self.assertEqual(d["state"], "ready")

    def test_nothing_on_usb_points_at_cable(self):
        d, _ = self._diag([], [])
        self.assertEqual(d["state"], "no_usb_device")
        self.assertIn("charge-only", d["message"])

    def test_on_usb_but_not_listed(self):
        d, _ = self._diag([], ["normal"])
        self.assertEqual(d["state"], "usb_not_listed")
        self.assertIn("Trust", d["message"])

    def test_recovery_mode(self):
        d, _ = self._diag([], ["recovery"])
        self.assertEqual(d["state"], "recovery_mode")

    def test_unpaired_phone_gets_pair_request_and_trust_message(self):
        d, calls = self._diag(["U1"], ["normal"], {
            "validate": "ERROR: Device U1 is not paired with this host",
            "pair": "ERROR: Please accept the trust dialog on the screen of device U1, then attempt to pair again.",
        })
        self.assertEqual(calls, ["validate", "pair"])
        self.assertEqual(d["state"], "needs_trust")
        self.assertIn("Trust This Computer", d["message"])

    def test_pairing_succeeds_after_trust(self):
        d, _ = self._diag(["U1"], ["normal"], {
            "validate": "ERROR: Device U1 is not paired with this host",
            "pair": "SUCCESS: Paired with device U1",
        })
        self.assertTrue(d["ready"])

    def test_locked(self):
        d, _ = self._diag(["U1"], ["normal"], {
            "validate": "ERROR: Could not validate with device U1 because a passcode is set. "
                        "Please enter the passcode on the device and retry."})
        self.assertEqual(d["state"], "locked")

    def test_trust_denied(self):
        d, _ = self._diag(["U1"], ["normal"], {
            "validate": "ERROR: Device U1 said that the user denied the trust dialog."})
        self.assertEqual(d["state"], "trust_denied")
        self.assertIn("Reset Location & Privacy", d["message"])

    def test_tools_missing(self):
        d, _ = self._diag([], [], tools=False)
        self.assertEqual(d["state"], "tools_missing")

    def test_messages_carry_no_udid(self):
        for msg in common.CONNECTION_MESSAGES.values():
            self.assertNotIn("%s", msg)

    def test_usb_parsing_ignores_non_ios_apple_devices(self):
        ioreg = ('+-o Keyboard  {\n  "idVendor" = 1452\n  "idProduct" = 615\n}\n'
                 '+-o iPhone  {\n  "idVendor" = 1452\n  "idProduct" = 4776\n}\n'
                 '+-o Thing  {\n  "idVendor" = 1452\n  "idProduct" = 4737\n}\n')
        with mock.patch.object(common.sys, "platform", "darwin"), \
                mock.patch.object(common.subprocess, "run", return_value=mock.Mock(stdout=ioreg)):
            self.assertEqual(common.usb_ios_devices(), ["normal", "recovery"])


class TestDeviceWindowMatch(unittest.TestCase):
    """A4: a phone named e.g. "Peach" has no "iPhone" in its Finder window title."""

    def test_custom_and_factory_names(self):
        import restorer
        with mock.patch.object(restorer, "device_window_names", return_value=['Bob\'s "Peach"']):
            m = restorer.device_window_match("U1")
        self.assertIn('wName is "Bob\'s \\"Peach\\""', m)
        self.assertIn('wName contains "iPhone"', m)

    def test_scripts_use_the_shared_match(self):
        text = (REPO_ROOT / "restorer.py").read_text()
        self.assertNotIn('if wName contains "iPhone" or wName contains "iPad" then', text)
        self.assertEqual(text.count('.replace("__DEVICE_WINDOW_MATCH__", match)'), 4)


class TestBackupProgress(unittest.TestCase):
    """Users (and their agents) hear "started" right away and get periodic check-ins."""

    def _runner(self, tmp):
        import backup_runner
        r = backup_runner.BackupRunner(udid="U1", backup_dir=Path(tmp), status_file=Path(tmp) / "s.json",
                                       play_audio=False, show_notifications=False, verbose=False)
        r.notify = mock.Mock()
        return r

    def test_per_batch_bars_do_not_count_as_overall_progress(self):
        import backup_progress
        with tempfile.TemporaryDirectory() as tmp:
            r = self._runner(tmp)
            r._process_line("Receiving files")
            r._process_line("[==================================================] 100% (13.6 MB/13.5 MB)")
            self.assertIsNone(r.overall_percent)
            r._process_line("[==========                                        ]  21% Finished")
            st = backup_progress.read_status(Path(tmp) / "s.json")
        self.assertEqual(r.overall_percent, 21.0)
        self.assertEqual(st["phase"], "streaming")
        self.assertEqual(st["overall_percent"], 21.0)
        self.assertIn("streaming_started_at", st)

    def test_overall_milestones_keep_coming_after_batch_bars(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._runner(tmp)
            r._process_line("Receiving files")
            seen = []
            for pct in range(0, 101, 5):
                r._process_line("[=====] 100% (1.0 MB/1.0 MB)")
                r._process_line(f"[=====] {pct}% Finished")
                seen.append(r.last_reported_pct)
        self.assertEqual(sorted(set(seen)), [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        banners = [c.kwargs["message"] for c in r.notify.call_args_list if "done" in c.kwargs.get("message", "")]
        self.assertEqual(banners, ["Backup 25% done.", "Backup 50% done.", "Backup 75% done."])

    def test_status_fields_survive_phase_changes_and_file_is_valid_json(self):
        import backup_progress
        with tempfile.TemporaryDirectory() as tmp:
            r = self._runner(tmp)
            r.update_status("streaming", bytes_written=5)
            r.update_status("verifying")
            st = backup_progress.read_status(Path(tmp) / "s.json")
        self.assertEqual(st["bytes_written"], 5)
        self.assertEqual(st["phase"], "verifying")
        self.assertEqual(st["pid"], os.getpid())

    def test_eta_prefers_phone_percentage_then_bytes(self):
        import backup_progress
        e = backup_progress.ProgressEstimator(estimated_total_bytes=10 * 10**9)
        e.add(0, 0)
        e.add(100, 10**9)  # 10 MB/s
        self.assertAlmostEqual(e.rate_bps(), 10**7)
        self.assertAlmostEqual(e.eta_seconds(None), 900)    # 9 GB left at 10 MB/s
        self.assertAlmostEqual(e.eta_seconds(20.0), 400)    # 100 s for 20% -> 400 s more

    def test_describe_flags_dead_engine_and_hides_udid(self):
        import backup_progress
        st = {"phase": "streaming", "pid": 999999, "udid": "U1",
              "updated_at": datetime.now(timezone.utc).isoformat()}
        with mock.patch.object(backup_progress, "pid_alive", return_value=False):
            d = backup_progress.describe(st)
        self.assertTrue(d["engine_died"])
        self.assertIn("stopped unexpectedly", d["summary"])
        self.assertNotIn("udid", d)

    def test_describe_flags_stall_but_not_while_waiting_for_passcode(self):
        import backup_progress
        old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with mock.patch.object(backup_progress, "pid_alive", return_value=True):
            self.assertTrue(backup_progress.describe({"phase": "streaming", "pid": 1, "updated_at": old})["stalled"])
            self.assertFalse(backup_progress.describe({"phase": "waiting_passcode", "pid": 1,
                                                       "updated_at": old})["stalled"])

    def test_progress_line(self):
        import backup_progress
        line = backup_progress.progress_line({"bytes_written": 12.4e9, "estimated_total_bytes": 27e9,
                                              "overall_percent": 46, "rate_bps": 31e6, "eta_seconds": 420})
        self.assertEqual(line, "Backup in progress: 12.4 GB copied of ~27 GB, 46%, 31 MB/s, ~7 min left")

    def test_watch_event_stream(self):
        """Passcode prompt, "started", periodic check-ins, then done; nothing else."""
        import io

        import backup_progress
        now = datetime.now(timezone.utc).isoformat()
        timeline = [
            {"phase": "waiting_passcode"},
            {"phase": "streaming", "streaming_started_at": now},
            {"phase": "streaming", "bytes_written": 1e9},
            {"phase": "streaming", "bytes_written": 2e9},
            {"phase": "streaming", "bytes_written": 3e9},
            {"phase": "verifying"},
            {"phase": "completed", "duration_seconds": 1500},
        ]
        it = iter(timeline)
        t = [0.0]
        out = io.StringIO()

        def status(_path=None):
            st = dict(next(it, timeline[-1]))
            st.update(pid=1, updated_at=now)
            return st

        def sleep(sec):
            t[0] += 150  # each poll is 2.5 "minutes" apart

        with mock.patch.object(backup_progress, "read_status", side_effect=status), \
                mock.patch.object(backup_progress, "pid_alive", return_value=True):
            rc = backup_progress.watch(Path("x"), checkin_minutes=5, out=out, clock=lambda: t[0], sleep=sleep)
        lines = [ln.split("] ", 1)[1] for ln in out.getvalue().splitlines()]
        self.assertEqual(rc, 0)
        self.assertEqual(lines[0], backup_progress.PHASE_LINES["waiting_passcode"])
        self.assertEqual(lines[1], backup_progress.PHASE_LINES["streaming"])
        self.assertEqual(sum(1 for ln in lines if ln.startswith("Backup in progress")), 1)
        self.assertEqual(lines[-2], backup_progress.PHASE_LINES["verifying"])
        self.assertEqual(lines[-1], "Backup complete and verified (took 25 min).")

    def test_watch_joining_mid_backup_reports_progress(self):
        import io

        import backup_progress
        early = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        seq = iter([{"phase": "streaming", "streaming_started_at": early, "bytes_written": 5e9},
                    {"phase": "failed", "error": "cable pulled"}])
        out = io.StringIO()
        with mock.patch.object(backup_progress, "read_status",
                               side_effect=lambda _p=None: dict(next(seq), pid=1,
                                                                updated_at=datetime.now(timezone.utc).isoformat())), \
                mock.patch.object(backup_progress, "pid_alive", return_value=True):
            rc = backup_progress.watch(Path("x"), out=out, clock=lambda: 0.0, sleep=lambda s: None)
        lines = out.getvalue().splitlines()
        self.assertEqual(rc, 1)
        self.assertIn("5.0 GB copied", lines[0])
        self.assertIn("FAILED: cable pulled", lines[1])

    def test_watch_with_nothing_running(self):
        import io

        import backup_progress
        out = io.StringIO()
        t = [0.0]
        with mock.patch.object(backup_progress, "read_status", return_value=None):
            rc = backup_progress.watch(Path("x"), out=out, clock=lambda: t[0],
                                       sleep=lambda s: t.__setitem__(0, t[0] + s), wait_for_start_sec=10)
        self.assertEqual(rc, 2)
        self.assertIn("No backup", out.getvalue())


class TestVerifierUdidCase(unittest.TestCase):
    def test_uppercase_info_plist_udid_is_not_a_mismatch(self):
        """idevicebackup2 writes the UDID upper-case in Info.plist; the folder is lower-case."""
        import plistlib

        import backup_verifier
        with tempfile.TemporaryDirectory() as tmp:
            from tests.fixtures import create_synthetic_backup
            b = create_synthetic_backup(Path(tmp))
            b = b.rename(b.with_name(b.name.lower()))  # real shape: lower-case folder...
            info = plistlib.loads((b / "Info.plist").read_bytes())
            info["Unique Identifier"] = b.name.upper()  # ...upper-case UDID in Info.plist
            (b / "Info.plist").write_bytes(plistlib.dumps(info))
            with mock.patch.object(backup_verifier, "BACKUP_ROOT", Path(tmp)):
                import contextlib
                import io
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    backup_verifier.inspect_backup(b, max_age_hours=1e9, interactive=False)
        self.assertNotIn("does not match folder name", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
