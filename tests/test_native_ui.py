"""Native tmux launcher and CLI UX. Real tmux on a private socket; harnesses are stubs, no model calls."""
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from sovet import cli
from sovet.completion import complete
from sovet.core import Project

REAL_TMUX = shutil.which("tmux")


def stub(project, *args):
    # Keeps "native ROLE"/"panel ROLE" in the command, as the real launcher does, but never runs a model.
    return f"sh -c 'sleep 300' {' '.join(args)}"


class Isolated(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sovet-ui-")
        base = Path(self.temp.name)
        self.p = Project.init(base / "paper")
        bins = base / "bin"
        bins.mkdir()
        for name in ("codex", "claude"):
            (bins / name).write_text("#!/bin/sh\nexit 99\n")
            (bins / name).chmod(0o755)
        if REAL_TMUX:
            # The user's tmux.conf must not shape the test server; -f only matters when it starts.
            (bins / "tmux").write_text(f'#!/bin/sh\nexec {shlex.quote(REAL_TMUX)} -f /dev/null "$@"\n')
            (bins / "tmux").chmod(0o755)
        (base / "tmux").mkdir()
        # Private tmux socket (never the user's server); registry and XDG config kept out of ~.
        env = {"TMUX_TMPDIR": str(base / "tmux"), "XDG_STATE_HOME": str(base / "state"),
               "XDG_CONFIG_HOME": str(base / "config"), "PATH": str(bins) + os.pathsep + os.environ["PATH"]}
        self.env = patch.dict(os.environ, env)
        self.env.start()
        os.environ.pop("TMUX", None)

    def tearDown(self):
        subprocess.run(["tmux", "kill-server"], capture_output=True)
        self.env.stop()
        self.temp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["-C", str(self.p.root), *argv])
        return code, out.getvalue()


