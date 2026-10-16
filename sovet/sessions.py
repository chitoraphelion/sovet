"""Project-wide session management, including detached/orphaned child processes."""
import json
import subprocess
from pathlib import Path

from .core import digest, find_root
from .processes import descendants, discover, process_table, terminate


def project_id(root):
    path = Path(root)
    slug = "".join(c if c.isascii() and c.isalnum() else "-" for c in path.name)[:25]
    return f"sovet-{slug}-{digest(str(path).encode())[:6]}"


def list_sessions(root=None, table=None):
    table = process_table() if table is None else table
    root = str(find_root(root)) if root else None
    groups = {}
    for item in discover("sovet", table):
        if root and item["root"] != str(Path(root).resolve()):
            continue
        key = item["root"] or f"pid:{item['pid']}"
        group = groups.setdefault(key, {"id": project_id(item["root"]) if item["root"] else str(item["pid"]),
                                        "root": item["root"], "processes": [], "pids": []})
        group["processes"].append(item)
        group["pids"] = sorted(set(group["pids"]) | {item["pid"]} | set(item["children"]))
    return sorted(groups.values(), key=lambda group: group["id"])


def tmux_sessions_for(table, selected):
    try:
        result = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}"],
                                text=True, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    names = set()
    for line in result.stdout.splitlines():
        try:
            name, pid = line.rsplit("\t", 1)
            if descendants(table, [int(pid)]) & selected:
                names.add(name)
        except ValueError:
            pass
    return sorted(names)


def stop_sessions(target=None, root=None, all_sessions=False, force=False, timeout=8):
    if all_sessions and (target or root):
        raise ValueError("--all нельзя совмещать с ID или --project")
    table = process_table()
    groups = list_sessions(table=table)
    pid_target = bool(target and target.isdecimal())
    if all_sessions:
        chosen = groups
    elif target:
        chosen = [g for g in groups if g["id"] == target or (pid_target and int(target) in g["pids"])]
    else:
        resolved = str(find_root(root))
        chosen = [g for g in groups if g["root"] == resolved]
    if target and not chosen:
        raise ValueError(f"Запущенная сессия sovet не найдена: {target}")
    seeds = [int(target)] if pid_target and chosen else [p["pid"] for g in chosen for p in g["processes"]]
    names = tmux_sessions_for(table, set(seeds)) if not pid_target else []
    result = terminate(seeds, table, timeout=timeout, force=force)
    # Only tmux sessions whose panes contained selected processes may be closed.
    if not result["remaining"]:
        for name in names:
            subprocess.run(["tmux", "kill-session", "-t", "=" + name], capture_output=True)
    result["sessions"] = [g["id"] for g in chosen]
    return result


def display(groups):
    if not groups:
        return "Нет запущенных сессий sovet."
    lines = []
    for group in groups:
        lines.append(f"{group['id']}  {group['root'] or '(проект неизвестен)'}")
        for proc in group["processes"]:
            lines.append(f"  PID {proc['pid']}  {proc['kind']}  {proc['state']}  "
                         f"дочерние: {','.join(map(str, proc['children'])) or '-'}")
    return "\n".join(lines)
