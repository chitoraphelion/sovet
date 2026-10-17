"""Pinned LongTails workspace, Lean compilation and independent Comparator audits."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from .core import Project, atomic_write, digest

LONGTAILS_URL = "https://github.com/k3nfalt/longtails.git"


def lean_root(project):
    root = (project.root / project.config["math"]["lean_root"]).resolve()
    if not root.is_relative_to(project.root):
        raise ValueError("Lean workspace должен находиться внутри проекта")
    if not (root / "lean-toolchain").exists():
        raise ValueError("Lean workspace не готов. Выполните sovet research setup.")
    return root


def setup(project: Project, source: Path | None = None, fetch_cache=False):
    root = (project.root / project.config["math"]["lean_root"]).resolve()
    if not root.is_relative_to(project.root) or root == project.root:
        raise ValueError("Для LongTails нужен отдельный подкаталог внутри проекта")
    if not root.exists():
        root.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "clone", "--no-hardlinks", str(source.expanduser().resolve()), str(root)] if source else [
            "git", "clone", "--depth", "1", LONGTAILS_URL, str(root)]
        subprocess.run(cmd, check=True)
    if not (root / "run_comparator.py").is_file() or not (root / "LongTails/Setups").is_dir():
        raise ValueError(f"Это не LongTails: {root}; существующие файлы не изменены")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    pin = {"repo": LONGTAILS_URL, "commit": commit, "root": str(root.relative_to(project.root)),
           "toolchain": (root / "lean-toolchain").read_text().strip(),
           "lake_manifest_sha": digest((root / "lake-manifest.json").read_bytes())
           if (root / "lake-manifest.json").exists() else None}
    atomic_write(project.team / "longtails.lock.json", json.dumps(pin, indent=2) + "\n")
    claims = project.team / "claims.json"
    if not claims.exists():
        atomic_write(claims, json.dumps({"claims": [], "schema": {
            "id": "claim identifier", "paper": "main.tex:line", "setup": "LongTails.Setups.Module.spec",
            "theorem": "LongTails.Theorems.Module.result", "budget": "explicit oracle bound",
            "status": "draft|compiled|audited", "notes": "assumptions / unresolved gaps"}}, indent=2) + "\n")
    if fetch_cache:
        subprocess.run(["lake", "exe", "cache", "get"], cwd=root, check=True)
    project.emit("system", "research", f"LongTails закреплён: {commit}; {pin['toolchain']}")
    return pin


def run_lean(project, action, target=None):
    root = lean_root(project)
    if action == "check":
        if not target:
            raise ValueError("Нужен относительный путь .lean")
        file = (root / target).resolve()
        if not file.is_relative_to(root) or file.suffix != ".lean" or not file.is_file():
            raise ValueError("Нужен существующий .lean внутри Lean workspace")
        cmd = ["lake", "env", "lean", "--json", str(file.relative_to(root))]
    elif action == "build":
        if target and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]*", target):
            raise ValueError("Ожидается имя Lean-модуля")
        cmd = ["bash", "build.sh", *([target] if target else [])] if (root / "build.sh").exists() else [
            "lake", "build", *([target] if target else [])]
    elif action == "audit":
        if not target or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*\.lean", target):
            raise ValueError("Нужен файл теоремы, например AcceleratedConvex.lean")
        if not (root / "LongTails/Theorems" / target).is_file():
            raise ValueError("Файл не найден в LongTails/Theorems")
        cmd = [sys.executable, "run_comparator.py", target]
    else:
        raise ValueError(f"Неизвестная проверка: {action}")
    before = source_revision(root)
    process = subprocess.Popen(cmd, cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True)
    try:
        output, _ = process.communicate(timeout=project.config["math"]["timeout"])
        result = subprocess.CompletedProcess(cmd, process.returncode, stdout=output)
    except BaseException:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise
    after = source_revision(root)
    sorry_warning = action != "audit" and bool(re.search(r"declaration uses [‘'`]?sorry", result.stdout))
    verified = result.returncode == 0 and before == after and not sorry_warning
    report = {"action": action, "target": target, "exit_code": result.returncode,
              "command": cmd, "output": result.stdout[-20000:],
              "source_sha": before, "source_changed_during_check": before != after,
              "compiled": result.returncode == 0, "sorry_warning": sorry_warning,
              "verified": verified, "audited": verified and action == "audit",
              "meaning": "Comparator audit" if action == "audit" else "compilation only; not a statement audit"}
    project.emit("lean", "verification", json.dumps(report, ensure_ascii=False))
    logs = project.team / "verification"
    logs.mkdir(exist_ok=True)
    atomic_write(logs / "latest.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def source_revision(root):
    rows = []
    # Include the specification, proof modules, and audit scripts; exclude generated .lake files.
    paths = list((root / "LongTails").rglob("*.lean")) + list((root / "tools").rglob("*.lean"))
    paths += list(root.glob("*.lean")) + [root / name for name in (
        "lean-toolchain", "lake-manifest.json", "lakefile.toml", "lakefile.lean", "run_comparator.py")]
    for p in sorted(set(paths)):
        if p.is_file():
            rows.append(str(p.relative_to(root)) + ":" + digest(p.read_bytes()))
    return digest("\n".join(rows).encode())


def verification_status(project):
    report_path = project.team / "verification/latest.json"
    if not report_path.exists():
        return {"verified": False, "reason": "Проверки ещё не запускались", "environment": environment(project)}
    report = json.loads(report_path.read_text())
    report["current"] = report["source_sha"] == source_revision(lean_root(project))
    report["verified"] = report["verified"] and report["current"]
    report["audited"] = report.get("audited", False) and report["current"]
    report["environment"] = environment(project)
    return report


def environment(project):
    root = (project.root / project.config["math"]["lean_root"]).resolve()
    expected = (root / "lean-toolchain").read_text().strip() if (root / "lean-toolchain").exists() else None
    installed = []
    if shutil.which("elan"):
        try:
            installed = subprocess.check_output(["elan", "toolchain", "list"], text=True, timeout=5).splitlines()
        except (OSError, subprocess.SubprocessError):
            pass
    return {"root": str(root), "expected_toolchain": expected,
            "toolchain_installed": bool(expected and any(line.split()[0] == expected for line in installed if line.split())),
            "mathlib_sources": (root / ".lake/packages/mathlib/Mathlib").is_dir(),
            "mathlib_cache": (root / ".lake/packages/mathlib/.lake/build/lib/lean/Mathlib").is_dir(),
            "prepare_command": f"sovet -C {str(project.root)!r} research setup --fetch-cache"}


def search(project, query):
    if not query.strip() or len(query) > 200:
        raise ValueError("Нужен поисковый запрос длиной 1–200 символов")
    root = lean_root(project)
    paths = [root / "LongTails", root / ".lake/packages/mathlib/Mathlib"]
    hits = []
    for folder in paths:
        if not folder.is_dir():
            continue
        for file in folder.rglob("*.lean"):
            for number, line in enumerate(file.read_text(errors="replace").splitlines(), 1):
                if query.casefold() in line.casefold():
                    hits.append({"file": str(file.relative_to(root)), "line": number, "text": line[:500]})
                    if len(hits) >= 60:
                        return hits
    return hits


def profile_context(project):
    if project.config.get("profile") != "math-research":
        return "Programming profile: implement scoped changes, run relevant checks and request independent peer review."
    return f"""Math-research profile for an optimization paper.
Formal workspace: {project.config['math']['lean_root']} (LongTails + its pinned mathlib and Comparator).
Read that workspace's AGENTS.md before modifying Lean. Use lean_search to find existing setups and lemmas;
lean_check for compiler diagnostics, lean_build for a module, longtails_audit for the public theorem/budget.
Keep exact assumptions, oracle model, output iterate, probability/integrability requirements and the budget.
No sorry/admit/new axioms in proofs. Compilation is not an independent specification/axiom audit.
Do not strengthen hypotheses or alter the setup just to make a target rate provable. Distinguish upper bounds
from optimality. Review the formal statement against the author's mathematical problem and primary literature.
Maintain .team/claims.json: link each paper claim to setup, theorem, explicit budget and verification evidence.
Mark an assertion audited only after successful Comparator output for the CURRENT source revision; after edits
rerun verification. Do not label prose claims formalized merely because a related lemma compiles.
Use research-wiki to record assumptions, failed attempts and verified results. Chitalka feedback is author input.
Research sequence: author setup -> {project.config["writer"]} derivation/formalization -> Lean build -> Comparator -> {"codex" if project.config["writer"] == "claude" else "claude"} review
of assumptions and oracle counts -> paper/source update -> author review in chitalka.
"""
