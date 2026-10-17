"""Independent native research lanes; peer review opens only after both finals."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .core import ROLES, lock


def default_lane(role):
    directory, topic, prefix = (("attacks/variance-reduction", "variance reduction", "VarianceReductionAttack")
                                if role == "codex" else ("attacks/saddles", "saddle points", "SaddleAttack"))
    scopes = [directory + "/"]
    for kind in ("Setups", "Algorithms", "Proofs", "Theorems"):
        base = "formal/longtails/LongTails/" + kind + "/" + prefix
        scopes += [base + ".lean", base + "/"]
    return {"directory": directory, "topic": topic, "task": directory + "/TASK.md", "write_scopes": scopes}


def enabled(project):
    return project.config.get('profile') == 'math-research' and project.config.get('research_mode') == 'parallel'


def lane(project, role):
    if role not in ROLES:
        return {}
    return project.config.get('research_lanes', {}).get(role, default_lane(role)) if enabled(project) else {}


def working_dir(project, role):
    spec = lane(project, role)
    if not spec:
        return project.root
    path = project.artifact(spec['directory'])
    path.mkdir(parents=True, exist_ok=True)
    return path


def _key(project, suffix):
    return 'parallel:' + project.config.get('research_run', 'default') + ':' + suffix


def _get(db, name, default=None):
    row = db.execute('SELECT value FROM metadata WHERE key=?', (name,)).fetchone()
    return json.loads(row[0]) if row else default


def _set(db, name, value):
    db.execute('INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (name, json.dumps(value, ensure_ascii=False)))


def final_phase(project):
    if not enabled(project):
        return False
    with project.db() as db:
        return bool(_get(db, _key(project, 'final_reviews'), False))


def ensure_job(project, role):
    if not enabled(project):
        return
    spec = lane(project, role)
    if not spec:
        raise ValueError('Для параллельного исследователя не задана рабочая область')
    with lock(project.team / 'parallel.lock'):
        with project.db() as db:
            if db.execute("SELECT 1 FROM jobs WHERE role=? AND status IN ('running','pending')", (role,)).fetchone():
                return
            if _get(db, _key(project, 'final:' + role)):
                return
            started = _get(db, _key(project, 'started'))
            if started is None:
                started = time.time()
                _set(db, _key(project, 'started'), started)
            used = _get(db, _key(project, 'steps:' + role), 0)
            if used >= project.config.get('research_steps', 4) or time.time() >= started + 3600 * project.config.get('research_hours', 2):
                return
        task_file = project.artifact(spec['task'])
        if not task_file.exists():
            working_dir(project, role)
            task_file.write_text(f"# {spec['topic']}\nRead the author's shared brief and existing paper. Propose new precise hypotheses, derive a method/bound or a substantive counterexample. Work independently in this lane; no interim peer review. Save hypotheses, attempts and FINAL.md with honest proof status.\n")
        task = task_file.read_text()
        prompt = (f"Самостоятельная исследовательская атака {spec['topic']}, этап {used+1}. "
                  f"Рабочие файлы: {spec['directory']}. Прочитай memory_read и свою постановку через artifact_read. "
                  "Выдвигай новые проверяемые гипотезы, выводи оценки, пробуй механизмы алгоритма и ищи контрпримеры. "
                  "Не занимайся косметикой прежнего отчёта и не запрашивай промежуточное ревью коллеги. "
                  "После содержательного checkpoint вызывай job_finish и inbox и продолжай в этом же нативном harness. "
                  "Не заканчивай работу на очередном плане: дойди до результата, содержательного опровержения либо исчерпания бюджета. "
                  "Перед окончанием запиши FINAL.md в своей папке и вызови attack_finish.\n\n" + task)
        project.enqueue(role, prompt, origin='parallel-research')
        with project.db() as db:
            _set(db, _key(project, 'steps:' + role), used+1)


def instructions(project, role):
    spec = lane(project, role)
    return f"""Native parallel research mode. BOTH agents are researchers and authors, not a writer/reviewer pair.
