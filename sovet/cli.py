from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .core import MEMORIES, ROLES, Project, digest, find_root, lock
from .integrations import chitalka, install_aris, metadata, scan_files, sync_comments, sync_aris_profile
from .runtime import LAUNCHER, execute_next, worker_lock

NAMES = {"codex": "Codex", "claude": "Claude"}
# Internal entry points launched by tmux, hooks or MCP clients. Added without help=,
# so argparse omits them from --help; completion.py hides them from Tab.
INTERNAL = ("native", "panel", "hook", "mcp", "watch")
DETACHED_SIZE = (160, 45)  # no terminal to measure: room for two ~80-column TUIs, not tmux 80x24
PLACEHOLDER = "sleep 600"
PANE_FORMAT = "\t".join(["#{window_id}", "#{window_name}", "#{@sovet_window}", "#{@sovet_cmd}", "#{pane_id}",
                         "#{@sovet_role}", "#{pane_dead}", "#{pane_pid}", "#{pane_start_command}"])


def flags(parser):
    profile = parser.add_mutually_exclusive_group()
    profile.add_argument("--math-research", action="store_const", const="math-research", dest="profile",
                         help="Lean/LongTails, ARIS, chitalka")
    profile.add_argument("--programming", action="store_const", const="programming", dest="profile",
                         help="без исследовательских инструментов")
    parser.add_argument("--writer", choices=ROLES, help="кто меняет файлы (второй агент — ревьюер)")
    for role in ROLES:
        parser.add_argument(f"--{role}-model", metavar="ID", help=f"точный ID модели {NAMES[role]}")
        parser.add_argument(f"--{role}-effort", choices=["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
                            if role == "codex" else ["low", "medium", "high", "xhigh", "max"])


def configure(project, args, persist=True):
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
    if getattr(args, "reader", False) or getattr(args, "pdf", None) or getattr(args, "latex_watch", False):
        project.config["reader"] = True
        # Remembered so a later plain `sovet up` reopens chitalka the same way.
        project.config["reader_options"] = {"pdf": getattr(args, "pdf", None),
                                            "watch": bool(getattr(args, "latex_watch", False))}
    if persist:
        project.save_config()
        sync_aris_profile(project)


def session_name(project):
    slug = "".join(c if c.isascii() and c.isalnum() else "-" for c in project.root.name)[:25]
    return f"sovet-{slug}-{digest(str(project.root).encode())[:6]}"


def launch_command(project, *args):
    return shlex.join([sys.executable, str(LAUNCHER), "--project", str(project.root), *args])


def role_label(project, role):
    cfg = project.config
    duty = "писатель" if role == cfg["writer"] else "ревьюер"
    return f"{NAMES[role]} · {duty} · {cfg['models'][role]} · {cfg['effort'][role]}"


def reader_command(project, args):
    options = project.config.get("reader_options") or {}
    reader = ["reader"]
    if options.get("pdf"):
        reader.append(options["pdf"])
    if options.get("watch"):
        reader.append("--watch")
    if getattr(args, "no_browser", False):
        reader.append("--no-browser")
    return launch_command(project, *reader)


def window_size(detached):
    """The real terminal when attaching (never larger: no cropping or panning); 160x45 otherwise."""
    if detached:
        return DETACHED_SIZE
    size = shutil.get_terminal_size((0, 0))
    return (size.columns, size.lines) if size.columns and size.lines else DETACHED_SIZE


class Tmux:
    """Queries always run (read-only); mutations are printed instead of executed in dry-run."""

    def __init__(self, dry_run=False):
        self.dry_run = dry_run

    def query(self, *args):
        if not shutil.which("tmux"):
            return None
        result = subprocess.run(["tmux", *args], capture_output=True, text=True)
        return result.stdout if result.returncode == 0 else None

    def __call__(self, *args, output=None):
        """Run a mutation; with output (a dry-run stand-in such as "@1\t%1") return its -P output."""
        if self.dry_run:
            print("tmux " + shlex.join(args))
            return output or ""
        if output:
            return subprocess.check_output(["tmux", *args], text=True).strip()
        subprocess.run(["tmux", *args], check=True, stdout=subprocess.DEVNULL)
        return ""


def pane_role(command):
    """Role launched by a sovet pane command, ignoring any words inside the project path."""
    found = re.findall(r"(?:^|[\s'\"])(native|panel)\s+(codex|claude)\b", command or "")
    return found[-1] if found else (None, None)


def inventory(t, name):
    panes = []
    for line in (t.query("list-panes", "-s", "-t", "=" + name, "-F", PANE_FORMAT) or "").splitlines():
        parts = line.split("\t", 8)
        if len(parts) != 9:
            continue
        window, window_name, kind, service, pane, role, dead, pid, command = parts
        launched, launched_role = pane_role(command)
        if not kind and window_name in ("agents", "watch", "chitalka"):
            kind = window_name  # sessions created before window tags existed
        panes.append({"window": window, "window_kind": kind, "pane": pane, "role": role or launched_role,
                      "dead": dead == "1", "pid": int(pid) if pid.isdecimal() else None,
                      "launch": launched, "service": service})
    return panes


def tmux_state(project):
    name = session_name(project)
    t = Tmux()
    exists = t.query("has-session", "-t", "=" + name) is not None
    panes = inventory(t, name) if exists else []
    agents = {p["role"]: p for p in panes if p["window_kind"] == "agents" and p["role"] in ROLES}
    services = {p["window_kind"]: p for p in panes if p["window_kind"] in ("watch", "chitalka")}
    return {"session": name, "exists": exists, "agents": agents, "services": services}


def tmux(project, args):
    dry = getattr(args, "dry_run", False)
    restart = getattr(args, "restart", False)
    missing = [tool for tool in ("tmux", *ROLES) if not shutil.which(tool)]
    if missing and not dry:
        raise ValueError(f"Не найдено в PATH: {', '.join(missing)}. Проверьте: sovet doctor")
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    t = Tmux(dry)
    name = session_name(project)
    target = "=" + name
    root = str(project.root)
    exists = t.query("has-session", "-t", target) is not None
    panes = inventory(t, name) if exists else []

    def window_of(kind):
        return next((p["window"] for p in panes if p["window_kind"] == kind), None)

    agents_window = window_of("agents")
    agents = {p["role"]: p for p in panes if p["window"] == agents_window and p["role"] in ROLES}
    legacy = [r for r, p in agents.items() if p["launch"] == "panel" and not p["dead"]]
    if legacy and not restart:
        running = [j for j in project.jobs() if j["status"] == "running" and j["role"] in legacy]
        if running:
            raise ValueError(
                f"В {name} ещё открыта старая curses-панель ({', '.join(legacy)}), и она выполняет задачу "
                f"#{running[0]['id']} в фоновом режиме. Автоматическая замена оборвала бы этот ход посреди правок. "
                "Дождитесь окончания (sovet status) или перезапустите явно: sovet up --restart")

    def open_window(kind):
        # Start with a placeholder so remain-on-exit is set before any real program can exit.
        stand_in = f"@{kind}\t%{'codex' if kind == 'agents' else kind}"
        if not exists and kind == "agents":
            cols, rows = window_size(getattr(args, "detach", False) or not interactive)
            out = t("new-session", "-d", "-P", "-F", "#{window_id}\t#{pane_id}", "-s", name, "-n", kind,
                    "-x", str(cols), "-y", str(rows), "-c", root, PLACEHOLDER, output=stand_in)
        else:
            out = t("new-window", "-d", "-P", "-F", "#{window_id}\t#{pane_id}", "-t", target + ":", "-n", kind,
                    "-c", root, PLACEHOLDER, output=stand_in)
        window, pane = out.split("\t")
        t("set-option", "-w", "-t", window, "@sovet_window", kind)
        t("set-option", "-w", "-t", window, "remain-on-exit", "on")
        return window, pane

    created = not exists
    try:
        if agents_window is None:
            agents_window, first = open_window("agents")
            agents = {"codex": {"pane": first, "dead": False, "launch": None}}
        else:
            t("set-option", "-w", "-t", agents_window, "remain-on-exit", "on")
            t("set-option", "-w", "-t", agents_window, "@sovet_window", "agents")
        t("set-option", "-w", "-t", agents_window, "pane-border-status", "top")
        t("set-option", "-w", "-t", agents_window, "pane-border-format",
          " #{?pane_active,▶ ,}#{?#{@sovet_label},#{@sovet_label},#{pane_title}} ")
        t("set-option", "-t", target + ":", "mouse", "on")  # option targets need "=name:", not "=name"
        for role in ROLES:  # codex first: it is the left column
            command = launch_command(project, "native", role)
            pane = agents.get(role)
            if pane is None:
                peer = agents.get("claude" if role == "codex" else "codex")
                anchor = peer["pane"] if peer else next(p["pane"] for p in panes if p["window"] == agents_window)
                side = ["-b"] if role == "codex" else []  # codex left of claude, claude right of codex
                new = t("split-window", "-h", *side, "-d", "-P", "-F", "#{pane_id}", "-t", anchor,
                        "-c", root, command, output=f"%{role}")
                agents[role] = {"pane": new}
            elif pane["dead"] or pane["launch"] != "native" or restart:
                t("respawn-pane", "-k", "-t", pane["pane"], "-c", root, command)
            t("set-option", "-p", "-t", agents[role]["pane"], "@sovet_role", role)
            t("set-option", "-p", "-t", agents[role]["pane"], "@sovet_label", role_label(project, role))
        t("select-layout", "-t", agents_window, "even-horizontal")

        services = {"watch": launch_command(project, "watch")}
        if project.config.get("reader"):
            services["chitalka"] = reader_command(project, args)
        elif window_of("chitalka"):
            t("kill-window", "-t", window_of("chitalka"))
        for kind, command in services.items():
            window = window_of(kind)
            if window is None:
                window, pane = open_window(kind)
            else:
                current = next(p for p in panes if p["window"] == window)
                pane = current["pane"]
                t("set-option", "-w", "-t", window, "@sovet_window", kind)
                if not (current["dead"] or restart):
                    if kind == "chitalka" and current["service"] != command and (
                            args.reader or args.pdf or args.latex_watch):
                        print("chitalka уже открыта с другими параметрами; они применятся после sovet up --restart")
                    continue
            t("set-option", "-w", "-t", window, "@sovet_cmd", command)
            t("respawn-pane", "-k", "-t", pane, "-c", root, command)
        t("select-window", "-t", agents_window)
        t("select-pane", "-t", agents[project.config["writer"]]["pane"])
    except Exception:
        if created and not dry:
            subprocess.run(["tmux", "kill-session", "-t", target], capture_output=True)
        raise
    if dry:
        return 0
    writer = NAMES[project.config["writer"]]
    hint = (f"tmux {name}: слева Codex, справа Claude. Курсор в панели {writer} (писатель).\n"
            "Ctrl-b ←/→ или щелчок мышью — другая панель; Ctrl-b w — окна watch/chitalka; "
            "Ctrl-b d — отсоединиться (агенты продолжат работу).\n"
            "Вернуться: sovet up · состояние: sovet status · остановить: sovet stop")
    if os.environ.get("TMUX") and not args.detach:
        subprocess.run(["tmux", "switch-client", "-t", target], check=True)
        return 0
    if args.detach or not interactive:
        print(hint + ("" if args.detach else "\nНет терминала, поэтому сессия открыта в фоне."))
        return 0
    code = subprocess.call(["tmux", "attach-session", "-t", target])
    print(hint.splitlines()[-1])
    return code


def live_processes(project):
    """ps-backed sovet processes of this project, or None when ps is unavailable."""
    from .sessions import list_sessions
    try:
        return [p for group in list_sessions(project.root) for p in group["processes"]]
    except RuntimeError:
        return None


def wait_for_role(project, role, timeout=20):
    """A just-replaced pane may still be releasing the role lock; wait briefly instead of failing."""
    deadline = time.monotonic() + timeout
    announced = False
    while True:
        try:
            with worker_lock(project, role):
                return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise ValueError(f"{NAMES[role]} этого проекта уже открыт в другом процессе (sovet status). "
                                 "Второй экземпляр в том же разговоре не запускается.")
            if not announced:
                print(f"Жду, пока предыдущий процесс {NAMES[role]} освободит роль…", flush=True)
                announced = True
            time.sleep(0.25)


def watch(project, once=False):
    stop = threading.Event()
    def end(*_):
        stop.set()
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, end)
    with lock(project.team / "watch.lock", blocking=False):
        if not once:
            print(f"sovet watch: {project.root}\nСледит за правками файлов и комментариями chitalka. "
                  "Это служебное окно; агенты — в окне agents (Ctrl-b w).", flush=True)
        last_error = None
        while not stop.is_set():
            try:
                project.config = Project(project.root).config
                changed = scan_files(project)
                feedback = sync_comments(project) if (project.config.get("profile") == "math-research" or
                                                       project.config.get("reader")) else 0
                if changed or feedback:
                    print(f"{time.strftime('%H:%M:%S')} изменений: {changed}; комментариев автора: {feedback}", flush=True)
                last_error = None
            except Exception as e:
                if str(e) != last_error:
                    project.emit("watch", "error", str(e))
                    print(f"watch: {e}", file=sys.stderr, flush=True)
                last_error = str(e)
            if once:
                return
            stop.wait(1)


