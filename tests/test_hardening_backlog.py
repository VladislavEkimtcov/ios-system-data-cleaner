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


if __name__ == "__main__":
    unittest.main()
