"""
Unit tests for market_pricing.py (Dynamic Secondary Market Valuation & Agent Stimulation).
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("IOS_CLEANER_STATE_DIR", tempfile.mkdtemp(prefix="ioscl_test_state_"))

import market_pricing
import storage_monitor


class TestMarketPricingSuite(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_prices_file = Path(self.temp_dir.name) / "test_market_prices.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_get_friendly_name(self):
        self.assertEqual(market_pricing.get_friendly_name("iPhone10,4"), "iPhone 8")
        self.assertEqual(market_pricing.get_friendly_name("iPhone12,8"), "iPhone SE (2nd generation)")
        self.assertEqual(market_pricing.get_friendly_name("iPhone13,1"), "iPhone 12 mini")
        self.assertEqual(market_pricing.get_friendly_name("CustomGizmo99"), "CustomGizmo99")

    def test_is_quote_stale_detection(self):
        # Empty record
        stale, reason = market_pricing.is_quote_stale({})
        self.assertTrue(stale)

        # Unverified confidence
        stale, reason = market_pricing.is_quote_stale({"confidence": "FALLBACK"})
        self.assertTrue(stale)

        # Fresh record (< 14 days)
        now_iso = datetime.now(timezone.utc).isoformat()
        stale, reason = market_pricing.is_quote_stale({
            "confidence": "LIVE_VERIFIED",
            "last_updated": now_iso,
        }, max_days=14)
        self.assertFalse(stale)
        self.assertIn("Fresh", reason)

        # Stale record (> 14 days)
        old_iso = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        stale, reason = market_pricing.is_quote_stale({
            "confidence": "LIVE_VERIFIED",
            "last_updated": old_iso,
        }, max_days=14)
        self.assertTrue(stale)
        self.assertIn("old", reason)

    def test_record_and_get_live_quote(self):
        entry = market_pricing.record_live_quote(
            model="iPhone14,4",
            refurb_usd=240.0,
            retail_usd=799.0,
            source="Swappa 2026-11",
            capacity_gb=128,
            prices_file=self.test_prices_file,
        )
        self.assertEqual(entry["refurb_replacement_usd"], 240.0)
        self.assertEqual(entry["confidence"], "LIVE_VERIFIED")

        val = market_pricing.get_device_valuation(
            "iPhone14,4",
            capacity_gb=128,
            prices_file=self.test_prices_file,
        )
        self.assertEqual(val["pricing_status"], "LIVE_VERIFIED")
        self.assertFalse(val["needs_research"])
        self.assertEqual(val["refurb_replacement_usd"], 240.0)
        self.assertEqual(val["friendly_name"], "iPhone 13 mini")

    def test_unrecorded_model_generates_stimulus(self):
        val = market_pricing.get_device_valuation(
            "iPhone99,9",
            prices_file=self.test_prices_file,
        )
        self.assertTrue(val["needs_research"])
        self.assertEqual(val["pricing_status"], "FALLBACK")
        self.assertIn("AGENT LIVE RESEARCH DIRECTIVE", val["stimulus_prompt"])
        self.assertIn("iPhone99,9", val["stimulus_prompt"])

    def test_storage_diff_includes_live_valuation(self):
        diff = storage_monitor.compute_storage_diff(demo=True)
        self.assertIsNotNone(diff)
        self.assertIn("refurb_avoided_usd", diff)
        self.assertIn("pricing_status", diff)
        self.assertIn("pricing_source", diff)
        self.assertIn("household_summary", diff)
        hh = diff["household_summary"]
        self.assertIn("total_refurb_savings_usd", hh)
        self.assertGreater(hh["total_refurb_savings_usd"], 0)


if __name__ == "__main__":
    unittest.main()
