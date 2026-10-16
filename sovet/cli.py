from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

from . import __version__
from .core import MEMORIES, ROLES, Project, digest, find_root, lock
from .integrations import chitalka, install_aris, scan_files, sync_comments, sync_aris_profile
from .runtime import LAUNCHER, execute_next, worker_lock


def flags(parser):
    profile = parser.add_mutually_exclusive_group()
    profile.add_argument("--math-research", action="store_const", const="math-research", dest="profile")
    profile.add_argument("--programming", action="store_const", const="programming", dest="profile")
    parser.add_argument("--writer", choices=ROLES)
    for role in ROLES:
        parser.add_argument(f"--{role}-model")
        parser.add_argument(f"--{role}-effort", choices=["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
                            if role == "codex" else ["low", "medium", "high", "xhigh", "max"])


def configure(project, args):
    if getattr(args, "profile", None) == "programming":
        project.config["reader"] = False
    for key in ("profile", "writer"):
        if getattr(args, key, None):
            project.config[key] = getattr(args, key)
    for role in ROLES:
        for key, group in (("model", "models"), ("effort", "effort")):
            value = getattr(args, f"{role}_{key}", None)
            if value:
                project.config[group][role] = value
    if getattr(args, "reader", False):
        project.config["reader"] = True
    project.save_config()
    sync_aris_profile(project)


def session_name(project):
    slug = "".join(c if c.isascii() and c.isalnum() else "-" for c in project.root.name)[:25]
    return f"sovet-{slug}-{digest(str(project.root).encode())[:6]}"


def launch_command(project, *args):
    return shlex.join([sys.executable, str(LAUNCHER), "--project", str(project.root), *args])


def tmux(project, args):
    if not shutil.which("tmux"):
        raise ValueError("Нужен tmux: brew install tmux")
    name = session_name(project)
    exists = subprocess.run(["tmux", "has-session", "-t", "=" + name], capture_output=True).returncode == 0
    if not exists:
        # Exact session/window/pane targets avoid ambiguous tmux prefix matching.
        first = subprocess.check_output(["tmux", "new-session", "-d", "-P", "-F", "#{pane_id}",
                                         "-s", name, "-n", "agents", "-c", str(project.root),
                                         launch_command(project, "panel", "codex")], text=True).strip()
        try:
            subprocess.run(["tmux", "split-window", "-h", "-t", first, "-c", str(project.root),
                            launch_command(project, "panel", "claude")], check=True)
            subprocess.run(["tmux", "select-pane", "-t", first], check=True)
        except Exception:
            subprocess.run(["tmux", "kill-session", "-t", "=" + name])
            raise
    windows = subprocess.check_output(["tmux", "list-windows", "-t", "=" + name, "-F", "#{window_name}"], text=True).splitlines()
    if args.profile == "programming" and not args.reader and "chitalka" in windows:
        subprocess.run(["tmux", "kill-window", "-t", name + ":chitalka"], check=True)
    if "watch" not in windows:
        subprocess.run(["tmux", "new-window", "-d", "-t", name + ":", "-n", "watch",
                        launch_command(project, "watch")], check=True)
    if getattr(args, "reader", False) and "chitalka" not in windows:
        reader = ["reader"]
        if args.pdf:
            reader += [args.pdf]
        if args.latex_watch:
            reader += ["--watch"]
        if args.no_browser:
            reader += ["--no-browser"]
        subprocess.run(["tmux", "new-window", "-d", "-t", name + ":", "-n", "chitalka",
                        launch_command(project, *reader)], check=True)
    print(f"tmux: {name}\nCodex слева · Claude справа. Ctrl+b ←/→; Ctrl+b d — отсоединиться.")
    if not args.detach:
        if os.environ.get("TMUX"):
            subprocess.run(["tmux", "switch-client", "-t", "=" + name], check=True)
        else:
            return subprocess.call(["tmux", "attach-session", "-t", "=" + name])
    return 0


def watch(project, once=False):
    stop = threading.Event()
    def end(*_):
        stop.set()
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, end)
    with lock(project.team / "watch.lock", blocking=False):
        last_error = None
        while not stop.is_set():
            try:
                project.config = Project(project.root).config
                changed = scan_files(project)
                feedback = sync_comments(project) if (project.config.get("profile") == "math-research" or
                                                       project.config.get("reader")) else 0
                if changed or feedback:
                    print(f"Изменений: {changed}; комментариев автора: {feedback}", flush=True)
                last_error = None
            except Exception as e:
                if str(e) != last_error:
                    project.emit("watch", "error", str(e))
                    print(f"watch: {e}", file=sys.stderr, flush=True)
                last_error = str(e)
            if once:
                return
            stop.wait(1)


