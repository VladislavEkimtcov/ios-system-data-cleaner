"""
Backup picker (backup_picker.py): reading Finder's pop-up labels and "Last Backed Up" line, and choosing the
entry that is this phone's validated backup. The scripted menu below is the real one captured from Finder on
2026-11-09: other phones' backups, a date suffix that is the folder's own time, and identical labels.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("IOS_CLEANER_STATE_DIR", tempfile.mkdtemp(prefix="ioscl_test_state_"))

import backup_picker as bp  # noqa: E402

NAME = "Яблоко"
NB = " "
REAL_MENU = [
    "User’s iPhone",
    f"Анна’s iPhone - Nov 7, 2026 at 8:49{NB}PM",
    f"Анна’s iPhone - Nov 7, 2026 at 8:49{NB}PM",
    f"Анна’s iPhone - Nov 7, 2026 at 8:49{NB}PM",
    f"Анна’s iPhone - Nov 7, 2026 at 8:49{NB}PM - Nov 7, 2026 at 9:11{NB}PM",
    f"Анна’s iPhone - Nov 7, 2026 at 8:49{NB}PM",
    "Яблоко  - Nov 9, 2026 at 8:39 PM",
    "Яблоко ", "Яблоко ",
    "Яблоко  - Nov 9, 2026 at 7:29 PM",
    "Яблоко  - Nov 9, 2026 at 9:05 PM",
    "Яблоко ",
]


def local_to_utc_naive(local_naive: datetime) -> datetime:
    return local_naive.astimezone(timezone.utc).replace(tzinfo=None)


class FakeFinder:
    """A pop-up whose entries each show a 'Last Backed Up' time; records what gets selected."""

    def __init__(self, shown_by_index, current=6, menu=REAL_MENU, can_open=True, hide_menu=False):
        self.menu, self.shown, self.current, self.open, self.can_open = menu, shown_by_index, current, False, can_open
        self.hide_menu, self.escaped = hide_menu, 0
        self.selected = []

    def escape(self):
        self.escaped += 1
        self.open = False

    def snapshot(self):
        items = [{"scope": "W", "cls": "pop up button", "name": "", "value": self.menu[self.current], "enabled": True}]
        if self.open and not self.hide_menu:
            items += [{"scope": "W", "cls": "menu item", "name": m, "value": "", "enabled": True} for m in self.menu]
        t = self.shown.get(self.current)
        if t:
            items.append({"scope": "W", "cls": "static text", "name": "", "value": t, "enabled": True})
        return {"window": "iPhone", "has_sheet": False, "items": items}

    def click(self, scope, cls, name, exact=False, nth=1):
        if cls == "pop up button":
            if not self.can_open:
                return "NOT_FOUND"
            self.open = True
            return "CLICKED"
        if cls == "menu item":
            self.current = nth - 1
            self.selected.append(self.current)
            self.open = False
            return "CLICKED"
        return "NOT_FOUND"


def at(hour, minute):
    n = datetime.now()
    return datetime(n.year, n.month, n.day, hour, minute)


class Parsing(unittest.TestCase):
    def test_entry_name_strips_date_suffixes_and_whitespace(self):
        self.assertEqual(bp.entry_name("Яблоко  - Nov 9, 2026 at 8:39 PM"), "Яблоко")
        self.assertEqual(bp.entry_name("Яблоко "), "Яблоко")
        self.assertEqual(bp.entry_name(REAL_MENU[4]), "Анна's iPhone")

    def test_names_equal_ignores_curly_apostrophes_and_case(self):
        self.assertTrue(bp.names_equal("Анна’s iPhone - Nov 7, 2026 at 8:49 PM", "анна's iphone"))
        self.assertFalse(bp.names_equal("Яблоко", "Яблоко 2"))

    def test_parse_when_today_yesterday_and_dates(self):
        now = datetime(2026, 11, 9, 22, 0)
        self.assertEqual(bp.parse_when(f"Last Backed Up: Today, 5:47{NB}PM", now), datetime(2026, 11, 9, 17, 47))
        self.assertEqual(bp.parse_when("Last Backed Up: Yesterday, 12:05 AM", now), datetime(2026, 11, 8, 0, 5))
        self.assertEqual(bp.parse_when("Last Backed Up: Nov 7, 2026 at 8:49 PM", now), datetime(2026, 11, 7, 20, 49))
        self.assertEqual(bp.parse_when("Last Backed Up: 11/7/2026, 12:00 PM", now), datetime(2026, 11, 7, 12, 0))

    def test_unreadable_time_is_none_never_a_guess(self):
        self.assertIsNone(bp.parse_when("Последнее копирование: сегодня, 17:47"))
        self.assertIsNone(bp.parse_when(None))

    def test_parse_menu_flags_dated_entries(self):
        snap = FakeFinder({}, current=6)
        snap.open = True
        menu = bp.parse_menu(snap.snapshot())
        self.assertEqual(len(menu), 12)
        self.assertTrue(menu[6]["dated"])
        self.assertFalse(menu[7]["dated"])

    def test_time_matches_within_tolerance_only(self):
        exp = local_to_utc_naive(at(17, 47).astimezone())
        self.assertTrue(bp.time_matches(at(17, 47), exp))
        self.assertTrue(bp.time_matches(at(17, 49), exp))
        self.assertFalse(bp.time_matches(at(20, 39), exp))
        self.assertFalse(bp.time_matches(None, exp))


class Choosing(unittest.TestCase):
    def setUp(self):
        self.expected = local_to_utc_naive(at(17, 47).astimezone())
        self.sleep = lambda s: None

    def test_keeps_the_current_entry_when_name_and_time_match(self):
        f = FakeFinder({7: f"Last Backed Up: Today, 5:47{NB}PM"}, current=7)
        res = bp.choose_backup(f, NAME, self.expected, self.sleep)
        self.assertTrue(res["ok"])
        self.assertTrue(res["kept"])
        self.assertEqual(f.selected, [])  # nothing needed clicking

    def test_a_dated_finder_copy_is_never_kept_even_when_the_time_matches(self):
        f = FakeFinder({6: f"Last Backed Up: Today, 5:47{NB}PM", 7: f"Last Backed Up: Today, 5:47{NB}PM"})
        res = bp.choose_backup(f, NAME, self.expected, self.sleep)
        self.assertTrue(res["ok"])
        self.assertFalse(res["kept"])
        self.assertEqual(f.current, 7)  # the first plain entry

    def test_dated_preselection_with_unreadable_menu_hands_off(self):
        f = FakeFinder({6: f"Last Backed Up: Today, 5:47{NB}PM"}, hide_menu=True)
        res = bp.choose_backup(f, NAME, self.expected, self.sleep)
        self.assertEqual((res["ok"], res["reason"]), (False, "menu_not_readable"))

    def test_picks_the_entry_whose_time_matches_among_identical_labels(self):
        # the preselected entry is a Finder copy showing a LATER (e.g. empty-phone) backup; one plain entry is right
        shown = {6: "Last Backed Up: Today, 8:39 PM", 7: "Last Backed Up: Today, 8:00 PM",
                 8: "Last Backed Up: Today, 5:47 PM"}
        f = FakeFinder(shown)
        res = bp.choose_backup(f, NAME, self.expected, self.sleep)
        self.assertTrue(res["ok"], res)
        self.assertEqual(f.current, 8)
        self.assertEqual(f.selected, [7, 8])  # plain entries first, in menu order

    def test_never_selects_another_phones_backup(self):
        shown = {i: "Last Backed Up: Today, 5:47 PM" for i in range(0, 6)}  # other phones even show the same time
        shown[6] = "Last Backed Up: Today, 9:00 PM"
        f = FakeFinder(shown)
        bp.choose_backup(f, NAME, self.expected, self.sleep)
        self.assertTrue(all(i >= 6 for i in f.selected))

    def test_no_matching_time_stops_and_reports_every_reading(self):
        shown = {i: "Last Backed Up: Today, 9:00 PM" for i in range(6, 12)}
        f = FakeFinder(shown)
        res = bp.choose_backup(f, NAME, self.expected, self.sleep)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "no_matching_time")
        self.assertGreaterEqual(len(res["readings"]), 4)  # the preselection + the 3 plain entries

    def test_no_entry_for_this_phone(self):
        f = FakeFinder({}, current=0)
        res = bp.choose_backup(f, "Dad's phone", self.expected, self.sleep)
        self.assertEqual((res["ok"], res["reason"]), (False, "no_entry_for_phone"))

    def test_unreadable_time_does_not_match(self):
        f = FakeFinder({i: "Последнее копирование: сегодня, 17:47" for i in range(6, 12)})
        self.assertFalse(bp.choose_backup(f, NAME, self.expected, self.sleep)["ok"])

    def test_cannot_open_menu(self):
        f = FakeFinder({6: "Last Backed Up: Today, 9:00 PM"}, can_open=False)
        self.assertEqual(bp.choose_backup(f, NAME, self.expected, self.sleep)["reason"], "cannot_open_menu")

    def test_unreadable_menu_closes_it_and_hands_off_without_guessing(self):
        f = FakeFinder({6: "Last Backed Up: Today, 9:00 PM"}, hide_menu=True)
        res = bp.choose_backup(f, NAME, self.expected, self.sleep)
        self.assertEqual((res["ok"], res["reason"]), (False, "menu_not_readable"))
        self.assertEqual(f.selected, [])
        self.assertEqual(f.escaped, 1)

    def test_unknown_capture_time_falls_back_to_name_and_says_so(self):
        f = FakeFinder({6: "Last Backed Up: Today, 9:00 PM"})
        res = bp.choose_backup(f, NAME, None, self.sleep)
        self.assertTrue(res["ok"])
        self.assertTrue(res["name_only"])


if __name__ == "__main__":
    unittest.main()