STATUS = {"pending": "ждёт", "running": "выполняется", "completed": "готово", "failed": "ошибка"}
DELIVERY = ("Задачи очереди получает нативная панель роли: сразу после завершения текущего хода (Stop hook) "
            "или, если агент простаивает, при вашем следующем сообщении в его панели. "
            "Сама по себе простаивающая панель ничего не начинает.")


def status_data(project):
    processes = live_processes(project)
    state = tmux_state(project)
    agents = {}
    for role in ROLES:
        live = [p for p in processes or [] if p["kind"] in (f"native:{role}", f"panel:{role}")]
        agents[role] = {**project.agent(role), "model": project.config["models"][role],
                        "effort": project.config["effort"][role],
                        "duty": "writer" if role == project.config["writer"] else "reviewer",
                        "pids": [p["pid"] for p in live],
                        "legacy_panel": any(p["kind"].startswith("panel:") for p in live),
                        # The agents table may still hold a pre-native batch ID; only this one is resumed.
                        "native_session": metadata(project, f"native:{role}:session"),
                        "pane": state["agents"].get(role)}
    services = {kind: [p["pid"] for p in processes or [] if p["kind"] == kind] for kind in ("watch", "reader")}
    return {"project": str(project.root), "profile": project.config["profile"], "writer": project.config["writer"],
            "reader": bool(project.config.get("reader")), "tmux": {"session": state["session"], "exists": state["exists"]},
            "ps_available": processes is not None, "agents": agents, "services": services, "jobs": project.jobs()}