def parser():
    p = argparse.ArgumentParser(prog="sovet", description="Claude пишет, Codex проверяет. Общая память, tmux, ARIS, chitalka, Lean.")
    p.add_argument("-C", "--project", help="путь к проекту (можно из подкаталога)")
    p.add_argument("--version", action="version", version=f"sovet {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    init = sub.add_parser("init", help="создать .team/ в проекте")
    init.add_argument("path", nargs="?", default=".")
    init.add_argument("--brief", default="")
    init.add_argument("--aris", type=Path, help="локальный checkout ARIS")
    flags(init)
    up = sub.add_parser("up", help="открыть двух агентов рядом в tmux")
    up.add_argument("--detach", action="store_true")
    up.add_argument("--reader", action="store_true", help="также запустить chitalka")
    up.add_argument("--pdf")
    up.add_argument("--latex-watch", action="store_true")
    up.add_argument("--no-browser", action="store_true", help="не открывать браузер при запуске reader")
    flags(up)
    config = sub.add_parser("config", help="показать/изменить конфиг проекта; флаги сохраняются")
    flags(config)
    for name in ("panel", "run"):
        s = sub.add_parser(name, help="терминальный чат" if name == "panel" else "обработать одну задачу без tmux")
        s.add_argument("role", choices=ROLES)
    send = sub.add_parser("send", help="поставить агенту задачу")
    send.add_argument("role", choices=ROLES)
    send.add_argument("text", help="текст или - для stdin")
    send.add_argument("--read-only", action="store_true")
    review = sub.add_parser("review", help="попросить ревьюера проверить текущий результат")
    review.add_argument("text", nargs="?", default="Проверьте актуальные изменения, требования и результаты проверок. Сообщите конкретные замечания.")
    events = sub.add_parser("events", help="общая история")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--json", action="store_true")
    sub.add_parser("status", help="сессии, очередь и роли")
    sub.add_parser("stop", help="остановить tmux-сессию проекта")
    doctor = sub.add_parser("doctor", help="проверить локальные инструменты без вызова моделей")
    doctor.add_argument("--json", action="store_true")
    w = sub.add_parser("watch", help="следить за файлами и обратной связью chitalka")
    w.add_argument("--once", action="store_true")
    memory = sub.add_parser("memory", help="общая память")
    memory.add_argument("name", choices=MEMORIES)
    memory.add_argument("text", nargs="?", help="новый текст или - для stdin")
    memory.add_argument("--append", action="store_true")
    edit = sub.add_parser("edit", help="открыть результат в $VISUAL/$EDITOR, сообщить обоим агентам")
    edit.add_argument("path")
    art = sub.add_parser("artifact", help="чтение и запись с контролем версии")
    art.add_argument("action", choices=["read", "write"])
    art.add_argument("path")
    art.add_argument("--expected-sha")
    art.add_argument("--text", help="текст или - для stdin")
    aris = sub.add_parser("aris", help="подключить оригинальные навыки ARIS в проект")
    aris.add_argument("--repo", type=Path, default=Path.home() / "code/aris")
    c = sub.add_parser("chitalka", help="нативные команды chitalka для текущего проекта")
    c.add_argument("args", nargs=argparse.REMAINDER)
    reader = sub.add_parser("reader", help="открыть PDF в chitalka")
    reader.add_argument("pdf", nargs="?")
    reader.add_argument("--watch", action="store_true")
    reader.add_argument("--no-browser", action="store_true")
    research = sub.add_parser("research", help="LongTails, Lean и Comparator")
    r = research.add_subparsers(dest="action", required=True)
    setup = r.add_parser("setup")
    setup.add_argument("--source", type=Path, help="локальный LongTails, из него создаётся отдельный checkout")
    setup.add_argument("--fetch-cache", action="store_true")
    r.add_parser("status")
    for name in ("check", "build", "audit", "search"):
        q = r.add_parser(name)
        q.add_argument("target", nargs="?" if name == "build" else None)
    mcp = sub.add_parser("mcp", help="внутренний stdio MCP для агентских CLI")
    mcp.add_argument("--role", choices=ROLES, required=True)
    mcp.add_argument("--depth", type=int, default=0)
    mcp.add_argument("--read-only", action="store_true")
    return p


def stdin_text(value):
    return sys.stdin.read() if value == "-" else value


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return dispatch(args) or 0
    except (ValueError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"sovet: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def dispatch(args):
    if args.cmd == "init":
        project = Project.init(Path(args.project or args.path), writer=args.writer or "claude", brief=args.brief)
        configure(project, args)
        repo = args.aris or Path.home() / "code/aris"
        if args.aris or (repo / "skills/research-wiki/SKILL.md").exists():
            install_aris(project, repo)
        scan_files(project)
        print(f"Готово: {project.root}\nИсполнитель: {project.config['writer']}; профиль: {project.config['profile']}")
        print("Дальше: sovet up" if not args.project else f"Дальше: sovet -C {shlex.quote(str(project.root))} up")
        return
    if args.cmd == "doctor":
        tools = {name: shutil.which(name) for name in ("codex", "claude", "tmux", "chitalka", "lean", "lake", "elan", "latexmk", "git")}
        try:
            project = Project(find_root(args.project))
            tools["project"] = str(project.root)
            tools["profile"] = project.config["profile"]
            tools["aris"] = project.config.get("aris")
            tools["longtails"] = (project.team / "longtails.lock.json").exists()
        except ValueError:
            pass
        print(json.dumps(tools, ensure_ascii=False, indent=2) if args.json else
              "\n".join(f"{'✓' if value else '·'} {name}: {value or 'не найдено'}" for name, value in tools.items()))
        return 0 if all(tools.get(k) for k in ("codex", "claude", "tmux")) else 1
    project = Project(find_root(args.project))
    if args.cmd in ("config", "up"):
        configure(project, args)
        if args.cmd == "config":
            print(json.dumps(project.config, ensure_ascii=False, indent=2))
        else:
            return tmux(project, args)
    elif args.cmd == "panel":
        from .ui import panel
        panel(project, args.role)
    elif args.cmd == "run":
        with worker_lock(project, args.role):
            result = execute_next(project, args.role)
            return 1 if result and not result["ok"] else 0
    elif args.cmd == "send":
        print(project.enqueue(args.role, stdin_text(args.text), read_only=args.read_only))
    elif args.cmd == "review":
        reviewer = next(role for role in ROLES if role != project.config["writer"])
        print(project.enqueue(reviewer, args.text, read_only=True, reply_to=project.config["writer"]))
    elif args.cmd == "events":
        rows = project.events(args.after, limit=10000)
        print(json.dumps(rows, ensure_ascii=False, indent=2) if args.json else
              "\n\n".join(f"#{e['id']} {e['actor']} / {e['kind']}\n{e['body']}" for e in rows))
    elif args.cmd == "status":
        print(json.dumps({"project": str(project.root), "profile": project.config["profile"],
                          "writer": project.config["writer"], "agents": {r: project.agent(r) for r in ROLES},
                          "jobs": project.jobs()}, ensure_ascii=False, indent=2))
    elif args.cmd == "watch":
        watch(project, args.once)
    elif args.cmd == "stop":
        return subprocess.call(["tmux", "kill-session", "-t", "=" + session_name(project)])
    elif args.cmd == "memory":
        if args.text is None:
            print((project.team / f"{args.name}.md").read_text())
        else:
            project.memory_write(args.name, stdin_text(args.text), append=args.append)
    elif args.cmd == "artifact":
        if args.action == "read":
            print(json.dumps(project.read_artifact(args.path), ensure_ascii=False, indent=2))
        else:
            if args.text is None or not args.expected_sha:
                raise ValueError("Для записи нужны --text и --expected-sha")
            print(json.dumps(project.write_artifact(args.path, stdin_text(args.text), args.expected_sha), ensure_ascii=False))
    elif args.cmd == "edit":
        candidate = (project.root / args.path).resolve()
        memories = {project.team / (name + ".md") for name in MEMORIES} | {project.team / "claims.json"}
        file = candidate if candidate in memories else project.artifact(args.path)
        file.parent.mkdir(parents=True, exist_ok=True)
        editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
        subprocess.run([*editor, str(file)], check=True)
        project.emit("user", "edit", f"Автор отредактировал {args.path}. Перечитайте файл перед следующим изменением.")
        scan_files(project)
    elif args.cmd == "aris":
        print(json.dumps(install_aris(project, args.repo), indent=2))
    elif args.cmd == "chitalka":
        # A literal -- is optional when forwarding arguments.
        forwarded = args.args[1:] if args.args[:1] == ["--"] else args.args
        chitalka(project, forwarded, capture=False)
    elif args.cmd == "reader":
        project.config["reader"] = True
        project.save_config()
        chitalka(project, [*([args.pdf] if args.pdf else []), *(["--watch"] if args.watch else []),
                           *(["--no-browser"] if args.no_browser else [])], capture=False)
    elif args.cmd == "research":
        from .research import run_lean, search, setup, verification_status
        if args.action == "setup":
            result = setup(project, args.source, args.fetch_cache)
        elif args.action == "search":
            result = search(project, args.target)
        elif args.action == "status":
            result = verification_status(project)
        else:
            result = run_lean(project, args.action, args.target)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, dict) and result.get("exit_code"):
            return result["exit_code"]
    elif args.cmd == "mcp":
        from .mcp import serve
        serve(project, args.role, args.depth, args.read_only)