@unittest.skipUnless(REAL_TMUX, "tmux not installed")
class TmuxLayoutTest(Isolated):
    def setUp(self):
        super().setUp()
        # An unrelated session with non-default indexes: must be untouched and must not matter.
        subprocess.run(["tmux", "new-session", "-d", "-s", "unrelated", "sleep 300"], check=True)
        for option, value in (("base-index", "7"), ("pane-base-index", "3")):
            subprocess.run(["tmux", "set-option", "-g", option, value], check=True)
        self.launch = patch.object(cli, "launch_command", stub)
        self.launch.start()
        self.name = cli.session_name(self.p)

    def tearDown(self):
        self.launch.stop()
        super().tearDown()

    def panes(self, window="agents"):
        fmt = "#{pane_id}\t#{@sovet_role}\t#{pane_left}\t#{pane_width}\t#{pane_active}\t#{pane_dead}\t#{pane_pid}\t#{@sovet_label}\t#{window_width}"
        out = subprocess.check_output(["tmux", "list-panes", "-t", f"={self.name}:{window}", "-F", fmt], text=True)
        rows = [line.split("\t") for line in out.splitlines()]
        return sorted(({"id": r[0], "role": r[1], "left": int(r[2]), "width": int(r[3]), "active": r[4] == "1",
                        "dead": r[5] == "1", "pid": r[6], "label": r[7], "window_width": int(r[8])} for r in rows),
                      key=lambda pane: pane["left"])

    def windows(self):
        return subprocess.check_output(["tmux", "list-windows", "-t", "=" + self.name, "-F", "#{window_name}"],
                                       text=True).split()

    def wait_dead(self, pane):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if subprocess.check_output(["tmux", "display", "-p", "-t", pane, "#{pane_dead}"], text=True).strip() == "1":
                return
            time.sleep(0.05)
        self.fail("pane did not die")

    def test_fresh_up_is_two_native_panes_50_50_focus_on_writer(self):
        code, out = self.run_cli("up", "--detach")
        self.assertEqual(code, 0)
        left, right = self.panes()
        self.assertEqual((left["role"], right["role"]), ("codex", "claude"))
        self.assertEqual(left["window_width"], 160)  # detached: fixed default, not tmux's 80x24
        self.assertLessEqual(abs(left["width"] - right["width"]), 1)
        self.assertTrue(right["active"])
        self.assertEqual(right["label"], "Claude · писатель · claude-opus-5-5 · high")
        self.assertIn("ревьюер", left["label"])
        self.assertEqual(self.windows(), ["agents", "watch"])
        self.assertIn("справа Claude", out)
        # Only this session gets mouse mode; the user's other sessions keep their settings.
        show = lambda session: subprocess.check_output(["tmux", "show-options", "-t", f"={session}:", "mouse"], text=True)
        self.assertEqual(show(self.name).strip(), "mouse on")
        self.assertEqual(show("unrelated").strip(), "")
        # The wrapper's -f /dev/null keeps the user's tmux.conf (e.g. status-position top) out.
        position = subprocess.check_output(["tmux", "show-options", "-gv", "status-position"], text=True)
        self.assertEqual(position.strip(), "bottom")

    def test_repeat_up_keeps_live_panes_and_respawns_dead_one(self):
        self.run_cli("up", "--detach")
        before = self.panes()
        self.run_cli("up", "--detach")
        self.assertEqual(self.panes(), before)
        claude = before[1]
        os.kill(int(claude["pid"]), 15)
        self.wait_dead(claude["id"])
        self.assertTrue(self.panes()[1]["dead"])  # remain-on-exit: layout survives an exited harness
        self.run_cli("up", "--detach")
        after = self.panes()
        self.assertEqual([p["id"] for p in after], [p["id"] for p in before])
        self.assertFalse(after[1]["dead"])
        self.assertNotEqual(after[1]["pid"], claude["pid"])

    def test_missing_pane_recreated_on_correct_side(self):
        self.run_cli("up", "--detach")
        codex = self.panes()[0]
        subprocess.run(["tmux", "kill-pane", "-t", codex["id"]], check=True)
        self.run_cli("up", "--detach")
        left, right = self.panes()
        self.assertEqual((left["role"], right["role"]), ("codex", "claude"))
        self.assertLessEqual(abs(left["width"] - right["width"]), 1)

    def legacy_session(self):
        root = str(self.p.root)
        first = subprocess.check_output(["tmux", "new-session", "-d", "-P", "-F", "#{pane_id}", "-s", self.name,
                                         "-n", "agents", "-c", root, stub(self.p, "panel", "codex")], text=True).strip()
        subprocess.run(["tmux", "split-window", "-h", "-t", first, stub(self.p, "panel", "claude")], check=True)
        subprocess.run(["tmux", "new-window", "-d", "-t", f"={self.name}:", "-n", "watch", stub(self.p, "watch")], check=True)
        return [p["id"] for p in self.panes()]

    def test_old_curses_session_migrates_in_place(self):
        ids = self.legacy_session()
        code, _ = self.run_cli("up", "--detach")
        self.assertEqual(code, 0)
        panes = self.panes()
        self.assertEqual([p["id"] for p in panes], ids)
        self.assertEqual([p["role"] for p in panes], ["codex", "claude"])
        commands = subprocess.check_output(["tmux", "list-panes", "-t", f"={self.name}:agents",
                                            "-F", "#{pane_start_command}"], text=True)
        self.assertNotIn("panel", commands)
        self.assertEqual(commands.count("native"), 2)
        self.assertEqual(subprocess.run(["tmux", "has-session", "-t", "=unrelated"]).returncode, 0)

    def test_old_panel_mid_turn_requires_explicit_restart(self):
        ids = self.legacy_session()
        self.p.enqueue("claude", "work")
        self.p.claim("claude")
        with patch("sys.stderr", io.StringIO()) as err:
            code, _ = self.run_cli("up", "--detach")
        self.assertEqual(code, 1)
        self.assertIn("--restart", err.getvalue())
        commands = subprocess.check_output(["tmux", "list-panes", "-t", f"={self.name}:agents",
                                            "-F", "#{pane_start_command}"], text=True)
        self.assertEqual(commands.count("panel"), 2)  # nothing was touched
        code, _ = self.run_cli("up", "--detach", "--restart")
        self.assertEqual(code, 0)
        self.assertEqual([p["id"] for p in self.panes()], ids)

    def test_reader_window_and_programming_removes_it(self):
        self.run_cli("up", "--detach", "--reader", "--latex-watch", "--no-browser")
        self.assertEqual(self.windows(), ["agents", "watch", "chitalka"])
        options = Project(self.p.root).config["reader_options"]
        self.assertEqual(options, {"pdf": None, "watch": True})
        self.run_cli("up", "--detach", "--programming")
        self.assertEqual(self.windows(), ["agents", "watch"])

    def test_dry_run_changes_nothing(self):
        config = (self.p.team / "config.json").read_bytes()
        code, out = self.run_cli("up", "--dry-run", "--codex-effort", "xhigh", "--reader")
        self.assertEqual(code, 0)
        self.assertIn("tmux new-session", out)
        self.assertIn("native claude", out)
        self.assertNotEqual(subprocess.run(["tmux", "has-session", "-t", "=" + self.name]).returncode, 0)
        self.assertEqual((self.p.team / "config.json").read_bytes(), config)


