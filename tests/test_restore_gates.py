"""
Restore safety gates (restore_gates.py): every gate, backup selection, the setup-state verdict,
Finder activation, and the password helper page.
"""

import json
import os
import plistlib
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("IOS_CLEANER_STATE_DIR", tempfile.mkdtemp(prefix="ioscl_test_state_"))

import restore_gates as cr  # noqa: E402

UDID = "00008030-000AAAA1111BBBB2"


def naive_utc(**delta) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(**delta)


def make_backup(path: Path, udid=UDID, encrypted=True, age_hours=1.0, version="16.7.16",
                state="finished", name="Peach ", model="iPhone10,4") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "Info.plist").write_bytes(plistlib.dumps({
        "Unique Identifier": udid, "Device Name": name, "Product Version": version, "Product Type": model}))
    (path / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": encrypted}))
    (path / "Status.plist").write_bytes(plistlib.dumps({"SnapshotState": state, "Date": naive_utc(hours=age_hours)}))
    (path / "Manifest.db").write_bytes(b"x" * 64)
    return path


HELLO = {"verdict": "hello", "activation_state": "Activated", "reasons": ["the phone has its factory name"], "device_name": "iPhone",
         "product_version": "16.7.16", "product_type": "iPhone10,4", "setup_state": ""}


class GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        make_backup(self.root / UDID)
        make_backup(self.root / f"{UDID}-prewipe")
        self.patches = [
            mock.patch.object(cr, "find_tool", return_value=Path("/bin/true")),
            mock.patch.object(cr, "connected_udids", return_value=[UDID]),
            mock.patch.object(cr, "device_setup_verdict", return_value=dict(HELLO)),
            mock.patch.object(cr, "backup_root", return_value=self.root),
            mock.patch.object(cr, "_battery", return_value=80),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def code(self, **kw):
        return cr.check_preconditions(**kw)

    def test_happy_path(self):
        res = self.code()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["plan"]["udid"], UDID)
        self.assertEqual(res["plan"]["backup_kind"], "clone")  # tie goes to the immutable clone

    def test_refuses_phone_that_is_set_up(self):
        with mock.patch.object(cr, "device_setup_verdict", return_value={**HELLO, "verdict": "set_up", "reasons": ["x"]}):
            res = self.code()
        self.assertEqual(res["code"], "phone_not_erased")
        self.assertTrue(any(o["id"] == "erase_first" for o in res["options"]))

    def test_refuses_unclear_phone_state(self):
        with mock.patch.object(cr, "device_setup_verdict", return_value={**HELLO, "verdict": "unknown", "reasons": ["the phone did not answer"]}):
            self.assertEqual(self.code()["code"], "phone_state_unclear")

    def test_refuses_unactivated_phone_with_alternatives(self):
        with mock.patch.object(cr, "device_setup_verdict", return_value={**HELLO, "activation_state": "Unactivated"}):
            res = self.code()
        self.assertEqual(res["code"], "phone_not_activated")
        ids = {o["id"] for o in res["options"]}
        self.assertTrue({"activate_via_finder", "activate_on_phone", "stop"} <= ids)

    def test_refuses_two_phones(self):
        with mock.patch.object(cr, "connected_udids", return_value=[UDID, "OTHER"]):
            self.assertEqual(self.code()["code"], "multiple_devices")

    def test_refuses_wrong_phone(self):
        self.assertEqual(self.code(udid="SOMEONE-ELSE")["code"], "wrong_device")

    def test_no_device_explains_why(self):
        with mock.patch.object(cr, "connected_udids", return_value=[]), \
             mock.patch.object(cr, "diagnose_connection", return_value={"message": "Use a data cable."}):
            res = self.code()
        self.assertEqual(res["code"], "no_device")
        self.assertIn("data cable", res["message"])

    def test_tools_missing_offers_alternatives(self):
        with mock.patch.object(cr, "find_tool", return_value=None):
            res = self.code()
        self.assertEqual(res["code"], "tools_missing")
        self.assertGreaterEqual(len(res["options"]), 2)

    def test_refuses_unencrypted_backup(self):
        make_backup(self.root / UDID, encrypted=False)
        make_backup(self.root / f"{UDID}-prewipe", encrypted=False)
        self.assertEqual(self.code()["code"], "backup_unusable")

    def test_refuses_other_phones_backup(self):
        make_backup(self.root / UDID, udid="OTHER")
        make_backup(self.root / f"{UDID}-prewipe", udid="OTHER")
        self.assertEqual(self.code()["code"], "backup_unusable")

    def test_refuses_unfinished_backup(self):
        make_backup(self.root / UDID, state="uploading")
        make_backup(self.root / f"{UDID}-prewipe", state="uploading")
        self.assertEqual(self.code()["code"], "backup_unusable")

    def test_refuses_old_backup_unless_allowed(self):
        make_backup(self.root / UDID, age_hours=100)
        make_backup(self.root / f"{UDID}-prewipe", age_hours=100)
        self.assertEqual(self.code()["code"], "backup_old")
        self.assertTrue(self.code(max_age_hours=200)["ok"])

    def test_refuses_phone_on_older_ios(self):
        make_backup(self.root / UDID, version="17.2")
        make_backup(self.root / f"{UDID}-prewipe", version="17.2")
        self.assertEqual(self.code()["code"], "phone_ios_older")

    def test_refuses_low_battery(self):
        with mock.patch.object(cr, "_battery", return_value=5):
            self.assertEqual(self.code()["code"], "battery_low")

    def test_no_backup_at_all(self):
        import shutil
        shutil.rmtree(self.root / UDID)
        shutil.rmtree(self.root / f"{UDID}-prewipe")
        self.assertEqual(self.code()["code"], "no_backup")

    def test_prefers_newer_live_over_older_clone(self):
        make_backup(self.root / f"{UDID}-prewipe", age_hours=5)
        make_backup(self.root / UDID, age_hours=1)
        self.assertEqual(self.code()["plan"]["backup_kind"], "live")

    def test_every_refusal_has_a_choice_and_plain_message(self):
        with mock.patch.object(cr, "device_setup_verdict", return_value={**HELLO, "verdict": "set_up", "reasons": ["x"]}):
            res = self.code()
        self.assertGreaterEqual(len(res["options"]), 2)
        for jargon in ("UDID", "lockdown", "idevicebackup2", "APFS"):
            self.assertNotIn(jargon, res["message"])

    def test_unactivated_phone_is_allowed_when_the_ui_restore_activates_it(self):
        with mock.patch.object(cr, "device_setup_verdict", return_value={**HELLO, "activation_state": "Unactivated"}):
            self.assertTrue(cr.check_preconditions(allow_unactivated=True)["ok"])


class SetupVerdictTests(unittest.TestCase):
    def verdict(self, base, purple):
        import common
        def fake(udid, *extra):
            return purple if extra else base
        with mock.patch.object(common, "_ideviceinfo", side_effect=fake):
            return common.device_setup_verdict("U")["verdict"]

    def test_set_up_phone_with_old_erase_record_is_not_hello(self):
        self.assertEqual(self.verdict({"DeviceName": "Peach", "obliteration": "x", "ActivationState": "Activated"},
                                      {"SetupState": "RestoredFromiTunesBackup"}), "set_up")

    def test_fresh_erase_is_hello(self):
        self.assertEqual(self.verdict({"DeviceName": "iPhone", "obliteration": "x", "ActivationState": "Activated"}, {}), "hello")

    def test_erase_record_alone_is_not_hello(self):
        # a restored phone: own name, activated, empty SetupState, old erase record
        self.assertEqual(self.verdict({"DeviceName": "Peach", "obliteration": "x", "ActivationState": "Activated"}, {}), "unknown")

    def test_unactivated_phone_is_hello(self):
        self.assertEqual(self.verdict({"DeviceName": "Peach", "ActivationState": "Unactivated"}, {}), "hello")

    def test_no_evidence_is_unknown_not_hello(self):
        self.assertEqual(self.verdict({"DeviceName": "Peach", "ActivationState": "Activated"}, {}), "unknown")

    def test_silent_phone_is_unknown(self):
        self.assertEqual(self.verdict({}, {}), "unknown")



class ITunesWipeDetectionNotFooledByOldEraseRecord(unittest.TestCase):
    def test_restored_phone_with_old_erase_record_is_not_wiped(self):
        from itunes_automation import ITunesAutomation
        info = {"UniqueDeviceID": "U", "DeviceName": "iPhone", "ActivationState": "Activated",
                "obliteration": "RUFDUyBEb25lIEAxNzk0MTUzOTQz"}  # "EACS Done @1794153943"

        def fake(*args):
            return {"SetupState": "RestoredFromiTunesBackup"} if args else info
        driver = ITunesAutomation()
        with mock.patch("storage_monitor.run_ideviceinfo", side_effect=fake), \
             mock.patch.object(driver, "is_itunes_running", return_value=False):
            self.assertFalse(driver.detect_device_wipe_state("U")["is_wiped"])


class PasswordHelperPage(unittest.TestCase):
    def _run_page(self, post_password, expect_ok):
        import json as _json, threading, urllib.request, time as _t
        import password_tracker as pt
        opened = []
        result = {}
        with mock.patch.object(pt.webbrowser, "open", side_effect=opened.append), \
             mock.patch.object(pt, "test_backup_password", side_effect=lambda u, p, **k: (p == "right", 0.01)), \
             mock.patch.object(pt, "store_in_keychain", return_value=(True, "ok")), \
             mock.patch.object(pt, "save_attempt"), mock.patch.object(pt, "keybag_fingerprint", return_value="fp"), \
             mock.patch.object(pt, "has_been_tried", return_value=None):
            t = threading.Thread(target=lambda: result.update(ok=pt.launch_web_ui(
                "U", port=18765, exit_on_success=True, timeout_minutes=0.05)), daemon=True)
            t.start()
            for _ in range(50):
                if opened:
                    break
                _t.sleep(0.05)
            url = opened[0]
            base, token = url.split("/?token=")
            req = urllib.request.Request(f"{base}/api/check?token={token}",
                                         data=_json.dumps({"password": post_password}).encode(),
                                         headers={"Content-Type": "application/json", "X-Session-Token": token,
                                                  "Host": base.split("//")[1]}, method="POST")
            try:
                urllib.request.urlopen(req, timeout=5).read()
            except Exception:
                pass
            t.join(timeout=10)
        self.assertEqual(result.get("ok"), expect_ok)

    def test_exits_by_itself_after_a_correct_password(self):
        self._run_page("right", True)

    def test_times_out_when_nothing_proven(self):
        self._run_page("wrong", False)


class ActivateViaFinder(unittest.TestCase):
    def setUp(self):
        import restorer
        self.r = restorer

    def test_already_activated_just_closes_the_window(self):
        with mock.patch.object(self.r, "query_device_info", return_value={"ActivationState": "Activated"}), \
             mock.patch.object(self.r, "select_device_in_finder") as sel, \
             mock.patch.object(self.r, "close_device_windows", return_value=1) as close:
            res = self.r.activate_via_finder("U")
        self.assertTrue(res["success"])
        sel.assert_not_called()
        close.assert_called_once()

    def test_waits_for_activation_then_closes_window(self):
        with mock.patch.object(self.r, "query_device_info", return_value={"ActivationState": "Unactivated"}), \
             mock.patch.object(self.r, "select_device_in_finder", return_value={"success": True}), \
             mock.patch.object(self.r, "close_device_windows", return_value=1) as close, \
             mock.patch.object(self.r.time, "sleep"), mock.patch.object(self.r, "wait_until", return_value=True):
            res = self.r.activate_via_finder("U", verbose=False)
        self.assertTrue(res["success"])
        close.assert_called_once()

    def test_timeout_does_not_close_the_window(self):
        with mock.patch.object(self.r, "query_device_info", return_value={"ActivationState": "Unactivated"}), \
             mock.patch.object(self.r, "select_device_in_finder", return_value={"success": True}), \
             mock.patch.object(self.r, "close_device_windows") as close, \
             mock.patch.object(self.r, "wait_until", return_value=False):
            res = self.r.activate_via_finder("U", verbose=False)
        self.assertFalse(res["success"])
        close.assert_not_called()

    def test_unopenable_phone_returns_handoff_and_leaves_windows_alone(self):
        handoff = {"reason": "agent_handoff", "action": "click the 'iPhone' row"}
        with mock.patch.object(self.r, "query_device_info", return_value={"ActivationState": "Unactivated"}), \
             mock.patch.object(self.r, "select_device_in_finder", return_value={"success": False, "reason": "x", "handoff": handoff}), \
             mock.patch.object(self.r, "close_device_windows") as close:
            res = self.r.activate_via_finder("U")
        self.assertFalse(res["success"])
        self.assertEqual(res["handoff"], handoff)
        close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
