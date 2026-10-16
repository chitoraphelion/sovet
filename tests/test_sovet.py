import concurrent.futures
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sovet.core import Project, digest
from sovet.integrations import ARIS_SKILLS, install_aris, scan_files, sync_comments, reply, sync_aris_profile
from sovet.mcp import call, dispatch, available_tools
from sovet.research import run_lean, verification_status
from sovet.runtime import NativeRun, command
from sovet.cli import configure, parser

ROOT = Path(__file__).resolve().parents[1]


class ProjectTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sovet-test-")
        self.root = Path(self.temp.name)
        self.p = Project.init(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_defaults_and_idempotent_init(self):
        self.assertEqual(self.p.config["writer"], "claude")
        self.p.memory_write("brief", "author requirement")
        Project.init(self.root, brief="must not replace")
        self.assertIn("author requirement", self.p.context("claude")[0])

    def test_queue_concurrent_claims_exactly_once(self):
        ids = [self.p.enqueue("claude", str(n)) for n in range(40)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            jobs = list(pool.map(lambda _: self.p.claim("claude"), ids))
        self.assertEqual(sorted(j["id"] for j in jobs), ids)
        self.assertIsNone(self.p.claim("claude"))

    def test_peer_result_resumes_writer_and_limits_cycles(self):
        result = call(self.p, "claude", 0, False, "ask_peer", {"prompt": "review"})
        review = self.p.claim("codex")
        self.assertEqual(result["job_id"], review["id"])
        self.assertTrue(review["read_only"])
        self.p.finish(review, "Fix line 12")
        receipt = self.p.claim("claude")
        self.assertFalse(receipt["read_only"])
        self.assertIn("Fix line 12", receipt["prompt"])
        with self.assertRaises(ValueError):
            call(self.p, "claude", receipt["depth"], False, "ask_peer", {"prompt": "again"})

    def test_unseen_events_not_lost_at_context_limit(self):
        for n in range(75):
            self.p.emit("user", "note", f"note-{n}")
        text, cursor = self.p.context("codex")
        self.assertIn("note-0", text)
        self.assertNotIn("note-74", text)
        self.p.acknowledge("codex", cursor)
        self.assertIn("note-74", self.p.context("codex")[0])

    def test_author_edit_conflict_and_readonly(self):
        file = self.root / "paper.tex"
        file.write_text("first")
        old = self.p.read_artifact("paper.tex")
        file.write_text("author revision")
        with self.assertRaisesRegex(ValueError, "Конфликт"):
            self.p.write_artifact("paper.tex", "stale", old["sha"], "claude")
        with self.assertRaises(ValueError):
            self.p.write_artifact("paper.tex", "reviewer edit", digest(file.read_bytes()), "codex")
        self.assertEqual(file.read_text(), "author revision")
        with self.assertRaises(ValueError):
            self.p.read_artifact("../outside")
        with self.assertRaises(ValueError):
            self.p.write_artifact(".git/config", "bad", digest(b""))

    def test_file_watch_diff_and_deletion(self):
        file = self.root / "main.tex"
        file.write_text("old\n")
        self.assertEqual(scan_files(self.p), 0)
        file.write_text("author\n")
        self.assertEqual(scan_files(self.p), 1)
        self.assertIn("+author", self.p.events()[-1]["body"])
        self.assertEqual(scan_files(self.p), 0)
        file.unlink()
        self.assertEqual(scan_files(self.p), 1)

    def test_watch_detects_changes_beyond_diff_preview(self):
        file = self.root / "long-proof.lean"
        file.write_text("x" * 300_000 + "old")
        scan_files(self.p)
        file.write_text("x" * 300_000 + "new")
        self.assertEqual(scan_files(self.p), 1)
        self.assertIn("перечитайте файл", self.p.events()[-1]["body"])
        self.assertEqual(scan_files(self.p), 0)

    def test_profiles_expose_only_relevant_tools(self):
        names = lambda: {t["name"] for t in available_tools(self.p)}
        self.assertNotIn("lean_check", names())
        self.assertNotIn("chitalka_list", names())
        self.p.config["profile"] = "math-research"
        self.assertIn("lean_check", names())
        self.assertIn("chitalka_list", names())
        error = dispatch(self.p, "codex", 0, True, {"id": 2, "method": "tools/call", "params": {
            "name": "artifact_write", "arguments": {"path": "a.txt", "text": "x", "expected_sha": digest(b"")}}})
        self.assertTrue(error["result"]["isError"])

    def test_model_overrides_and_programming_disable_reader(self):
        self.p.config["reader"] = True
        configure(self.p, parser().parse_args(["config", "--programming", "--codex-model", "another-model",
                                               "--codex-effort", "xhigh", "--claude-effort", "medium"]))
        cfg = Project(self.root).config
        self.assertEqual(cfg["models"]["codex"], "another-model")
        self.assertEqual(cfg["effort"]["codex"], "xhigh")
        self.assertEqual(cfg["effort"]["claude"], "medium")
        self.assertFalse(cfg["reader"])

    @unittest.skipUnless(shutil.which("chitalka"), "native chitalka CLI not installed")
    def test_native_chitalka_author_revision_no_reply_loop(self):
        folder = self.root / "kommentarii"
        folder.mkdir()
        file = folder / "kommentarii.json"
        item = {"id": "abc123", "status": "open", "quote": "x", "comment": "fix", "thread": [], "source": {"file": "main.tex", "line": 8}}
        file.write_text(json.dumps([item]))
        self.assertEqual(sync_comments(self.p), 1)
        self.assertEqual(sync_comments(self.p), 0)
        self.assertEqual(self.p.jobs()[0]["role"], "claude")
        reply(self.p, "abc123", "fixed", done=True)
        self.assertEqual(sync_comments(self.p), 0)
        item = json.loads(file.read_text())[0]
        item["thread"].append({"who": "author", "text": "one more change", "at": "now"})
        item["status"] = "open"
        file.write_text(json.dumps([item]))
        self.assertEqual(sync_comments(self.p), 1)
        self.assertEqual(len(self.p.jobs()), 2)

    def test_mcp_stdio_handshake(self):
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        result = subprocess.run([sys.executable, str(ROOT / "bin/sovet"), "-C", str(self.root), "mcp", "--role", "codex"],
                                input="\n".join(map(json.dumps, messages)) + "\n", text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("ask_peer", [t["name"] for t in rows[1]["result"]["tools"]])

    def fake_cli(self):
        folder = self.root / "fake-bin"
        folder.mkdir(exist_ok=True)
        script = '''import json, os, sys, time
role = os.path.basename(sys.argv[0])
prompt = sys.stdin.read()
if os.getenv("FAKE_HANG"):
    time.sleep(30)
if os.getenv("FAKE_FAIL"):
    print(json.dumps({"type":"result", "is_error":True,"result":"test failure"}))
    sys.exit(1)
if role == "codex":
    print(json.dumps({"type":"thread.started", "thread_id":"codex-persistent"}))
    print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"codex checked"}}))
    print(json.dumps({"type":"turn.completed"}))
else:
    print(json.dumps({"type":"system","session_id":"claude-persistent"}))
    print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":"claude implemented"}]}}))
    print(json.dumps({"type":"result","session_id":"claude-persistent","result":"claude implemented","is_error":False}))
'''
        for role in ("codex", "claude"):
            file = folder / role
            file.write_text(f"#!{sys.executable}\n" + script)
            file.chmod(0o755)
        return {"PATH": str(folder) + os.pathsep + os.environ["PATH"]}

    def test_native_adapters_session_resume_and_permissions(self):
        with patch.dict(os.environ, self.fake_cli()):
            for role in ("codex", "claude"):
                self.p.enqueue(role, "work")
                job = self.p.claim(role)
                self.assertIn(role, NativeRun().run(self.p, role, job))
                self.assertEqual(self.p.agent(role)["session"], role + "-persistent")
                cmd, readonly = command(self.p, role, job, self.p.agent(role)["session"])
                self.assertIn("--resume" if role == "claude" else "resume", cmd)
                self.assertEqual(readonly, role == "codex")
                self.assertNotIn("--dangerously-skip-permissions", cmd)

    def test_native_failure_does_not_ack_context(self):
        env = self.fake_cli() | {"FAKE_FAIL": "1"}
        self.p.enqueue("claude", "work")
        with patch.dict(os.environ, env), self.assertRaisesRegex(RuntimeError, "test failure"):
            NativeRun().run(self.p, "claude", self.p.claim("claude"))
        self.assertEqual(self.p.agent("claude")["cursor"], 0)

    def test_native_cancel_stops_process_without_ack(self):
        env = self.fake_cli() | {"FAKE_HANG": "1"}
        self.p.enqueue("claude", "work")
        job = self.p.claim("claude")
        run = NativeRun()
        errors = []
        def work():
            try:
                run.run(self.p, "claude", job)
            except RuntimeError as e:
                errors.append(str(e))
        with patch.dict(os.environ, env):
            thread = threading.Thread(target=work)
            thread.start()
            deadline = time.monotonic() + 5
            while run.process is None and time.monotonic() < deadline:
                time.sleep(0.01)
            run.cancel()
            thread.join(timeout=8)
            self.assertFalse(thread.is_alive())
            self.assertTrue(errors)
            self.assertEqual(self.p.agent("claude")["cursor"], 0)

    def test_writer_lease_can_be_cancelled_before_model_starts(self):
        run = NativeRun()
        errors = []
        with (self.p.team / "writer.lock").open("a+") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX)
            def work():
                try:
                    run.run(self.p, "claude", {"id": 1, "prompt": "work"})
                except RuntimeError as e:
                    errors.append(str(e))
            thread = threading.Thread(target=work)
            thread.start()
            time.sleep(0.05)
            run.cancel()
            thread.join(timeout=2)
        self.assertTrue(errors)
        self.assertIsNone(run.process)

    def test_aris_profile_switch_preserves_user_skills(self):
        repo = self.root / ".aris-source"
        for name in ARIS_SKILLS:
            folder = repo / "skills" / name
            folder.mkdir(parents=True)
            (folder / "SKILL.md").write_text("skill")
        wiki = self.root / "research-wiki"
        wiki.mkdir()
        (wiki / "index.md").write_text("existing wiki")
        user_skill = self.root / ".claude/skills/user-skill"
        user_skill.mkdir(parents=True)
        self.p.config["profile"] = "math-research"
        with patch("sovet.integrations.subprocess.check_output", return_value="abc123\n"):
            install_aris(self.p, repo)
        self.assertTrue((self.root / ".agents/skills/aris-research-wiki").is_symlink())
        self.p.config["profile"] = "programming"
        sync_aris_profile(self.p)
        self.assertFalse((self.root / ".agents/skills/aris-research-wiki").exists())
        self.assertTrue(user_skill.is_dir())
        self.assertEqual((wiki / "index.md").read_text(), "existing wiki")

    def test_lean_report_becomes_stale(self):
        root = self.root / "formal/longtails"
        root.mkdir(parents=True)
        (root / "lean-toolchain").write_text("leanprover/lean4:v4.34.1")
        (root / "Test.lean").write_text("example : 1 = 1 := rfl\n")
        process = unittest.mock.Mock()
        process.communicate.return_value = ("ok", None)
        process.returncode = 0
        with patch("sovet.research.subprocess.Popen", return_value=process):
            report = run_lean(self.p, "check", "Test.lean")
        self.assertTrue(report["verified"])
        self.assertTrue(verification_status(self.p)["current"])
        (root / "Test.lean").write_text("example : 1 = 2 := rfl\n")
        self.assertFalse(verification_status(self.p)["verified"])


if __name__ == "__main__":
    unittest.main()
