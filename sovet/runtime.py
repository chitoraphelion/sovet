"""Native CLI adapters: JSON event streams + explicit, durable session IDs."""
from __future__ import annotations

import json
import fcntl
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .core import atomic_write, lock
from .integrations import aris_context
from .research import profile_context
from .processes import register, unregister

LAUNCHER = Path(__file__).resolve().parents[1] / "bin/sovet"


def protocol(project, role, read_only):
    reader_instructions = ""
    if project.config.get("profile") == "math-research" or project.config.get("reader"):
        reader_instructions = """Chitalka is the author's native PDF and Markdown reader with KaTeX math. Use chitalka_list and chitalka_reply,
preserve all author text and thread entries. Inspect document, source.file/line/lineEnd and quote.
For source.how=markdown, the source range identifies a Markdown block and quote preserves TeX formulas;
edit .md and verify the rendering (it auto-refreshes, no latexmk). For PDF, edit sources and verify/rebuild
PDF. Then reply in the same comment. If latexmk -pvc already runs, inspect kommentarii/latexmk.log instead
of launching a second watcher. Only the writer replies to comments. Never close an unverified fix.
Chitalka N-notes (*.notes.json) are private reading notes; never read them or deliver them to shared memory, wiki, reviews or tasks."""
    return f"""You are {role}, one member of a sovet team. Project: {project.root}.
Your native conversation is persistent. Your teammate has a separate conversation.
The current shared memory and event journal are supplied before every turn.
Writer: {project.config['writer']}. This turn is {'READ-ONLY review' if read_only else 'implementation'}.
Treat user edits as authoritative. Re-read changed files before editing. For authored deliverables
use sovet artifact_read / artifact_write with expected_sha; never overwrite a conflicting version.
Record verified results and decisions with memory_append. Do not copy private reasoning to shared memory.
Use ask_peer for independent review. It queues work and returns immediately: never poll or wait for a peer,
never call claude/codex CLI yourself, and never create recursive agent chains. Finish your turn normally;
the peer result will be delivered back in a separate turn. Keep requests bounded and concrete.
{reader_instructions}
{profile_context(project)}
{aris_context(project) if project.config.get('profile') == 'math-research' else 'Use independent peer review; research automation is disabled in programming mode.'}
"""


def command(project, role, job, session):
    read_only = bool(job.get("read_only")) or role != project.config["writer"]
    binary = shutil.which(role)
    if not binary:
        raise ValueError(f"{role} не найден в PATH")
    mcp_args = [str(LAUNCHER), "--project", str(project.root), "mcp", "--role", role,
                "--depth", str(job.get("depth", 0))]
    if read_only:
        mcp_args.append("--read-only")
    mcp = {"command": sys.executable, "args": mcp_args}
    model = project.config["models"][role]
    effort = project.config["effort"][role]
    if role == "codex":
        cmd = [binary, "exec", "--json", "--skip-git-repo-check", "-m", model,
               "-s", "read-only" if read_only else "workspace-write",
               "-c", f'model_reasoning_effort="{effort}"',
               "-c", "mcp_servers.sovet.command=" + json.dumps(mcp["command"]),
               "-c", "mcp_servers.sovet.args=" + json.dumps(mcp["args"]),
               "-c", f"mcp_servers.sovet.tool_timeout_sec={project.config['math']['timeout'] + 30}"]
        if session:
            cmd.extend(["resume", session])
        cmd.append("-")
    else:
        cfg_path = project.team / f"mcp-{role}.json"
        atomic_write(cfg_path, json.dumps({"mcpServers": {"sovet": mcp}}))
        cmd = [binary, "-p", "--output-format", "stream-json", "--verbose", "--model", model,
               "--effort", effort, "--permission-mode", "dontAsk" if read_only else "acceptEdits",
               "--strict-mcp-config",
               "--mcp-config", str(cfg_path), "--append-system-prompt", protocol(project, role, read_only),
               "--allowedTools", ("Read,Grep,Glob,mcp__sovet__*" if read_only else
                                   "Read,Grep,Glob,Edit,Write,Bash,mcp__sovet__*")]
        if read_only:
            # Plan mode rejects even some explicitly allowed MCP reads. A fixed read-only
            # builtin surface plus dontAsk admits our checks and denies unlisted tools.
            cmd.extend(["--tools", "Read,Grep,Glob,ToolSearch"])
        if session:
            cmd.extend(["--resume", session])
    return cmd, read_only


