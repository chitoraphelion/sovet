"""Launch unmodified interactive Codex/Claude Code UIs on the inherited terminal.

Codex applies its normal /hooks review-and-trust flow to our session-scoped
hooks. We never bypass that flow. Before hook approval, MCP memory_read/inbox
remain usable; automatic context injection starts after the hooks are trusted.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import uuid
from pathlib import Path

from .core import atomic_write, lock
from .integrations import aris_context, metadata
from .processes import register, unregister
from .research import profile_context
from .runtime import codex_transport_args

LAUNCHER = Path(__file__).resolve().parents[1] / "bin/sovet"


def instructions(project, role):
    writer = project.config["writer"]
    return f"""You are {role} in a sovet team, inside your ORIGINAL INTERACTIVE CLI.
Project: {project.root}. Writer: {writer}. {'You implement changes.' if role == writer else 'You review; do not edit project source files.'}
Your colleague uses the other native pane and has a separate conversation. Shared memory is .team/brief.md,
.team/state.md and .team/decisions.md, plus the sovet event journal. Read memory_read and inbox before work
and whenever the author changes requirements. Hooks may inject newer events; re-read changed files before writing.
Author edits are authoritative. Use artifact_read/artifact_write with expected_sha for authored deliverables;
do not overwrite a conflicting version. Save verified decisions with memory_append, never private reasoning.
Use ask_peer to queue a focused review request. This does not run another hidden model or steal its session.
For this workspace use ONLY sovet ask_peer for cross-model consultation: no codex-companion, extra model
worker, or ARIS reviewer subprocess. Keep both native foreground conversations in their own harnesses.
An idle colleague receives it on its NEXT native turn; if needed ask the user to tell that pane to check inbox.
Never poll, inject terminal keystrokes, or run a second CLI against a foreground session ID.
Use inbox to claim queued work, job_finish to return the concrete result. Stop hooks can also publish final
answers. Respect the supplied depth limit; peer-result receipts must not create another review chain.
If a prompt conflicts with a queued job, follow the user's newest instruction and explain the conflict.
Chitalka reads PDF and Markdown with KaTeX math. Comments are author feedback: inspect document,
source.file/line/lineEnd, quote and suggestion, edit source and verify the result. For source.how=markdown,
line ranges identify the Markdown block and quote preserves TeX formulas; saving .md auto-refreshes the reader
without latexmk. For PDF, rebuild/verify the PDF. Reply via chitalka_reply. Only the writer replies/closes comments. Chitalka N-notes (*.notes.json) are private reading notes:
never read them or deliver them to shared memory, research wiki, reviews or tasks. If latexmk -pvc is already running,
read kommentarii/latexmk.log rather than starting another watcher.
{profile_context(project)}
{aris_context(project) if project.config.get('profile') == 'math-research' else 'Research automation is disabled in programming mode.'}
"""


def prepare_native(project, role):
    folder = project.team / "native"
    folder.mkdir(exist_ok=True)
    cmd = [sys.executable, str(LAUNCHER), "--project", str(project.root)]
    # Keep hook definitions stable: otherwise Codex would request hook review on
    # every harmless reopen. Each role has an exclusive live-harness lease.
    instance = metadata(project, f"native:{role}:instance") or uuid.uuid4().hex
    metadata(project, f"native:{role}:instance", instance)
    hook_events = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")
    hooks = {event: [{"hooks": [{"type": "command", "command": shlex.join(["env", "SOVET_NATIVE_INSTANCE=" + instance] + cmd + ["hook", "--role", role, "--event", event]),
                                 "timeout": 3 if event == "SessionEnd" else 10}]}] for event in hook_events}
    mcp_args = [str(LAUNCHER), "--project", str(project.root), "mcp", "--role", role, "--native"]
    if role != project.config["writer"]:
        mcp_args.append("--read-only")
    mcp = {"command": sys.executable, "args": mcp_args}
    settings = folder / f"{role}-settings.json"
    mcp_file = folder / f"{role}-mcp.json"
    atomic_write(settings, json.dumps({"hooks": hooks}, ensure_ascii=False, indent=2) + "\n")
    atomic_write(mcp_file, json.dumps({"mcpServers": {"sovet": mcp}}, indent=2) + "\n")
    atomic_write(folder / f"{role}-instructions.md", instructions(project, role))
    return {"hooks": hooks, "settings": settings, "mcp_file": mcp_file, "mcp": mcp}


def native_command(project, role, prepared=None):
    binary = shutil.which(role)
    if not binary:
        raise ValueError(f"Не найден {role} в PATH")
    prepared = prepared or prepare_native(project, role)
    read_only = role != project.config["writer"]
    session = metadata(project, f"native:{role}:session")
    legacy = project.agent(role)["session"]
    if not session and legacy:
        # Preserve the old batch transcript, but do not dump its machine-sized
        # context bundles into a user's first real TUI conversation.
        metadata(project, f"legacy:{role}:session", legacy)
    if session:
        metadata(project, f"native:{role}:expected_session", session)
    if role == "claude":
        args = [binary, "--model", project.config["models"][role], "--effort", project.config["effort"][role],
                "--mcp-config", str(prepared["mcp_file"]), "--settings", str(prepared["settings"]),
                "--allowedTools", "mcp__sovet__*",
                "--append-system-prompt", instructions(project, role), "--system-prompt-snapshot", "off",
                "--name", f"sovet · {project.root.name} · {role}"]
        if read_only:
            args += ["--permission-mode", "plan"]
        if session:
            args += ["--resume", session]
        else:
            session = str(uuid.uuid4())
            metadata(project, f"native:{role}:expected_session", session)
            # Bind after SessionStart, not before a failed launch creates a phantom resume ID.
            args += ["--session-id", session]
    else:
        args = [binary, "-m", project.config["models"][role], "-s", "read-only" if read_only else "workspace-write",
                "-a", "on-request", "-C", str(project.root),
                "-c", "model_reasoning_effort=" + json.dumps(project.config["effort"][role]),
                "-c", "developer_instructions=" + json.dumps(instructions(project, role)),
                "-c", "mcp_servers.sovet.command=" + json.dumps(prepared["mcp"]["command"]),
                "-c", "mcp_servers.sovet.args=" + json.dumps(prepared["mcp"]["args"]),
                "-c", f"mcp_servers.sovet.tool_timeout_sec={project.config['math']['timeout'] + 30}"]
        args += codex_transport_args(project)
        for event, handlers in prepared["hooks"].items():
            # CLI TOML overrides accept arrays of inline tables, not JSON objects.
            command = handlers[0]["hooks"][0]["command"]
            timeout = handlers[0]["hooks"][0]["timeout"]
            value = '[{hooks=[{type="command",command=' + json.dumps(command) + f',timeout={timeout}' + '}]}]'
            args += ["-c", f"hooks.{event}={value}"]
        if session:
            args += ["resume", session]
    return args


def run_native(project, role):
    # Prevent a headless runner and an interactive harness from owning one role together.
    with lock(project.team / f"worker-{role}.lock", blocking=False):
        # This exclusive role lease proves no older sovet worker can still own these jobs.
        with project.db() as db:
            interrupted = [dict(row) for row in db.execute("SELECT * FROM jobs WHERE role=? AND status='running'", (role,))]
        for job in interrupted:
            project.finish(job, error="Предыдущий исполнитель завершился; проверьте изменения перед повторным поручением.")
        metadata(project, f"native:{role}:job", 0)
        prepared = prepare_native(project, role)
        command = native_command(project, role, prepared)
        env = dict(os.environ, SOVET_PROJECT=str(project.root), SOVET_ROLE=role, SOVET_NATIVE="1")
        if project.config.get("aris"):
            env["ARIS_REPO"] = project.config["aris"]["repo"]
        process, record = None, None
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT)}
        def shutdown(signum, _frame):
            # tmux respawn sends HUP to the launcher; some native CLIs ignore
            # HUP. Unwind through cleanup before releasing the role lease.
            raise SystemExit(128 + signum)
        signal.signal(signal.SIGHUP, shutdown)
        signal.signal(signal.SIGTERM, shutdown)
        try:
            process = subprocess.Popen(command, cwd=project.root, env=env)
            # Set this after spawn so the child retains its own SIGINT behavior.
            # The foreground native CLI owns Ctrl+C, not this waiting parent.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            record = register("sovet", "native:" + role, project.root, pid=process.pid)
            return process.wait()
        except BaseException:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            unregister(record)
            metadata(project, f"native:{role}:connected", False)
