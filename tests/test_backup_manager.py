"""
Whole-new-backup policy and backup management: rotating a backup aside, keeping older safety copies,
the stats list, the clean-up proposal and the guarded Trash move.
"""

import os
import plistlib
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

import backup_manager as bm  # noqa: E402
import common  # noqa: E402

UDID = "00008030-000AAAA1111BBBB2"


def naive_utc(**delta) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(**delta)


def make_backup(root: Path, name: str, apps=0, age_hours=1.0, manifest=b"x" * 64, udid=UDID) -> Path:
    p = root / name
    p.mkdir(parents=True, exist_ok=True)
    when = naive_utc(hours=age_hours)
    (p / "Info.plist").write_bytes(plistlib.dumps({
        "Unique Identifier": udid, "Device Name": "Peach ", "Product Version": "16.7.16", "Product Type": "iPhone10,4",
        "Last Backup Date": when,
        "Applications": {f"com.example.app{i}": {} for i in range(apps)}}))
    (p / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": True}))
    (p / "Status.plist").write_bytes(plistlib.dumps({"SnapshotState": "finished", "Date": when}))
    (p / "Manifest.db").write_bytes(manifest)
    return p


class RotationAndClones(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bk_root_"))

    def test_rotate_moves_the_backup_aside_without_touching_it(self):
        live = make_backup(self.root, UDID, apps=3, age_hours=5)
        before = (live / "Manifest.db").read_bytes()
        dest = common.rotate_backup_aside(live)
        self.assertFalse(live.exists())
        self.assertRegex(dest.name, rf"^{UDID}-\d{{8}}-\d{{6}}$")
        self.assertEqual((dest / "Manifest.db").read_bytes(), before)

    def test_rotate_with_nothing_there_is_a_noop(self):
        self.assertIsNone(common.rotate_backup_aside(self.root / UDID))

    def test_rotating_twice_in_the_same_second_never_collides(self):
        a = make_backup(self.root, UDID, age_hours=5)
        first = common.rotate_backup_aside(a)
        b = make_backup(self.root, UDID, age_hours=5)
        second = common.rotate_backup_aside(b)
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_dir() and second.is_dir())

    def test_a_stale_clone_is_kept_as_an_older_safety_copy(self):
        live = make_backup(self.root, UDID, apps=45, age_hours=20, manifest=b"old" * 30)
        clone = common.make_prewipe_clone(live, verbose=False)
        self.assertEqual(clone.name, f"{UDID}-prewipe")
        # a NEWER backup of the phone (with different data) replaces the live folder
        for f in live.iterdir():
            f.unlink()
        live.rmdir()
        newer = make_backup(self.root, UDID, apps=0, age_hours=1, manifest=b"new" * 30)
        fresh = common.make_prewipe_clone(newer, verbose=False)
        self.assertEqual((fresh / "Manifest.db").read_bytes(), b"new" * 30)
        kept = [p for p in self.root.iterdir() if p.name.startswith(f"{UDID}-prewipe-")]
        self.assertEqual(len(kept), 1, "the older clone must be kept, not discarded")
        self.assertEqual((kept[0] / "Manifest.db").read_bytes(), b"old" * 30)


class RunnerRollback(unittest.TestCase):
    """If a new backup fails, the previous one must be exactly where it was."""

    def _runner(self, root):
        import backup_runner
        r = object.__new__(backup_runner.BackupRunner)
        r.udid, r.backup_dir, r.logs = UDID, root, []
        r.log = r.logs.append
        return r

    def test_failed_run_removes_the_partial_and_restores_the_previous_backup(self):
        root = Path(tempfile.mkdtemp(prefix="bk_root_"))
        old = make_backup(root, UDID, apps=45, age_hours=5, manifest=b"old" * 20)
        r = self._runner(root)
        r._archived = common.rotate_backup_aside(old)
        (root / UDID).mkdir()                       # the engine's half-written new backup
        (root / UDID / "partial").write_bytes(b"?")
        r._roll_back(partial=True)
        self.assertEqual((root / UDID / "Manifest.db").read_bytes(), b"old" * 20)
        self.assertFalse((root / UDID / "partial").exists())
        self.assertEqual([p.name for p in root.iterdir()], [UDID])

    def test_backup_that_fails_verification_is_kept_as_failed_and_the_previous_comes_back(self):
        root = Path(tempfile.mkdtemp(prefix="bk_root_"))
        old = make_backup(root, UDID, apps=45, age_hours=5, manifest=b"old" * 20)
        r = self._runner(root)
        r._archived = common.rotate_backup_aside(old)
        make_backup(root, UDID, apps=0, age_hours=0.1, manifest=b"bad" * 20)
        r._roll_back(partial=False)
        self.assertEqual((root / UDID / "Manifest.db").read_bytes(), b"old" * 20)
        failed = [p for p in root.iterdir() if "-failed-" in p.name]
        self.assertEqual(len(failed), 1)
        self.assertEqual((failed[0] / "Manifest.db").read_bytes(), b"bad" * 20)


class ListAndPropose(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bk_root_"))
        patcher = mock.patch.object(bm, "backup_root", return_value=self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stats_cover_apps_and_age(self):
        make_backup(self.root, UDID, apps=45, age_hours=2)
        (row,) = bm.list_backups(UDID)
        self.assertEqual(row["facts"]["app_count"], 45)
        self.assertEqual(row["kind"], "main")
        self.assertTrue(row["usable"])

    def test_other_phones_are_never_listed_as_this_phones(self):
        make_backup(self.root, UDID, apps=1)
        make_backup(self.root, "00008101-OTHER", apps=1, udid="00008101-OTHER")
        self.assertEqual([b["folder"] for b in bm.list_backups(UDID)], [UDID])
        self.assertEqual([o["folder"] for o in bm.other_phones(UDID)], ["00008101-OTHER"])

    def test_exact_duplicates_are_proposed_but_the_protected_copy_is_kept(self):
        data = b"same" * 20
        make_backup(self.root, UDID, apps=45, age_hours=2, manifest=data)
        make_backup(self.root, f"{UDID}-prewipe", apps=45, age_hours=2, manifest=data)
        make_backup(self.root, f"{UDID}-20261110-085915", apps=45, age_hours=2, manifest=data)
        moves = bm.propose_moves(bm.list_backups(UDID))
        self.assertEqual([m["folder"] for m in moves], [f"{UDID}-20261110-085915"])
        self.assertIn("duplicate", moves[0]["why"])

    def test_the_only_backup_with_the_apps_is_never_proposed(self):
        make_backup(self.root, f"{UDID}-20261109-175000", apps=45, age_hours=20, manifest=b"a" * 30)
        make_backup(self.root, UDID, apps=0, age_hours=1, manifest=b"b" * 30)
        moves = bm.propose_moves(bm.list_backups(UDID))
        self.assertEqual(moves, [])

    def test_an_older_backup_superseded_by_one_with_as_many_apps_is_proposed(self):
        make_backup(self.root, f"{UDID}-20261109-175000", apps=40, age_hours=20, manifest=b"a" * 30)
        make_backup(self.root, UDID, apps=45, age_hours=1, manifest=b"b" * 30)
        moves = bm.propose_moves(bm.list_backups(UDID))
        self.assertEqual([m["folder"] for m in moves], [f"{UDID}-20261109-175000"])
        self.assertIn("superseded", moves[0]["why"])

    def test_failed_backups_come_first(self):
        make_backup(self.root, UDID, apps=45, age_hours=1, manifest=b"b" * 30)
        make_backup(self.root, f"{UDID}-failed-20261110-090000", apps=0, age_hours=1, manifest=b"c" * 30)
        self.assertEqual(bm.propose_moves(bm.list_backups(UDID))[0]["folder"], f"{UDID}-failed-20261110-090000")

    def test_an_unfinished_backup_is_proposed(self):
        # 2026-11-11: a run that stopped at the passcode left Info.plist only (0 GB); it was never proposed
        make_backup(self.root, UDID, apps=45, age_hours=1, manifest=b"b" * 30)
        stopped = make_backup(self.root, f"{UDID}-20261111-104559", apps=45, age_hours=5)
        for f in ("Manifest.plist", "Manifest.db", "Status.plist"):
            (stopped / f).unlink()
        moves = bm.propose_moves(bm.list_backups(UDID))
        self.assertEqual([m["folder"] for m in moves], [f"{UDID}-20261111-104559"])
        self.assertIn("unfinished", moves[0]["why"])

    def test_an_unfinished_main_backup_is_never_proposed(self):
        stopped = make_backup(self.root, UDID, apps=45, age_hours=1)
        (stopped / "Manifest.plist").unlink()
        self.assertEqual(bm.propose_moves(bm.list_backups(UDID)), [])

    def test_describe_warns_about_app_less_backups_and_names_the_next_move(self):
        data = b"same" * 20
        make_backup(self.root, UDID, apps=0, age_hours=1, manifest=data)
        make_backup(self.root, f"{UDID}-prewipe", apps=45, age_hours=20, manifest=b"z" * 30)
        make_backup(self.root, f"{UDID}-20261110-085915", apps=0, age_hours=1, manifest=data)
        backups = bm.list_backups(UDID)
        text = "\n".join(bm.describe(backups, bm.propose_moves(backups), [], 100.0))
        self.assertIn("WARNING: only 0 apps listed", text)
        self.assertIn("PROPOSED_NEXT_MOVE: move", text)


class AutoSourceSkipsAppLessBackups(unittest.TestCase):
    def setUp(self):
        import restore_gates as gates
        self.gates = gates
        self.root = Path(tempfile.mkdtemp(prefix="bk_root_"))
        p = mock.patch.object(gates, "backup_root", return_value=self.root)
        p.start()
        self.addCleanup(p.stop)

    def test_auto_picks_the_older_backup_with_the_apps_and_says_what_that_costs(self):
        make_backup(self.root, f"{UDID}-prewipe", apps=45, age_hours=16)
        make_backup(self.root, UDID, apps=0, age_hours=1)
        src = self.gates.choose_source(UDID, "auto")
        self.assertEqual(src["kind"], "clone")
        self.assertEqual([s["app_count"] for s in src["skipped"]], [0])

    def test_an_explicit_source_is_respected(self):
        make_backup(self.root, f"{UDID}-prewipe", apps=45, age_hours=16)
        make_backup(self.root, UDID, apps=0, age_hours=1)
        self.assertEqual(self.gates.choose_source(UDID, "live")["kind"], "live")

    def test_normal_case_is_unchanged_newest_wins(self):
        make_backup(self.root, f"{UDID}-prewipe", apps=44, age_hours=16)
        make_backup(self.root, UDID, apps=45, age_hours=1)
        src = self.gates.choose_source(UDID, "auto")
        self.assertEqual(src["kind"], "live")
        self.assertEqual(src["skipped"], [])


class TrashGuards(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bk_root_"))
        self.home = Path(tempfile.mkdtemp(prefix="bk_home_"))
        for p in (mock.patch.object(bm, "backup_root", return_value=self.root),
                  mock.patch.object(bm.Path, "home", return_value=self.home),
                  mock.patch.object(bm.sys, "platform", "darwin")):
            p.start()
            self.addCleanup(p.stop)

    def test_trash_moves_the_folder_and_keeps_it_recoverable(self):
        make_backup(self.root, UDID, apps=45, age_hours=1, manifest=b"b" * 30)
        old = make_backup(self.root, f"{UDID}-20261109-175000", apps=40, age_hours=20, manifest=b"a" * 30)
        res = bm.trash_backup(UDID, old.name)
        self.assertTrue(res["ok"], res)
        self.assertFalse(old.exists())
        self.assertTrue((self.home / ".Trash" / old.name / "Manifest.db").is_file())

    def test_windows_moves_aside_next_to_the_backup_root_not_the_recycle_bin(self):
        make_backup(self.root, UDID, apps=45, age_hours=1, manifest=b"b" * 30)
        old = make_backup(self.root, f"{UDID}-20261109-175000", apps=40, age_hours=20, manifest=b"a" * 30)
        with mock.patch.object(bm.sys, "platform", "win32"):
            res = bm.trash_backup(UDID, old.name)
        self.assertTrue(res["ok"], res)
        self.assertFalse(old.exists())
        moved = self.root.parent / bm.REMOVED_DIR_NAME / old.name
        self.addCleanup(lambda: __import__("shutil").rmtree(moved.parent, ignore_errors=True))
        self.assertTrue((moved / "Manifest.db").is_file())
        self.assertIn("Removed Backups", res["message"])
        self.assertFalse((self.home / ".Trash").exists())

    def test_refuses_live_protected_and_foreign_folders(self):
        make_backup(self.root, UDID, apps=45)
        make_backup(self.root, f"{UDID}-prewipe", apps=45)
        make_backup(self.root, f".protected-{UDID}-golden-20261109", apps=45)
        for name in (UDID, f"{UDID}-prewipe", f".protected-{UDID}-golden-20261109", "00008101-OTHER", "../etc"):
            self.assertFalse(bm.trash_backup(UDID, name)["ok"], name)
            self.assertTrue((self.root / name).exists() or name in ("00008101-OTHER", "../etc"))

    def test_refuses_the_only_backup_with_the_apps(self):
        rich = make_backup(self.root, f"{UDID}-20261109-175000", apps=45, age_hours=20, manifest=b"a" * 30)
        make_backup(self.root, UDID, apps=0, age_hours=1, manifest=b"b" * 30)
        res = bm.trash_backup(UDID, rich.name)
        self.assertFalse(res["ok"])
        self.assertTrue(rich.exists())

    def test_refuses_to_trash_the_last_usable_backup(self):
        only = make_backup(self.root, f"{UDID}-20261109-175000", apps=0)
        self.assertFalse(bm.trash_backup(UDID, only.name)["ok"])
        self.assertTrue(only.exists())


if __name__ == "__main__":
    unittest.main()