Your lane: {spec['topic']}. Your task file, relative to project root: {spec['task']}.
Your writable deliverables: {json.dumps(spec['write_scopes'], ensure_ascii=False)}.
Your native working directory is your own lane. Read shared inputs through artifact_read; use artifact_write
for allowed Lean files outside that directory. Do not edit the other lane, old shared proofs, global claims,
or the original paper. Put new claims, experiments, hypotheses and failed attempts in your lane.
Shared memory_append is available for concise factual discoveries; never write private reasoning there.
Spend the effort on new mathematics and mechanisms, not constant-polishing or repeated certification of old results.
Maintain precise assumptions and honest proof status. Explore conjectures aggressively, but never present them as proved.
No intermediate peer review, no reviewer ping-pong, no hidden CLI workers. The other agent is independently attacking
its own direction. ask_peer is unavailable until BOTH agents publish final dossiers using attack_finish.
Within your native turn use job_finish then inbox for the next meaningful step; continue until your final dossier
or the dispatch budget is reached. No terminal injection, waiting/polling for the colleague, or permission bypass.
Dispatch budget: {project.config.get('research_hours', 2)} hours and {project.config.get('research_steps', 4)} substantive steps per lane.
This budget stops new queued steps; it never kills your interactive CLI or imposes a 30-minute turn cutoff.
The final dossier must contain actual hypotheses/results, derivations or counterexamples, a comparison to baselines,
and explicit unresolved gaps. Compilation, Comparator and mathematical novelty remain separate claims.
"""


def finish(project, role, path, summary):
    if not enabled(project):
        raise ValueError('Параллельное исследование не включено')
    if not project.can_implement(role, path):
        raise ValueError('Итог должен находиться в собственной рабочей области')
    file = project.artifact(path)
    if not file.is_file() or not file.read_text().strip() or not summary.strip():
        raise ValueError('Нужен непустой итоговый файл и краткое резюме')
    now = time.time()
    with project.db() as db:
        db.execute('BEGIN IMMEDIATE')
        _set(db, _key(project, 'final:' + role), {'path': path, 'summary': summary, 'at': now})
        finals = {r: _get(db, _key(project, 'final:' + r)) for r in ROLES}
        if all(finals.values()) and not _get(db, _key(project, 'final_reviews'), False):
            # Atomic final-only handoff: neither an early nor a duplicate review is created.
            for reviewer in ROLES:
                author = next(r for r in ROLES if r != reviewer)
                prompt = (f"Оба исследователя завершили самостоятельную атаку. Теперь ОДНО финальное независимое ревью "
                          f"результата {author}: {finals[author]['path']}. {finals[author]['summary']}\n"
                          "Проверь основное утверждение/гипотезу, предположения, доказательство и оракульные затраты. "
                          "Не меняй файлы автора, не начинай новую цепочку взаимных поручений. Верни конкретный итог.")
                row = db.execute('INSERT INTO jobs(role,prompt,origin,depth,read_only,reply_to,created) VALUES(?,?,?,?,?,?,?)',
                                 (reviewer, prompt, 'final-review', 0, 1, author, now))
                db.execute('INSERT INTO events(at,actor,kind,body,data) VALUES(?,?,?,?,?)',
                           (now, 'parallel', 'request', prompt, json.dumps({'job': row.lastrowid, 'to': reviewer})))
            _set(db, _key(project, 'final_reviews'), True)
        opened = bool(_get(db, _key(project, 'final_reviews'), False))
        db.execute('INSERT INTO events(at,actor,kind,body,data) VALUES(?,?,?,?,?)',
                   (now, role, 'research-final', summary, json.dumps({'path': path})))
    return {'final_saved': True, 'final_reviews_queued': opened,
            'next': 'Finish your current job, then check inbox. An idle peer checks its queued final review on its next native turn.'}
