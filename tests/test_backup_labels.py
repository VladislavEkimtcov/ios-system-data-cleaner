"""
Backup labels (backup_labels.py): descriptive Finder labels for a phone's backups, kept reversible, touching
nothing but the Device Name, and never another phone's backups. Plus the picker's exact-label mode.
"""

import os
import plistlib
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

import backup_labels as bl  # noqa: E402
import backup_picker as bp  # noqa: E402
import common  # noqa: E402

UDID = "00008101-000AAAA1111BBBB3"
OTHER = "00008030-000CCCC2222DDDD4"
MADE = datetime(2026, 11, 9, 23, 47, tzinfo=timezone.utc)


def write_info(folder: Path, udid=UDID, name="Яблоко ", extra=None):
    folder.mkdir(parents=True)
    d = {"Unique Identifier": udid, "Device Name": name, "Display Name": name, "Last Backup Date": MADE.replace(tzinfo=None),
         "Product Version": "16.7.16", "Applications": {"com.x": {"PlaceholderIcon": b"\x89PNG\x00\x01"}}}
    d.update(extra or {})
    (folder / "Info.plist").write_bytes(plistlib.dumps(d))   # XML, like the real ones
    (folder / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": True}))   # a finished backup has one
    return folder / "Info.plist"


class Labeler(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.main = write_info(self.root / UDID)
        self.safety = write_info(self.root / f"{UDID}-prewipe")
        self.extra = write_info(self.root / f"{UDID}-20261109-203901")
        self.other = write_info(self.root / f"{OTHER}-prewipe", udid=OTHER, name="Анна’s iPhone")
        for p in (mock.patch.object(bl, "backup_root", return_value=self.root), mock.patch.object(bl, "_busy", return_value=False)):
            p.start()
            self.addCleanup(p.stop)

    def names(self):
        return {p.parent.name: plistlib.loads(p.read_bytes())["Display Name"] for p in (self.main, self.safety, self.extra, self.other)}

    def test_preview_changes_nothing(self):
        before = {p: p.read_bytes() for p in (self.main, self.safety, self.extra, self.other)}
        rows = bl.apply(UDID, dry_run=True)
        self.assertEqual(len(rows), 3)
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_apply_gives_each_copy_a_distinct_descriptive_label(self):
        bl.apply(UDID, dry_run=False)
        n = self.names()
        self.assertTrue(n[UDID].startswith("Яблоко · main · Nov 9, "))
        self.assertTrue(n[f"{UDID}-prewipe"].startswith("Яблоко · safety copy · Nov 9, "))
        self.assertTrue(n[f"{UDID}-20261109-203901"].startswith("Яблоко · extra copy · Nov 9, "))   # no Finder date: say when it was made
        self.assertNotEqual(n[UDID], n[f"{UDID}-prewipe"])

    def test_only_the_label_changes_and_the_real_name_is_kept(self):
        before = plistlib.loads(self.main.read_bytes())
        bl.apply(UDID, dry_run=False)
        after = plistlib.loads(self.main.read_bytes())
        changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        self.assertEqual(changed, {"Display Name", "Original Display Name"})   # the phone's real Device Name is untouched
        self.assertEqual(after["Original Display Name"], "Яблоко ")
        self.assertEqual(after["Device Name"], "Яблоко ")
        self.assertEqual(common.backup_device_name(after), "Яблоко ")

    def test_other_phones_backups_are_never_touched(self):
        before = self.other.read_bytes()
        bl.apply(UDID, dry_run=False)
        self.assertEqual(self.other.read_bytes(), before)

    def test_idempotent(self):
        bl.apply(UDID, dry_run=False)
        once = {p: p.read_bytes() for p in (self.main, self.safety, self.extra)}
        rows = bl.apply(UDID, dry_run=False)
        self.assertFalse(any(r["changed"] for r in rows))
        self.assertEqual({p: p.read_bytes() for p in once}, once)

    def test_revert_restores_the_original_plists(self):
        before = {p: plistlib.loads(p.read_bytes()) for p in (self.main, self.safety, self.extra)}
        bl.apply(UDID, dry_run=False)
        bl.apply(UDID, revert=True, dry_run=False)
        self.assertEqual({p: plistlib.loads(p.read_bytes()) for p in before}, before)

    def test_refuses_while_a_restore_is_running(self):
        with mock.patch.object(bl, "_busy", return_value=True):
            with self.assertRaises(RuntimeError):
                bl.apply(UDID, dry_run=False)
        self.assertEqual(self.names()[UDID], "Яблоко ")

    def test_names_with_markup_characters_are_escaped(self):
        write_info(self.root / f"{UDID}-20261109-210000", name="Tom & Jerry <phone>")
        bl.apply(UDID, dry_run=False)
        d = plistlib.loads((self.root / f"{UDID}-20261109-210000" / "Info.plist").read_bytes())
        self.assertEqual(d["Original Display Name"], "Tom & Jerry <phone>")

    def test_unfinished_and_failed_copies_are_labelled_dont_use(self):
        # 2026-11-11: a run that stopped at the passcode left Info.plist only; iTunes listed it as "extra copy"
        stopped = write_info(self.root / f"{UDID}-20261111-104559")
        (stopped.parent / "Manifest.plist").unlink()
        failed = write_info(self.root / f"{UDID}-failed-20261111-104559")
        bl.apply(UDID, dry_run=False)
        for p in (stopped, failed):
            self.assertIn("unfinished, don't use", plistlib.loads(p.read_bytes())["Display Name"])
        self.assertTrue(self.names()[UDID].startswith("Яблоко · main · "))

    def test_binary_plists_from_finder_are_relabelled_and_stay_binary(self):
        folder = self.root / f"{UDID}-20261109-220000"
        folder.mkdir()
        (folder / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": True}))
        (folder / "Info.plist").write_bytes(plistlib.dumps(
            {"Unique Identifier": UDID, "Device Name": "Яблоко ", "Product Version": "16.7.16"}, fmt=plistlib.FMT_BINARY))
        bl.apply(UDID, dry_run=False)
        raw = (folder / "Info.plist").read_bytes()
        self.assertTrue(raw.startswith(b"bplist"))
        d = plistlib.loads(raw)
        self.assertEqual((d["Display Name"], d["Original Display Name"], d["Product Version"]), ("Яблоко · extra copy", "Яблоко ", "16.7.16"))
        self.assertEqual(d["Device Name"], "Яблоко ")
        bl.apply(UDID, revert=True, dry_run=False)
        self.assertEqual(plistlib.loads((folder / "Info.plist").read_bytes())["Display Name"], "Яблоко ")

    def test_finders_own_date_on_extra_copies_is_kept(self):
        d = plistlib.loads(self.extra.read_bytes())
        d["Display Name"] = "Яблоко  - Nov 9, 2026 at 8:39 PM"
        self.extra.write_bytes(plistlib.dumps(d))
        bl.apply(UDID, dry_run=False)
        self.assertEqual(plistlib.loads(self.extra.read_bytes())["Display Name"], "Яблоко · extra copy - Nov 9, 2026 at 8:39 PM")
        bl.apply(UDID, dry_run=False)   # idempotent with the suffix too
        self.assertEqual(plistlib.loads(self.extra.read_bytes())["Display Name"], "Яблоко · extra copy - Nov 9, 2026 at 8:39 PM")
        bl.apply(UDID, revert=True, dry_run=False)
        self.assertEqual(plistlib.loads(self.extra.read_bytes())["Display Name"], "Яблоко  - Nov 9, 2026 at 8:39 PM")

    def test_migrates_the_earlier_device_name_edit_back_and_labels_display_name(self):
        # an earlier version rewrote Device Name (which Finder ignores) and kept the real one in Original Device Name
        for p in (self.main, self.safety, self.extra):
            d = plistlib.loads(p.read_bytes())
            d["Device Name"], d["Original Device Name"] = "Яблоко · old label", "Яблоко "
            p.write_bytes(plistlib.dumps(d))
        bl.apply(UDID, dry_run=False)
        for p in (self.main, self.safety, self.extra):
            d = plistlib.loads(p.read_bytes())
            self.assertEqual(d["Device Name"], "Яблоко ")
            self.assertNotIn("Original Device Name", d)
            self.assertIn("Яблоко ·", d["Display Name"])
            self.assertEqual(d["Original Display Name"], "Яблоко ")

    def test_unreadable_plist_aborts_before_any_folder_is_written(self):
        bad = self.root / f"{UDID}-20261109-230000"
        bad.mkdir()
        (bad / "Info.plist").write_bytes(b"not a plist at all")
        before = {p: p.read_bytes() for p in (self.main, self.safety, self.extra)}
        with self.assertRaises(Exception):
            bl.apply(UDID, dry_run=False)
        self.assertEqual({p: p.read_bytes() for p in before}, before)   # nothing half-applied


class EnsureLabelsHook(Labeler):
    def test_never_raises_and_reports_trouble(self):
        with mock.patch.object(bl, "apply", side_effect=RuntimeError("boom")):
            res = bl.ensure_labels(UDID, say=lambda m: None)
        self.assertFalse(res["ok"])
        self.assertIn("boom", res["message"])

    def test_applies_then_is_quiet_when_up_to_date(self):
        said = []
        first = bl.ensure_labels(UDID, say=said.append)
        self.assertEqual((first["ok"], first["changed"]), (True, 3))
        self.assertTrue(said and said[0].startswith("LABELS:"))
        said.clear()
        second = bl.ensure_labels(UDID, say=said.append)
        self.assertEqual(second["changed"], 0)
        self.assertEqual(said, [])

    def test_preview_mode_changes_nothing(self):
        before = self.main.read_bytes()
        res = bl.ensure_labels(UDID, say=lambda m: None, apply_changes=False)
        self.assertEqual(res["changed"], 3)
        self.assertEqual(self.main.read_bytes(), before)

    def test_without_a_phone_it_declines_quietly(self):
        with mock.patch.object(bl, "connected_udids", return_value=[]):
            self.assertFalse(bl.ensure_labels(None, say=lambda m: None)["ok"])


class PickerWithLabels(unittest.TestCase):
    def test_phone_entries_are_recognised_by_base_name(self):
        self.assertTrue(bp.names_equal("Яблоко · safety copy · Nov 9, 5:47 PM", "Яблоко"))
        self.assertTrue(bp.names_equal("Яблоко · extra copy - Nov 9, 2026 at 8:39 PM", "Яблоко "))
        self.assertFalse(bp.names_equal("Яблоко 2 · main", "Яблоко"))
        self.assertFalse(bp.names_equal("Анна’s iPhone", "Яблоко"))

    def test_exact_label_is_chosen_among_a_phones_entries(self):
        label = "Яблоко · safety copy · Nov 9, 5:47 PM"
        menu = ["Яблоко · main · Nov 9, 5:47 PM", label, "Яблоко · extra copy - Nov 9, 2026 at 8:39 PM"]
        shown = "Last Backed Up: Today, 5:47 PM"

        class F:
            def __init__(self):
                self.current, self.open, self.selected = 2, False, []

            def snapshot(self):
                items = [{"scope": "W", "cls": "pop up button", "name": "", "value": menu[self.current], "enabled": True},
                         {"scope": "W", "cls": "static text", "name": "", "value": shown, "enabled": True}]
                if self.open:
                    items += [{"scope": "W", "cls": "menu item", "name": m, "value": "", "enabled": True} for m in menu]
                return {"window": "iPhone", "has_sheet": False, "items": items}

            def click(self, scope, cls, name, exact=False, nth=1):
                if cls == "pop up button":
                    self.open = True
                else:
                    self.current, self.open = nth - 1, False
                    self.selected.append(self.current)
                return "CLICKED"

        f = F()
        # "Today, 5:47 PM" is read relative to the real clock, so build the expected time from today's date
        expected = datetime.now().replace(hour=17, minute=47, second=0, microsecond=0).astimezone().astimezone(timezone.utc).replace(tzinfo=None)
        res = bp.choose_backup(f, "Яблоко", expected, lambda s: None, expected_label=label)
        self.assertTrue(res["ok"], res)
        self.assertEqual(f.current, 1)
        self.assertEqual(f.selected, [1])   # went straight to the exact entry


if __name__ == "__main__":
    unittest.main()
