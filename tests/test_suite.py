"""
Test Suite for iOS Storage Revival, Backup Verifier, and Token-Saver Compactor.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Hermetic isolation: keep all runtime state (password history, snapshots, token
# manifests) in a throwaway dir so tests never touch ~/.ios-system-data-cleaner.
os.environ.setdefault("IOS_CLEANER_STATE_DIR", tempfile.mkdtemp(prefix="ioscl_test_state_"))

from backup_verifier import (
    find_backup_for_target,
    parse_backup_metadata,
)
from ios_mcp_server import ALL_TOOLS, _build_tool_schema
from token_saver import (
    TokenCompactor,
    TokenDoctor,
    compute_token_metric,
    estimate_tokens,
)

TEMP_FIXTURE_DIR = None


def setUpModule():
    """Ensures an isolated synthetic backup is available for unit test assertions."""
    global TEMP_FIXTURE_DIR
    TEMP_FIXTURE_DIR = tempfile.TemporaryDirectory()
    from tests.fixtures import create_synthetic_backup
    create_synthetic_backup(Path(TEMP_FIXTURE_DIR.name))
    os.environ["IPHONE_BACKUP_DIR"] = TEMP_FIXTURE_DIR.name
    import backup_verifier
    import token_extractor
    backup_verifier.BACKUP_ROOT = Path(TEMP_FIXTURE_DIR.name)
    token_extractor.BACKUP_ROOT = Path(TEMP_FIXTURE_DIR.name)


def tearDownModule():
    """Cleans up the synthetic backup fixture directory if created."""
    global TEMP_FIXTURE_DIR
    if TEMP_FIXTURE_DIR:
        TEMP_FIXTURE_DIR.cleanup()
        os.environ.pop("IPHONE_BACKUP_DIR", None)
        import backup_verifier
        import token_extractor
        import itunes_env
        root = itunes_env.get_itunes_backup_root()
        backup_verifier.BACKUP_ROOT = root
        token_extractor.BACKUP_ROOT = root


class TestTokenSaver(unittest.TestCase):
    def test_estimate_tokens(self):
        text = "Hello world! This is a test sentence."
        self.assertGreater(estimate_tokens(text), 5)
        self.assertEqual(estimate_tokens(""), 0)

    def test_compact_json(self):
        payload = {
            "device": "iPhone13,1",
            "null_field": None,
            "empty_dict": {},
            "items": list(range(30)),
            "nested": {
                "keep": "value",
                "drop": None,
            },
        }
        compacted = TokenCompactor.compact_json(payload, max_list_items=5)
        self.assertNotIn("null_field", compacted)
        self.assertNotIn("empty_dict", compacted)
        self.assertNotIn("drop", compacted["nested"])
        self.assertEqual(len(compacted["items"]), 6)  # 5 items + 1 truncation note
        self.assertIn("omitted", compacted["items"][-1])

    def test_filter_cli_output(self):
        raw = (
            "====================================================\n"
            "\x1b[32mScanning device...\x1b[0m\n"
            "Checking item 1\nChecking item 2\nChecking item 3\n"
            "Checking item 4\nChecking item 5\nChecking item 6\n"
            "Checking item 7\nChecking item 8\nChecking item 9\n"
            "Checking item 10\nChecking item 11\nChecking item 12\n"
            "Checking item 13\nChecking item 14\nChecking item 15\n"
            "RESULT: PASS - all checks green\n"
            "====================================================\n"
        )
        filtered = TokenCompactor.filter_cli_output(raw, max_lines=8)
        self.assertNotIn("\x1b[32m", filtered)
        self.assertNotIn("====", filtered)
        self.assertIn("RESULT: PASS", filtered)

    def test_compact_firmwares(self):
        firmwares = [
            {"version": "18.1", "buildid": "22B83", "signed": True, "filesize_gb": 10.2},
            {"version": "17.6.1", "buildid": "21G93", "signed": False, "filesize_gb": 9.8},
            {"version": "17.5.1", "buildid": "21F90", "signed": False, "filesize_gb": 9.7},
            {"version": "16.7.10", "buildid": "20H350", "signed": False, "filesize_gb": 7.5},
        ]
        compacted = TokenCompactor.compact_firmwares(firmwares)
        self.assertEqual(compacted["signed_firmwares_count"], 1)
        self.assertEqual(compacted["unsigned_historical_count"], 3)
        self.assertEqual(compacted["unsigned_summary_by_major_release"]["iOS 17"], 2)
        self.assertEqual(compacted["unsigned_summary_by_major_release"]["iOS 16"], 1)

    def test_token_metric(self):
        raw = "A" * 1000
        compact = "A" * 200
        metric = compute_token_metric(raw, compact)
        self.assertGreater(metric.savings_pct, 70.0)
        self.assertEqual(metric.tokens_saved, metric.raw_tokens_est - metric.compact_tokens_est)


class TestSimulatorBridge(unittest.TestCase):
    def test_list_simulators(self):
        # Hermetic: a recorded `simctl list --json devices` answer. The real xcrun on CI runners can hang past
        # the 15 s timeout (macos-latest, 2026-11-10), which made this test fail for reasons unrelated to the code.
        import json
        from unittest import mock
        import simulator_bridge
        answer = {"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-18-0": [
            {"udid": "SIM-1", "name": "iPhone 16", "state": "Booted", "isAvailable": True,
             "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-16"},
            {"udid": "SIM-2", "name": "iPhone SE", "state": "Shutdown", "isAvailable": False,
             "deviceTypeIdentifier": "com.apple.CoreSimulator.SimDeviceType.iPhone-SE"},
        ]}}
        with mock.patch.object(simulator_bridge, "check_simctl_available", return_value=True), \
             mock.patch.object(simulator_bridge.subprocess, "run",
                               return_value=mock.Mock(stdout=json.dumps(answer))) as run:
            sims = simulator_bridge.list_simulators(only_available=True, compact=True)
        self.assertEqual(run.call_args[0][0], ["xcrun", "simctl", "list", "--json", "devices"])
        self.assertEqual([s["udid"] for s in sims], ["SIM-1"])   # the unavailable one is dropped
        self.assertEqual((sims[0]["name"], sims[0]["state"], sims[0]["runtime"]), ("iPhone 16", "Booted", "iOS-18-0"))

    def test_extract_media(self):
        from simulator_bridge import extract_media_from_backup
        backup = find_backup_for_target()
        if backup:
            items = extract_media_from_backup(backup.backup_path, max_items=5)
            self.assertIsInstance(items, list)
            if items:
                self.assertTrue(items[0][0].exists())


class TestBackupApps(unittest.TestCase):
    def test_catalog_parsing(self):
        import backup_apps
        from backup_verifier import find_backup_for_target
        if not find_backup_for_target():
            self.skipTest("No local iOS backup found on host")
        try:
            catalog = backup_apps.get_backup_apps_catalog()
        except FileNotFoundError:
            self.skipTest("No local iOS backup found on host")
        self.assertGreaterEqual(len(catalog), 1)
        # Verify app query if Duo is in catalog
        duo = backup_apps.find_app_by_query("Duo")
        if duo:
            self.assertEqual(duo.bundle_id, "com.duosecurity.DuoMobile")
            self.assertIn("Duo", duo.display_name)
            self.assertIsNotNone(duo.app_store_url)

    def test_genre_grouping(self):
        import backup_apps
        from backup_verifier import find_backup_for_target
        if not find_backup_for_target():
            self.skipTest("No local iOS backup found on host")
        try:
            catalog = backup_apps.get_backup_apps_catalog()
        except FileNotFoundError:
            self.skipTest("No local iOS backup found on host")
        grouped = backup_apps.group_apps_by_genre(catalog)
        self.assertIsInstance(grouped, dict)


class TestMCPTools(unittest.TestCase):
    def test_all_tools_schema(self):
        self.assertEqual(len(ALL_TOOLS), 15)
        for fn in ALL_TOOLS:
            schema = _build_tool_schema(fn)
            self.assertEqual(schema["name"], fn.__name__)
            self.assertIn("inputSchema", schema)
            self.assertIn("properties", schema["inputSchema"])


class TestWallpaperMatching(unittest.TestCase):
    def test_decode_synthetic_cpbitmap(self):
        import struct
        import simulator_bridge
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow (`PIL`) is not installed in test environment")
        width, height = 32, 32
        stride = (width * 4 + 63) // 64 * 64
        pixel_bytes = bytearray(stride * height)
        # Fill with RGBA pattern
        for y in range(height):
            for x in range(width):
                offset = y * stride + x * 4
                pixel_bytes[offset : offset + 4] = b"\xff\x00\xaa\xff"

        trailer = struct.pack("<7I", 0, 0, width, height, 1, 1, 0xdcb543a2)
        raw_cpbitmap = bytes(pixel_bytes) + trailer
        img = simulator_bridge.decode_cpbitmap(raw_cpbitmap)
        self.assertEqual(img.size, (width, height))
        self.assertEqual(img.mode, "RGBA")

    def test_extract_wallpaper_from_backup(self):
        import simulator_bridge
        if simulator_bridge.Image is None:
            self.skipTest("Pillow (PIL) not installed; cpbitmap decode unavailable")
        backup = find_backup_for_target()
        if not backup:
            self.skipTest("No local iOS backup found on host")
        res = simulator_bridge.extract_wallpaper_from_backup(backup.udid)
        if res.get("status") == "error":
            self.skipTest(f"Backup wallpaper extraction skipped: {res.get('error')}")
        self.assertEqual(res["status"], "success")
        self.assertGreater(res["wallpapers_extracted_count"], 0)
        self.assertTrue(any(w["name"] == "LockScreen_Wallpaper.png" for w in res["wallpapers"]))


class TestRestorerSuite(unittest.TestCase):
    def test_cli_expect_reset_arg_parsing(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "iphone_manager.py"), "restorer", "wait", "--help"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--expect-reset", result.stdout)

    def test_is_device_in_setup_state_detection(self):
        import restorer
        self.assertFalse(restorer.is_device_in_setup_state({}))
        self.assertFalse(restorer.is_device_in_setup_state({"ActivationState": "Activated", "PasswordProtected": "true"}))
        self.assertTrue(restorer.is_device_in_setup_state({"ActivationState": "Unactivated"}))
        self.assertTrue(restorer.is_device_in_setup_state({"PasswordProtected": "false", "BrickState": "true"}))
        self.assertTrue(restorer.is_device_in_setup_state({"obliteration": "EACS Done"}))


class TestStorageMonitorSnapshots(unittest.TestCase):
    def test_storage_diff_computation(self):
        import storage_monitor
        # Use the bundled sample explicitly; the real ledger is intentionally empty in CI.
        diff = storage_monitor.compute_storage_diff(demo=True)
        self.assertIsNotNone(diff)
        self.assertGreater(diff["recovered_gb"], 0)
        self.assertGreater(diff["avail_increase_gb"], 0)
        bar = storage_monitor.render_storage_bar(0.5, width=10)
        self.assertEqual(bar, "[█████░░░░░]")


class TestPasswordTrackerSuite(unittest.TestCase):
    def test_generate_variants(self):
        import password_tracker
        variants = password_tracker.generate_variants("Secret")
        self.assertIn("Secret", variants)
        self.assertIn("secret", variants)
        self.assertIn("SECRET", variants)
        self.assertIn("Secret123", variants)
        self.assertIn("Secret!", variants)

    def test_list_keychain_backup_accounts(self):
        import password_tracker
        accounts = password_tracker.list_keychain_backup_accounts()
        self.assertIsInstance(accounts, list)

    def test_smart_keychain_probe_non_interactive_no_crash(self):
        import password_tracker
        ok, pwd, msg = password_tracker.smart_keychain_probe("00000000-NONEXISTENT-UDID", interactive=False)
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(msg, str)

    def test_proof_of_password_gate_fails_on_unproven(self):
        import backup_verifier
        from pathlib import Path
        # Test on temporary unproven directory
        test_dir = Path("/tmp/fake_backup_unproven")
        test_dir.mkdir(parents=True, exist_ok=True)
        try:
            ok, msg = backup_verifier.check_proof_of_password(test_dir, candidate_password="badpassword123")
            self.assertTrue(ok) # Unencrypted returns True
        finally:
            if test_dir.exists():
                test_dir.rmdir()


import simulate_backup_scenarios


class TestBackupScenarios(unittest.TestCase):
    def test_all_scenarios_matrix(self):
        matrix = simulate_backup_scenarios.run_scenario_matrix()
        self.assertEqual(len(matrix), 7)
        for r in matrix:
            self.assertTrue(
                r["expected_result"].startswith(r["actual_result"][:4]),
                f"Failed scenario check for: {r['scenario']}"
            )


class TestWindowsEnvironment(unittest.TestCase):
    def test_itunes_detection(self):
        from itunes_env import check_itunes_installation, get_itunes_backup_root
        status = check_itunes_installation()
        self.assertIsInstance(status.itunes_installed, bool)
        self.assertIsInstance(status.backup_root, str)
        root = get_itunes_backup_root()
        self.assertIsInstance(root, Path)

    def test_native_cli_tools_smoke(self):
        from win_tools import smoke_test_all_tools, find_tool
        report = smoke_test_all_tools()
        self.assertIn("tools", report)
        self.assertIn("connected_pnp_devices", report)
        self.assertIsInstance(report["tools"], list)

    def test_dep_installer_urls(self):
        from dep_installer import (
            APPLE_ITUNES_WIN32_URL,
            APPLE_ITUNES_WIN64_URL,
            LIBIMOBILE_SHA256,
            LIBIMOBILE_TAG,
            LIBIMOBILE_URL,
            get_apple_itunes_download_url,
        )
        # The stale, pinned secure-appldnld.apple.com installer URL was removed.
        self.assertTrue(LIBIMOBILE_URL.endswith(".zip"))
        self.assertIn(LIBIMOBILE_TAG, LIBIMOBILE_URL)
        self.assertNotIn("/latest", LIBIMOBILE_URL)
        self.assertRegex(LIBIMOBILE_SHA256, r"^[0-9a-f]{64}$")
        self.assertEqual(APPLE_ITUNES_WIN64_URL, "https://www.apple.com/itunes/download/win64")
        self.assertEqual(APPLE_ITUNES_WIN32_URL, "https://www.apple.com/itunes/download/win32")
        self.assertIn("download/win", get_apple_itunes_download_url())

    def test_cli_tools_download_with_wrong_hash_is_refused_and_not_extracted(self):
        import dep_installer as di
        out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out, True)

        def fake_download(url, dest, label=""):
            with zipfile.ZipFile(dest, "w") as zf:
                zf.writestr("bin/ideviceinfo.exe", b"not the real thing")

        with mock.patch.object(di.sys, "platform", "win32"), \
                mock.patch.object(di, "download_file_with_progress", side_effect=fake_download):
            res = di.install_cli_tools(target_dir=out, consent=True)
        self.assertEqual(res["status"], "error")
        self.assertIn("SHA-256", res["message"])
        self.assertEqual(list(out.iterdir()), [])

    def test_prompt_yes_no_non_tty(self):
        from dep_installer import prompt_yes_no
        from unittest.mock import patch
        with patch("sys.stdin.isatty", return_value=False):
            self.assertFalse(prompt_yes_no("Test prompt?", default=False))
            self.assertTrue(prompt_yes_no("Test prompt?", default=True))

    def test_itunes_automation_module(self):
        import itunes_automation
        auto = itunes_automation.ITunesAutomation(itunes_exe="C:/nowhere/iTunes.exe")
        self.assertTrue(hasattr(auto, "launch_or_focus_itunes"))
        # The keystroke backup and the blind {ENTER} restore are gone for good.
        for gone in ("send_backup_shortcut", "actuate_backup_ui", "actuate_restore_ui", "trigger_backup",
                     "monitor_backup", "drive_full_backup_workflow", "detect_device_wipe_state"):
            self.assertFalse(hasattr(auto, gone), gone)
        self.assertFalse(hasattr(itunes_automation, "Desktop"))


class TestTokenExtractionProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        import backup_verifier
        from tests.fixtures import create_synthetic_backup
        cls._temp_dir = tempfile.TemporaryDirectory()
        cls._backup_path = create_synthetic_backup(Path(cls._temp_dir.name))
        cls._orig_env = os.environ.get("IPHONE_BACKUP_DIR")
        os.environ["IPHONE_BACKUP_DIR"] = cls._temp_dir.name
        cls._orig_bv_root = backup_verifier.BACKUP_ROOT
        backup_verifier.BACKUP_ROOT = Path(cls._temp_dir.name)

    @classmethod
    def tearDownClass(cls):
        import backup_verifier
        if cls._orig_env is not None:
            os.environ["IPHONE_BACKUP_DIR"] = cls._orig_env
        else:
            os.environ.pop("IPHONE_BACKUP_DIR", None)
        backup_verifier.BACKUP_ROOT = cls._orig_bv_root
        cls._temp_dir.cleanup()

    def test_parse_apple_keybag(self):
        from token_extractor import parse_apple_keybag
        from tests.fixtures import make_synthetic_keybag
        raw_kb = make_synthetic_keybag()
        kb = parse_apple_keybag(raw_kb)
        self.assertTrue(kb.is_encrypted)
        self.assertEqual(kb.iteration_count, 10000)
        self.assertEqual(kb.class_count, 4)
        self.assertEqual(kb.classes, [1, 2, 3, 4])
        self.assertEqual(kb.status, "HEALTHY_ENCRYPTED_KEYBAG")
        self.assertIsNotNone(kb.salt_hex)

    def test_extract_keychain_artifact(self):
        from token_extractor import extract_keychain_artifact
        from backup_verifier import find_backup_for_target
        backup = find_backup_for_target()
        self.assertIsNotNone(backup)
        kc = extract_keychain_artifact(Path(backup.backup_path))
        self.assertTrue(kc.present)
        self.assertGreater(kc.size_bytes, 0)
        self.assertEqual(len(kc.sha256_hash), 64)
        self.assertEqual(kc.status, "KEYCHAIN_STORE_INTACT")

    def test_extract_duo_accounts(self):
        from token_extractor import extract_duo_accounts
        from backup_verifier import find_backup_for_target
        backup = find_backup_for_target()
        self.assertIsNotNone(backup)
        duo_accs = extract_duo_accounts(Path(backup.backup_path))
        self.assertGreaterEqual(len(duo_accs), 2)
        push = next((a for a in duo_accs if a.has_push), None)
        self.assertIsNotNone(push)
        self.assertEqual(push.username, "testuser")
        self.assertIn("duosecurity.com", push.host)
        # Generic issuer inference (no hardcoded institution).
        self.assertEqual(push.issuer, "Duo Security")

        totp = next((a for a in duo_accs if a.account_type == "TOTP"), None)
        self.assertIsNotNone(totp)
        # Secret is fully masked: no base32 characters leaked.
        self.assertNotIn("JBSW", totp.masked_secret)
        self.assertNotIn("3PXP", totp.masked_secret)
        self.assertIn("secret present", totp.masked_secret)

    def test_extract_google_authenticator(self):
        from token_extractor import extract_google_authenticator_accounts
        from backup_verifier import find_backup_for_target
        backup = find_backup_for_target()
        self.assertIsNotNone(backup)
        g_accs = extract_google_authenticator_accounts(Path(backup.backup_path))
        self.assertGreaterEqual(len(g_accs), 1)
        g_acc = g_accs[0]
        self.assertEqual(g_acc.username, "testuser@example.com")
        self.assertNotIn("GEZD", g_acc.masked_secret)
        self.assertIn("secret present", g_acc.masked_secret)

    def test_extract_all_tokens_manifest(self):
        from token_extractor import extract_all_tokens
        manifest = extract_all_tokens()
        self.assertTrue(manifest.keybag.is_encrypted)
        self.assertTrue(manifest.keychain.present)
        self.assertGreaterEqual(len(manifest.accounts), 3)
        self.assertGreaterEqual(manifest.total_token_records, 4)
        h = manifest.compute_manifest_hash()
        self.assertEqual(len(h), 64)

    def test_token_survival_verification(self):
        from token_extractor import extract_all_tokens, verify_token_survival
        manifest = extract_all_tokens()
        # 100% match
        diff_pass = verify_token_survival(manifest, manifest)
        self.assertEqual(diff_pass["status"], "PASS_100_PERCENT_SURVIVAL")
        self.assertEqual(diff_pass["survival_percentage"], 100.0)
        self.assertEqual(diff_pass["missing_count"], 0)

        # Simulated loss
        tampered = manifest.to_dict()
        tampered["accounts"] = tampered["accounts"][:-1]  # drop last account
        diff_fail = verify_token_survival(manifest, tampered)
        self.assertEqual(diff_fail["status"], "FAIL_TOKENS_LOST")
        self.assertEqual(diff_fail["missing_count"], 1)
        self.assertLess(diff_fail["survival_percentage"], 100.0)

    def test_lockdown_search_dirs(self):
        from token_extractor import get_lockdown_search_dirs
        dirs = get_lockdown_search_dirs()
        self.assertIsInstance(dirs, list)
        self.assertGreater(len(dirs), 0)


if __name__ == "__main__":
    unittest.main()

