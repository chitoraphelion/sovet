"""Bounded unattended turns using the existing CLI adapters and shared queue."""
from __future__ import annotations

import json
import math
import signal
import threading
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from .core import Project, atomic_write, lock
from .integrations import scan_files, sync_comments
from .processes import tracked
from .runtime import NativeRun, execute_next, worker_lock


class NightRun:
    def __init__(self, project, task, hours=8, max_turns=32, output=print, retries=3, reviewer_after=None):
        if not math.isfinite(hours) or hours <= 0 or max_turns < 1:
            raise ValueError("Нужны конечные --hours > 0 и --max-turns >= 1")
        if retries < 0:
            raise ValueError("--retries должен быть >= 0")
        self.reviewer_not_before = 0.0
        if reviewer_after:
            try:
                when = datetime.fromisoformat(reviewer_after)
                if when.tzinfo is None:
                    raise ValueError("timezone required")
                self.reviewer_not_before = when.timestamp()
            except (ValueError, TypeError, OverflowError):
                raise ValueError("--reviewer-after: нужен ISO timestamp с timezone, например 2026-10-17T20:31:00+03:00") from None
        self.retries = retries
        self.retry_counts = {}
        self.original_jobs = {}
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
                "hours": self.hours, "max_turns": self.max_turns, "retries": self.retries,
                "reviewer_not_before": self.reviewer_not_before or None,
                "writer": p.config["writer"], "models": p.config["models"], "effort": p.config["effort"],
                "mode": "headless; exclusive role leases; bounded retries for network failures and known quota resets",
                "note": "Native tmux agent panes must be closed for this project. No models run in --dry-run."}

    def pending(self, role=None, include_deferred=False):
        writer = self.project.config["writer"]
        reviewer = "codex" if writer == "claude" else "claude"
        conditions, params = ["status='pending'"], []
        if role:
            conditions.append("role=?")
            params.append(role)
        if not include_deferred and time.time() < self.reviewer_not_before:
            conditions.append("role!=?")
            params.append(reviewer)
        with self.project.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE " + " AND ".join(conditions) + " ORDER BY id LIMIT 1", params).fetchone()
            return dict(row) if row else None

    def reviewer_wait(self, started, turns, deadline, reviewer):
        state = {**self.plan(), "started": started, "turns": turns,
                 "reason": "waiting_reviewer", "error": None,
                 "pending_job": (self.pending(reviewer, include_deferred=True) or {}).get("id"),
                 "resume_at": self.reviewer_not_before}
        atomic_write(self.project.team / "night/latest.json", json.dumps(state, ensure_ascii=False, indent=2) + "\n")
        self.output(f"Сохраняю ходы для ревью {reviewer}; ожидание до {datetime.fromtimestamp(self.reviewer_not_before).astimezone().isoformat()}.")
        self.wait_for_retry(max(0, self.reviewer_not_before-time.time()), deadline)

    def retry_delay(self, job, error):
        attempt = self.retry_counts.get(job["id"], 0)
        if attempt >= self.retries:
            return None
        message = error.lower()
        if message == "превышено время хода":
            return 0
        network = ("connection reset", "stream disconnected", "error sending request",
                   "request timed out", "connection timed out", "connection refused",
                   "failed to send request", "temporary failure in name resolution",
                   "http 502", "http 503", "http 504", "502 bad gateway", "503 service unavailable")
        if any(token in message for token in network):
            return 15 * (2 ** attempt)
        if "usage_limit_reached" in message or "hit your session limit" in message or "rate_limit" in message:
            # A warning is not a rejection. Wait only for an explicit provider reset.
            log = self.project.team / f"logs/{job['role']}-{job['id']}.jsonl"
            reset = None
            if log.exists():
                for line in log.read_text().splitlines():
                    try:
                        event = json.loads(line)
                        info = event.get("rate_limit_info") or {}
                        value = info.get("resetsAt")
                        if event.get("type") == "rate_limit_event" and info.get("status") == "rejected" and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                            reset = value
                    except (ValueError, AttributeError, TypeError):
                        continue
            if reset is not None:
                return max(60, reset - time.time() + 60)
        return None

    def queue_retry(self, job, checkpoint_only=False):
        original = self.original_jobs.get(job["id"], job)
        prompt = (f"Продолжи задачу #{original['id']} после временного отказа провайдера. "
                  "Используй сохранённую сессию. Перечитай текущие файлы, память и очередь: "
                  "могли сохраниться частичные правки и уже поставленные запросы ревью. "
                  "Не перезаписывай свежие правки, не начинай выполненную работу заново "
                  "и не дублируй существующие запросы коллегам.\n\n" + original["prompt"])
        if checkpoint_only and job["role"] != self.project.config["writer"]:
            prompt += ("\n\nТекущий ход — ТОЛЬКО короткий частичный вердикт после таймаута ревью. "
                       "Не редактируй файлы: сохрани read-only режим. Перечисли конкретные проверенные пункты, "
                       "обнаруженные ошибки и то, что осталось непроверенным. Не начинай новую формализацию. "
                       "Заверши ответ, чтобы ведущий мог сохранить checkpoint и продолжить.")
        elif checkpoint_only:
            prompt += ("\n\nТекущий ход — ТОЛЬКО восстановление checkpoint после таймаута. "
                       "Исходное задание выше дано для контекста, не пытайся выполнить его целиком. "
                       "Перечитай сохранённые правки и последние успешные проверки, обнови NIGHT_REPORT.md, "
                       "claims и общую память, явно отделив сделанное от незаконченного. "
                       "Не начинай новую исследовательскую ветку или новую формализацию. "
                       "Заверши ход коротким отчётом и одним конкретным следующим шагом; координатор продолжит работу.")
        new_id = self.project.enqueue(job["role"], prompt, origin="retry", depth=job["depth"],
                                      read_only=bool(job["read_only"]), reply_to=job["reply_to"])
        self.retry_counts[new_id] = self.retry_counts.get(job["id"], 0) + 1
        self.original_jobs[new_id] = original
        return new_id

    def wait_for_retry(self, delay, deadline):
        until = min(deadline, time.monotonic() + delay)
        while not self.stopped.is_set():
            remaining = until - time.monotonic()
            if remaining <= 0:
                break
            self.stopped.wait(min(30, remaining))

    def seed(self):
        text = self.task.read_text()
        if not text.strip():
            raise ValueError("Файл задания стал пустым; остановка")
        self.project.enqueue(self.project.config["writer"],
            f"Ночной исследовательский цикл. Перечитай {self.task.relative_to(self.project.root)}, "
            "общую память, последние замечания ревьюера и актуальные исходники. "
            "Сделай ровно один небольшой проверяемый шаг по заданию и заверши ход. "
            "Не объединяй сверку текущего результата, новую исследовательскую ветку и её формализацию в одном ходе. "
            "Обнови NIGHT_REPORT.md: результаты, точные предположения, проверки с командами, "
            "нерешённые вопросы и следующий шаг. Сохрани полезный checkpoint перед завершением хода. "
            "Не жди ответа коллеги: независимое ревью запустит координатор. Не запускай дополнительные CLI. "
            "Если ревьюер временно недоступен, продолжай ограниченные задачи самостоятельно, "
            "но явно отмечай новые результаты как не прошедшие независимое ревью.\n\n" + text,
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
            atomic_write(p.team / "night/latest.json", json.dumps({**self.plan(), "started": started,
                         "turns": 0, "reason": "running", "error": None}, ensure_ascii=False, indent=2) + "\n")
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
                    reserve = min(2, self.max_turns-1)
                    if (reserve and turns >= self.max_turns-reserve
                            and time.time() < self.reviewer_not_before
                            and self.pending(reviewer, include_deferred=True)):
                        self.reviewer_wait(started, turns, deadline, reviewer)
                        continue
                    job = self.pending()
                    if job is None:
                        self.seed()
                        job = self.pending()
                    p.config["turn_timeout"] = min(p.config["turn_timeout"], remaining)
                    atomic_write(p.team / "night/latest.json", json.dumps({**self.plan(), "started": started,
                                 "turns": turns, "reason": "running", "error": None,
                                 "active_job": job["id"], "active_role": job["role"], "updated": time.time()},
                                 ensure_ascii=False, indent=2) + "\n")
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
                            failure = (result or {}).get("error", "Нет результата хода")
                            delay = self.retry_delay(job, failure)
                            if delay is not None:
                                retry_id = self.queue_retry(job, checkpoint_only=failure == "Превышено время хода")
                                resume_at = time.time() + delay
                                wait_state = {**self.plan(), "started": started, "turns": turns,
                                              "reason": "waiting_retry", "error": failure,
                                              "pending_job": retry_id, "resume_at": resume_at,
                                              "retry_attempt": self.retry_counts[retry_id]}
                                atomic_write(p.team / "night/latest.json", json.dumps(wait_state, ensure_ascii=False, indent=2) + "\n")
                                p.emit("night", "waiting_retry", json.dumps(wait_state, ensure_ascii=False))
                                self.output(f"Временный сбой {job['role']}. Повтор {self.retry_counts[retry_id]}/{self.retries} задачи #{job['id']} через {delay:.0f} с; продолжение #{retry_id} сохранено.")
                                if turns >= self.max_turns:
                                    reason = "max_turns"
                                    break
                                if job["role"] == reviewer:
                                    self.reviewer_not_before = max(self.reviewer_not_before, resume_at)
                                    wait_state.update(reason="running", reviewer_not_before=self.reviewer_not_before)
                                    atomic_write(p.team / "night/latest.json", json.dumps(wait_state, ensure_ascii=False, indent=2) + "\n")
                                    self.output(f"{writer} продолжает работу; ревью {reviewer} остаётся в очереди до восстановления доступа.")
                                    continue
                                self.wait_for_retry(delay, deadline)
                                wait_state.update(reason="running", resume_at=None, error=None)
                                atomic_write(p.team / "night/latest.json", json.dumps(wait_state, ensure_ascii=False, indent=2) + "\n")
                                continue
                            reason, error = "failed", failure
                        break
                    if job["role"] == writer and self.pending(reviewer, include_deferred=True) is None:
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
                          "reason": reason, "error": error, "pending_job": (self.pending(include_deferred=True) or {}).get("id"),
                          "research_report": "NIGHT_REPORT.md", "logs": ".team/logs"}
                atomic_write(p.team / "night/latest.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                p.emit("night", "stop", json.dumps(report, ensure_ascii=False))
                self.output(f"Ночной цикл завершён: {reason}; ходов {turns}. " + (error or "Checkpoint: .team/night/latest.json"))
            return report
