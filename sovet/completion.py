import json
import subprocess
from pathlib import Path

from .completion_support import candidates, paths
from .core import DEFAULTS, find_root


def complete(words):
    from .cli import parser
    project = None
    for flag in ("-C", "--project"):
        if flag in words and words.index(flag) + 1 < len(words) - 1:
            project = words[words.index(flag) + 1]
    try:
        root = find_root(project)
        config = json.loads((root / ".team/config.json").read_text())
    except (ValueError, OSError):
        root, config = Path.cwd(), DEFAULTS

    # Forward the native chitalka command surface, including comment-ID completion.
    if "chitalka" in words[:-1]:
        try:
            offset = words.index("chitalka") + 1
            result = subprocess.run(["chitalka", "_complete", *words[offset:]], cwd=root,
                                    text=True, capture_output=True, timeout=5)
            return result.stdout.splitlines() if result.returncode == 0 else []
        except (OSError, subprocess.SubprocessError):
            return []

    def dynamic(chain, dest, prefix, _):
        if dest in ("project", "aris", "repo", "source"):
            return paths(prefix, directories=True)
        if dest in ("codex_model", "claude_model"):
            role = dest.split("_")[0]
            return sorted({DEFAULTS["models"][role], config["models"][role]})
        if chain == ["stop"] and dest == "target":
            try:
                from .sessions import list_sessions
                return [s["id"] for s in list_sessions()]
            except RuntimeError:
                return []
        if dest in ("path", "pdf", "task"):
            return paths(prefix, directories=chain == ["init"], base=root)
        if chain[:1] == ["research"] and dest == "target":
            lean = root / config["math"]["lean_root"]
            if chain[-1] == "audit":
                return [p.name for p in (lean / "LongTails/Theorems").glob("*.lean")]
            if chain[-1] == "build":
                return [".".join(p.relative_to(lean).with_suffix("").parts) for p in (lean / "LongTails").rglob("*.lean")]
            if chain[-1] == "check":
                return paths(prefix, base=lean)
        return None
    from .cli import INTERNAL
    # No user-facing positional takes these values, so they can be dropped everywhere.
    return [c for c in candidates(parser(), words, dynamic) if c not in INTERNAL]
