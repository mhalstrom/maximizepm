"""Helpers for the commands part of an adapter (part 10, docs/agent-adapters.md, #5186): read the custom slash
commands that an agent keeps as Markdown files with a front matter (Claude Code commands and skills, Codex prompts).
An adapter's read_commands(folder) calls these; a user's adapter module may import them too.

Each entry has the shape of agents.COMMAND_KEYS: {"name": no "/", "description": str, "hint": the argument hint}.
A file that cannot be read is left out: a list for the message box never fails a page."""

import os
import re
from pathlib import Path

DESCRIPTION_MAX = 160   # the list shows one line for each command
FRONT_MAX = 64 * 1024   # the most of a file read: the front matter and the first lines are at its start


def front_matter(text):
    """({key: value}, body) of a Markdown text with an optional front matter between two '---' lines. A small YAML
    subset: 'key: value' lines, quoted or not, and a folded or literal block ('key: >' or 'key: |' with indented
    lines after it). Lists and maps are not read: no command field needs them."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    out, key, block, end = {}, None, [], None
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            end = i
            break
        if key and (line.startswith((" ", "\t")) or not line.strip()):
            block.append(line.strip())
            continue
        if key:
            out[key], key, block = " ".join(b for b in block if b), None, []
        m = re.match(r"([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if not m:
            continue
        k, v = m.group(1).lower(), m.group(2).strip()
        if v in (">", "|", ">-", "|-", ""):
            key, block = k, []
            continue
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    if key:
        out[key] = " ".join(b for b in block if b)
    if end is None:  # no closing line: no front matter
        return {}, text
    return out, "\n".join(lines[end + 1:])


def _first_line(body):
    """The first line of text of a body, without Markdown heading marks: the description of a command that has none."""
    for line in body.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s
    return ""


def _clip(s):
    s = " ".join((s or "").split())
    return s if len(s) <= DESCRIPTION_MAX else s[:DESCRIPTION_MAX - 1].rstrip() + "…"


def read_md(path):
    """({front matter}, body) of a Markdown file, or None when it cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return front_matter(f.read(FRONT_MAX))
    except OSError:
        return None


def entry(name, meta, body, prefix=""):
    """One command entry from a file's front matter and body."""
    hint = meta.get("argument-hint") or meta.get("argument_hint") or ""
    return {"name": prefix + name, "description": _clip(meta.get("description") or _first_line(body)),
            "hint": _clip(hint)}


def md_commands(folder, prefix="", recursive=True):
    """The commands of each *.md file in folder (and its subfolders when recursive): the file name without .md is
    the command's name. Sorted by name."""
    d = Path(folder)
    if not d.is_dir():
        return []
    out = []
    try:
        files = sorted(d.rglob("*.md") if recursive else d.glob("*.md"))
    except OSError:
        return []
    for p in files:
        got = read_md(p)
        if got is None or not p.is_file():
            continue
        out.append(entry(p.stem, got[0], got[1], prefix))
    return out


def skill_commands(folder, prefix=""):
    """The skills in folder (each a <name>/SKILL.md) that a person can call as a slash command: the name of the front
    matter, else the folder name. A skill with 'user-invocable: false' is left out."""
    d = Path(folder)
    if not d.is_dir():
        return []
    out = []
    try:
        dirs = sorted(p for p in d.iterdir() if p.is_dir())
    except OSError:
        return []
    for sub in dirs:
        got = read_md(sub / "SKILL.md")
        if got is None:
            continue
        meta, body = got
        if str(meta.get("user-invocable", "")).lower() == "false":
            continue
        out.append(entry(meta.get("name") or sub.name, meta, body, prefix))
    return out


def project_dirs(folder, home=None):
    """folder and its parents up to the root of its git repository (the first one with .git), never the home folder
    itself or above it: the folders whose .claude (or other agent folder) belongs to a session that runs in folder."""
    if not folder:
        return []
    home = Path(home or os.path.expanduser("~")).resolve()
    try:
        d = Path(folder).expanduser().resolve()
    except OSError:
        return []
    out = []
    while True:
        if d == home or d == d.parent or home.is_relative_to(d):
            break
        out.append(d)
        if (d / ".git").exists():
            break
        d = d.parent
    return out


def unique(entries):
    """entries with each name once: the first one wins (the caller lists the built-in and nearer ones first)."""
    seen, out = set(), []
    for e in entries:
        if e["name"] in seen:
            continue
        seen.add(e["name"])
        out.append(e)
    return out
