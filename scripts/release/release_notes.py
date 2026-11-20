#!/usr/bin/env python3
"""The notes of a MaximizePM release on GitHub, from the release report (#6185).

    release_notes.py <report.json> <version> <release-notes.md> [--project maximizepm ...]

<report.json> is the output of `maxpm --json target report <deploy id>`. The notes are, in this order:
a first line with the version and the number of tasks; then the report's summary when it is written (#6493:
what changes for a user by area, the risks and what to check, under the hood; the same text as the report,
so the two never differ), with no #<id> (a number of the private queue, which GitHub reads as one of its
own issues). Before the summary is written (MaximizePM's own text only counts the tasks): one section for
each group of the report (an area, or 'Other changes') with the 'changed' line of each done task, which its
session wrote for a person. Last, desktop/release-notes.md from its '## Download' heading on, with {TAG} and
{VERSION} filled in, as the desktop-release workflow does.

The repository is public: only the done tasks of the named projects come in (default: maximizepm), and a
line that names a home folder or the queue's local folders (data/, private/) is left out. Standard library
only; the notes go to stdout.
"""
import argparse
import json
import re
import sys

# A line with one of these is left out of the public notes: a home folder, or the git-ignored local folders.
PRIVATE = re.compile(r"(/Users/|/home/|~/|\bdata/|\bprivate/)")
TASK_KINDS = ("work", "operational")
# '(#12, #34)' after a point, and any other '#12': the ids of the private queue.
REFS = re.compile(r"\s*\((?:#\d+[,\s]*)+\)")
REF = re.compile(r"(?<![\w&])#\d+\b")


def heading(group, projects):
    """The section heading of a group of the report: the area's (or an old report's goal's) name in words, or
    'Other changes'."""
    key, name = group.get("key") or "", group.get("name") or ""
    if not key.startswith(("area:", "goal:")) or not name or name in projects:
        return "Other changes"
    words = name.replace("-", " ").replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def written(summary):
    """The report's summary as public Markdown, or '' when nobody wrote it: no #<id>, no line with a private path."""
    if not summary.get("by") or summary.get("by") == "maxpm":
        return ""
    lines = []
    for line in (summary.get("text") or "").splitlines():
        if PRIVATE.search(line):
            continue
        line = REF.sub("", REFS.sub("", line)).rstrip()
        line = re.sub(r"\s+([.,;:])", r"\1", re.sub(r"(?<=\S)  +", " ", line))
        if line or (lines and lines[-1]):
            lines.append(line)
    return "\n".join(lines).strip()


def build(report, version, tail, projects=("maximizepm",)):
    """The notes as Markdown."""
    tasks = report.get("tasks") or {}
    sections, count, seen = [], 0, set()
    by_heading = {}
    for g in report.get("groups") or []:
        lines = []
        for i in g.get("items") or []:
            t = tasks.get(str(i)) or tasks.get(i)
            if not t or t["id"] in seen:
                continue
            if t.get("status") != "done" or t.get("project") not in projects or t.get("kind", "work") not in TASK_KINDS:
                continue
            seen.add(t["id"])
            count += 1
            text = " ".join((t.get("changed") or "").split())
            if text and not PRIVATE.search(text):
                lines.append(f"- {text}")
        if lines:
            h = heading(g, projects)
            if h not in by_heading:
                by_heading[h] = []
                sections.append(h)
            by_heading[h] += lines
    if "Other changes" in sections:  # the tasks of no goal come last
        sections.remove("Other changes")
        sections.append("Other changes")
    head = f"MaximizePM {version} ships {count} task{'s' if count != 1 else ''}."
    summary = report.get("summary") or {}
    text = written(summary)
    if text:
        out = [head, "", text]
    else:
        out = [head + " The main changes:"]
        for h in sections:
            out += ["", f"## {h}", ""] + by_heading[h]
    if tail:
        at = tail.find("## Download")
        rest = tail[at:] if at >= 0 else ""
        if rest:
            out += ["", rest.replace("{TAG}", f"v{version}").replace("{VERSION}", version).rstrip()]
    return "\n".join(out) + "\n"


def main(argv=None):
    p = argparse.ArgumentParser(description="the notes of a MaximizePM release, from the release report")
    p.add_argument("report", help="the JSON of maxpm --json target report <deploy id>")
    p.add_argument("version", help="the version, for example 1.4.0")
    p.add_argument("notes", help="desktop/release-notes.md of the release (its Download part follows the changes)")
    p.add_argument("--project", action="append", help="a project whose tasks come in (repeat; default maximizepm)")
    a = p.parse_args(argv)
    with open(a.report, encoding="utf-8") as f:
        report = json.load(f)
    try:
        with open(a.notes, encoding="utf-8") as f:
            tail = f.read()
    except OSError:
        tail = ""
    sys.stdout.write(build(report, a.version, tail, tuple(a.project or ["maximizepm"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
