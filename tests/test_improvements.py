"""
Coverage for the hardening work: the clone helper, no-plaintext password storage, the web
UI's token/Host guards, the MCP stdio round trip, stage-finder refusals, the tokens-verify
same-backup warning, the fail-closed proof gate, and year-derived variants.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("IOS_CLEANER_STATE_DIR", tempfile.mkdtemp(prefix="ioscl_test_state_"))

import common  # noqa: E402
from tests.fixtures import create_synthetic_backup  # noqa: E402


class TestCloneHelper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backup = create_synthetic_backup(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_clone_creates_prewipe_and_is_idempotent(self):
        clone = common.make_prewipe_clone(self.backup, verbose=False)
        self.assertTrue(clone.name.endswith("-prewipe"))
        self.assertTrue((clone / "Manifest.db").is_file())
        # Running again returns the same clone and never nests a copy inside it.
        clone2 = common.make_prewipe_clone(self.backup, verbose=False)
        self.assertEqual(clone, clone2)
        self.assertFalse((clone / self.backup.name).exists(), "clone must not nest the backup inside itself")

    def test_clone_refuses_incomplete_backup(self):
        broken = self.root / "broken"
        broken.mkdir()
        (broken / "Info.plist").write_bytes(b"not really")
        with self.assertRaises(common.CloneError):
            common.make_prewipe_clone(broken, verbose=False)


class TestNoPlaintextPasswords(unittest.TestCase):
    def test_save_attempt_writes_no_plaintext(self):
        import password_tracker
        secret = "SuperSecretValue123!"
        password_tracker.save_attempt("UDID-TEST", secret, success=False, duration_sec=0.1, keybag_fp="abc123")
        raw = password_tracker.HISTORY_JSON.read_text(encoding="utf-8")
        self.assertNotIn(secret, raw, "plaintext password must never hit disk")
        self.assertIn("pw_hash", raw)

    def test_variants_use_current_year(self):
        import password_tracker
        variants = password_tracker.generate_variants("base")
        year = datetime.now().year
        self.assertIn(f"base{year}", variants)
        self.assertNotIn("base2024", variants if year != 2024 else ["sentinel"])


class TestWebUIGuards(unittest.TestCase):
    def setUp(self):
        import password_tracker
        self.pt = password_tracker
        self.port = _free_port()
        import socketserver
        socketserver.TCPServer.allow_reuse_address = True
        self.token = "test-token-abc"
        password_tracker.PasswordServerHandler.target_udid = "UDID-TEST"
        password_tracker.PasswordServerHandler.session_token = self.token
        password_tracker.PasswordServerHandler.allowed_hosts = (f"127.0.0.1:{self.port}", f"localhost:{self.port}")
        self.httpd = socketserver.TCPServer(("127.0.0.1", self.port), password_tracker.PasswordServerHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _get(self, path, host=None, token=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url)
        req.add_header("Host", host or f"127.0.0.1:{self.port}")
        if token:
            req.add_header("X-Session-Token", token)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_api_requires_token(self):
        code, _ = self._get("/api/history")  # no token
        self.assertEqual(code, 403)
        code, _ = self._get("/api/history", token=self.token)
        self.assertEqual(code, 200)

    def test_bad_host_rejected(self):
        code, _ = self._get("/api/history", host="evil.example.com", token=self.token)
        self.assertEqual(code, 403)

    def test_index_served(self):
        code, body = self._get("/")
        self.assertEqual(code, 200)
        self.assertIn(b"Password Prover", body)


class TestMCPStdioRoundTrip(unittest.TestCase):
    def test_initialize_list_and_call(self):
        tmp = tempfile.mkdtemp(prefix="mcp_backup_")
        create_synthetic_backup(Path(tmp))
        env = {**os.environ, "IPHONE_BACKUP_DIR": tmp}
        proc = subprocess.Popen(
            [sys.executable, str(REPO_ROOT / "ios_mcp_server.py"), "--stdio-fallback"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        reqs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            # A tool whose code prints (verify_backup -> inspect_backup): stdout must stay clean.
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "verify_backup", "arguments": {}}},
        ]
        out, _ = proc.communicate("\n".join(json.dumps(r) for r in reqs) + "\n", timeout=60)
        lines = [json.loads(l) for l in out.splitlines() if l.strip()]
        by_id = {m["id"]: m for m in lines}
        self.assertEqual(by_id[1]["result"]["serverInfo"]["name"], "ios-system-data-cleaner")
        self.assertEqual(len(by_id[2]["result"]["tools"]), 14)
        # tools/call returned valid JSON-RPC (not corrupted by the tool's prints).
        self.assertIn("content", by_id[3]["result"])
        payload = json.loads(by_id[3]["result"]["content"][0]["text"])
        self.assertIn("verdict", payload)


class TestStageFinderRefusal(unittest.TestCase):
    def test_refuses_when_not_welcome_view(self):
        import restorer
        from unittest.mock import patch
        with patch.object(restorer, "detect_finder_setup_view",
                          return_value={"found": True, "has_welcome": False, "selected_backup": ""}):
            res = restorer.stage_finder_restore_selection()
        self.assertFalse(res["success"])
        self.assertEqual(res["reason"], "not_welcome_view")

    def test_refuses_on_backup_mismatch(self):
        import restorer
        from unittest.mock import patch
        with patch.object(restorer, "detect_finder_setup_view",
                          return_value={"found": True, "has_welcome": True, "selected_backup": "Someone Else's iPhone"}):
            res = restorer.stage_finder_restore_selection(expected_backup_name="My iPhone")
        self.assertFalse(res["success"])
        self.assertEqual(res["reason"], "backup_mismatch")


class TestAutoConfirmRequiresClone(unittest.TestCase):
    def test_auto_confirm_requires_prewipe_clone(self):
        import restorer
        from unittest.mock import patch
        tmp = tempfile.mkdtemp(prefix="restore_backup_")
        backup = create_synthetic_backup(Path(tmp))
        udid = backup.name
        with patch.object(restorer, "connected_udids", return_value=[udid]), \
             patch.object(restorer, "get_connected_udids", return_value=[udid]), \
             patch.object(restorer, "query_device_info", return_value={"DeviceName": "x"}), \
             patch.object(restorer, "backup_root", return_value=Path(tmp)):
            # No -prewipe clone present -> safety halt.
            with self.assertRaises(restorer.RestorerSafetyError):
                restorer.auto_confirm_finder_restore_sheet(expected_udid=udid)


class TestTokensVerifySameBackup(unittest.TestCase):
    def test_same_backup_warns(self):
        import token_extractor
        pre = {
            "last_backup_date": "2026-01-01T00:00:00",
            "accounts": [{"fingerprint": "f1", "app_name": "A", "account_name": "a", "account_type": "TOTP"}],
            "keychain": {"present": True, "size_bytes": 10},
            "lockdown": {"present": True},
        }
        post = dict(pre)  # identical -> same backup
        diff = token_extractor.verify_token_survival(pre, post)
        self.assertTrue(any("SAME backup" in w or "same" in w.lower() for w in diff.get("warnings", [])))

    def test_accounts_not_inspectable(self):
        import token_extractor
        pre = {"last_backup_date": "x", "accounts": [], "keychain": {"present": True, "size_bytes": 10}, "lockdown": {"present": True}}
        post = {"last_backup_date": "y", "accounts": [], "keychain": {"present": True, "size_bytes": 10}, "lockdown": {"present": True}}
        diff = token_extractor.verify_token_survival(pre, post)
        self.assertEqual(diff["status"], "ACCOUNTS_NOT_INSPECTABLE")
        self.assertIsNone(diff["survival_percentage"])


class TestProofGateFailsClosed(unittest.TestCase):
    def test_no_proving_tool_blocks(self):
        import backup_verifier
        from unittest.mock import patch
        tmp = tempfile.mkdtemp(prefix="proof_")
        backup = create_synthetic_backup(Path(tmp))  # encrypted fixture
        import password_tracker
        with patch.object(password_tracker, "proof_tool_available", return_value=False):
            ok, msg = backup_verifier.check_proof_of_password(Path(backup), candidate_password=None, interactive=False)
        self.assertFalse(ok)
        self.assertIn("No password-proving tool", msg)


class TestITunesDriveWorkflowNoCrash(unittest.TestCase):
    def test_drive_workflow_handles_bool_verdict(self):
        # Regression: drive_full_backup_workflow used to read .verdict/.reasons off the bool
        # returned by inspect_backup and call it without max_age_hours, crashing every run.
        import itunes_automation
        from unittest.mock import patch
        drv = itunes_automation.ITunesAutomation()
        fake_prog = itunes_automation.BackupProgress(
            udid="u", backup_path="/tmp/nope", exists=True, size_bytes=1, size_gb=0.0,
            file_count=1, state="finished", is_encrypted=True, last_updated=0.0, elapsed_seconds=0.0)
        with patch.object(drv, "trigger_backup", return_value={"process": None}), \
             patch.object(drv, "monitor_backup", return_value=fake_prog), \
             patch.object(itunes_automation, "inspect_backup", return_value=True):
            res = drv.drive_full_backup_workflow("u")
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["verdict"], "PASS")


class TestReviveStopsOnFail(unittest.TestCase):
    def test_revive_returns_nonzero_when_gate_fails(self):
        import iphone_manager
        from unittest.mock import patch, MagicMock
        args = MagicMock(auto_drive=False)
        fake_backup = MagicMock(backup_path="/tmp/b", udid="u", is_encrypted=True, size_gb=1.0, folder_name="u")
        with patch("iphone_manager.find_backup_for_target", return_value=fake_backup), \
             patch("storage_monitor.run_ideviceinfo", return_value={"UniqueDeviceID": "u", "DeviceName": "d"}), \
             patch("win_tools.detect_apple_pnp_devices", return_value=[]), \
             patch("itunes_env.check_itunes_installation"), \
             patch("backup_verifier.inspect_backup", return_value=False), \
             patch("shutil.disk_usage", return_value=type("U", (), {"free": 999 * 10**9, "total": 10**12, "used": 0})()):
            rc = iphone_manager.cmd_revive(args)
        self.assertEqual(rc, 1)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


if __name__ == "__main__":
    unittest.main()
