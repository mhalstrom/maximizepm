"""The cost view (#5857): each session's, item's and project's tokens and dollars in a period, from the agents' chat
logs; the plan windows; the optional cost limit (none by default) with one alert at 80% and at 100%."""

import os
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

from river import core, cost
from river.core import RiverError

M = 1_000_000


def call(t, model="claude-opus-5-5", input=0, cache_read=0, write_5m=0, write_1h=0, output=0):
    return (t, {"input": input, "cache_read": cache_read, "write_5m": write_5m, "write_1h": write_1h,
                "cache_write": write_5m + write_1h, "output": output, "model": model})


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.c = core.connect(os.path.join(self.dir.name, "t.db"))
        self.addCleanup(self.c.close)
        cost._REQS.clear()
        self.addCleanup(cost._REQS.clear)
        core.project_add(self.c, "app")
        core.project_add(self.c, "site")
        core.register(self.c, "mark", human=True)
        core.register(self.c, "dev")
        self.now = core.now()
        self.logs = {}
        p = mock.patch.object(core, "_session_requests",
                              lambda sid, root=None: ({str(i): r for i, r in enumerate(self.logs[sid])}, False)
                              if sid in self.logs else None)
        p.start()
        self.addCleanup(p.stop)

    def session(self, agent, sid, calls):
        self.c.execute("INSERT INTO agent_sessions (agent, session_id, first_seen) VALUES (?,?,?)",
                       (agent, sid, core.iso(self.now - timedelta(hours=3))))
        self.c.commit()
        self.logs[sid] = calls

    def hold(self, agent, item, start, end=None):
        self.c.execute("INSERT INTO holds (item_id, agent, started_at, ended_at) VALUES (?,?,?,?)",
                       (item, agent, core.iso(start), core.iso(end) if end else None))
        self.c.commit()


class Report(Base):
    def test_calls_go_to_the_item_held_then_and_dollars_include_output(self):
        a = core.item_add(self.c, "app", "first", actor="t")["id"]
        b = core.item_add(self.c, "site", "second", actor="t")["id"]
        h = lambda n: self.now - timedelta(minutes=n)
        self.hold("dev", a, h(100), h(50))
        self.hold("dev", b, h(50))
        self.session("dev", "s1", [call(h(90), input=M, output=M),      # item a: 4 + 20 dollars
                                   call(h(40), cache_read=10 * M),     # item b: 10 x 0.2 = 2
                                   call(h(30), model="claude-haiku-5-5", output=2 * M)])  # b too: 2 x 0.5 = 1
        r = cost.report(self.c, "today")
        s = r["sessions"][0]
        self.assertEqual((s["agent"], s["calls"], s["usd"]), ("dev", 3, 27.0))
        self.assertEqual(s["items"], [a, b])
        its = {i["id"]: i for i in r["items"]}
        self.assertEqual((its[a]["usd"], its[b]["usd"]), (24.0, 3.0))
        self.assertEqual(r["items"][0]["id"], a)  # most expensive first
        self.assertEqual({p["project"]: p["usd"] for p in r["projects"]}, {"app": 24.0, "site": 3.0})
        self.assertEqual(r["total"]["usd"], 27.0)
        self.assertTrue(r["output_priced"])
        self.assertEqual(r["total"]["tokens"], 2 * M + 10 * M + 2 * M)

    def test_a_call_with_no_item_goes_to_the_sessions_project_and_a_model_with_no_price_counts_tokens(self):
        self.hold("dev", core.item_add(self.c, "site", "old", actor="t")["id"],
                  self.now - timedelta(days=3), self.now - timedelta(days=2))
        self.session("dev", "s1", [call(self.now - timedelta(minutes=5), model="gpt-9", input=M)])
        r = cost.report(self.c, "today")
        self.assertEqual(r["projects"][0]["project"], "site")  # the project of its last held item
        self.assertEqual((r["total"]["usd"], r["total"]["unpriced"], r["total"]["input"]), (0.0, 1, M))

    def test_the_period_leaves_older_calls_out(self):
        self.session("dev", "s1", [call(self.now - timedelta(days=3), input=M),
                                   call(self.now - timedelta(minutes=1), input=M)])
        self.assertEqual(cost.report(self.c, "7d")["total"]["calls"], 2)
        self.assertEqual(cost.report(self.c, "today")["total"]["calls"], 1)
        self.assertEqual(cost.report(self.c, "all")["total"]["calls"], 2)
        with self.assertRaises(RiverError):
            cost.report(self.c, "yesterday")

    def test_a_price_without_output_leaves_output_out_and_says_so(self):
        core.config_set(self.c, "model_prices", "opus=4/0.2/5/8")
        self.session("dev", "s1", [call(self.now - timedelta(minutes=1), input=M, output=M)])
        r = cost.report(self.c, "today")
        self.assertEqual(r["total"]["usd"], 4.0)
        self.assertFalse(r["output_priced"])
        self.assertEqual(core.model_price(self.c, "opus"), (4.0, 0.2, 5.0, 8.0))  # the cache cost keeps four
        with self.assertRaises(RiverError):
            core.config_set(self.c, "model_prices", "opus=1/2/3/4/5/6")

    def test_csv(self):
        self.session("dev", "s1", [call(self.now - timedelta(minutes=1), input=M)])
        text = cost.csv_text(cost.report(self.c, "today"), "sessions")
        self.assertTrue(text.startswith("agent,model,state,calls,input,"))
        self.assertIn("dev,claude-opus-5-5,", text)


