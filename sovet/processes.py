"""Stdlib process discovery/termination. Kept identical in sovet and chitalka.

ps is authoritative. Registry records add project metadata and identify orphaned
children, but are never sufficient to signal a PID without checking its identity.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import signal
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    uid: int
    state: str
    started: str
    command: str

    @property
    def identity(self):
        return (self.pid, self.uid, self.started, hashlib.sha256(self.command.encode()).hexdigest())


def parse_ps(text):
    rows = {}
    for line in text.splitlines():
        parts = line.split(None, 9)
        if len(parts) != 10:
            continue
        try:
            p = Process(int(parts[0]), int(parts[1]), int(parts[2]), parts[3], " ".join(parts[4:9]), parts[9])
        except ValueError:
            continue
        if p.uid == os.getuid() and not p.state.startswith("Z"):
            rows[p.pid] = p
    return rows


def process_table():
    try:
        result = subprocess.run(["ps", "-ww", "-axo", "pid=,ppid=,uid=,stat=,lstart=,command="],
                                capture_output=True, text=True, errors="replace", timeout=10, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError("Не удалось прочитать ps; без проверки процессов остановка запрещена. " + str(e)) from e
    return parse_ps(result.stdout)


def state_dir(tool):
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / tool / "sessions"


def register(tool, kind, root, details=None, pid=None):
    """Best effort registration: sandboxed ps must not break an ordinary run."""
    try:
        p = process_table().get(pid or os.getpid())
        if p is None:
            return None
        folder = state_dir(tool)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        record = {"pid": p.pid, "uid": p.uid, "started": p.started, "command_sha": p.identity[3],
                  "tool": tool, "kind": kind, "root": str(Path(root).resolve()), "details": details or {}}
        fd, tmp = tempfile.mkstemp(prefix=".session-", dir=folder)
        try:
            with os.fdopen(fd, "w") as file:
                json.dump(record, file, ensure_ascii=False)
            target = folder / f"{p.pid}.json"
            os.replace(tmp, target)
            return target
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except (RuntimeError, OSError):
        return None


def unregister(record):
    if record:
        Path(record).unlink(missing_ok=True)


@contextmanager
def tracked(tool, kind, root, details=None, pid=None):
    record = register(tool, kind, root, details, pid)
    try:
        yield
    finally:
        unregister(record)


def descendants(table, seeds):
    found = {pid for pid in seeds if pid in table}
    while True:
        more = {p.pid for p in table.values() if p.ppid in found}
        if more <= found:
            return found
        found |= more


def cwd(pid):
    proc = Path(f"/proc/{pid}/cwd")
    try:
        return proc.resolve(strict=True)
    except OSError:
        pass
    try:
        result = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                                text=True, capture_output=True, timeout=3)
        for line in result.stdout.splitlines():
            if line.startswith("n/"):
                return Path(line[1:]).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def cli_args(command, tool):
    """Match an executable/module, never a tool name somewhere in a shell prompt."""
    try:
        args = shlex.split(command)
    except ValueError:
        return None
    if not args:
        return None
    if Path(args[0]).name in (tool, tool + ".py"):
        return args[1:]
    if Path(args[0]).name.lower().startswith("python"):
        if len(args) >= 3 and args[1] == "-m" and args[2] in (tool, tool + ".cli"):
            return args[3:]
        if len(args) >= 2 and Path(args[1]).name in (tool, tool + ".py"):
            return args[2:]
    return None


def legacy(p, tool):
    args = cli_args(p.command, tool)
    if args is None:
        return None
    root_arg = None
    if tool == "sovet":
        while args and args[0] in ("-C", "--project"):
            if len(args) < 2:
                return None
            root_arg, args = args[1], args[2:]
        if not args or args[0] not in ("native", "panel", "watch", "reader", "run", "research", "mcp"):
            return None
        if args[0] == "research" and args[1:2] != ["setup"]:
            return None
        kind = args[0] + (":" + args[1] if args[0] in ("native", "panel", "run") and len(args) > 1 else "")
    else:
        if args and args[0] in {"list", "show", "reply", "done", "reopen", "init", "where", "sessions", "ps",
                                 "stop", "completion", "_complete", "--help", "-h", "--version", "-V"}:
            return None
        if "--root" in args and args.index("--root") + 1 < len(args):
            root_arg = args[args.index("--root") + 1]
        kind = "reader"
    working = None if root_arg and Path(root_arg).expanduser().is_absolute() else cwd(p.pid)
    if root_arg:
        root = Path(root_arg).expanduser()
        root = root.resolve() if root.is_absolute() else (working / root).resolve() if working else None
    else:
        root = working
    return {"pid": p.pid, "kind": kind, "root": str(root) if root else None, "details": {}, "legacy": True}


def discover(tool, table=None):
    table = process_table() if table is None else table
    found = {}
    for path in state_dir(tool).glob("*.json"):
        try:
            item = json.loads(path.read_text())
            p = table.get(item["pid"])
            expected = (item["pid"], item["uid"], item["started"], item["command_sha"])
            if not p or p.identity != expected or item.get("tool") != tool:
                continue
            found[p.pid] = item
        except (OSError, ValueError, KeyError, TypeError):
            continue
    for p in table.values():
        if p.pid == os.getpid() or p.pid in found:
            continue
        item = legacy(p, tool)
        if item:
            found[p.pid] = item
    for pid, item in found.items():
        item["started"] = table[pid].started
        item["state"] = table[pid].state
        item["children"] = sorted(descendants(table, [pid]) - {pid})
    return list(sorted(found.values(), key=lambda item: item["pid"]))


def terminate(seeds, table=None, timeout=8.0, force=False):
    """TERM the selected tree; optionally KILL survivors. Recheck before every signal."""
    if timeout < 0:
        raise ValueError("--timeout должен быть неотрицательным")
    table = process_table() if table is None else table
    selected = descendants(table, seeds) - {os.getpid()}
    identities = {pid: table[pid].identity for pid in selected}
    errors = {}
    signalled = set()

    def refresh():
        current = process_table()
        return {pid: p for pid, p in current.items() if pid in identities and p.identity == identities[pid]}

    def send(pids, sig):
        # Revalidate immediately before each signal, including stale/PID-reuse records.
        for pid in pids:
            p = process_table().get(pid)
            if not p or p.identity != identities.get(pid) or pid == os.getpid():
                continue
            try:
                os.kill(pid, sig)
                signalled.add(pid)
            except ProcessLookupError:
                pass
            except PermissionError as e:
                errors[pid] = str(e)

    send([pid for pid in seeds if pid in identities], signal.SIGTERM)
    start = time.monotonic()
    cascaded = False
    remaining = refresh()
    while remaining and time.monotonic() - start < timeout:
        if not cascaded and time.monotonic() - start >= min(1.0, timeout / 2):
            send(remaining, signal.SIGTERM)
            cascaded = True
        time.sleep(min(0.1, max(0, timeout - (time.monotonic() - start))))
        remaining = refresh()
    if remaining and force:
        send(remaining, signal.SIGKILL)
        deadline = time.monotonic() + 2
        while remaining and time.monotonic() < deadline:
            time.sleep(0.05)
            remaining = refresh()
    return {"stopped": sorted(selected - set(remaining)), "remaining": sorted(remaining),
            "signalled": sorted(signalled), "errors": errors}
