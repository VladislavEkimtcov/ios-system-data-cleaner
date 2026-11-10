"""
Coverage for the hardening work: the clone helper, no-plaintext password storage, the web
UI's token/Host guards, the MCP stdio round trip, the tokens-verify
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

    def test_stale_clone_is_refreshed_not_reused(self):
        import plistlib
        from datetime import datetime, timedelta
        clone = common.make_prewipe_clone(self.backup, verbose=False)
        # Make the clone look like an older backup, and mark it so we can tell it was replaced.
        old = datetime.now() - timedelta(hours=4)
        with (clone / "Status.plist").open("wb") as f:
            plistlib.dump({"Date": old, "SnapshotState": "finished"}, f)
        (clone / "STALE_MARKER").write_text("x")
        with (self.backup / "Status.plist").open("wb") as f:
            plistlib.dump({"Date": datetime.now(), "SnapshotState": "finished"}, f)
        clone2 = common.make_prewipe_clone(self.backup, verbose=False)
        self.assertEqual(clone, clone2)
        self.assertFalse((clone2 / "STALE_MARKER").exists(), "stale clone must be replaced")
        self.assertGreater(common._backup_date(clone2), old)
        # Nothing stray left behind next to the backups.
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.name.startswith(".")), [])

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
        self.assertEqual(len(by_id[2]["result"]["tools"]), 15)
        # tools/call returned valid JSON-RPC (not corrupted by the tool's prints).
        self.assertIn("content", by_id[3]["result"])
        payload = json.loads(by_id[3]["result"]["content"][0]["text"])
        self.assertIn("verdict", payload)


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


class TestReviveSequence(unittest.TestCase):
    """revive = backup engine -> (verify, clone, list inside it) -> restore-route gate -> erase wording."""

    def _run(self, engine_rc=0, route=None, ready=True, udids=("u",), use_existing=False):
        import iphone_manager
        import restore_gates
        from unittest.mock import patch, MagicMock
        args = MagicMock(use_existing=use_existing)
        diag = {"ready": ready, "state": "ready" if ready else "no_usb_device", "message": "m", "udids": list(udids)}
        route = route if route is not None else {"kind": "finder", "exe": None, "version": None}
        with patch("common.diagnose_connection", return_value=diag), \
                patch.object(iphone_manager, "_run_backup_engine", return_value=engine_rc) as engine, \
                patch.object(restore_gates, "restore_route", return_value=route), \
                patch("token_extractor.extract_all_tokens", side_effect=RuntimeError("skip")):
            rc = iphone_manager.cmd_revive(args)
        return rc, engine

    def test_failed_backup_blocks_the_erase(self):
        rc, engine = self._run(engine_rc=1)
        self.assertEqual(rc, 1)
        engine.assert_called_once_with("u")

    def test_good_backup_and_restore_app_means_ready(self):
        self.assertEqual(self._run()[0], 0)

    def test_no_restore_app_backs_up_but_says_do_not_erase(self):
        rc, engine = self._run(route={"kind": None, "exe": None, "version": None})
        self.assertEqual(rc, 2)
        engine.assert_called_once()

    def test_no_phone_or_two_phones_stop_before_the_backup(self):
        rc, engine = self._run(ready=False)
        self.assertEqual(rc, 1)
        engine.assert_not_called()
        rc, engine = self._run(udids=("a", "b"))
        self.assertEqual(rc, 1)
        engine.assert_not_called()

    def test_existing_backup_must_pass_the_gate(self):
        import iphone_manager
        from unittest.mock import patch
        with patch.object(iphone_manager, "_existing_backup_gate", return_value=None) as gate:
            rc, engine = self._run(use_existing=True)
        self.assertEqual(rc, 1)
        gate.assert_called_once_with("u")
        engine.assert_not_called()

    def test_closing_message_never_says_restore_backup_button(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(self._run()[0], 0)
        out = buf.getvalue()
        self.assertNotIn("Restore Backup...", out)
        self.assertIn("Skip Backup", out)
        self.assertIn("Hello", out)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


if __name__ == "__main__":
    unittest.main()