class Limits(Base):
    def test_no_limit_by_default(self):
        self.assertEqual(core.setting(self.c, "cost_limit"), "")
        self.assertEqual(cost.limits(self.c), [])
        self.assertEqual(cost.watch(self.c), [])

    def test_the_forms_of_a_limit(self):
        self.assertEqual(cost.parse_limit("$40/day"), {"usd": 40.0, "per": "day"})
        self.assertEqual(cost.parse_limit("20M/week"), {"eq": 20 * M, "per": "week"})
        self.assertIsNone(cost.parse_limit(""))
        for bad in ("40", "$40/hour", "lots"):
            with self.assertRaises(RiverError):
                core.config_set(self.c, "cost_limit", bad)

    def test_one_alert_at_80_and_one_at_100_percent(self):
        core.config_set(self.c, "cost_limit", "$10/day")
        core.config_set(self.c, "cost_limit", "$100/week", project="app")
        self.session("dev", "s1", [call(self.now - timedelta(minutes=2), input=2 * M)])  # $8
        got = cost.limits(self.c)
        self.assertEqual([(x["project"], x["percent"]) for x in got], [(None, 80.0), ("app", 0.0)])  # no app use yet
        self.assertEqual(len(cost.watch(self.c, force=True)), 1)
        self.assertEqual(cost.watch(self.c, force=True), [])  # once
        cost._REQS.clear()
        self.logs["s1"].append(call(self.now - timedelta(minutes=1), input=M))  # $12
        self.assertEqual(cost.watch(self.c), [])  # the loop looks again only after WATCH_EVERY
        self.assertEqual(len(cost.watch(self.c, force=True)), 1)
        bodies = [r[0] for r in self.c.execute("SELECT body FROM messages WHERE to_agent='mark' ORDER BY id")]
        self.assertEqual(len(bodies), 2)
        self.assertIn("Cost: $8.00 of $10/day (80%)", bodies[0])
        self.assertIn("Cost: $12.00 of $10/day (120%)", bodies[1])
        self.assertIn("Nothing stops by itself", bodies[1])


class Route(Base):
    def get(self, path):
        from river import server
        os.environ["MAXPM_DB"] = os.path.join(self.dir.name, "t.db")
        self.addCleanup(os.environ.pop, "MAXPM_DB", None)
        h = server.Handler.__new__(server.Handler)
        h.path, h.client_address, h.headers = path, ("127.0.0.1", 1), {"Host": "127.0.0.1:8765"}
        got = {"headers": {}}
        h._send = lambda code, body, *a, **k: got.update(code=code, body=body)
        h.send_response = lambda code: got.update(code=code)
        h.send_header = lambda k, v: got["headers"].update({k: v})
        h.end_headers = lambda: None
        import io
        h.wfile = io.BytesIO()
        h.do_GET()
        got["raw"] = h.wfile.getvalue()
        return got

    def test_the_report_and_the_csv(self):
        self.session("dev", "s1", [call(self.now - timedelta(minutes=1), input=M)])
        got = self.get("/api/cost?period=today")
        self.assertEqual((got["code"], got["body"]["total"]["usd"]), (200, 4.0))
        got = self.get("/api/cost?period=today&csv=sessions")
        self.assertEqual(got["headers"]["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn(b"dev,claude-opus-5-5", got["raw"])
        self.assertEqual(self.get("/api/cost?period=never")["code"], 400)

    def test_the_page_answers_at_cost(self):
        from river import server
        self.assertIn("cost", server.PAGE_VIEWS)  # /cost gets the page, which opens the Cost view


if __name__ == "__main__":
    unittest.main()
