"""Use chitalka's public CLI and original ARIS skill files, without forking either."""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .core import Project, atomic_write, digest

ARIS_URL = "https://github.com/wanshuiyin/Auto-claude-code-research-in-sleep.git"
ARIS_SKILLS = ("research-wiki", "research-refine", "auto-review-loop", "lean-formalize", "proof-checker")


def chitalka(project: Project, args, capture=True):
    binary = shutil.which("chitalka")
    if not binary:
        raise ValueError("chitalka не найдена. Установите: uv tool install -e ~/code/python/chitalka")
    return subprocess.run([binary, *args, "--root", str(project.root)], cwd=project.root,
                          text=True, capture_output=capture, check=True, timeout=30 if capture else None)


def comments(project: Project, all_comments=False):
    args = ["list", "--json"] + (["--all"] if all_comments else [])
    return json.loads(chitalka(project, args).stdout)


def reply(project: Project, comment_id: str, text: str, done=False):
    return chitalka(project, ["reply", comment_id, text, *(["--done"] if done else [])]).stdout


def metadata(project, key, value=None):
    with project.db() as db:
        if value is not None:
            db.execute("INSERT INTO metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, json.dumps(value)))
        row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None


def sync_comments(project: Project):
    """Author revisions trigger once; agent replies and status=done never ping-pong."""
    paths = [project.root / "kommentarii/kommentarii.json", project.root / "notes/kommentarii.json"]
    existing = next((p for p in paths if p.exists()), None)
    if not existing:
        return 0
    raw_sha = digest(existing.read_bytes())
    if metadata(project, "chitalka:file") == raw_sha:
        return 0
    items = comments(project, all_comments=True)
    count = 0
    for item in items:
        author = {key: item.get(key) for key in ("id", "quote", "comment", "suggestion", "source", "category")}
        author["thread"] = [m for m in item.get("thread", []) if m.get("who") == "author"]
        sha = digest(json.dumps(author, ensure_ascii=False, sort_keys=True).encode())
        key = "chitalka:" + item["id"]
        previous = metadata(project, key)
        thread = item.get("thread") or ([{"who": "ai"}] if item.get("reply") else [])
        waiting = not thread or thread[-1].get("who") != "ai"
        changed = previous is not None and (previous["sha"] != sha or previous["status"] == "done")
        if item.get("status") != "done" and (changed or (previous is None and waiting)):
            body = "Комментарий автора из chitalka:\n" + json.dumps(item, ensure_ascii=False, indent=2)
            project.emit("user", "chitalka", body, {"comment_id": item["id"]})
            if project.config["auto_comments"]:
                project.enqueue(project.config["writer"], body + "\nИсправьте исходник, проверьте сборку и ответьте "
                                "через chitalka_reply. Если требуется решение автора, оставьте комментарий открытым.",
                                origin="chitalka")
            count += 1
        metadata(project, key, {"sha": sha, "status": item.get("status", "open")})
    metadata(project, "chitalka:file", raw_sha)
    return count


def watched_files(project: Project):
    ignore = set(project.config["watch_ignore"])
    extensions = set(project.config["watch_extensions"])
    for folder, dirs, files in os.walk(project.root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in ignore and not d.startswith(".")
                         and not (Path(folder) / d).is_symlink())
        for name in sorted(files):
            path = Path(folder) / name
            if name.startswith(".") or name.lower().endswith(".notes.json") or path.suffix not in extensions or path.is_symlink():
                continue
            yield path
    for name in ("brief.md", "state.md", "decisions.md", "claims.json"):
        yield project.team / name


def scan_files(project: Project):
    baseline = bool(metadata(project, "files:initialized"))
    old_stats = metadata(project, "files:stats") or {}
    stats = {}
    with project.db() as db:
        previous = {r["path"]: dict(r) for r in db.execute("SELECT * FROM snapshots")}
    seen = set()
    changes = []
    for p in watched_files(project):
        rel = str(p.relative_to(project.root))
        try:
            st = p.stat()
            stat = [st.st_mtime_ns, st.st_size, st.st_ino]
            stats[rel] = stat
            if old_stats.get(rel) == stat and rel in previous:
                seen.add(rel)
                continue
            sha256, preview = hashlib.sha256(), bytearray()
            with p.open("rb") as file:
                for block in iter(lambda: file.read(65536), b""):
                    sha256.update(block)
                    if len(preview) < 256_000:
                        preview.extend(block[:256_000 - len(preview)])
            if b"\x00" in preview:
                continue
            text = bytes(preview).decode("utf-8", errors="replace")
        except OSError:
            continue
        seen.add(rel)
        sha = sha256.hexdigest()
        old = previous.get(rel)
        if old and old["sha"] == sha:
            continue
        if baseline:
            diff = "".join(difflib.unified_diff((old["content"] if old else "").splitlines(True),
                                             text.splitlines(True), fromfile=rel + " (before)", tofile=rel))
            changes.append((rel, diff[:6000] or "Изменилось содержимое за пределами сохранённого фрагмента; перечитайте файл."))
        with project.db() as db:
            db.execute("INSERT INTO snapshots(path,sha,content) VALUES(?,?,?) "
                       "ON CONFLICT(path) DO UPDATE SET sha=excluded.sha,content=excluded.content", (rel, sha, text))
    for rel in previous.keys() - seen:
        if (project.root / rel).exists():
            continue  # file was filtered out or lies beyond this bounded scan, not deleted
        changes.append((rel, "Файл удалён"))
        with project.db() as db:
            db.execute("DELETE FROM snapshots WHERE path=?", (rel,))
    if changes:
        project.emit("filesystem", "changes", "Изменения в рабочих файлах. Сохраните актуальные правки автора; "
                     "принадлежность внешней правки неизвестна. Перечитайте файлы перед записью.\n" +
                     "\n".join(f"{path}:\n{diff}" for path, diff in changes)[:18000],
                     {"paths": [p for p, _ in changes]})
    metadata(project, "files:initialized", True)
    metadata(project, "files:stats", stats)
    return len(changes)


def install_aris(project: Project, repo: Path):
    repo = repo.expanduser().resolve()
    for skill in ARIS_SKILLS:
        if not (repo / "skills" / skill / "SKILL.md").exists():
            raise ValueError(f"Не найден навык ARIS: {repo / 'skills' / skill}")
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    project.config["aris"] = {"repo": str(repo), "commit": commit, "skills": list(ARIS_SKILLS)}
    project.save_config()
    sync_aris_profile(project)
    atomic_write(project.team / "aris.lock.json", json.dumps(project.config["aris"], indent=2) + "\n")
    project.emit("system", "aris", f"Подключён ARIS {commit}: {', '.join(ARIS_SKILLS)}")
    return project.config["aris"]


def sync_aris_profile(project):
    cfg = project.config.get("aris")
    if not cfg:
        return
    repo = Path(cfg["repo"])
    enabled = project.config.get("profile") == "math-research"
    # Only our own namespaced symlinks are removed when switching to programming.
    for folder in (".agents/skills", ".claude/skills"):
        for skill in ARIS_SKILLS:
            link = project.root / folder / ("aris-" + skill)
            target = repo / "skills" / skill
            if not enabled:
                if link.is_symlink() and link.resolve() == target:
                    link.unlink()
                continue
            if link.is_symlink() and link.resolve() == target:
                continue
            if link.exists() or link.is_symlink():
                raise ValueError(f"Уже существует чужой путь: {link}")
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target, target_is_directory=True)
    if enabled:
        helpers = project.root / ".aris/tools"
        if not helpers.exists():
            helpers.parent.mkdir(exist_ok=True)
            helpers.symlink_to(repo / "tools", target_is_directory=True)
        if not (project.root / "research-wiki/index.md").exists():
            subprocess.run([sys.executable, str(repo / "tools/research_wiki.py"), "init", "research-wiki/"],
                           cwd=project.root, check=True, capture_output=True, text=True, timeout=30)


def aris_context(project):
    cfg = project.config.get("aris")
    if not cfg:
        return "ARIS пока не подключён: sovet aris --repo ~/code/aris"
    repo = Path(cfg["repo"])
    return (f"ARIS: {repo}, закреплённый commit {cfg['commit']}. "
            "Для исследовательской памяти прочитайте " + str(repo / "skills/research-wiki/SKILL.md") +
            ". Для независимого ревью: " + str(repo / "skills/auto-review-loop/SKILL.md") +
            ". Используйте методику ARIS, передавайте ревью через ask_peer. "
            "Настройки sovet имеют приоритет: Sol 6.1 high / Opus 5.5 high, без запуска вложенных "
            "Codex/Claude CLI или отдельного reviewer-моста ARIS. Wiki хранится в research-wiki/ проекта. "
            "Инициализируйте wiki по навыку, если задача исследовательская; сохраняйте решения и неудачные попытки.")