def status_text(data):
    def short(text, width=64):
        line = " ".join(str(text or "").split())
        return line if len(line) <= width else line[:width - 1] + "…"
    lines = [f"sovet · {data['project']}",
             f"Профиль: {data['profile']} · chitalka: {'да' if data['reader'] else 'нет'} · "
             f"писатель: {NAMES[data['writer']]}",
             f"tmux: {data['tmux']['session']} — " + ("запущена (открыть: sovet up)" if data["tmux"]["exists"]
                                                       else "не запущена (sovet up)"), ""]
    for role in (data["writer"], *[r for r in ROLES if r != data["writer"]]):
        a = data["agents"][role]
        side = "слева" if role == "codex" else "справа"
        if a["legacy_panel"]:
            state = "старая curses-панель → sovet up --restart"
        elif a["pids"]:
            state = f"открыт, PID {','.join(map(str, a['pids']))}"
        elif a["pane"] and a["pane"]["dead"]:
            state = "панель закрыта (sovet up перезапустит)"
        else:
            state = "не запущен" if data["ps_available"] else "неизвестно (ps недоступен)"
        session = f" · разговор {a['native_session'][:8]}…" if a.get("native_session") else ""
        model = f"{a['model']} · {a['effort']}"
        lines.append(f"  {NAMES[role]:<7}{('писатель' if a['duty'] == 'writer' else 'ревьюер'):<10}"
                     f"{model:<28} {side:<7} {state}{session}")
    services = " · ".join(f"{name} PID {','.join(map(str, pids))}" if pids else f"{name}: нет"
                          for name, pids in data["services"].items())
    lines += [f"  Службы: {services}", ""]
    jobs = data["jobs"]
    waiting = [j for j in jobs if j["status"] == "pending"]
    running = [j for j in jobs if j["status"] == "running"]
    lines.append(f"Очередь: ждут {len(waiting)}, выполняются {len(running)}")
    shown = (running + waiting)[:8] or jobs[:3]
    for j in shown:
        lines.append(f"  #{j['id']:<4} → {NAMES[j['role']]:<7}{STATUS.get(j['status'], j['status']):<12}"
                     f"от {j['origin']:<12} {short(j['prompt'])}")
    if waiting:
        lines += ["", DELIVERY]
    lines.append("Подробно: sovet events · sovet status --json")
    return "\n".join(lines)


