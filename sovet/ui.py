"""Two terminal clients backed by a durable inbox, not keystroke injection into CLIs."""
from __future__ import annotations

import curses
import queue
import signal
import textwrap
import threading
import time
from collections import deque

from .core import Project
from .runtime import NativeRun, execute_next, worker_lock


def panel(project, role):
    output = queue.Queue()
    stop = threading.Event()
    controller = [None]
    busy = threading.Event()

    def shutdown(*_):
        stop.set()
        if controller[0]:
            controller[0].cancel()

    def work():
        try:
            with worker_lock(project, role):
                # Never silently rerun a possibly partly applied task after a crash.
                with project.db() as db:
                    db.execute("UPDATE jobs SET status='failed',error='Worker stopped during previous turn; inspect changes before retry',"
                               "finished=? WHERE role=? AND status='running'", (time.time(), role))
                while not stop.is_set():
                    project.config = Project(project.root).config
                    run = NativeRun()
                    controller[0] = run
                    busy.set()
                    job = execute_next(project, role, output.put, run)
                    busy.clear()
                    controller[0] = None
                    if job is None:
                        stop.wait(0.3)
        except BlockingIOError:
            output.put("У этого агента уже запущен worker. Закройте вторую панель.")
            stop.set()
        except Exception as e:
            output.put(f"Worker error: {e}")
            stop.set()

    thread = threading.Thread(target=work, daemon=True)
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, shutdown)
    def interrupt(*_):
        if controller[0]:
            controller[0].cancel()
            output.put("Отменяю текущий ход…")
    signal.signal(signal.SIGINT, interrupt)

    def screen(win):
        curses.curs_set(1)
        win.timeout(100)
        win.keypad(True)
        history = deque(maxlen=2000)
        history.extend([f"sovet / {role} — {project.root}",
                        "Сообщение + Enter. /ask TEXT → коллеге, /sync, /status, /quit.",
                        "Ctrl+C отменяет ход, PgUp/PgDn — журнал. Правки поступают перед следующим ходом."])
        for e in project.events(limit=500)[-15:]:
            if e["actor"] == role and e["kind"] in ("answer", "error"):
                history.extend(e["body"].splitlines())
        buf, pos, scroll = "", 0, 0
        while not stop.is_set():
            while True:
                try:
                    text = output.get_nowait()
                except queue.Empty:
                    break
                # Strip terminal control sequences from model/tool output.
                import re
                text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", str(text)).replace("\x00", "")
                history.extend(text.splitlines() or [""])
            height, width = win.getmaxyx()
            if height < 6 or width < 15:
                win.erase()
                win.refresh()
                time.sleep(0.1)
                continue
            lines = []
            for line in history:
                lines.extend(textwrap.wrap(line, max(1, width - 2), replace_whitespace=False) or [""])
            visible = height - 4
            scroll = min(scroll, max(0, len(lines) - visible))
            end = len(lines) - scroll
            win.erase()
            cfg = project.config
            state = "работает" if busy.is_set() else "ожидает"
            label = "исполнитель" if role == cfg["writer"] else "ревьюер"
            title = f" {role} · {cfg['models'][role]} / {cfg['effort'][role]} · {label} · {state} "
            win.addnstr(0, 0, title, width - 1, curses.A_REVERSE)
            for y, line in enumerate(lines[max(0, end-visible):end], 1):
                try:
                    win.addnstr(y, 0, line, width - 1)
                except curses.error:
                    pass
            unseen = len(project.events(after=project.agent(role)["cursor"], limit=999))
            win.addnstr(height - 3, 0, f"новых событий: {unseen} | очередь: SQLite | Ctrl+C отмена", width - 1, curses.A_DIM)
            start = max(0, pos - width + 5)
            win.addnstr(height - 2, 0, "> " + buf[start:], width - 1)
            win.move(height - 2, min(width - 2, pos - start + 2))
            win.refresh()
            try:
                key = win.get_wch()
            except curses.error:
                continue
            if key in ("\n", "\r"):
                text = buf.strip()
                buf, pos, scroll = "", 0, 0
                if not text:
                    continue
                history.append("> " + text)
                if text == "/quit":
                    shutdown()
                elif text == "/status":
                    for j in project.jobs()[:8]:
                        history.append(f"#{j['id']} {j['role']} {j['status']} {j['prompt'][:70]}")
                elif text == "/sync":
                    project.enqueue(role, "Прочитайте новые события и правки автора, согласуйте с текущим результатом.")
                elif text.startswith("/ask "):
                    peer = "claude" if role == "codex" else "codex"
                    project.enqueue(peer, text[5:], origin="user", read_only=peer != cfg["writer"])
                else:
                    project.enqueue(role, text)
            elif key == "\x03":
                if controller[0]:
                    controller[0].cancel()
                    history.append("Отменяю текущий ход…")
            elif key == "\x04":
                shutdown()
            elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                if pos:
                    buf = buf[:pos-1] + buf[pos:]
                    pos -= 1
            elif key == curses.KEY_DC:
                buf = buf[:pos] + buf[pos+1:]
            elif key == curses.KEY_LEFT:
                pos = max(0, pos - 1)
            elif key == curses.KEY_RIGHT:
                pos = min(len(buf), pos + 1)
            elif key in (curses.KEY_HOME, "\x01"):
                pos = 0
            elif key in (curses.KEY_END, "\x05"):
                pos = len(buf)
            elif key == "\x15":
                buf, pos = buf[pos:], 0
            elif key == curses.KEY_PPAGE:
                scroll += visible
            elif key == curses.KEY_NPAGE:
                scroll = max(0, scroll - visible)
            elif isinstance(key, str) and key.isprintable():
                buf = buf[:pos] + key + buf[pos:]
                pos += len(key)
    thread.start()
    try:
        curses.wrapper(screen)
    finally:
        shutdown()
        thread.join(timeout=12)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
