"""
Finder UI restore (finder_restore.py): the accessibility-snapshot parser, the screen classifier,
and the whole state machine driven against a scripted fake Finder (no Mac UI, no phone).
Covers the happy path, Keychain-prefilled vs empty password sheets, and every handoff to the agent.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("IOS_CLEANER_STATE_DIR", tempfile.mkdtemp(prefix="ioscl_test_state_"))

import finder_restore as fr  # noqa: E402

UDID = "00008030-000AAAA1111BBBB2"
PLAN = {"udid": UDID, "backup_name": "Peach", "backup_made": "2026-11-09T10:00:00", "backup_age_hours": 1.0,
        "warnings": []}


def item(scope, cls, name="", value="", enabled=True):
    return {"scope": scope, "cls": cls, "name": name, "value": value, "enabled": enabled}


def screen(window="iPhone", sheet=False, items=()):
    return {"window": window, "has_sheet": sheet, "items": list(items)}


NO_WINDOW = screen(window=None)
WELCOME = screen(items=[item("W", "static text", value="Welcome to your new iPhone"),
                        item("W", "radio button", "Restore from this backup:", "0"),
                        item("W", "pop up button", "Peach", "Peach"),
                        item("W", "button", "Continue")])
PW_PREFILLED = screen(sheet=True, items=[item("S", "static text", value="Enter the password"),
                                         item("S", "text field", "", "••••"),
                                         item("S", "button", "Cancel"), item("S", "button", "Restore", enabled=True)])
PW_EMPTY = screen(sheet=True, items=[item("S", "text field"), item("S", "button", "Cancel"),
                                     item("S", "button", "Restore", enabled=False)])
PW_TYPED = screen(sheet=True, items=[item("S", "text field", "", "••••"), item("S", "button", "Restore", enabled=True)])
RESTORING = screen(items=[item("W", "static text", value="Restoring from backup…")])
SUMMARY = screen(items=[item("W", "button", "Back Up Now"), item("W", "button", "Restore Backup…")])
WRONG_PW = screen(sheet=True, items=[item("S", "static text", value="The password you entered is incorrect."),
                                     item("S", "button", "OK")])
MYSTERY = screen(items=[item("W", "static text", value="something new")])


class FakeDriver:
    """Plays a script of screens (one per snapshot; the last repeats) and records clicks."""

    def __init__(self, screens, click_results=None):
        self.screens = list(screens)
        self.clicks = []
        self.click_results = click_results or {}
        self.opened = 0
        self.open_result = {"success": True}

    def snapshot(self):
        return self.screens.pop(0) if len(self.screens) > 1 else self.screens[0]

    def click(self, scope, cls, name, exact=False, nth=1):
        self.clicks.append((scope, cls, name))
        return self.click_results.get((scope, cls, name), "CLICKED")

    def open_device(self):
        self.opened += 1
        return self.open_result


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make_run(screens, devices=None, name="Peach", click_results=None, **kw):
    clock = Clock()
    drv = FakeDriver(screens, click_results)
    notes, said = [], []
    run = fr.Run(UDID, dict(PLAN), drv, sleep=clock.sleep, now=clock.now, notify=notes.append, say=said.append,
                 devices=devices or (lambda: [UDID]), phone_name=lambda: name, finish_timeout=60,
                 **{"open_url": mock.Mock(return_value=True), **kw})
    return run, drv, notes, said


class ParseAndClassify(unittest.TestCase):
    def test_parse_snapshot(self):
        text = "\n".join(["WINDOW|||iPhone", "W|||button|||Continue||||||true", "SHEET|||",
                          "S|||button|||Restore||||||false"])
        snap = fr.parse_snapshot(text)
        self.assertEqual(snap["window"], "iPhone")
        self.assertTrue(snap["has_sheet"])
        self.assertFalse(snap["items"][1]["enabled"])

    def test_parse_empty_is_no_window(self):
        self.assertEqual(fr.classify(fr.parse_snapshot("")), "no_window")

    def test_classify_every_screen(self):
        for snap, want in [(NO_WINDOW, "no_window"), (WELCOME, "welcome"), (PW_PREFILLED, "password_sheet"),
                           (WRONG_PW, "other_sheet"), (RESTORING, "restoring"), (SUMMARY, "summary"),
                           (MYSTERY, "unknown"),
                           (screen(items=[item("W", "static text", value="Activating iPhone…")]), "activating")]:
            self.assertEqual(fr.classify(snap), want)

    def test_summary_restore_backup_button_is_not_a_restoring_screen(self):
        self.assertEqual(fr.classify(SUMMARY), "summary")

    def test_describe_never_includes_field_values(self):
        d = json.dumps(fr._describe(PW_PREFILLED))
        self.assertNotIn("••••", d)


class StateMachine(unittest.TestCase):
    def test_happy_path_keychain_prefilled(self):
        run, drv, notes, _ = make_run([NO_WINDOW, WELCOME, PW_PREFILLED, PW_PREFILLED, PW_PREFILLED, RESTORING, NO_WINDOW],
                                      devices=mock.Mock(side_effect=[[], [], [UDID]]))
        code, res = run.drive(600)
        self.assertEqual(code, 0, res)
        self.assertTrue(res["restored"])
        self.assertEqual(drv.opened, 1)
        self.assertIn(("W", "radio button", "Restore from this backup"), drv.clicks)
        self.assertIn(("W", "button", "Continue"), drv.clicks)
        self.assertIn(("S", "button", "Restore"), drv.clicks)
        self.assertEqual(notes, [])  # prefilled: nobody needs to be bothered

    def test_window_closing_mid_restore_waits_instead_of_reopening(self):
        run, drv, _, _ = make_run([RESTORING, NO_WINDOW], devices=lambda: [UDID])  # phone still on USB
        code, res = run.drive(600)
        self.assertEqual(code, 0, res)
        self.assertEqual(drv.opened, 0)

    def test_empty_password_sheet_is_left_to_the_person(self):
        run, drv, notes, _ = make_run([WELCOME, PW_EMPTY, PW_EMPTY, PW_TYPED, PW_TYPED, RESTORING, SUMMARY], )
        code, res = run.drive(600)
        self.assertEqual(code, 0, res)
        self.assertEqual(len(notes), 1)
        self.assertNotIn(("S", "button", "Restore"), drv.clicks)  # never clicks a half-typed password

    def test_never_clicks_restore_before_the_sheet_settles(self):
        run, drv, _, _ = make_run([PW_PREFILLED], poll=0.1)
        run.drive(1.0)  # shorter than the 1.5s settle time
        self.assertNotIn(("S", "button", "Restore"), drv.clicks)

    def test_summary_without_welcome_is_refused_and_untouched(self):
        run, drv, _, _ = make_run([SUMMARY])
        code, res = run.drive(60)
        self.assertEqual(code, 2)
        self.assertEqual(res["code"], "phone_not_erased")
        self.assertEqual(drv.clicks, [])

    def test_backup_mismatch_stops_the_run(self):
        other = screen(items=[item("W", "radio button", "Restore from this backup:", "1"),
                              item("W", "pop up button", "Dad's iPhone", "Dad's iPhone"), item("W", "button", "Continue")])
        run, drv, _, _ = make_run([other], click_results={("W", "pop up button", ""): "NOT_FOUND"})
        code, res = run.drive(60)
        self.assertEqual(code, 4)
        self.assertEqual(res["step"], "choose_backup")
        self.assertNotIn(("W", "button", "Continue"), drv.clicks)

    def test_unverifiable_backup_time_stops_before_continue(self):
        # the plan knows the capture time, Finder's line shows another one: never press Continue
        run, drv, _, _ = make_run([WELCOME, WELCOME, WELCOME])
        run.plan["last_backup_date"] = "2026-11-09T23:47:00"
        drv.click_results[("W", "pop up button", "")] = "NOT_FOUND"
        code, res = run.drive(60)
        self.assertEqual((code, res["step"]), (4, "choose_backup"))
        self.assertNotIn(("W", "button", "Continue"), drv.clicks)

    def test_stale_backup_list_restarts_finders_device_service_once_before_handing_off(self):
        run, drv, _, said = make_run([WELCOME, WELCOME, WELCOME, WELCOME, WELCOME, WELCOME])
        run.plan["last_backup_date"] = "2026-11-09T23:47:00"
        drv.click_results[("W", "pop up button", "")] = "NOT_FOUND"
        drv.healed = 0
        drv.heal = lambda: setattr(drv, "healed", drv.healed + 1)
        code, res = run.drive(60)
        self.assertEqual((code, res["step"]), (4, "choose_backup"))   # still can't prove it: hands off
        self.assertEqual(drv.healed, 1)                                 # ...but only after ONE heal
        self.assertNotIn(("W", "button", "Continue"), drv.clicks)       # and never pressed Continue unverified
        self.assertTrue(any("out of date" in m for m in said))

    def test_wrong_password_is_dismissed_and_watched_and_restore_continues(self):
        run, drv, notes, _ = make_run([PW_EMPTY, PW_TYPED, WRONG_PW, PW_EMPTY, PW_TYPED, RESTORING, SUMMARY],
                                      launch_helper=mock.Mock(return_value=None))
        code, res = run.drive(600)
        self.assertEqual(code, 0, res)
        self.assertEqual(drv.clicks.count(("S", "button", "OK")), 1)
        self.assertNotIn(("S", "button", "Restore"), drv.clicks)  # the person drives the box
        run.launch_helper.assert_not_called()  # one slip is not yet worth a helper

    def test_rejected_keychain_password_is_not_retried_and_offers_the_helper_at_once(self):
        retry = screen(sheet=True, items=[item("S", "static text", value="The password you entered to unlock your iPhone backup was incorrect. Please try again."),
                                          item("S", "text field", "", ""), item("S", "button", "Cancel"), item("S", "button", "OK")])
        proc = mock.Mock()
        proc.poll.return_value = None
        launcher = mock.Mock(return_value=("http://127.0.0.1:8765/?token=t", proc))
        run, drv, notes, said = make_run([PW_PREFILLED, PW_PREFILLED, PW_PREFILLED, retry, retry, retry, PW_TYPED, RESTORING, SUMMARY],
                                         launch_helper=launcher)
        code, res = run.drive(900)
        self.assertEqual(code, 0, res)
        self.assertEqual(drv.clicks.count(("S", "button", "Restore")), 1)   # the Keychain attempt
        self.assertNotIn(("S", "button", "OK"), drv.clicks)                  # never pressed OK on the retry sheet
        launcher.assert_called_once()
        run.open_url.assert_called_once_with("http://127.0.0.1:8765/?token=t")   # opened for the person
        self.assertEqual(sum(m.startswith("OFFER_PASSWORD_HELPER") for m in said), 1)
        self.assertIn("opened it in the system browser", [m for m in said if m.startswith("OFFER")][0])

    def test_repeated_wrong_passwords_launch_and_offer_the_helper_once(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        launcher = mock.Mock(return_value=("http://127.0.0.1:8765/?token=t", proc))
        run, _, notes, said = make_run([PW_EMPTY, WRONG_PW, PW_EMPTY, WRONG_PW, PW_EMPTY, WRONG_PW, PW_EMPTY,
                                        PW_TYPED, RESTORING, SUMMARY], launch_helper=launcher)
        code, res = run.drive(900)
        self.assertEqual(code, 0, res)
        launcher.assert_called_once()
        offers = [m for m in said if m.startswith("OFFER_PASSWORD_HELPER")]
        self.assertEqual(len(offers), 1)
        run.open_url.assert_called_once_with("http://127.0.0.1:8765/?token=t")
        self.assertIn("opened it in the system browser", offers[0])
        proc.terminate.assert_called()  # cleaned up when the run ends

    def test_helper_that_proves_the_password_is_reported(self):
        proc = mock.Mock()
        proc.poll.side_effect = [None, None, 0, 0, 0, 0, 0, 0]
        proc.returncode = 0
        run, _, _, said = make_run([WRONG_PW, WRONG_PW, PW_EMPTY, PW_EMPTY, PW_TYPED, RESTORING, SUMMARY],
                                   launch_helper=mock.Mock(return_value=("http://x", proc)))
        run.drive(900)
        self.assertTrue(any(m.startswith("PASSWORD_FOUND") for m in said))

    def test_undismissable_wrong_password_alert_hands_off(self):
        run, _, _, _ = make_run([WRONG_PW], click_results={("S", "button", "OK"): "NOT_FOUND"})
        code, res = run.drive(60)
        self.assertEqual((code, res["step"]), (4, "wrong_password_alert"))

    def test_unrecognised_sheet_hands_off(self):
        odd = screen(sheet=True, items=[item("S", "static text", value="Something else"), item("S", "button", "OK")])
        run, drv, _, _ = make_run([odd])
        code, res = run.drive(60)
        self.assertEqual((code, res["step"]), (4, "unexpected_sheet"))
        self.assertEqual(drv.clicks, [])

    def test_stuck_continue_hands_off_to_agent(self):
        run, _, _, _ = make_run([WELCOME])
        code, res = run.drive(600)
        self.assertEqual(code, 4)
        self.assertEqual(res["step"], "continue")
        self.assertIn("computer-use", " ".join(res["agent_instructions"]))
        self.assertIn("--resume", res["resume"])

    def test_missing_continue_button_hands_off(self):
        run, _, _, _ = make_run([WELCOME], click_results={("W", "button", "Continue"): "NOT_FOUND"})
        code, res = run.drive(60)
        self.assertEqual((code, res["step"]), (4, "continue"))

    def test_unrecognised_screen_hands_off_after_a_few_looks(self):
        run, _, _, _ = make_run([MYSTERY])
        code, res = run.drive(600)
        self.assertEqual((code, res["step"]), (4, "unrecognised_screen"))

    def test_unopenable_device_hands_off(self):
        run, drv, _, _ = make_run([NO_WINDOW])
        drv.open_result = {"success": False, "reason": "not listed",
                           "handoff": {"action": "click the iPhone row", "position_points": [10, 20]}}
        code, res = run.drive(60)
        self.assertEqual((code, res["step"]), (4, "open_device"))
        self.assertIn("click the iPhone row", " ".join(res["agent_instructions"]))

    def test_finder_scripting_failure_hands_off(self):
        run, drv, _, _ = make_run([WELCOME])
        drv.snapshot = mock.Mock(side_effect=fr.FinderUIError("macOS blocked scripting Finder"))
        code, res = run.drive(60)
        self.assertEqual((code, res["step"]), (4, "finder_unreachable"))

    def test_phone_back_with_a_different_name_is_unconfirmed(self):
        run, _, _, _ = make_run([RESTORING, SUMMARY], name="iPhone")
        code, res = run.drive(600)
        self.assertEqual((code, res["code"]), (3, "unconfirmed"))

    def test_name_back_early_is_not_done_until_the_phone_says_restored(self):
        states = iter(["", "", "RestoredFromiTunesBackup"])
        run, _, _, _ = make_run([RESTORING, NO_WINDOW], setup_state=lambda: next(states))
        code, res = run.drive(600)
        self.assertEqual((code, res["restored"]), (0, True))

    def test_transfer_process_still_running_blocks_completion(self):
        active = iter([True, True, False])
        run, _, _, _ = make_run([RESTORING, NO_WINDOW], restore_active=lambda: next(active))
        code, _ = run.drive(600)
        self.assertEqual(code, 0)

    def test_never_restored_state_is_unconfirmed(self):
        run, _, _, _ = make_run([RESTORING, NO_WINDOW], setup_state=lambda: "")
        run.finish_timeout = 30
        code, res = run.drive(600)
        self.assertEqual((code, res["code"]), (3, "unconfirmed"))

    def test_welcome_again_after_restore_started_hands_off(self):
        run, _, _, _ = make_run([RESTORING, WELCOME])
        code, res = run.drive(600)
        self.assertEqual((code, res["step"]), (4, "back_at_welcome"))

    def test_timeout_is_resumable(self):
        run, _, _, _ = make_run([RESTORING])
        run.finish_timeout = 0
        code, res = run.drive(10)
        self.assertEqual(code, 1)
        self.assertIn("--resume", res["resume"])


class EntryPoint(unittest.TestCase):
    def setUp(self):
        # never let a test relabel the real backups on this Mac
        import backup_labels
        p = mock.patch.object(backup_labels, "ensure_labels", return_value={"ok": True, "changed": 0, "message": ""})
        self.labels = p.start()
        self.addCleanup(p.stop)

    def test_dry_run_only_previews_the_labels(self):
        ok = {"ok": True, "plan": dict(PLAN)}
        with mock.patch.object(fr.gates, "check_preconditions", return_value=ok):
            fr.restore(None, "auto", 48, confirm=False, hello_confirmed=False, resume=False, timeout_sec=60, driver=FakeDriver([WELCOME]))
        self.assertFalse(self.labels.call_args.kwargs["apply_changes"])

    def test_real_run_labels_before_the_gates_so_the_plan_has_the_new_label(self):
        order = []
        self.labels.side_effect = lambda *a, **k: order.append(("labels", k["apply_changes"])) or {"ok": True}
        refusal = fr.gates.refusal("phone_not_erased", "no")
        with mock.patch.object(fr.gates, "check_preconditions", side_effect=lambda *a, **k: order.append("gates") or refusal):
            fr.restore(None, "auto", 48, True, True, False, 60, driver=FakeDriver([WELCOME]))
        self.assertEqual(order, [("labels", True), "gates"])

    def test_successful_restore_labels_finders_new_copy_afterwards(self):
        ok = {"ok": True, "plan": dict(PLAN)}
        with mock.patch.object(fr.gates, "check_preconditions", return_value=ok), \
             mock.patch.object(fr, "_alert"), mock.patch.object(fr, "_write_status"), \
             mock.patch.object(fr.Run, "drive", return_value=(0, {"ok": True, "restored": True})):
            code, _ = fr.restore(None, "auto", 48, True, True, False, 60, driver=FakeDriver([WELCOME]))
        self.assertEqual(code, 0)
        self.assertEqual(self.labels.call_count, 2)
        self.assertTrue(self.labels.call_args.kwargs.get("apply_changes", True))

    def test_failed_restore_does_not_relabel(self):
        ok = {"ok": True, "plan": dict(PLAN)}
        with mock.patch.object(fr.gates, "check_preconditions", return_value=ok), \
             mock.patch.object(fr, "_alert"), mock.patch.object(fr, "_write_status"), \
             mock.patch.object(fr.Run, "drive", return_value=(1, {"ok": False})):
            fr.restore(None, "auto", 48, True, True, False, 60, driver=FakeDriver([WELCOME]))
        self.assertEqual(self.labels.call_count, 1)  # only the pre-restore call
    def test_dry_run_touches_nothing(self):
        ok = {"ok": True, "plan": {**PLAN, "backup_name": "Peach"}}
        drv = FakeDriver([WELCOME])
        with mock.patch.object(fr.gates, "check_preconditions", return_value=ok):
            code, res = fr.restore(None, "auto", 48, confirm=False, hello_confirmed=False, resume=False,
                                   timeout_sec=60, driver=drv)
        self.assertEqual(code, 0)
        self.assertTrue(res["dry_run"])
        self.assertEqual(drv.clicks, [])

    def test_confirm_without_hello_is_refused(self):
        ok = {"ok": True, "plan": dict(PLAN)}
        drv = FakeDriver([WELCOME])
        with mock.patch.object(fr.gates, "check_preconditions", return_value=ok):
            code, res = fr.restore(None, "auto", 48, confirm=True, hello_confirmed=False, resume=False,
                                   timeout_sec=60, driver=drv)
        self.assertEqual((code, res["code"]), (2, "needs_hello_confirmation"))
        self.assertEqual(drv.clicks, [])

    def test_gate_refusal_stops_before_finder_is_touched(self):
        refusal = fr.gates.refusal("phone_not_erased", "no")
        drv = FakeDriver([WELCOME])
        with mock.patch.object(fr.gates, "check_preconditions", return_value=refusal):
            code, _ = fr.restore(None, "auto", 48, True, True, False, 60, driver=drv)
        self.assertEqual(code, 2)
        self.assertEqual(drv.snapshot.__self__.clicks, [])

    def test_resume_midflow_skips_the_erased_gate_but_rechecks_the_phone(self):
        status = {"plan": dict(PLAN), "phase": "handoff", "gates_passed_at": datetime.now(timezone.utc).isoformat()}
        drv = FakeDriver([PW_PREFILLED])
        with mock.patch.object(fr, "_read_json", return_value=status), \
             mock.patch.object(fr, "connected_udids", return_value=["SOMEONE-ELSE"]):
            code, res = fr.restore(None, "auto", 48, True, True, True, 60, driver=drv)
        self.assertEqual((code, res["code"]), (2, "device_changed"))

    def test_resume_without_confirmation_is_refused(self):
        status = {"plan": dict(PLAN), "phase": "handoff", "gates_passed_at": datetime.now(timezone.utc).isoformat()}
        with mock.patch.object(fr, "_read_json", return_value=status), \
             mock.patch.object(fr, "connected_udids", return_value=[UDID]):
            code, res = fr.restore(None, "auto", 48, False, False, True, 60, driver=FakeDriver([WELCOME]))
        self.assertEqual((code, res["code"]), (2, "needs_confirmation"))


class ScriptGeneration(unittest.TestCase):
    def test_click_script_matches_by_role_and_name_not_coordinates(self):
        s = fr.click_script('(wName starts with "iPhone")', "S", "button", "Restore", True)
        self.assertIn('sheet 1 of w', s)
        self.assertIn('n is "Restore"', s)
        self.assertNotIn("position", s)

    def test_snapshot_script_names_the_device_window(self):
        s = fr.snapshot_script('(wName starts with "Peach")')
        self.assertIn('starts with "Peach"', s)


if __name__ == "__main__":
    unittest.main()