class CliUxTest(Isolated):
    def test_up_launches_native_never_panel(self):
        with patch.object(cli.Tmux, "query", return_value=None), patch.object(cli.Tmux, "__call__", autospec=True,
                                                                             return_value="@1\t%1") as call:
            self.run_cli("up", "--detach")
        commands = " ".join(" ".join(map(str, c.args[1:])) for c in call.call_args_list)
        self.assertIn(cli.launch_command(self.p, "native", "codex"), commands)
        self.assertIn(cli.launch_command(self.p, "native", "claude"), commands)
        self.assertNotIn(" panel ", commands)
        self.assertNotIn(" run ", commands)

    def test_missing_harness_is_reported_before_tmux(self):
        with patch.object(cli.shutil, "which", side_effect=lambda name: None if name == "codex" else "/bin/" + name), \
                patch("sys.stderr", io.StringIO()) as err:
            code, _ = self.run_cli("up", "--detach")
        self.assertEqual(code, 1)
        self.assertIn("codex", err.getvalue())

    def test_window_size_real_terminal_or_detached_default(self):
        self.assertEqual(cli.window_size(True), (160, 45))
        for real in ((120, 35), (240, 60)):
            with patch.object(cli.shutil, "get_terminal_size", return_value=os.terminal_size(real)):
                self.assertEqual(cli.window_size(False), real)  # never enlarged beyond the real screen
        with patch.object(cli.shutil, "get_terminal_size", return_value=os.terminal_size((0, 0))):
            self.assertEqual(cli.window_size(False), (160, 45))

    def test_pane_role_ignores_project_path_words(self):
        self.assertEqual(cli.pane_role('"python sovet --project /x/native codex/p native claude"'), ("native", "claude"))
        self.assertEqual(cli.pane_role("python sovet --project /p panel codex"), ("panel", "codex"))
        self.assertEqual(cli.pane_role("zsh"), (None, None))

    def test_native_and_legacy_panel_call_run_native(self):
        calls = []
        fake = types.ModuleType("sovet.native")
        fake.run_native = lambda project, role: calls.append((project.root, role)) or 7
        with patch.dict(sys.modules, {"sovet.native": fake}):
            self.assertEqual(self.run_cli("native", "claude")[0], 7)
            self.assertEqual(self.run_cli("panel", "codex")[0], 7)
        self.assertEqual(calls, [(self.p.root, "claude"), (self.p.root, "codex")])

    def test_hook_and_native_mcp_are_wired(self):
        fake = types.ModuleType("sovet.bridge")
        fake.main_hook = lambda project, role, event: 0 if (role, event) == ("claude", "Stop") else 1
        with patch.dict(sys.modules, {"sovet.bridge": fake}):
            self.assertEqual(self.run_cli("hook", "--role", "claude", "--event", "Stop")[0], 0)
        with patch("sovet.mcp.serve") as serve:
            self.run_cli("mcp", "--role", "codex", "--native", "--read-only")
        serve.assert_called_once()
        self.assertTrue(serve.call_args.kwargs.get("native"))

    def test_run_refuses_while_role_is_open(self):
        live = [{"pid": 4242, "kind": "native:claude"}]
        self.p.enqueue("claude", "work")
        with patch.object(cli, "live_processes", return_value=live), patch("sys.stderr", io.StringIO()) as err:
            code, _ = self.run_cli("run", "claude")
        self.assertEqual(code, 1)
        self.assertIn("открыт в tmux", err.getvalue())
        self.assertEqual(self.p.jobs()[0]["status"], "pending")

    def test_status_is_readable_and_json_available(self):
        self.p.enqueue("codex", "Проверь лемму 3\nи оценку")
        from sovet.integrations import metadata
        self.p.session("codex", "batch0ld-legacy")       # pre-native headless conversation
        metadata(self.p, "native:claude:session", "nat1ve42-claude")
        live = [{"pid": 4242, "kind": "native:claude"}, {"pid": 4243, "kind": "watch"}]
        with patch.object(cli, "live_processes", return_value=live):
            _, text = self.run_cli("status")
            _, raw = self.run_cli("status", "--json")
        self.assertIn("Claude", text)
        self.assertIn("открыт, PID 4242", text)
        self.assertIn("Codex", text)
        self.assertIn("не запущен", text)
        self.assertIn("#1", text)
        self.assertIn("Проверь лемму 3 и оценку", text)
        self.assertIn("Stop hook", text)
        self.assertIn("разговор nat1ve42…", text)
        self.assertNotIn("batch0ld", text)  # only the resumed native conversation is shown
        data = json.loads(raw)
        self.assertEqual(data["agents"]["claude"]["pids"], [4242])
        self.assertEqual(data["jobs"][0]["status"], "pending")

    def test_send_explains_delivery(self):
        with patch.object(cli, "live_processes", return_value=[]):
            _, out = self.run_cli("send", "claude", "fix")
            _, raw = self.run_cli("review", "--json")
        self.assertIn("в очередь Claude", out)
        self.assertIn("не открыт", out)
        self.assertEqual(json.loads(raw)["role"], "codex")

    def test_completion_hides_internal_commands(self):
        top = complete([""])
        for name in cli.INTERNAL:
            self.assertNotIn(name, top)
        self.assertIn("up", top)
        self.assertIn("--restart", complete(["up", "--re"]))


if __name__ == "__main__":
    unittest.main()
