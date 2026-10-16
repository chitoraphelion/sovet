import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sovet import processes
from sovet.completion import complete
from sovet.completion_support import install, script
from sovet.sessions import list_sessions, stop_sessions


def proc(pid=12345, command="python /tmp/sovet -C /tmp/paper watch", started="Fri Oct 16 18:00:00 2026", ppid=1):
    return processes.Process(pid, ppid, os.getuid(), "S", started, command)


class ProcessTests(unittest.TestCase):
    def test_ps_parser_filters_other_users_and_zombies(self):
        text = f"123 1 {os.getuid()} S Fri Oct 16 18:00:00 2026 python /tmp/sovet -C '/tmp/paper space' watch\n"
        text += f"124 1 {os.getuid()+1} S Fri Oct 16 18:00:00 2026 private command\n"
        text += f"125 1 {os.getuid()} Z Fri Oct 16 18:00:00 2026 zombie\n"
        table = processes.parse_ps(text)
        self.assertEqual(set(table), {123})
        self.assertIn("paper space", table[123].command)

    def test_cli_match_is_not_substring_match(self):
        self.assertIsNone(processes.cli_args("zsh -c 'sovet -C /tmp/paper watch'", "sovet"))
        self.assertIsNone(processes.cli_args("python unrelated.py sovet watch", "sovet"))
        self.assertEqual(processes.cli_args("python -m sovet.cli watch", "sovet"), ["watch"])

    def test_stale_registry_and_optional_registration(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"XDG_STATE_HOME": temp}):
            p = proc(command="some-native-agent")
            with patch.object(processes, "process_table", return_value={p.pid: p}):
                record = processes.register("sovet", "agent:claude", "/tmp/paper", pid=p.pid)
            self.assertTrue(record.exists())
            reused = proc(command="some-native-agent", started="Fri Oct 16 19:00:00 2026")
            self.assertEqual(processes.discover("sovet", {p.pid: reused}), [])
            with patch.object(processes, "process_table", side_effect=RuntimeError("sandbox")):
                self.assertIsNone(processes.register("sovet", "watch", "/tmp/paper"))

    def test_kill_rechecks_pid_identity(self):
        p = proc()
        replaced = proc(command="unrelated")
        with patch.object(processes, "process_table", return_value={p.pid: replaced}), patch.object(processes.os, "kill") as kill:
            result = processes.terminate([p.pid], {p.pid: p}, timeout=0, force=True)
        kill.assert_not_called()
        self.assertEqual(result["remaining"], [])

    def test_force_kills_only_selected_descendants(self):
        parent, child, unrelated = proc(12345), proc(12346, command="native-child", ppid=12345), proc(12347)
        table = {p.pid: p for p in (parent, child, unrelated)}
        def send(pid, sig):
            if sig == signal.SIGKILL:
                table.pop(pid, None)
        with patch.object(processes, "process_table", side_effect=lambda: dict(table)), patch.object(processes.os, "kill", side_effect=send) as kill:
            result = processes.terminate([parent.pid], dict(table), timeout=0, force=True)
        self.assertEqual(result["remaining"], [])
        self.assertEqual(result["stopped"], [12345, 12346])
        self.assertIn(unrelated.pid, table)
        self.assertNotIn(unrelated.pid, [call.args[0] for call in kill.call_args_list])

    def test_ps_failure_refuses_stop(self):
        with patch("sovet.sessions.process_table", side_effect=RuntimeError("ps blocked")), self.assertRaises(RuntimeError):
            stop_sessions(all_sessions=True)


class CompletionTests(unittest.TestCase):
    def test_commands_options_enums(self):
        self.assertEqual(complete(["ses"]), ["sessions"])
        self.assertEqual(complete(["research", "a"]), ["audit"])
        self.assertEqual(complete(["send", "cl"]), ["claude"])
        self.assertEqual(complete(["config", "--codex-effort", "xh"]), ["xhigh"])
        self.assertEqual(complete(["config", "--codex-effort=xh"]), ["--codex-effort=xhigh"])
        self.assertIn("--math-research", complete(["up", "--m"]))

    def test_stop_ids_are_dynamic(self):
        with patch("sovet.sessions.list_sessions", return_value=[{"id": "sovet-paper-123abc"}]):
            self.assertEqual(complete(["stop", "sovet-p"]), ["sovet-paper-123abc"])

    def test_install_is_idempotent_and_preserves_rc(self):
        with tempfile.TemporaryDirectory() as temp:
            rc = Path(temp) / ".zshrc"
            rc.write_text("# existing user config\nalias x=y\n")
            target = install("sovet", "zsh", temp)
            first = rc.read_text()
            install("sovet", "zsh", temp)
            self.assertEqual(first, rc.read_text())
            self.assertIn("alias x=y", first)
            self.assertEqual(first.count("# >>> sovet completion >>>"), 1)
            self.assertTrue(target.exists())
            self.assertIn("compdef _sovet sovet", script("sovet", "zsh"))


if __name__ == "__main__":
    unittest.main()