class NativeRun:
    def __init__(self):
        self.process = None
        self.cancelled = threading.Event()
        self.record = None

    def cancel(self):
        self.cancelled.set()
        p = self.process
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGINT)
            except ProcessLookupError:
                pass

    def run(self, project, role, job, output=lambda text: None):
        try:
            return self._leased_run(project, role, job, output)
        finally:
            if self.process and self.process.poll() is None:
                self.cancel()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.process.wait()
            unregister(self.record)

    def _leased_run(self, project, role, job, output):
        read_only = bool(job.get("read_only")) or role != project.config["writer"]
        if read_only:
            return self._run(project, role, job, output)
        # A role change during a running turn must not create two concurrent writers.
        with (project.team / "writer.lock").open("a+") as lease:
            while True:
                try:
                    fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if self.cancelled.wait(0.2):
                        raise RuntimeError("Ход отменён во время ожидания исполнителя")
            try:
                return self._run(project, role, job, output)
            finally:
                fcntl.flock(lease, fcntl.LOCK_UN)

    def _run(self, project, role, job, output=lambda text: None):
        context, cursor = project.context(role)
        session = project.agent(role)["session"]
        cmd, read_only = command(project, role, job, session)
        prompt = protocol(project, role, read_only) + "\n" + context + "\n\nТекущая задача:\n" + job["prompt"]
        env = dict(os.environ)
        env.update({"SOVET_PROJECT": str(project.root), "SOVET_ROLE": role})
        if project.config.get("aris"):
            env["ARIS_REPO"] = project.config["aris"]["repo"]
        # Native CLI instances own their own hooks/login; never bypass their permission system.
        logdir = project.team / "logs"
        logdir.mkdir(exist_ok=True)
        stderr_path = logdir / f"{role}-{job['id']}.stderr.log"
        events_path = logdir / f"{role}-{job['id']}.jsonl"
        chunks, final, failure, completed = [], "", None, False
        if self.cancelled.is_set():
            raise RuntimeError("Ход отменён")
        with stderr_path.open("w") as err, events_path.open("w") as raw:
            self.process = subprocess.Popen(cmd, cwd=project.root, env=env, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=err, text=True,
                                            encoding="utf-8", errors="replace", start_new_session=True)
            p = self.process
            self.record = register("sovet", "agent:" + role, project.root, {"job": job["id"]}, pid=p.pid)
            lines = queue.Queue()
            def reader():
                try:
                    for line in p.stdout:
                        lines.put(line)
                finally:
                    lines.put(None)
            thread = threading.Thread(target=reader, daemon=True)
            thread.start()
            # Large project context cannot fill a pipe and deadlock stdout consumption.
            def writer():
                try:
                    p.stdin.write(prompt)
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    try:
                        p.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass
            threading.Thread(target=writer, daemon=True).start()
            started = time.monotonic()
            cancelled_at = None
            while True:
                if self.cancelled.is_set() and cancelled_at is None:
                    self.cancel()
                    cancelled_at = time.monotonic()
                if time.monotonic() - started > project.config["turn_timeout"]:
                    failure = "Превышено время хода"
                    self.cancel()
                    cancelled_at = cancelled_at or time.monotonic()
                if cancelled_at and time.monotonic() - cancelled_at > 5 and p.poll() is None:
                    os.killpg(p.pid, signal.SIGKILL)
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    break
                raw.write(line)
                raw.flush()
                try:
                    event = json.loads(line)
                except ValueError:
                    output(line.rstrip())
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")
                sid = event.get("thread_id") if role == "codex" else event.get("session_id")
                if sid:
                    project.session(role, sid)
                if role == "codex":
                    if kind == "item.completed":
                        item = event.get("item", {})
                        if item.get("type") == "agent_message":
                            text = item.get("text", "")
                            chunks.append(text)
                            output(text)
                        elif item.get("type") in ("command_execution", "file_change", "mcp_tool_call"):
                            output("· " + str(item.get("command") or item.get("tool") or item.get("type"))[:180])
                    elif kind == "turn.completed":
                        completed = True
                    elif kind in ("turn.failed", "error"):
                        failure = str(event.get("error") or event.get("message") or event)
                else:
                    if kind == "assistant":
                        for block in event.get("message", {}).get("content", []):
                            if block.get("type") == "text":
                                chunks.append(block["text"])
                                output(block["text"])
                            elif block.get("type") == "tool_use":
                                output("· " + block.get("name", "tool"))
                    elif kind == "result":
                        final = event.get("result", "")
                        completed = not event.get("is_error", False)
                        if event.get("is_error"):
                            failure = final or str(event.get("errors") or event.get("subtype"))
            try:
                code = p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.cancel()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait()
                raise RuntimeError(f"{role} закрыл stdout, но не завершился; процесс остановлен")
            p.stdout.close()
        if self.cancelled.is_set():
            raise RuntimeError(failure or "Ход отменён; следующая задача продолжит сохранённую сессию")
        if code or failure or not completed:
            tail = stderr_path.read_text()[-2500:]
            raise RuntimeError(failure or f"{role}: exit={code}, нет события завершения. {tail}")
        result = final or "\n\n".join(chunks)
        if not result.strip():
            raise RuntimeError(f"{role} завершился без ответа; журнал: {events_path}")
        project.acknowledge(role, cursor)
        return result


def execute_next(project, role, output=print, controller=None):
    job = project.claim(role)
    if not job:
        return None
    controller = controller or NativeRun()
    output(f"\n▶ #{job['id']} от {job['origin']}: {job['prompt'][:250]}")
    try:
        result = controller.run(project, role, job, output)
        project.finish(job, result)
        output(f"\n✓ #{job['id']}")
        return {"id": job["id"], "ok": True}
    except Exception as e:
        project.finish(job, error=str(e))
        output(f"\n✗ #{job['id']}: {e}")
        return {"id": job["id"], "ok": False, "error": str(e)}


def worker_lock(project, role):
    return lock(project.team / f"worker-{role}.lock", blocking=False)
