"""Events in the CDEvents form (cdevents.dev, a draft standard of the CD Foundation; we follow spec 0.4).

An event is a JSON object:

    {"context": {"version": "0.4.1", "id": "<unique for its source>", "source": "<who sent it>",
                 "type": "dev.cdevents.<subject>.<predicate>.<version>", "timestamp": "<RFC 3339>"},
     "subject": {"id": "<the run, test or service>", "source": "...", "type": "pipelineRun", "content": {...}},
     "customData": {"maxpm": {...}}, "customDataContentType": "application/json"}

The subjects that change the records (store.apply): pipelinerun (a stage run), taskrun (a step), testsuiterun,
testcaserun, service (deployed, upgraded, rolledback, removed) and environment (created, modified, deleted). Any
other type is kept in pipeline_events and changes no record.

What CDEvents does not say goes in customData.maxpm, which every event of ours carries and an adapter sets:
pipeline, stage, kind, version (the commit), ref, run (a stage run's id), run_key (its pipelinerun subject id),
where (the host), parallel, suite, seconds, detail, file, note.

The CDEvents draft may change: only this module and store.apply read its form; the records are ours.
"""

import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from .. import core

SPEC = "0.4.1"

# The subject type in an event, from the subject in its type string.
SUBJECT_TYPES = {"pipelinerun": "pipelineRun", "taskrun": "taskRun", "testsuiterun": "testSuiteRun",
                 "testcaserun": "testCaseRun", "service": "service", "environment": "environment",
                 "build": "build", "artifact": "artifact", "change": "change", "branch": "branch",
                 "repository": "repository", "incident": "incident"}

# The version of each subject's event schema in spec 0.4, written into the types we make.
TYPE_VERSIONS = {"pipelinerun": "0.2.0", "taskrun": "0.2.0", "testsuiterun": "0.2.0", "testcaserun": "0.2.0",
                 "service": "0.2.0", "environment": "0.2.0"}

_TYPE = re.compile(r"^dev\.cdevents\.([a-z]+)\.([a-z]+)(?:\.(\d+\.\d+\.\d+(?:-[\w.]+)?))?$")


def make(subject, predicate, subject_id, content=None, maxpm=None, source="maxpm", at=None, event_id=None):
    """One event of ours. maxpm is the customData.maxpm object; None values are left out."""
    ctx = {"version": SPEC, "id": event_id or str(uuid.uuid4()), "source": source,
           "type": f"dev.cdevents.{subject}.{predicate}.{TYPE_VERSIONS.get(subject, '0.1.0')}",
           "timestamp": at or core.iso(core.now())}
    ev = {"context": ctx,
          "subject": {"id": str(subject_id), "source": source, "type": SUBJECT_TYPES.get(subject, subject),
                      "content": {k: v for k, v in (content or {}).items() if v is not None}}}
    extra = {k: v for k, v in (maxpm or {}).items() if v is not None}
    if extra:
        ev["customData"] = {"maxpm": extra}
        ev["customDataContentType"] = "application/json"
    return ev


def parse(event):
    """The parts of an event that store.apply reads, after a check of its form. Refuses an event with no type,
    id or source, or a type that is not dev.cdevents.<subject>.<predicate>."""
    if isinstance(event, (str, bytes)):
        try:
            event = json.loads(event)
        except ValueError as e:
            raise core.RiverError(f"the event is not JSON ({e}); send one CDEvents object") from None
    if not isinstance(event, dict):
        raise core.RiverError("the event is not a JSON object; send one CDEvents object")
    ctx = event.get("context")
    subj = event.get("subject")
    if not isinstance(ctx, dict) or not isinstance(subj, dict):
        raise core.RiverError("the event has no context or no subject; a CDEvents event has both")
    m = _TYPE.match(str(ctx.get("type") or ""))
    if not m:
        raise core.RiverError(f"the event type {ctx.get('type')!r} is not dev.cdevents.<subject>.<predicate>"
                              ".<version>")
    if not ctx.get("id") or not ctx.get("source"):
        raise core.RiverError("the event has no context.id or no context.source; CDEvents needs both")
    custom = event.get("customData")
    if isinstance(custom, str):
        try:
            custom = json.loads(custom)
        except ValueError:
            custom = None
    extra = custom.get("maxpm") if isinstance(custom, dict) else None
    content = subj.get("content")
    return {"event": event, "subject": m.group(1), "predicate": m.group(2), "type": ctx["type"],
            "id": str(ctx["id"]), "source": str(ctx["source"]), "at": timestamp(ctx.get("timestamp")),
            "chain": ctx.get("chainId"), "subject_id": str(subj.get("id") or ""),
            "content": content if isinstance(content, dict) else {},
            "maxpm": extra if isinstance(extra, dict) else {}}


_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")


def timestamp(s):
    """An RFC 3339 time (any offset, any fraction) as the queue's UTC form; now when there is none. Refuses a
    time in another form."""
    if not s:
        return core.iso(core.now())
    m = _STAMP.match(str(s).strip())
    if not m:
        raise core.RiverError(f"the event time {s!r} is not RFC 3339 (2026-11-18T14:52:03Z)")
    t = datetime.strptime(f"{m.group(1)}T{m.group(2)}", "%Y-%m-%dT%H:%M:%S")
    off = m.group(4) or "Z"
    if off != "Z":
        sign = 1 if off[0] == "+" else -1
        hh, mm = int(off[1:3]), int(off[-2:])
        t -= sign * timedelta(hours=hh, minutes=mm)
    return core.iso(t.replace(tzinfo=timezone.utc))


# The outcome words of CDEvents (and the usual words of the forges) as the states of our records.
OUTCOMES = {"success": "passed", "succeeded": "passed", "pass": "passed", "passed": "passed",
            "failure": "failed", "fail": "failed", "failed": "failed", "error": "failed",
            "cancel": "cancelled", "cancelled": "cancelled", "canceled": "cancelled",
            "skip": "skipped", "skipped": "skipped"}


def outcome(word, default="passed"):
    """A run's or a step's state from an outcome word; default when there is none."""
    if not word:
        return default
    return OUTCOMES.get(str(word).lower(), "failed")


def test_outcome(word):
    """A test's state: CDEvents keeps error apart from fail, like JUnit."""
    w = str(word or "").lower()
    if w == "error":
        return "error"
    return OUTCOMES.get(w, "failed") if w else "passed"
