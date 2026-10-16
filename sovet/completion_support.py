"""Small dependency-free shell completion engine, shared with chitalka."""
import argparse
import os
import re
import shlex
from pathlib import Path


def paths(prefix, directories=False, base=None):
    expanded = os.path.expanduser(prefix)
    folder, stem = os.path.split(expanded)
    location = Path(folder or ".")
    if not location.is_absolute() and base:
        location = Path(base) / location
    display_folder = os.path.split(prefix)[0]
    try:
        return [os.path.join(display_folder, p.name) + ("/" if p.is_dir() else "")
                for p in sorted(location.iterdir()) if p.name.startswith(stem)
                and (stem.startswith(".") or not p.name.startswith(".")) and (not directories or p.is_dir())]
    except OSError:
        return []


def candidates(parser, words, dynamic=None):
    """Use the real argparse tree for commands/options/enums; no model/network calls."""
    words = words or [""]
    prefix, prior = words[-1], words[:-1]
    current, chain, pending, position = parser, [], None, 0
    for token in prior:
        if pending is not None:
            pending = None
            continue
        if token.startswith("-"):
            option = token.split("=", 1)[0]
            action = current._option_string_actions.get(option) or parser._option_string_actions.get(option)
            if action and action.nargs != 0 and "=" not in token:
                pending = action
            continue
        subs = [a for a in current._actions if isinstance(a, argparse._SubParsersAction)]
        child = next((a.choices[token] for a in subs if token in a.choices), None)
        if child is not None:
            chain.append(token)
            current, position = child, 0
            continue
        positional = [a for a in current._get_positional_actions() if not isinstance(a, argparse._SubParsersAction)]
        if position < len(positional) and positional[position].nargs not in ("*", "+", argparse.REMAINDER):
            position += 1
    if pending is None and prefix.startswith("--") and "=" in prefix:
        option, value = prefix.split("=", 1)
        action = current._option_string_actions.get(option) or parser._option_string_actions.get(option)
        if action:
            choices = list(action.choices or []) if action.choices else (dynamic(chain, action.dest, value, words) if dynamic else None)
            return sorted(option + "=" + str(c) for c in (choices or []) if str(c).startswith(value))
    if pending:
        values = list(pending.choices) if pending.choices else None
        if values is None and dynamic:
            values = dynamic(chain, pending.dest, prefix, words)
        return sorted(str(value) for value in (values or []) if str(value).startswith(prefix))
    if prefix.startswith("-"):
        return sorted({flag for p in (parser, current) for flag in p._option_string_actions if flag.startswith(prefix)})
    positional = [a for a in current._get_positional_actions() if not isinstance(a, argparse._SubParsersAction)]
    action = positional[position] if position < len(positional) else None
    values = list(action.choices) if action and action.choices else None
    if values is None and dynamic:
        values = dynamic(chain, action.dest if action else None, prefix, words)
    if values is None:
        values = []
        for a in current._actions:
            if isinstance(a, argparse._SubParsersAction):
                values += list(a.choices)
    if not prefix:
        values = list(values) + list(current._option_string_actions)
    return sorted({str(value) for value in values if str(value).startswith(prefix) and "\n" not in str(value)})


def script(tool, shell):
    if shell == "zsh":
        return f'''# {tool}: generated Tab completion
if (( ! $+functions[compdef] )); then
  autoload -Uz compinit
  compinit
fi
_{tool}() {{
  local -a suggestions
  suggestions=("${{(@f)$({tool} _complete "${{words[@]:1:$((CURRENT - 1))}}" 2>/dev/null)}}")
  (( ${{#suggestions}} )) && compadd -- "${{suggestions[@]}}"
}}
compdef _{tool} {tool}
'''
    if shell == "bash":
        return f'''# {tool}: generated Tab completion (also supports Bash 3.2)
_{tool}() {{
  local item
  COMPREPLY=()
  while IFS= read -r item; do
    [[ -n "$item" ]] && COMPREPLY+=("$item")
  done < <({tool} _complete "${{COMP_WORDS[@]:1:COMP_CWORD}}" 2>/dev/null)
}}
complete -o filenames -F _{tool} {tool}
'''
    raise ValueError("Поддерживаются zsh и bash")


def install(tool, shell, home=None):
    home = Path(home or Path.home())
    target = home / ".local/share" / tool / "completion" / f"{tool}.{shell}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(script(tool, shell))
    rc = home / (".zshrc" if shell == "zsh" else ".bashrc")
    old = rc.read_text() if rc.exists() else ""
    begin, end = f"# >>> {tool} completion >>>", f"# <<< {tool} completion <<<"
    block = f"{begin}\nif [[ $- == *i* ]]; then\n  source {shlex.quote(str(target))}\nfi\n{end}"
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.S)
    text = pattern.sub(lambda _: block, old) if pattern.search(old) else old.rstrip() + "\n\n" + block + "\n"
    if text != old:
        rc.write_text(text)
    return target
