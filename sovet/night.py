"""Bounded unattended turns using the existing CLI adapters and shared queue."""
from __future__ import annotations

import json
import math
import signal
import threading
import time
from contextlib import ExitStack
from pathlib import Path

from .core import Project, atomic_write, lock
from .integrations import scan_files, sync_comments
from .processes import tracked
from .runtime import NativeRun, execute_next, worker_lock


class NightRun:
    def __init__(self, project, task, hours=8, max_turns=32, output=print):
        if not math.isfinite(hours) or hours <= 0 or max_turns < 1:
            raise ValueError("Нужны конечные --hours > 0 и --max-turns >= 1")
        self.project = project
        self.task = (project.root / task).resolve()
        if not self.task.is_relative_to(project.root) or not self.task.is_file():
            raise ValueError("--task: нужен существующий файл задания внутри проекта")
        if not self.task.read_text().strip():
            raise ValueError("Файл задания пуст")
        self.hours, self.max_turns, self.output = hours, max_turns, output
        self.stopped = threading.Event()
        self.controller = None

    def stop(self, *_):
        self.stopped.set()
        if self.controller:
            self.controller.cancel()

    def plan(self):
        p = self.project
        return {"project": str(p.root), "task": str(self.task.relative_to(p.root)),
                "hours": self.hours, "max_turns": self.max_turns,
                "writer": p.config["writer"], "models": p.config["models"], "effort": p.config["effort"],
                "mode": "headless; exclusive role leases; stops on the first failed turn",
                "note": "Native tmux agent panes must be closed for this project. No models run in --dry-run."}

    def pending(self, role=None):
        with self.project.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE status='pending' " +
                             ("AND role=? " if role else "") + "ORDER BY id LIMIT 1",
                             (role,) if role else ()).fetchone()
            return dict(row) if row else None

    def seed(self):
        text = self.task.read_text()
        if not text.strip():
            raise ValueError("Файл задания стал пустым; остановка")
        self.project.enqueue(self.project.config["writer"],
            f"Ночной исследовательский цикл. Перечитай {self.task.relative_to(self.project.root)}, "
            "общую память, последние замечания ревьюера и актуальные исходники. "
            "Сделай следующий ограниченный проверяемый шаг по заданию. "
            "Обнови NIGHT_REPORT.md: результаты, точные предположения, проверки с командами, "
            "нерешённые вопросы и следующий шаг. Сохрани полезный checkpoint перед завершением хода. "
            "Не жди ответа коллеги: независимое ревью запустит координатор. Не запускай дополнительные CLI.\n\n" + text,
            origin="night")

    def run(self):
        p = self.project
        writer = p.config["writer"]
        reviewer = "codex" if writer == "claude" else "claude"
        with ExitStack() as stack:
            try:
                stack.enter_context(lock(p.team / "night.lock", blocking=False))
                # Hold BOTH leases for the entire run, including gaps between turns.
                for role in ("codex", "claude"):
                    stack.enter_context(worker_lock(p, role))
            except BlockingIOError:
                raise ValueError("Роль занята: сначала закройте нативные панели этого проекта (sovet stop).") from None
            with p.db() as db:
                running = list(db.execute("SELECT id FROM jobs WHERE status='running'"))
            if running:
                raise ValueError("В очереди остались незавершённые running-задачи. Проверьте их через sovet status; автоматический повтор не выполняется.")
            if threading.current_thread() is threading.main_thread():
                for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                    previous = signal.signal(sig, self.stop)
                    stack.callback(signal.signal, sig, previous)
            stack.enter_context(tracked("sovet", "night", p.root, self.plan()))
            deadline = time.monotonic() + self.hours * 3600
            started = time.time()
            turns, reason, error = 0, "max_turns", None
            p.emit("night", "start", json.dumps(self.plan(), ensure_ascii=False))
            self.output(f"Ночной цикл: {writer} пишет → {reviewer} проверяет; {self.hours:g} ч, до {self.max_turns} ходов. Ctrl+C — остановить.")
            try:
                while turns < self.max_turns:
                    if self.stopped.is_set():
                        reason = "stopped"
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        reason = "deadline"
                        break
                    # Reload author requirements/settings and changes before each turn.
                    p.config = Project(p.root).config
                    if p.config["writer"] != writer:
                        raise ValueError("Писатель изменён во время цикла; остановка перед следующим ходом")
                    scan_files(p)
                    if p.config.get("profile") == "math-research" or p.config.get("reader"):
                        sync_comments(p)
                    job = self.pending()
                    if job is None:
                        self.seed()
                        job = self.pending()
                    p.config["turn_timeout"] = min(p.config["turn_timeout"], remaining)
                    self.controller = NativeRun()
                    result = execute_next(p, job["role"], output=self.output, controller=self.controller)
                    self.controller = None
                    turns += 1
                    if not result or not result["ok"]:
                        if self.stopped.is_set():
                            reason = "stopped"
                        elif time.monotonic() >= deadline:
                            reason = "deadline"
                        else:
                            reason, error = "failed", (result or {}).get("error", "Нет результата хода")
                        break
                    if job["role"] == writer and self.pending(reviewer) is None:
                        p.enqueue(reviewer,
                            f"Независимо проверь последний результат писателя по {self.task.relative_to(p.root)} "
                            "и NIGHT_REPORT.md. Читай действительные исходники и свидетельства проверок. "
                            "Проверь постановку, применимость известных результатов, математические выводы, "
                            "модель оракула, все затраты и отличия от работ Тюрина. Не редактируй исходники. "
                            "Верни конкретные замечания и следующий полезный шаг. Компиляция Lean сама по себе "
                            "не доказывает соответствие постановки задаче автора. Не запускай дополнительных агентов.",
                            origin="night", read_only=True, reply_to=writer)
            except Exception as exc:
                reason, error = "failed", str(exc)
            finally:
                p.config = Project(p.root).config
                report = {**self.plan(), "started": started, "finished": time.time(), "turns": turns,
                          "reason": reason, "error": error, "pending_job": (self.pending() or {}).get("id"),
                          "research_report": "NIGHT_REPORT.md", "logs": ".team/logs"}
                atomic_write(p.team / "night/latest.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                p.emit("night", "stop", json.dumps(report, ensure_ascii=False))
                self.output(f"Ночной цикл завершён: {reason}; ходов {turns}. " + (error or "Checkpoint: .team/night/latest.json"))
            return report
