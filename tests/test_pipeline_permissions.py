"""What a pipeline's agents may do with no permission prompt (#6154): the list (at once, ask first, never), the run's
flags, and the refused steps of a run that become cards; a person's yes runs the task again with that step allowed."""

import json
from pathlib import Path

from river import core, runners
from river.core import RiverError
from river.pipeline import define, permissions, versions

from tests.test_runners import Base


class Permissions(Base):
    def test_the_list_and_who_changes_it(self):
        d = permissions.show(self.c, "app")
        self.assertTrue(d["default"])
        self.assertEqual(d["allow"], permissions.DEFAULT["allow"])
        with self.assertRaises(RiverError):  # an agent that is not the manager
            permissions.change(self.c, "app", allow=["Bash(npm test:*)"], actor="w1")
        for bad in ("rm -rf /", "Bash(", ""):
            with self.assertRaises(RiverError, msg=bad):
                permissions.change(self.c, "app", allow=[bad], actor="mark")
        with self.assertRaises(RiverError):  # the agent reports through maxpm
            permissions.change(self.c, "app", never=["Bash(maxpm:*)"], actor="mark")
        d = permissions.change(self.c, "app", allow=["Bash(npm test:*)"], ask=["Bash(./deploy.sh:*)"],
                               never=["Bash(rm:*)"], actor="mark")
        self.assertFalse(d["default"])
        self.assertIn("Bash(npm test:*)", d["allow"])
        d = permissions.change(self.c, "app", allow=["Bash(rm:*)"], actor="mark")  # it moves to allow
        self.assertEqual((d["never"], "Bash(rm:*)" in d["allow"]), ([], True))
        with self.assertRaises(RiverError):  # nothing changes
            permissions.change(self.c, "app", allow=["Bash(rm:*)"], actor="mark")
        exp = define.export(self.c, "app")["permissions"]
        self.assertEqual(exp["ask"], ["Bash(./deploy.sh:*)"])
        n = versions.listing(self.c, "app")
        self.assertIn("permissions: allow:", str(n))
        first = [v for v in n["versions"] if "permissions" not in (v["why"] or "")][0]["n"]
        versions.restore(self.c, "app", first, "back to before the list", actor="mark")
        self.assertTrue(permissions.show(self.c, "app")["default"])  # a version brings its list back
        with self.assertRaises(RiverError):
            permissions.set_whole(self.c, "app", {"allow": ["Read"], "never": ["Read"]}, actor="mark")

    def test_a_run_gets_the_list_as_its_own_flags_and_in_its_prompt(self):
        permissions.change(self.c, "app", allow=["Bash(npm test:*)"], ask=["Bash(./deploy.sh:*)"],
                           never=["Bash(rm:*)"], actor="mark")
        self.fault_on_runner()
        runners.serve_pass(self.c)
        script = self.spawned[0]["script"]
        line = [x for x in script.splitlines() if x.startswith("exec claude")][0]
        allowed = line.split("--allowedTools ", 1)[1].split(" --disallowedTools ")[0]
        self.assertIn("'Bash(npm test:*)'", allowed)
        self.assertIn("'Bash(maxpm:*)'", allowed)
        self.assertIn("--disallowedTools 'Bash(./deploy.sh:*)' 'Bash(rm:*)'", line)
        self.assertIn("--permission-mode dontAsk", line)
        self.assertIn("What needs a yes first: Bash(./deploy.sh:*)", script)
        self.assertIn("What you never do: Bash(rm:*)", script)

    def end_with(self, run, denials):
        """The run's transcript ends with Claude Code's result line, then the ssh process exits."""
        run = self.c.execute("SELECT * FROM runner_runs WHERE id=?", (run["id"],)).fetchone()
        Path(run["transcript"]).write_text(json.dumps({"type": "result", "result": "stopped: a step was refused",
                                                       "total_cost_usd": 0.2, "permission_denials": denials}) + "\n")
        self.proc.code = 0
        return runners.serve_pass(self.c)

    def test_refused_steps_become_cards_and_a_yes_runs_the_task_again(self):
        permissions.change(self.c, "app", ask=["Bash(./deploy.sh:*)"], never=["Bash(rm:*)"], actor="mark")
        run = self.fault_on_runner()
        runners.serve_pass(self.c)
        self.end_with(run, [
            {"tool_name": "Bash", "tool_use_id": "a", "tool_input": {"command": "./deploy.sh prod"}},
            {"tool_name": "Bash", "tool_use_id": "b", "tool_input": {"command": "rm -rf build"}},
            {"tool_name": "WebFetch", "tool_use_id": "c", "tool_input": {"url": "https://example.com/x"}},
            {"tool_name": "Bash", "tool_use_id": "d", "tool_input": {"command": "./deploy.sh prod"}}])
        result = self.c.execute("SELECT result FROM runner_runs WHERE id=?", (run["id"],)).fetchone()[0]
        self.assertIn("refused Bash(rm -rf build) (never)", result)
        asks = self.c.execute("SELECT * FROM pipeline_permit_asks ORDER BY id").fetchall()
        self.assertEqual([(a["rule"], a["listed"], a["state"]) for a in asks],
                         [("Bash(./deploy.sh prod)", "Bash(./deploy.sh:*)", "asked"),
                          ("WebFetch(https://example.com/x)", None, "asked")])
        cards = [core.item_show(self.c, a["card"]) for a in asks]
        self.assertEqual({c["doer"] for c in cards}, {"human"})
        task = core.item_show(self.c, run["item"])
        self.assertEqual(sorted(task["waits_on"]), sorted(a["card"] for a in asks))  # the task waits on its cards
        audit = [r[0] for r in self.c.execute("SELECT action FROM pipeline_changes WHERE kind='agent'")]
        self.assertEqual(sorted(audit), ["ask", "ask", "refused"])
        # The run's own agent cannot give the yes; a person can.
        core.done(self.c, asks[1]["card"], actor="w1", note="I allow it myself")
        core.done(self.c, asks[0]["card"], actor="mark", note="yes, once")
        out = runners.serve_pass(self.c)
        self.assertEqual(sorted((p["rule"], p["yes"]) for p in out["permits"]),
                         [("Bash(./deploy.sh prod)", True), ("WebFetch(https://example.com/x)", False)])
        again = self.c.execute("SELECT * FROM runner_runs ORDER BY id DESC").fetchone()
        self.assertNotEqual(again["id"], run["id"])
        self.assertEqual(again["item"], run["item"])
        runners.serve_pass(self.c)
        line = [x for x in self.spawned[-1]["script"].splitlines() if x.startswith("exec claude")][0]
        self.assertIn("'Bash(./deploy.sh prod)'", line.split("--disallowedTools")[0])  # allowed for this task
        self.assertIn("A yes allowed for this task: Bash(./deploy.sh prod)", self.spawned[-1]["script"])
        listed = permissions.show(self.c, "app")["asks"]
        self.assertEqual({a["state"] for a in listed}, {"yes", "no"})

    def test_the_rules_match_as_claude_code_reads_them(self):
        m = permissions.matches
        self.assertTrue(m("Bash(git push:*)", "Bash", "git push origin main"))
        self.assertFalse(m("Bash(git push:*)", "Bash", "git pull"))
        self.assertTrue(m("Bash(make deploy)", "Bash", "make deploy"))
        self.assertFalse(m("Bash(make deploy)", "Bash", "make deploy-all"))
        self.assertTrue(m("Edit", "Edit", "/x/y.py"))
        self.assertTrue(m("Edit(src/*)", "Edit", "src/a.py"))
        self.assertTrue(m("WebFetch(domain:example.com)", "WebFetch", "https://example.com/a"))
        self.assertFalse(m("Read", "Edit", "a"))
