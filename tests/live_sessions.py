"""Opt-in ps + real CLI shutdown test. Stops only fixtures created by this test."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from sovet.core import Project
from sovet.processes import process_table, terminate
from sovet.sessions import list_sessions, stop_sessions

LAUNCHER = Path(__file__).resolve().parents[1] / "bin/sovet"


def wait_for(test, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = test()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Fixture did not become ready")


def main():
    processes = []
    with tempfile.TemporaryDirectory(prefix="sovet process test ") as folder:
        root = Path(folder)
        env = dict(os.environ, XDG_STATE_HOME=str(root / "registry"))
        p = Project.init(root / "paper with spaces")
        other = Project.init(root / "other")
        bins = root / "bin"
        bins.mkdir()
        fake = f'''#!{sys.executable}
import json, os, subprocess, sys, time
sys.stdin.read()
a = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
b = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
with open(os.environ['LIVE_PID_FILE'], 'w') as f:
    json.dump([os.getpid(), a.pid, b.pid], f)
print(json.dumps({{"type":"thread.started","thread_id":"fixture"}}), flush=True)
time.sleep(60)
'''
        for name in ("codex", "latexmk"):
            file = bins / name
            file.write_text(fake)
            file.chmod(0o755)
        env["PATH"] = str(bins) + os.pathsep + env["PATH"]
        env["LIVE_PID_FILE"] = str(root / "child-pids.json")
        previous = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = env["XDG_STATE_HOME"]
        try:
            watcher = subprocess.Popen([sys.executable, str(LAUNCHER), "-C", str(other.root), "watch"],
                                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            processes.append(watcher)
            wait_for(lambda: list_sessions(root=other.root))
            p.enqueue("codex", "fixture")
            runner = subprocess.Popen([sys.executable, str(LAUNCHER), "-C", str(p.root), "run", "codex"],
                                      env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            processes.append(runner)
            marker = root / "child-pids.json"
            wait_for(marker.exists)
            children = json.loads(marker.read_text())
            session = wait_for(lambda: list_sessions(root=p.root))[0]
            result = stop_sessions(target=session["id"], timeout=3, force=True)
            runner.wait(timeout=5)
            assert not result["remaining"], result
            assert not set(children) & set(process_table()), children
            assert watcher.poll() is None, "Unrelated sovet project was stopped"
            print("PASS sovet: ps discovery, project with spaces, detached descendants, unrelated project preserved", flush=True)

            marker.unlink()
            (p.root / "main.tex").write_text("\\documentclass{article}\n\\begin{document}test\\end{document}\n")
            comments = p.root / "kommentarii/kommentarii.json"
            comments.parent.mkdir(exist_ok=True)
            comments.write_text('[{"id":"author-feedback","status":"open"}]')
            original = comments.read_bytes()
            reader = subprocess.Popen(["chitalka", "--root", str(p.root), "--watch", "--no-browser", "--port", "0"],
                                      env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            processes.append(reader)
            wait_for(marker.exists)
            children = json.loads(marker.read_text())
            wait_for(lambda: any(s["pid"] == reader.pid for s in json.loads(subprocess.check_output(
                ["chitalka", "sessions", "--root", str(p.root), "--json"], env=env, text=True))))
            result = subprocess.run(["chitalka", "stop", str(reader.pid), "--timeout", "3", "--force", "--json"],
                                    env=env, text=True, capture_output=True, timeout=10)
            assert result.returncode == 0, result.stdout + result.stderr
            reader.wait(timeout=5)
            assert not set(children) & set(process_table()), children
            assert comments.read_bytes() == original
            assert watcher.poll() is None
            print("PASS chitalka: reader + latexmk + detached child stopped; comments and unrelated session preserved", flush=True)
        finally:
            for process in processes:
                if process.poll() is None:
                    terminate([process.pid], timeout=1, force=True)
                process.wait(timeout=5)
            if previous is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = previous


if __name__ == "__main__":
    main()