def queued_message(project, role, job):
    live = live_processes(project)
    open_now = live is not None and any(p["kind"] == f"native:{role}" for p in live)
    where = (f"{NAMES[role]} открыт: получит её после текущего хода или при вашем следующем сообщении в его панели."
             if open_now else f"{NAMES[role]} сейчас не открыт: sovet up, затем напишите агенту в его панели.")
    return f"Задача #{job} поставлена в очередь {NAMES[role]}. {where}"


def parser():
    p = argparse.ArgumentParser(
        prog="sovet", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Два нативных агента в одном окне tmux: Codex слева, Claude справа.\n"
                    "Писатель меняет файлы, ревьюер проверяет. Общая память проекта, ARIS, chitalka, Lean.",
        epilog="Начало: sovet init && sovet up. Подробно: README.md, docs/usage.md.")
    p.add_argument("-C", "--project", help="путь к проекту (можно из подкаталога)")
    p.add_argument("--version", action="version", version=f"sovet {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="КОМАНДА")
    init = sub.add_parser("init", help="создать .team/ в проекте")
    init.add_argument("path", nargs="?", default=".")
    init.add_argument("--brief", default="")
    init.add_argument("--aris", type=Path, help="локальный checkout ARIS")
    flags(init)
    up = sub.add_parser("up", help="открыть Codex и Claude рядом в tmux (или вернуться в сессию)")
    up.add_argument("--detach", action="store_true", help="запустить в фоне, не подключаясь")
    up.add_argument("--restart", action="store_true",
                    help="перезапустить агентов и службы этой сессии (история разговоров сохраняется)")
    up.add_argument("--dry-run", action="store_true", help="показать команды tmux и ничего не менять")
    up.add_argument("--reader", action="store_true", help="также открыть chitalka (запоминается)")
    up.add_argument("--document", "--pdf", dest="pdf", metavar="DOCUMENT", help="Markdown или PDF для chitalka (запоминается)")
    up.add_argument("--latex-watch", action="store_true", help="chitalka пересобирает LaTeX (запоминается)")
    up.add_argument("--no-browser", action="store_true", help="не открывать браузер для chitalka")
    flags(up)
    config = sub.add_parser("config", help="показать/изменить конфиг проекта; флаги сохраняются")
    flags(config)
    for name in ("native", "panel"):
        s = sub.add_parser(name)  # panel: pre-native alias kept for old tmux sessions
        s.add_argument("role", choices=ROLES)
    night = sub.add_parser("night", help="ограниченный ночной цикл без TUI: писатель → ревьюер → исправления")
    night.add_argument("--task", default="RESEARCH_BRIEF.md", help="файл задания внутри проекта")
    night.add_argument("--hours", type=float, default=8, help="максимальная длительность (по умолчанию 8 ч)")
    night.add_argument("--max-turns", type=int, default=32, help="не более указанного числа ходов обоих агентов")
    night.add_argument("--retries", type=int, default=3, help="повторы сетевого сбоя или известного лимита на задачу (по умолчанию 3)")
    night.add_argument("--dry-run", action="store_true", help="показать план, не запускать модели")
    run = sub.add_parser("run", help="выполнить одну задачу очереди без TUI (только если роль не открыта)")
    run.add_argument("role", choices=ROLES)
    send = sub.add_parser("send", help="поставить задачу в очередь агента")
    send.add_argument("role", choices=ROLES)
    send.add_argument("text", help="текст или - для stdin")
    send.add_argument("--read-only", action="store_true")
    send.add_argument("--json", action="store_true")
    review = sub.add_parser("review", help="поставить ревьюеру задачу проверить текущий результат")
    review.add_argument("text", nargs="?", default="Проверьте актуальные изменения, требования и результаты проверок. Сообщите конкретные замечания.")
    review.add_argument("--json", action="store_true")
    events = sub.add_parser("events", help="общая история")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--json", action="store_true")
    status = sub.add_parser("status", help="кто открыт, tmux, очередь")
    status.add_argument("--json", action="store_true")
    sessions = sub.add_parser("sessions", aliases=["ps"], help="все запущенные сессии по ps")
    sessions.add_argument("--json", action="store_true")
    sessions.add_argument("--ids", action="store_true", help=argparse.SUPPRESS)
    stop = sub.add_parser("stop", help="остановить сессию и её дочерние процессы")
    stop.add_argument("target", nargs="?", help="ID сессии или PID; по умолчанию текущий проект")
    stop.add_argument("--all", action="store_true", help="все сессии sovet текущего пользователя")
    stop.add_argument("--force", action="store_true", help="SIGKILL оставшихся после SIGTERM")
    stop.add_argument("--timeout", type=float, default=8)
    stop.add_argument("--json", action="store_true")
    doctor = sub.add_parser("doctor", help="проверить локальные инструменты без вызова моделей")
    doctor.add_argument("--json", action="store_true")
    completion = sub.add_parser("completion", help="Tab-автодополнение zsh/bash")
    completion.add_argument("shell", choices=["zsh", "bash"])
    completion.add_argument("--install", action="store_true", help="подключить в конфиг текущего пользователя")
    w = sub.add_parser("watch")
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
    reader = sub.add_parser("reader", help="открыть Markdown или PDF в chitalka")
    reader.add_argument("pdf", nargs="?", metavar="DOCUMENT")
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
    hook = sub.add_parser("hook")
    hook.add_argument("--role", choices=ROLES, required=True)
    hook.add_argument("--event", required=True)
    mcp = sub.add_parser("mcp")
    mcp.add_argument("--role", choices=ROLES, required=True)
    mcp.add_argument("--depth", type=int, default=0)
    mcp.add_argument("--read-only", action="store_true")
    mcp.add_argument("--native", action="store_true")
    return p


def stdin_text(value):
    return sys.stdin.read() if value == "-" else value


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_complete"]:
        from .completion import complete
        print("\n".join(complete(argv[1:])))
        return 0
    args = parser().parse_args(argv)
    try:
        if args.cmd in ("native", "panel"):
            # run_native registers the harness itself as native:ROLE; this is its launcher.
            from .processes import tracked
            with tracked("sovet", "launcher:" + args.role, find_root(args.project)):
                return dispatch(args) or 0
        if args.cmd in ("run", "watch", "reader", "mcp") or (args.cmd == "research" and args.action == "setup"):
            from .processes import tracked
            root = find_root(args.project)
            kind = args.cmd + (":" + args.role if args.cmd == "run" else "")
            with tracked("sovet", kind, root):
                return dispatch(args) or 0
        return dispatch(args) or 0
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"sovet: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def dispatch(args):
    if args.cmd == "completion":
        from .completion_support import install, script
        print(install("sovet", args.shell) if args.install else script("sovet", args.shell), end="\n" if args.install else "")
        return
    if args.cmd in ("sessions", "ps"):
        from .sessions import list_sessions, display
        items = list_sessions(args.project)
        print("\n".join(item["id"] for item in items) if args.ids else
              json.dumps(items, ensure_ascii=False, indent=2) if args.json else display(items))
        return
    if args.cmd == "stop":
        from .sessions import stop_sessions
        result = stop_sessions(args.target, args.project, args.all, args.force, args.timeout)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else
              f"Остановлены PID: {result['stopped'] or '-'}; остались: {result['remaining'] or '-'}"
              + ("" if result["remaining"] else "\nИстория, память .team/ и очередь сохранены; sovet up — запустить снова."))
        return 1 if result["remaining"] or result["errors"] else 0
    if args.cmd == "init":
        project = Project.init(Path(args.project or args.path), writer=args.writer or "claude", brief=args.brief)
        configure(project, args)
        repo = args.aris or Path.home() / "code/aris"
        if args.aris or (repo / "skills/research-wiki/SKILL.md").exists():
            install_aris(project, repo)
        scan_files(project)
        print(f"Готово: {project.root}\nПисатель: {project.config['writer']}; профиль: {project.config['profile']}")
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
        configure(project, args, persist=not getattr(args, "dry_run", False))
        if args.cmd == "config":
            print(json.dumps(project.config, ensure_ascii=False, indent=2))
        else:
            return tmux(project, args)
    elif args.cmd in ("native", "panel"):
        wait_for_role(project, args.role)
        try:
            from .native import run_native
        except ImportError as e:
            raise RuntimeError(f"нативный запуск недоступен в этой версии sovet: {e}") from e
        return run_native(project, args.role)
    elif args.cmd == "hook":
        from .bridge import main_hook
        return main_hook(project, args.role, args.event)
    elif args.cmd == "night":
        from .night import NightRun
        runner = NightRun(project, args.task, args.hours, args.max_turns, retries=args.retries)
        if args.dry_run:
            print(json.dumps(runner.plan(), ensure_ascii=False, indent=2))
        else:
            report = runner.run()
            return 1 if report["error"] else 0
    elif args.cmd == "run":
        live = live_processes(project) or []
        if any(p["kind"] in (f"native:{args.role}", f"panel:{args.role}") for p in live):
            raise ValueError(f"{NAMES[args.role]} открыт в tmux; задачу получит его панель. "
                             "sovet run нужен только когда роль не открыта.")
        try:
            with worker_lock(project, args.role):
                result = execute_next(project, args.role)
        except BlockingIOError:
            raise ValueError(f"{NAMES[args.role]} этого проекта уже занят другим процессом (sovet status)") from None
        if result is None:
            print("Очередь пуста.")
        return 1 if result and not result["ok"] else 0
    elif args.cmd in ("send", "review"):
        if args.cmd == "send":
            role = args.role
            job = project.enqueue(role, stdin_text(args.text), read_only=args.read_only)
        else:
            role = next(r for r in ROLES if r != project.config["writer"])
            job = project.enqueue(role, args.text, read_only=True, reply_to=project.config["writer"])
        print(json.dumps({"job": job, "role": role}) if args.json else queued_message(project, role, job))
    elif args.cmd == "events":
        rows = project.events(args.after, limit=10000)
        print(json.dumps(rows, ensure_ascii=False, indent=2) if args.json else
              "\n\n".join(f"#{e['id']} {e['actor']} / {e['kind']}\n{e['body']}" for e in rows))
    elif args.cmd == "status":
        data = status_data(project)
        print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else status_text(data))
    elif args.cmd == "watch":
        watch(project, args.once)
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
        if args.native:
            serve(project, args.role, args.depth, args.read_only, native=True)
        else:
            serve(project, args.role, args.depth, args.read_only)
