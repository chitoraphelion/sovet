"""Native harness lifecycle hooks: bind IDs, inject context, drain bounded inboxes.

No terminal keystrokes, transcript rewriting, or competing headless session resume.
Idle native harnesses receive an inbox on their next real turn; Stop can continue
a currently running harness with one newly queued task.
"""
from __future__ import annotations

import json
import os
import sys
import time

from .core import Project, lock
from .integrations import metadata


def key(role, name):
    return f"native:{role}:{name}"


def active_job(project, role):
    job_id = metadata(project, key(role, "job"))
    if not job_id:
        return None
    with project.db() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=? AND role=? AND status='running'", (job_id, role)).fetchone()
        return dict(row) if row else None


def claim_inbox(project, role):
    existing = active_job(project, role)
    if existing:
        return existing
    job = project.claim(role)
    if job:
        metadata(project, key(role, "job"), job["id"])
    return job


def complete_job(project, role, result, error=None, job_id=None):
    job = active_job(project, role)
    if not job or (job_id is not None and job["id"] != job_id):
        raise ValueError("У этой нативной сессии нет такой активной задачи")
    project.finish(job, result=result, error=error)
    metadata(project, key(role, "job"), 0)
    return {"job_id": job["id"], "status": "failed" if error else "completed"}


def inbox(project, role):
    with lock(project.team / f"native-{role}.lock"):
        job = claim_inbox(project, role)
        return {"job": job, "delivery": "native turn", "instructions":
                "Выполните задачу и вызовите job_finish с результатом; Stop hook также сохраняет финальный ответ. "
                "Не запускайте другой CLI для продолжения этого же session ID."}


def task_context(project, job):
    if not job:
        return ""
    return (f"\nПоручение sovet #{job['id']} от {job['origin']}:\n{job['prompt']}\n"
            f"Режим поручения: {'только ревью, не меняйте исходники' if job['read_only'] else 'реализация'}. "
            f"Глубина взаимных поручений: {job['depth']} из {project.config['max_peer_depth']}. "
            "Не ожидайте коллегу в цикле и не запускайте вложенные Claude/Codex. "
            "Сохраните результат через job_finish либо завершите ответ: Stop hook передаст результат.\n")


def hook(project: Project, role: str, event: str, payload: dict, instance=None):
    sid = payload.get("session_id") or payload.get("thread_id")
    # Hooks are scoped to the launched role, but subagent lifecycles must not bind it.
    if payload.get("agent_id") or payload.get("agent_type"):
        return {}
    with lock(project.team / f"native-{role}.lock"):
        if instance and metadata(project, key(role, "instance")) != instance:
            return {}
        if sid:
            expected = metadata(project, key(role, "expected_session"))
            if not instance and expected and expected != sid:
                return {}  # late hooks from an older harness cannot steal the role
            current = metadata(project, key(role, "session"))
            if event not in ("SessionStart", "UserPromptSubmit") and current and current != sid:
                return {}  # a late Stop from the previous /clear conversation
            project.session(role, sid)
            metadata(project, key(role, "session"), sid)
            metadata(project, key(role, "established"), True)
        if event == "SessionStart":
            metadata(project, key(role, "connected"), True)
            text, _ = project.context(role)
            return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text +
                    "\nВы работаете в нативном harness sovet. Перед выполнением работы проверяйте inbox. "
                    "Общая память и авторские правки имеют приоритет перед устаревшим контекстом."}}
        if event == "UserPromptSubmit":
            counter = (metadata(project, key(role, "turn")) or 0) + 1
            metadata(project, key(role, "turn"), counter)
            prompt = payload.get("prompt")
            if isinstance(prompt, str) and prompt.strip():
                project.emit("user", "native-prompt", prompt, {"to": role, "session": sid})
            text, cursor = project.context(role)
            metadata(project, key(role, "context_cursor"), cursor)
            job = claim_inbox(project, role)
            return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text + task_context(project, job)}}
        if event == "Stop":
            answer = payload.get("last_assistant_message") or ""
            turn = metadata(project, key(role, "turn")) or 0
            marker = [sid, turn, answer]
            if answer and metadata(project, key(role, "last_answer")) != marker:
                job = active_job(project, role)
                if job:
                    complete_job(project, role, answer)
                else:
                    project.emit(role, "answer", answer, {"session": sid, "native": True})
                metadata(project, key(role, "last_answer"), marker)
            cursor = metadata(project, key(role, "context_cursor"))
            if cursor:
                project.acknowledge(role, cursor)
            # Respect both harnesses' continuation guards. Never loop on a clock.
            if not payload.get("stop_hook_active"):
                job = claim_inbox(project, role)
                if job:
                    return {"decision": "block", "reason": task_context(project, job)}
            return {}
        if event in ("SessionEnd", "Interrupt", "StopFailure"):
            if event == "SessionEnd":
                metadata(project, key(role, "connected"), False)
            job = active_job(project, role)
            if job:
                complete_job(project, role, "", error=f"Нативный ход остановлен: {event}")
            return {}
    return {}


def main_hook(project, role, event):
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook input must be an object")
        output = hook(project, role, event, payload, os.environ.get("SOVET_NATIVE_INSTANCE"))
        print(json.dumps(output, ensure_ascii=False), flush=True)
        return 0
    except Exception as e:
        # A broken bridge must not prevent the user from using the original CLI.
        print(f"sovet hook: {e}", file=sys.stderr)
        print("{}", flush=True)
        return 0
