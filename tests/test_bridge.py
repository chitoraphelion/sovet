import json
import sqlite3
import signal
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from sovet.bridge import hook, inbox, complete_job
from sovet.core import Project
from sovet.integrations import metadata
from sovet.mcp import call, available_tools
from sovet.native import native_command, prepare_native, run_native


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sovet native ")
        self.p = Project.init(Path(self.temp.name), brief="Author requirements")

    def tearDown(self):
        self.temp.cleanup()

    def test_hooks_bind_and_publish_real_native_turn(self):
        result = hook(self.p, "claude", "SessionStart", {"session_id": "native-session"})
        self.assertIn("Author requirements", result["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.p.agent("claude")["session"], "native-session")
        hook(self.p, "claude", "UserPromptSubmit", {"session_id": "native-session", "prompt": "Author changed the goal"})
        hook(self.p, "claude", "Stop", {"session_id": "native-session", "last_assistant_message": "Done"})
        hook(self.p, "claude", "Stop", {"session_id": "native-session", "last_assistant_message": "Done"})
        answers = [e for e in self.p.events() if e["kind"] == "answer"]
        self.assertEqual(len(answers), 1)
        self.assertTrue(any(e["body"] == "Author changed the goal" for e in self.p.events()))

    def test_peer_job_runs_in_foreground_native_inbox(self):
        sent = call(self.p, "claude", 0, False, "ask_peer", {"prompt": "review latest proof"}, native=True)
        self.assertIn("next native turn", sent["delivery"])
        response = hook(self.p, "codex", "UserPromptSubmit", {"session_id": "codex-session", "prompt": "check inbox"})
        self.assertIn("review latest proof", response["hookSpecificOutput"]["additionalContext"])
        hook(self.p, "codex", "Stop", {"session_id": "codex-session", "last_assistant_message": "Fix lemma 2"})
        receipt = inbox(self.p, "claude")["job"]
        self.assertEqual(receipt["origin"], "peer-result")
        self.assertIn("Fix lemma 2", receipt["prompt"])
        with self.assertRaises(ValueError):
            call(self.p, "claude", 0, False, "ask_peer", {"prompt": "again"}, native=True)
        complete_job(self.p, "claude", "Updated proof", job_id=receipt["id"])
        self.assertFalse([j for j in self.p.jobs() if j["status"] in ("running", "pending")])

    def test_stop_continuation_is_bounded(self):
        self.p.enqueue("claude", "pending author comment")
        output = hook(self.p, "claude", "Stop", {"session_id": "s", "last_assistant_message": "Initial answer"})
        self.assertEqual(output["decision"], "block")
        self.p.enqueue("claude", "second task")
        output = hook(self.p, "claude", "Stop", {"session_id": "s", "last_assistant_message": "First task done", "stop_hook_active": True})
        self.assertNotIn("decision", output)
        self.assertTrue(any(j["status"] == "pending" for j in self.p.jobs()))

    def test_readonly_peer_task_cannot_write_artifact(self):
        self.p.enqueue("claude", "review only", read_only=True)
        inbox(self.p, "claude")
        with self.assertRaises(ValueError):
            call(self.p, "claude", 0, False, "artifact_write", {"path": "proof.tex", "text": "bad", "expected_sha": "x"}, native=True)

    def test_old_session_and_subagent_hooks_cannot_steal_role(self):
        metadata(self.p, "native:claude:expected_session", "new")
        hook(self.p, "claude", "SessionStart", {"session_id": "old"})
        self.assertIsNone(self.p.agent("claude")["session"])
        hook(self.p, "claude", "SessionStart", {"session_id": "new", "agent_id": "child"})
        self.assertIsNone(self.p.agent("claude")["session"])

    def test_same_harness_can_clear_but_old_harness_cannot_bind(self):
        metadata(self.p, "native:claude:instance", "current")
        hook(self.p, "claude", "SessionStart", {"session_id": "first"}, instance="current")
        hook(self.p, "claude", "SessionStart", {"session_id": "second"}, instance="current")
        self.assertEqual(self.p.agent("claude")["session"], "second")
        hook(self.p, "claude", "SessionStart", {"session_id": "stale"}, instance="old")
        self.assertEqual(self.p.agent("claude")["session"], "second")

    def test_finish_once_and_role_validation(self):
        self.p.enqueue("codex", "review", reply_to="claude")
        job = self.p.claim("codex")
        self.assertTrue(self.p.finish(job, "done"))
        self.assertFalse(self.p.finish(job, "duplicate"))
        self.assertEqual(len([j for j in self.p.jobs() if j["role"] == "claude"]), 1)
        with self.assertRaises(ValueError):
            complete_job(self.p, "claude", "not my job", job_id=job["id"])

    def test_review_completion_and_return_message_are_atomic(self):
        self.p.enqueue("codex", "review", reply_to="claude")
        job = self.p.claim("codex")
        with self.p.db() as db:
            db.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON jobs WHEN NEW.origin='peer-result' "
                       "BEGIN SELECT RAISE(ABORT, 'simulated queue failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.p.finish(job, "review result")
        self.assertEqual(self.p.jobs()[0]["status"], "running")
        self.assertFalse([e for e in self.p.events() if e["kind"] == "answer"])
        with self.p.db() as db:
            db.execute("DROP TRIGGER fail_receipt")
        self.assertTrue(self.p.finish(job, "review result"))
        self.assertEqual(self.p.claim("claude")["origin"], "peer-result")

    def test_commands_are_genuine_interactive_harnesses(self):
        with patch("sovet.native.shutil.which", side_effect=lambda role: "/bin/" + role):
            for role in ("codex", "claude"):
                args = native_command(self.p, role)
                self.assertEqual(args[0], "/bin/" + role)
                self.assertNotIn("exec", args)
                self.assertNotIn("-p", args)
                self.assertNotIn("--output-format", args)
                self.assertNotIn("--dangerously-bypass-hook-trust", args)
                self.assertNotIn("--dangerously-skip-permissions", args)
                if role == "codex":
                    for option in [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "-c"]:
                        tomllib.loads(option)  # exact CLI override values must be valid TOML
                else:
                    self.assertIn("--session-id", args)
        cfg = prepare_native(self.p, "claude")
        self.assertIn("SessionStart", json.loads(cfg["settings"].read_text())["hooks"])
        self.assertIn("--native", cfg["mcp"]["args"])
        self.assertIn("inbox", [t["name"] for t in available_tools(self.p, native=True)])

    def test_migration_keeps_legacy_history_without_resuming_batch_transcript(self):
        self.p.session("claude", "old-batch-session")
        with patch("sovet.native.shutil.which", return_value="/bin/claude"):
            args = native_command(self.p, "claude")
        self.assertIn("--session-id", args)
        self.assertNotIn("--resume", args)
        self.assertEqual(metadata(self.p, "legacy:claude:session"), "old-batch-session")
        metadata(self.p, "native:claude:session", "real-native-session")
        with patch("sovet.native.shutil.which", return_value="/bin/claude"):
            args = native_command(self.p, "claude")
        self.assertEqual(args[args.index("--resume") + 1], "real-native-session")

    def test_launcher_hup_terminates_child_before_releasing_role(self):
        from unittest.mock import Mock
        child = Mock()
        child.pid = 12345
        child.poll.return_value = None
        original = {sig: signal.getsignal(sig) for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)}
        def wait(*_args, **_kwargs):
            if not child.terminate.called:
                signal.getsignal(signal.SIGHUP)(signal.SIGHUP, None)
            return 0
        child.wait.side_effect = wait
        with patch("sovet.native.subprocess.Popen", return_value=child), \
             patch("sovet.native.shutil.which", return_value="/fake/claude"), \
             patch("sovet.native.register", return_value=None), self.assertRaises(SystemExit):
            run_native(self.p, "claude")
        child.terminate.assert_called_once()
        self.assertGreaterEqual(child.wait.call_count, 2)
        for sig, handler in original.items():
            self.assertEqual(signal.getsignal(sig), handler)


if __name__ == "__main__":
    unittest.main()
