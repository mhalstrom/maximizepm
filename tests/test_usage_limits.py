"""Tests for #6348: per-agent usage limit reading, status/page reporting, automatic fallback restart, and launch
avoidance while an agent is at its usage limit."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from river import agents, cli, core, cost, server, sessions


def _db(tmp):
    conn = core.connect(Path(tmp) / "test.db")
    core.project_add(conn, "app", path=str(tmp))
    return conn


class ResetTimeAndAdapterLimits(unittest.TestCase):
    NOW = datetime(2026, 11, 20, 16, 0, 0, tzinfo=timezone.utc)

    def test_parse_reset_time_formats(self):
        at, txt = agents.parse_reset_time("Individual quota reached. Resets in ~30m", now=self.NOW)
        self.assertEqual(at, "2026-11-20T16:30:00Z")
        self.assertIn("30m", txt)

        at, _ = agents.parse_reset_time("Try again in 2h 15m", now=self.NOW)
        self.assertEqual(at, "2026-11-20T18:15:00Z")

        at, _ = agents.parse_reset_time("You've hit your limit · resets 3pm (America/Los_Angeles)", now=self.NOW)
        # 2026-11-20 is PST (UTC-8), so 3pm PST = 23:00 UTC
        self.assertEqual(at, "2026-11-20T23:00:00Z")

        at, _ = agents.parse_reset_time("resets 2026-11-20T19:00:00Z", now=self.NOW)
        self.assertEqual(at, "2026-11-20T19:00:00Z")

    def test_antigravity_limit_stop_and_screen_limits(self):
        ad = agents.get("antigravity")
        hit = ad.limit_stop([
            "Generating...",
            "Individual quota reached for Gemini 4 Neon. Resets in ~30m",
            "? for shortcuts",
        ], now=self.NOW)
        self.assertIsNotNone(hit)
        self.assertIn("Individual quota reached", hit["message"])
        self.assertEqual(hit["resets_at"], "2026-11-20T16:30:00Z")
        # The real /usage screen: RealScreensAndLogs.test_real_usage_screens.

    def test_claude_limit_stop_and_screen_limits(self):
        ad = agents.get("claude")
        hit = ad.limit_stop([
            "You've hit your limit · resets 3pm (America/Los_Angeles)",
        ], now=self.NOW)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["resets_at"], "2026-11-20T23:00:00Z")

        lims = ad.screen_limits([
            "Settings: Status  Config  Usage",
            "Current session",
            "████████░░░░░░░░ 42% used",
            "Resets in 1h 30m",
            "Current week (all models)",
            "████░░░░░░░░░░░░ 18% used",
            "Resets Nov 25, 3pm (UTC)",
        ], now=self.NOW)
        self.assertEqual(len(lims), 2)
        self.assertEqual(lims[0]["name"], "Current session")
        self.assertEqual(lims[0]["used_percent"], 42.0)
        self.assertEqual(lims[0]["remaining_percent"], 58.0)
        self.assertEqual(lims[0]["resets_at"], "2026-11-20T17:30:00Z")
        self.assertEqual(lims[1]["remaining_percent"], 82.0)

    def test_codex_and_grok_limit_stop_and_screen_limits(self):
        codex = agents.get("codex")
        hit = codex.limit_stop(["You've hit your usage limit. Try again in 45 minutes."], now=self.NOW)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["resets_at"], "2026-11-20T16:45:00Z")

        lims = codex.screen_limits([
            "5h limit: [████████--] 80% left (resets in 3h)",
            "Weekly limit: [██████----] 60% left (resets Nov 25, 3pm (UTC))",
        ], now=self.NOW)
        self.assertEqual(len(lims), 2)
        self.assertEqual(lims[0]["name"], "5-hour window")
        self.assertEqual(lims[0]["remaining_percent"], 80.0)
        self.assertEqual(lims[0]["used_percent"], 20.0)

        grok = agents.get("grok")
        ghit = grok.limit_stop(["You hit your free usage limit. Resets in 2h"], now=self.NOW)
        self.assertIsNotNone(ghit)
        self.assertEqual(ghit["resets_at"], "2026-11-20T18:00:00Z")


class RealScreensAndLogs(unittest.TestCase):
    """The texts the agents show (from real panes), the Codex log read with no session, and windows that reset."""
    NOW = datetime(2026, 11, 20, 16, 0, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _db(self.tmp.name)
        self.p_now = mock.patch.object(core, "now", side_effect=lambda: self.NOW)
        self.p_now.start()

    def tearDown(self):
        self.p_now.stop()
        self.conn.close()
        self.tmp.cleanup()

    def test_antigravity_real_quota_message(self):
        # The words of an agy pane on 2026-11-20 (#6348).
        hit = agents.get("antigravity").limit_stop([
            "Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 2h49m14s.",
            "Error ID: dce466d8-09f7-40fd-a887-d5af5a42f2da-1",
            "────────────────────────",
        ], now=self.NOW)
        self.assertEqual(hit["resets_at"], "2026-11-20T18:49:14Z")

    def test_real_usage_screens(self):
        # The /usage and /status screens of agy, Claude Code 2.1 and Codex 0.159 on 2026-11-20 (#6348).
        agy = agents.get("antigravity").screen_limits([
            "└ Models & Quota",
            "GEMINI MODELS",
            "  Models within this group: Gemini 4 Neon, Gemini 4 Carbon,",
            "  Weekly Limit Remaining",
            "    [█████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░] 26.07%",
            "    Refreshes in 155h 31m",
            "  Five Hour Limit Remaining",
            "    [░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░] 0.00%",
            "    Refreshes in 2h 31m",
            "CLAUDE AND GPT MODELS",
            "  Weekly Limit Remaining",
            "    [██████████████████████████████████████████████████] 100.00%",
            "    Refreshes in 167h 55m",
            "  Five Hour Limit Remaining",
            "    [██████████████████████████████████████████████████] 99.99%",
            "    Refreshes in 4h 55m",
        ], now=self.NOW)
        self.assertEqual([(w["name"], w["remaining_percent"]) for w in agy],
                         [("Gemini models: weekly", 26.1), ("Gemini models: 5-hour", 0.0),
                          ("Claude and GPT models: weekly", 100.0), ("Claude and GPT models: 5-hour", 100.0)])
        self.assertEqual(agy[1]["resets_at"], "2026-11-20T18:31:00Z")

        cc = agents.get("claude").screen_limits([
            "  Settings  Status   Config   Usage   Stats",
            "  Current week (all models)",
            "  ███████████                                        22% used",
            "  Resets Nov 25 at 11:59pm (America/Los_Angeles)",
            "  Current week (Fable)",
            "  ██▌                                                5% used",
            "  Resets Nov 25 at 11:59pm (America/Los_Angeles)",
        ], now=self.NOW)
        self.assertEqual([(w["name"], w["remaining_percent"]) for w in cc],
                         [("Current week (all models)", 78.0), ("Current week (Fable)", 95.0)])
        self.assertEqual(cc[0]["resets_at"], "2026-11-26T07:59:00Z")

        with mock.patch.object(core, "_machine_zone", return_value=timezone(timedelta(hours=-8))):
            cx = agents.get("codex").screen_limits([
                "  5h limit:            [████████████████████] 100% left (resets 2:09 PM)",
                "  Weekly limit:        [████████████████████] 99% left (resets 2:46 PM on 26 Nov)",
                "  Credits:             75000 credits",
            ], now=self.NOW)
        self.assertEqual([(w["name"], w["remaining_percent"]) for w in cx],
                         [("5-hour window", 100.0), ("weekly window", 99.0)])
        self.assertEqual(cx[0]["resets_at"], "2026-11-20T22:09:00Z")  # 2:09 PM at UTC-8
        self.assertEqual(cx[1]["resets_at"], "2026-11-26T22:46:00Z")

    def test_codex_log_read_with_no_session(self):
        day = Path(self.tmp.name) / "codex" / "2026" / "11" / "20"
        day.mkdir(parents=True)
        reset = int(datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc).timestamp())
        (day / "rollout-2026-11-20T10-00-00-abc.jsonl").write_text(
            '{"type":"event_msg","payload":{"type":"token_count","rate_limits":{"primary":{"used_percent":40,'
            f'"window_minutes":300,"resets_at":{reset}}},"secondary":{{"used_percent":10,"window_minutes":10080}}}}}}}}\n')
        ad = agents.get("codex")
        with mock.patch.object(ad.module, "chat_root", return_value=Path(self.tmp.name) / "codex"):
            got = core.record_log_limits(self.conn)
            self.assertIn("codex", got)
            self.assertEqual(core.record_log_limits(self.conn), {})  # the same log: nothing written again
        row = core.agent_limit_status(self.conn, "codex")
        self.assertEqual(row["remaining_pct"], 60.0)
        self.assertEqual(row["source"], "log")
        # After its reset time, the 5-hour window starts again at 0% used.
        self.NOW = datetime(2026, 11, 20, 18, 30, tzinfo=timezone.utc)
        row = core.agent_limit_status(self.conn, "codex")
        self.assertEqual(row["remaining_pct"], 90.0)
        self.assertTrue(row["windows"][0]["reset_since"])

    def _watch(self, text):
        screen = {"ended": False, "running": "antigravity", "tty": None, "target": "maxpm:agy-1", "text": text}
        with mock.patch.object(server, "agent_terminals", return_value={"agy-1": "%1"}), \
             mock.patch.object(server, "_pane_screen", return_value=screen), \
             mock.patch.object(server, "_pane_session_id", return_value=None), \
             mock.patch.object(server, "_pane_process", return_value=None), \
             mock.patch.object(server, "_handle_limit_stops", return_value=[]) as h:
            server.watch_prompts(self.conn)
        return h.call_count

    def test_limit_words_on_a_working_screen_or_seen_before_do_not_count(self):
        core.register(self.conn, "agy-1")
        self.addCleanup(server.LIMIT_SEEN.clear)
        msg = "Individual quota reached. Please upgrade your subscription. Resets in 29m45s."
        # A diff on a working screen (agy's spinner line) that shows the words: no limit stop.
        self.assertEqual(self._watch(f"+ {msg}\n⠋ Generating\n"), 0)
        self.assertIsNone(core.agent_limit_status(self.conn, "antigravity"))
        # The real stop counts once; the same message on the next pass does not move the reset time.
        self.assertEqual(self._watch(msg + "\n> \n"), 1)
        at = core.agent_limit_status(self.conn, "antigravity")["resets_at"]
        self.NOW += timedelta(minutes=5)
        self.assertEqual(self._watch(msg + "\n> \n"), 0)
        self.assertEqual(core.agent_limit_status(self.conn, "antigravity")["resets_at"], at)


class FallbackAndLaunchAvoidance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _db(self.tmp.name)
        self.t0 = datetime(2026, 11, 20, 16, 0, 0, tzinfo=timezone.utc)
        self.p_now = mock.patch.object(core, "now", side_effect=lambda: self.t0)
        self.p_now.start()

    def tearDown(self):
        self.p_now.stop()
        self.conn.close()
        self.tmp.cleanup()

    def test_agent_order_setting(self):
        # Empty by default (#6361): the launch_agents entries in their order, so a new queue never starts Antigravity.
        self.assertEqual(core.setting(self.conn, "agent_order"), "")
        self.assertEqual([p["agent"] for p in core.agent_providers(self.conn)], ["claude"])
        self.assertEqual(core.parse_agent_order("antigravity, claude-code, codex"), ["antigravity", "claude", "codex"])
        self.assertEqual(core.parse_agent_order("antigravity -> claude-code"), ["antigravity", "claude"])
        with self.assertRaises(core.RiverError):
            core.config_set(self.conn, "agent_order", "antigravity, antigravity")
        with self.assertRaises(core.RiverError):
            core.config_set(self.conn, "agent_order", "antigravity, nonexistent")

    def test_fallback_takes_the_first_other_agent_of_the_list_that_is_not_at_its_limit(self):
        core.config_set(self.conn, "launch_agents", "Claude Code=@claude-code; Codex=@codex; Antigravity=@antigravity")
        core.config_set(self.conn, "agent_order", "codex, antigravity, claude-code")
        self.assertEqual(core.fallback_agent(self.conn, "antigravity")[0], "codex")
        core.record_agent_limit(self.conn, "codex", limited=True, reason="5-hour limit reached")
        self.assertEqual(core.fallback_agent(self.conn, "antigravity")[0], "claude")
        core.record_agent_limit(self.conn, "claude", limited=True)
        self.assertIsNone(core.fallback_agent(self.conn, "antigravity"))

    def test_launch_target_avoids_limited_agent_until_reset(self):
        core.config_set(self.conn, "default_agent", "antigravity")
        it = core.item_add(self.conn, "app", "Build feature")
        core.record_agent_limit(self.conn, "antigravity", limited=True,
                                resets_at=core.iso(self.t0 + timedelta(minutes=30)),
                                resets_text="in ~30m", reason="Individual quota reached", source="limit_stop")
        # Default launch switches automatically from Antigravity to Claude Code
        t = core.launch_target(self.conn, item=it["id"])
        self.assertEqual(t["platform"], "claude-code")
        self.assertIn("usage limit", t["fallback_why"])

        # Explicit launch of limited agent is refused
        with self.assertRaises(core.RiverError) as ctx:
            core.launch_target(self.conn, item=it["id"], agent="Antigravity CLI")
        self.assertIn("usage limit", str(ctx.exception))

        # When fallback is also limited, launch refuses
        core.record_agent_limit(self.conn, "claude", limited=True,
                                resets_at=core.iso(self.t0 + timedelta(minutes=20)),
                                reason="You've hit your limit", source="limit_stop")
        with self.assertRaises(core.RiverError) as ctx2:
            core.launch_target(self.conn, item=it["id"])
        self.assertIn("no fallback agent is available", str(ctx2.exception))

        # After Antigravity's reset time passes, launch picks Antigravity again
        self.t0 = self.t0 + timedelta(minutes=31)
        t2 = core.launch_target(self.conn, item=it["id"])
        self.assertEqual(t2["platform"], "antigravity")
        self.assertNotIn("fallback_why", t2)

    def test_status_cost_and_session_usage_report_agent_limits(self):
        core.record_agent_limit(self.conn, "antigravity", limited=True,
                                resets_at=core.iso(self.t0 + timedelta(minutes=30)),
                                resets_text="in ~30m", reason="Individual quota reached", source="limit_stop")
        core.record_agent_limit(self.conn, "claude", limited=False,
                                windows=[{"name": "Current session", "used_percent": 35.0, "remaining_percent": 65.0,
                                          "resets_at": core.iso(self.t0 + timedelta(hours=2))}],
                                source="screen")
        st = core.status(self.conn)
        self.assertEqual([r["agent_type"] for r in st["agent_limits"] if r["known"]], ["claude", "antigravity"])
        # Codex and Hermes have no reading: they show too, as not known (mark 2026-11-20: also with no session of the agent)
        self.assertEqual([r["agent_type"] for r in st["agent_limits"]], ["claude", "codex", "antigravity", "hermes"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.render_status(st)
        out = buf.getvalue()
        self.assertIn("Agent usage:", out)
        self.assertIn("Antigravity CLI: AT LIMIT", out)
        # #6518: the part used, as the usage dialog and the providers' own pages say it.
        self.assertIn("Claude Code: 35% used", out)
        self.assertNotIn("% left", out)
        self.assertNotIn("% remaining", out)
        self.assertEqual(core.used_text(65.0), "35% used")
        self.assertEqual(core.used_text(0), "100% used")
        self.assertEqual(core.used_text(100.4), "0% used")

        # Page state carries agent_limits
        pst = core.state(self.conn)
        self.assertEqual(len(pst["agent_limits"]), 4)
        self.assertIn("Codex: not known", out)

        # Cost plans carries recorded agent limits
        pl = cost.plans(self.conn, [])
        self.assertTrue(any(w["agent"] == "Antigravity CLI" and w.get("limited") for w in pl["windows"]))
        self.assertTrue(any(w["agent"] == "Claude Code" and w.get("remaining_percent") == 65.0 for w in pl["windows"]))

        # Session usage panel falls back to recorded agent_limits
        core.register(self.conn, "agy-1")
        with core.tx(self.conn):
            self.conn.execute("UPDATE agents SET agent_type='antigravity' WHERE name='agy-1'")
        su = sessions.session_usage(self.conn, "agy-1")
        self.assertIsNotNone(su["limits"])
        self.assertEqual(su["limits"][0]["remaining_percent"], 0.0)


class ModelGroupLimits(unittest.TestCase):
    """#6370: agy's quota is per model group. Gemini at 0% blocks only a launch of a Gemini model."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _db(self.tmp.name)
        self.t0 = datetime(2026, 11, 20, 17, 35, 0, tzinfo=timezone.utc)
        self.p_now = mock.patch.object(core, "now", side_effect=lambda: self.t0)
        self.p_now.start()

    def tearDown(self):
        self.p_now.stop()
        self.conn.close()
        self.tmp.cleanup()

    def _windows(self, gemini_5h=0.0, claude_5h=99.3):
        at = core.iso(self.t0 + timedelta(hours=2, minutes=5))
        def w(name, rem):
            return {"name": name, "remaining_percent": rem, "used_percent": round(100 - rem, 1),
                    "resets_at": at, "resets_text": "in 2h 5m"}
        return [w("Gemini models: weekly", 26.1), w("Gemini models: 5-hour", gemini_5h),
                w("Claude and GPT models: weekly", 100.0), w("Claude and GPT models: 5-hour", claude_5h)]

    def test_one_empty_group_limits_only_its_models(self):
        # The real agy /usage of 2026-11-20 17:35Z: watch_prompts passes limited=True when any window is full.
        st = core.record_agent_limit(self.conn, "antigravity", limited=True, windows=self._windows(), source="screen")
        self.assertFalse(st["limited"])
        self.assertIsNone(core.agent_is_limited(self.conn, "antigravity"))  # no model known: the agent is free
        for model in ("claude-opus-5-5-high", "opus", "gpt-oss-120b-medium"):
            self.assertIsNone(core.agent_is_limited(self.conn, "antigravity", model=model), model)
        lim = core.agent_is_limited(self.conn, "antigravity", model="gemini-4-neon-high")
        self.assertEqual(lim["reason"], "Gemini models: 5-hour at limit")
        self.assertEqual(lim["resets_at"], "2026-11-20T19:40:00Z")
        # agy's own default model (agy_model) counts when the launch names none.
        core.config_set(self.conn, "agy_model", "gemini-4-neon")
        self.assertIsNotNone(core.agent_is_limited(self.conn, "antigravity"))

    def test_every_group_empty_limits_the_whole_agent(self):
        st = core.record_agent_limit(self.conn, "antigravity", limited=True, windows=self._windows(claude_5h=0.0))
        self.assertTrue(st["limited"])
        self.assertIsNotNone(core.agent_is_limited(self.conn, "antigravity", model="claude-opus-5-5-high"))

    def test_the_group_limit_ends_at_its_reset(self):
        core.record_agent_limit(self.conn, "antigravity", limited=True, windows=self._windows())
        self.t0 += timedelta(hours=2, minutes=6)
        self.assertIsNone(core.agent_is_limited(self.conn, "antigravity", model="gemini-4-neon-high"))

    def test_fallback_and_launch_use_the_model_of_the_launch(self):
        core.config_set(self.conn, "launch_agents", "Claude Code=@claude-code; Antigravity=@antigravity")
        core.record_agent_limit(self.conn, "antigravity", limited=True, windows=self._windows())
        # Claude Code at its limit: agy takes a Claude model, not a Gemini one.
        self.assertEqual(core.fallback_agent(self.conn, "claude", model="claude-opus-5-5-high")[0], "antigravity")
        self.assertIsNone(core.fallback_agent(self.conn, "claude", model="gemini-4-neon-high"))
        it = core.item_add(self.conn, "app", "Build feature")
        t = core.launch_target(self.conn, item=it["id"], agent="Antigravity", model="claude-opus-5-5-high")
        self.assertEqual(t["platform"], "antigravity")
        self.assertNotIn("fallback_why", t)
        with self.assertRaises(core.RiverError) as ctx:
            core.launch_target(self.conn, item=it["id"], agent="Antigravity", model="gemini-4-neon-high")
        self.assertIn("Gemini models: 5-hour at limit", str(ctx.exception))


class LimitStopModelGroup(unittest.TestCase):
    """#6382: an agy session that stops at its limit limits only the model group of its own model."""
    STOP = "Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 1h50m."

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _db(self.tmp.name)
        self.t0 = datetime(2026, 11, 20, 17, 52, 0, tzinfo=timezone.utc)
        self.p_now = mock.patch.object(core, "now", side_effect=lambda: self.t0)
        self.p_now.start()
        self.addCleanup(self.p_now.stop)
        p = mock.patch.object(server, "LIMIT_SEEN", {})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)

    def _stop(self, model, footer=None):
        """watch_prompts reads a pane of an agy worker that shows agy's limit-stop message."""
        core.register(self.conn, "agy-w")
        with core.tx(self.conn):
            self.conn.execute("UPDATE agents SET agent_type='antigravity', model=? WHERE name='agy-w'", (model,))
        it = core.item_add(self.conn, "app", "Build feature")
        core.claim(self.conn, it["id"], "agy-w")
        text = self.STOP + "\n? for shortcuts" + (" " * 40 + footer if footer else "") + "\n"
        screen = {"ended": False, "running": "antigravity", "tty": None, "target": "maxpm:agy-w", "text": text}

        def fake_kill(conn, agent, reason, actor=None, by_serve=False):
            return core._killed(conn, agent, reason, actor or "maxpm", pid=None, killed=True)
        with mock.patch.object(server, "agent_terminals", return_value={"agy-w": "%1"}), \
             mock.patch.object(server, "_pane_screen", return_value=screen), \
             mock.patch.object(server, "_pane_session_id", return_value=None), \
             mock.patch.object(server, "_pane_process", return_value=None), \
             mock.patch.object(server, "kill_agent", side_effect=fake_kill), \
             mock.patch.object(server, "_can_open_terminal", return_value=None), \
             mock.patch.object(server, "_open_terminal", return_value={}):
            server.watch_prompts(self.conn)
        return it

    def test_a_gemini_stop_leaves_the_claude_models_free(self):
        self._stop(None, footer="auto · Gemini 4 Neon · high")  # the model from agy's footer
        it = core.item_add(self.conn, "app", "Another feature")
        st = core.agent_limit_status(self.conn, "antigravity")
        self.assertFalse(st["limited"])
        self.assertEqual([w["name"] for w in st["windows"]], ["Gemini models: limit stop"])
        # #6387: the usage panel names the session that stopped, and when.
        self.assertEqual((st["windows"][0]["session"], st["windows"][0]["at"]), ("agy-w", "2026-11-20T17:52:00Z"))
        self.assertIsNone(core.agent_is_limited(self.conn, "antigravity", model="claude-opus-5-5-medium"))
        self.assertIsNone(core.agent_is_limited(self.conn, "antigravity", model="gpt-oss-120b-medium"))
        lim = core.agent_is_limited(self.conn, "antigravity", model="gemini-4-neon-high")
        self.assertEqual(lim["resets_at"], "2026-11-20T19:42:00Z")
        # The case of 2026-11-20: a launch with a Claude model starts; one with a Gemini model is refused.
        core.config_set(self.conn, "launch_agents", "Claude Code=@claude-code; Antigravity CLI=@antigravity")
        t = core.launch_target(self.conn, item=it["id"], agent="Antigravity CLI", model="claude-opus-5-5-medium")
        self.assertEqual(t["platform"], "antigravity")
        with self.assertRaises(core.RiverError) as ctx:
            core.launch_target(self.conn, item=it["id"], agent="Antigravity CLI", model="gemini-4-neon-high")
        self.assertIn("Gemini models: limit stop", str(ctx.exception))
        # The group's limit ends at its reset.
        self.t0 += timedelta(hours=1, minutes=51)
        self.assertIsNone(core.agent_is_limited(self.conn, "antigravity", model="gemini-4-neon-high"))

    def test_the_model_the_session_declared_counts_with_no_footer(self):
        self._stop("claude-opus-5-5-medium")
        self.assertIsNone(core.agent_is_limited(self.conn, "antigravity", model="gemini-4-neon"))
        self.assertIsNotNone(core.agent_is_limited(self.conn, "antigravity", model="gpt-oss-120b-medium"))

    def test_the_whole_agent_when_usage_shows_the_other_group_full(self):
        at = core.iso(self.t0 + timedelta(hours=3))
        core.record_agent_limit(self.conn, "antigravity", windows=[
            {"name": "Gemini models: 5-hour", "used_percent": 40.0, "remaining_percent": 60.0, "resets_at": at},
            {"name": "Claude and GPT models: 5-hour", "used_percent": 100.0, "remaining_percent": 0.0,
             "resets_at": at}])
        self._stop("gemini-4-neon")
        self.assertTrue(core.agent_limit_status(self.conn, "antigravity")["limited"])
        self.assertIsNotNone(core.agent_is_limited(self.conn, "antigravity"))

    def test_no_model_known_limits_the_whole_agent(self):
        self._stop(None)
        self.assertIsNotNone(core.agent_is_limited(self.conn, "antigravity", model="claude-opus-5-5-medium"))

    def test_a_short_gemini_stop_while_claude_is_full_ends_at_its_own_reset(self):
        # #6441, the live row of 2026-11-20: the probe at 19:28:55Z read Gemini 5-hour at 0% used and Claude and GPT
        # 5-hour full until 22:04:55Z; at 19:40:32Z a Gemini session stopped with 'Resets in 26s'. The stop limited all
        # of agy until 22:04:55Z, so no agy session started and the usage list showed Gemini at its limit.
        self.t0 = datetime(2026, 11, 20, 19, 28, 55, tzinfo=timezone.utc)
        core.record_agent_limit(self.conn, "antigravity", limited=True, source="probe", windows=[
            {"name": "Gemini models: weekly", "used_percent": 73.3, "remaining_percent": 26.7,
             "resets_at": "2026-11-27T04:39:55Z"},
            {"name": "Gemini models: 5-hour", "used_percent": 0.0, "remaining_percent": 100.0, "resets_at": None},
            {"name": "Claude and GPT models: weekly", "used_percent": 50.7, "remaining_percent": 49.3,
             "resets_at": "2026-11-27T17:04:55Z"},
            {"name": "Claude and GPT models: 5-hour", "used_percent": 100.0, "remaining_percent": 0.0,
             "resets_at": "2026-11-20T22:04:55Z"}])
        self.t0 = datetime(2026, 11, 20, 19, 40, 32, tzinfo=timezone.utc)
        with mock.patch.object(self, "STOP", "⚠ Individual quota reached. Please upgrade your subscription to "
                                             "increase your limits. Resets in 26s."):
            self._stop("gemini-4-neon")
        row = self.conn.execute("SELECT limited FROM agent_limits WHERE agent_type='antigravity'").fetchone()
        self.assertEqual(row["limited"], 0)  # the group windows hold the limit, each with its own reset
        # While both groups are full, the whole agent waits for the first reset: the Gemini stop's, not Claude's.
        st = core.agent_limit_status(self.conn, "antigravity")
        self.assertTrue(st["limited"])
        self.assertEqual(st["resets_at"], "2026-11-20T19:40:58Z")
        lines = {x["name"]: x for x in core.agent_usage(self.conn, "antigravity")["lines"]}
        self.assertEqual((lines["Gemini models"]["limited"], lines["Gemini models"]["resets_at"]),
                         (True, "2026-11-20T19:40:58Z"))
        self.assertEqual(lines["Claude and GPT models"]["resets_at"], "2026-11-20T22:04:55Z")
        # After the stop's 26 seconds: Gemini runs again, Claude and GPT wait for 22:04:55Z.
        self.t0 = datetime(2026, 11, 20, 19, 41, 30, tzinfo=timezone.utc)
        self.assertFalse(core.agent_limit_status(self.conn, "antigravity")["limited"])
        self.assertIsNone(core.agent_is_limited(self.conn, "antigravity", model="gemini-4-neon"))
        lim = core.agent_is_limited(self.conn, "antigravity", model="claude-opus-5-5-medium")
        self.assertEqual(lim["resets_at"], "2026-11-20T22:04:55Z")
        lines = {x["name"]: x for x in core.agent_usage(self.conn, "antigravity")["lines"]}
        self.assertFalse(lines["Gemini models"]["limited"])
        self.assertTrue(lines["Claude and GPT models"]["limited"])
        # The provider entries of the usage list: the Gemini entry is free, the Claude entry at its limit.
        core.config_set(self.conn, "agent_order", "antigravity model gemini-4-neon max 4, "
                                                  "antigravity model claude-opus-5-5-medium")
        ents = {p["model"]: p for p in core.agent_providers(self.conn)}
        self.assertFalse(ents["gemini-4-neon"]["limited"])
        self.assertTrue(ents["claude-opus-5-5-medium"]["limited"])
        self.assertEqual(ents["claude-opus-5-5-medium"]["resets_at"], "2026-11-20T22:04:55Z")
        it = core.item_add(self.conn, "app", "A Gemini task")
        core.config_set(self.conn, "launch_agents", "Claude Code=@claude-code; Antigravity CLI=@antigravity")
        t = core.launch_target(self.conn, item=it["id"], agent="Antigravity CLI", model="gemini-4-neon")
        self.assertEqual(t["platform"], "antigravity")

    def test_explicit_antigravity_launch_uses_free_model_entry_or_items_own_opus(self):
        # #6547: with agy_model=gemini-4-neon and a Gemini limit stop (weekly, 6 days away), launching with
        # --agent 'Antigravity CLI' checked agy_model (Gemini) and refused even though agent_order had a free
        # Claude Opus entry or the item had its own model opus.
        core.config_set(self.conn, "launch_agents", "Claude Code=@claude-code; Antigravity CLI=@antigravity")
        core.config_set(self.conn, "agy_model", "gemini-4-neon")
        core.config_set(self.conn, "default_model", "opus")
        core.record_agent_limit(self.conn, "antigravity", limited=True,
                                resets_at=core.iso(self.t0 + timedelta(days=6)),
                                reason="Individual quota reached.", source="limit_stop",
                                session="agy-1", model="gemini-4-neon")
        # 1. With agent_order listing Gemini (limited) then Claude Opus (free): --agent 'Antigravity CLI' picks
        # the free Claude Opus entry instead of refusing on agy_model (Gemini).
        core.config_set(self.conn, "agent_order",
                        "antigravity model gemini-4-neon effort medium max 4, "
                        "antigravity model claude-opus-5-5-medium max 2, claude-code")
        plain = core.item_add(self.conn, "app", "Plain task")
        t = core.launch_target(self.conn, item=plain["id"], agent="Antigravity CLI")
        self.assertEqual((t["platform"], t["model"]), ("antigravity", "claude-opus-5-5-medium"))
        self.assertIn("Gemini models: limit stop", t["fallback_why"])
        # Explicit --model gemini-4-neon is still refused while Gemini is at its limit.
        with self.assertRaises(core.RiverError) as ctx:
            core.launch_target(self.conn, item=plain["id"], agent="Antigravity CLI", model="gemini-4-neon")
        self.assertIn("Gemini models: limit stop", str(ctx.exception))
        # 2. Item with its own model opus: matches the Claude entry even when Gemini is first in agent_order.
        own_opus = core.item_add(self.conn, "app", "Opus task", models={"model": "opus"})
        t2 = core.launch_target(self.conn, item=own_opus["id"], agent="Antigravity CLI")
        self.assertEqual((t2["platform"], t2["model"]), ("antigravity", "claude-opus-5-5-medium"))
        # 3. Without agent_order: an item's own model opus (or claude-opus-5-5-medium) resolves on Antigravity
        # instead of falling back to agy_model (Gemini) and getting refused.
        core.config_unset(self.conn, "agent_order")
        t3 = core.launch_target(self.conn, item=own_opus["id"], agent="Antigravity CLI")
        self.assertEqual(t3["platform"], "antigravity")
        self.assertIn("claude-opus", t3["model"])
        own_id = core.item_add(self.conn, "app", "Direct model task", models={"model": "claude-opus-5-5-medium"})
        t4 = core.launch_target(self.conn, item=own_id["id"], agent="Antigravity CLI")
        self.assertEqual((t4["platform"], t4["model"]), ("antigravity", "claude-opus-5-5-medium"))
        # Without agent_order and with no own model on the item, agy would run agy_model (Gemini) -> refused.
        with self.assertRaises(core.RiverError) as ctx2:
            core.launch_target(self.conn, item=plain["id"], agent="Antigravity CLI")
        self.assertIn("Gemini models: limit stop", str(ctx2.exception))


class LimitStopHandbackAndRestart(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _db(self.tmp.name)
        self.t0 = datetime(2026, 11, 20, 16, 0, 0, tzinfo=timezone.utc)
        self.p_now = mock.patch.object(core, "now", side_effect=lambda: self.t0)
        self.p_now.start()

    def tearDown(self):
        self.p_now.stop()
        self.conn.close()
        self.tmp.cleanup()

    def test_watch_prompts_stops_limited_worker_and_restarts_on_fallback_in_same_worktree(self):
        core.register(self.conn, "app-old1")
        with core.tx(self.conn):
            self.conn.execute("UPDATE agents SET agent_type='antigravity' WHERE name='app-old1'")
        it = core.item_add(self.conn, "app", "Implement feature X")
        core.claim(self.conn, it["id"], "app-old1")
        # Give app-old1 an active worktree
        wt_dir = Path(self.tmp.name) / "wt-old1"
        wt_dir.mkdir()
        core.workspace_add(self.conn, "app-old1", core._project(self.conn, "app")["id"],
                           self.tmp.name, str(wt_dir), "maxpm/app-old1", "abc123")

        launched = []

        def fake_kill(conn, agent, reason, actor=None, by_serve=False):
            return core._killed(conn, agent, reason, actor or "maxpm", pid=None, killed=True)

        def fake_open(t, env, runner, conn):
            launched.append((t, env))
            return {}

        screen = {"ended": False, "running": "antigravity", "tty": None, "target": "maxpm:app-old1",
                  "text": "Individual quota reached for Gemini 4 Neon. Resets in ~30m\n? for shortcuts\n"}
        with mock.patch.object(server, "agent_terminals", return_value={"app-old1": "%1"}), \
             mock.patch.object(server, "_pane_screen", return_value=screen), \
             mock.patch.object(server, "_pane_session_id", return_value=None), \
             mock.patch.object(server, "_pane_process", return_value=None), \
             mock.patch.object(server, "kill_agent", side_effect=fake_kill), \
             mock.patch.object(server, "_can_open_terminal", return_value=None), \
             mock.patch.object(server, "_open_terminal", side_effect=fake_open):
            res = server.watch_prompts(self.conn)

        self.assertIn("limit_restarts", res)
        self.assertEqual(len(res["limit_restarts"]), 1)
        rr = res["limit_restarts"][0]
        self.assertEqual(rr["agent"], "app-old1")
        self.assertEqual(rr["item_id"], it["id"])
        self.assertEqual(rr["fallback"], "Claude Code")
        new_name = rr["restarted"]
        self.assertIsNotNone(new_name)

        # Antigravity is recorded as limited
        lim = core.agent_is_limited(self.conn, "antigravity")
        self.assertIsNotNone(lim)
        self.assertEqual(lim["resets_at"], "2026-11-20T16:30:00Z")

        # Old session stopped and item has handback note + fallback start event
        events = [r["change"] for r in self.conn.execute("SELECT change FROM events WHERE item_id=?", (it["id"],))]
        self.assertTrue(any("usage limit" in e for e in events))
        self.assertTrue(any(f"started {new_name}" in e for e in events))

        # Worktree was reassigned to the new session so it continues the uncommitted work in the same folder
        self.assertIsNone(core.workspace_of(self.conn, "app-old1"))
        new_w = core.workspace_of(self.conn, new_name)
        self.assertIsNotNone(new_w)
        self.assertEqual(new_w["path"], str(wt_dir))
        self.assertEqual(len(launched), 1)
        self.assertIn("uncommitted work", launched[0][0]["custom_prompt"])

    def test_a_stop_before_the_claim_starts_the_pushed_item_on_the_fallback(self):
        # #6418: agy answered its first prompt with its limit, so the session never claimed the item serve pushed to
        # it. Before, serve only sent an alert and the item lay open after connect_within.
        core.register(self.conn, "app-new1")
        with core.tx(self.conn):
            self.conn.execute("UPDATE agents SET agent_type='antigravity' WHERE name='app-new1'")
        it = core.item_add(self.conn, "app", "Implement feature Y")
        other = core.item_add(self.conn, "app", "Implement feature Z")
        core.scope_start(self.conn, "app-new1", [f"tasks:{it['id']}"], "Dispatch started this session for it")
        core.push(self.conn, it["id"], "app-new1", "Dispatch", "mark")
        launched = []

        def fake_kill(conn, agent, reason, actor=None, by_serve=False):
            return core._killed(conn, agent, reason, actor or "maxpm", pid=None, killed=True)

        screen = {"ended": False, "running": "antigravity", "tty": None, "target": "maxpm:app-new1",
                  "text": "Individual quota reached. Resets in 3h15m2s\n? for shortcuts\n"}
        with mock.patch.object(server, "agent_terminals", return_value={"app-new1": "%1"}), \
             mock.patch.object(server, "_pane_screen", return_value=screen), \
             mock.patch.object(server, "_pane_session_id", return_value=None), \
             mock.patch.object(server, "_pane_process", return_value=None), \
             mock.patch.object(server, "kill_agent", side_effect=fake_kill), \
             mock.patch.object(server, "_can_open_terminal", return_value=None), \
             mock.patch.object(server, "_open_terminal", side_effect=lambda t, env, r, c: launched.append((t, env))):
            res = server.watch_prompts(self.conn)

        rr = res["limit_restarts"][0]
        self.assertEqual((rr["agent"], rr["item_id"], rr["fallback"]), ("app-new1", it["id"], "Claude Code"))
        new = rr["restarted"]
        self.assertEqual(core.item_show(self.conn, it["id"])["reserved_for"], new)
        self.assertIsNone(core.item_show(self.conn, other["id"])["reserved_for"])
        self.assertIsNotNone(self.conn.execute("SELECT stop_at FROM agents WHERE name='app-new1'").fetchone()[0])
        # It did no work, so the new session starts the item as a Dispatch does: no prompt to continue a diff.
        self.assertEqual(len(launched), 1)
        self.assertFalse(launched[0][0].get("custom_prompt"))
        self.assertEqual(launched[0][1]["MAXPM_FOCUS"], f"item:{it['id']}")
        events = [r["change"] for r in self.conn.execute("SELECT change FROM events WHERE item_id=?", (it["id"],))]
        self.assertTrue(any(f"started {new}" in e and "take it" in e for e in events))


class UsageProbe(unittest.TestCase):
    """#6364: with no session of Claude Code or agy, maxpm serve opens the agent in a hidden pane, types /usage, reads
    the limits, closes the screen with Esc and ends the pane."""
    CLAUDE_IDLE = ["╭" + "─" * 40, "│ ❯                    │", "❯ ", "  ? for shortcuts"]
    CLAUDE_USAGE = ["  Settings  Status   Config   Usage   Stats", "  Current week (all models)",
                    "  ███████████                                        22% used",
                    "  Resets Nov 25 at 11:59pm (America/Los_Angeles)"]
    AGY_IDLE = ["? for shortcuts"]
    AGY_USAGE = ["└ Models & Quota", "GEMINI MODELS", "  Five Hour Limit Remaining",
                 "    [░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░] 0.00%", "    Refreshes in 2h 31m"]

    def setUp(self):
        import json
        import os
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.conn = _db(self.tmp.name)
        claude_json = Path(self.tmp.name) / "claude.json"
        folder = self.conn.execute("SELECT path FROM projects WHERE name='app'").fetchone()["path"]
        claude_json.write_text(json.dumps({"projects": {folder: {"hasTrustDialogAccepted": True}}}))
        self.calls, self.screens = [], {"claude": (self.CLAUDE_IDLE, self.CLAUDE_USAGE),
                                        "antigravity": (self.AGY_IDLE, self.AGY_USAGE)}
        self.typed = {}  # pane -> what was typed
        for k, v in {"TMUX_RUNNER": self._tmux, "PROBE_POLL": 0, "PROBE_SYNC": True, "PROBE_EXE": lambda e: True,
                     "PROBE": {"running": False, "at": {}, "now": None, "error": {}}, "CLAUDE_JSON": str(claude_json)}.items():
            p = mock.patch.object(server, k, v)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ, {"MAXPM_DB": str(self.db)})
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)

    def _tmux(self, args):
        self.calls.append(args)
        if args[0] == "has-session":
            return None
        if args[0] in ("new-session", "new-window"):
            name = args[args.index("-n") + 1]
            pane = f"%{name}"
            self.typed[pane] = ""
            return pane
        pane = args[args.index("-t") + 1] if "-t" in args else None
        if args[0] == "send-keys" and "-l" in args:
            self.typed[pane] += args[-1] + "\n"
            return ""
        if args[0] == "capture-pane":
            aid = pane.split("-", 1)[1]
            idle, usage = self.screens[aid]
            return "\n".join(usage if "/usage" in self.typed[pane] else idle)
        return ""

    def test_reads_both_agents_and_closes_their_panes(self):
        got = server.probe_usage(self.conn)
        self.assertEqual(got, ["claude", "antigravity"])
        rows = {r["agent_type"]: r for r in core.agent_limits_list(self.conn)}
        self.assertEqual((rows["claude"]["remaining_pct"], rows["claude"]["source"]), (78.0, "probe"))
        # The screen shows only the Gemini group, at 0% left: it limits the Gemini models, not all of agy (#6441).
        self.assertFalse(rows["antigravity"]["limited"])
        self.assertTrue(rows["antigravity"]["groups"][0]["full"])
        self.assertIsNotNone(core.agent_is_limited(self.conn, "antigravity", model="gemini-4-neon"))
        self.assertFalse(rows["codex"]["known"])  # Codex: its log, not a probe
        for pane in ("%usage-claude", "%usage-antigravity"):
            line = self.typed[pane].splitlines()[0]
            self.assertIn("env -u MAXPM_AGENT -u MAXPM_FOCUS -u MAXPM_TOKEN", line)
            self.assertIn(f"MAXPM_DB={self.db}", line)
            keys = [c for c in self.calls if pane in c]
            self.assertEqual(keys[-2], ["send-keys", "-t", pane, "Escape"])  # Esc closes /usage (p137)
            self.assertEqual(keys[-1][:3], ["kill-pane", "-t", pane])
        self.assertIn("claude --setting-sources user --strict-mcp-config", self.typed["%usage-claude"])
        self.assertIn("trustedWorkspaces", self.typed["%usage-antigravity"])
        self.assertTrue(all(c[c.index("-s") + 1] == server.PROBE_SESSION for c in self.calls if c[0] == "new-session"))
        # Within usage_probe_every: no probe again.
        self.assertEqual(server.probe_usage(self.conn), [])

    def test_probe_also_while_a_session_runs_but_not_when_off_or_without_a_trusted_folder(self):
        # #6387: ten Claude Code sessions ran and the page said 'not known': they record no reading by themselves.
        core.register(self.conn, "cc1")
        self.conn.execute("UPDATE agents SET agent_type='claude-code' WHERE name='cc1'")
        self.conn.commit()
        self.assertEqual([a for a, _ in server.usage_probes_due(self.conn)], ["claude", "antigravity"])
        core.unregister(self.conn, "cc1")
        server.CLAUDE_JSON = str(Path(self.tmp.name) / "none.json")
        self.assertEqual([a for a, _ in server.usage_probes_due(self.conn)], ["antigravity"])
        core.config_set(self.conn, "usage_probe_every", "0s")
        self.assertEqual(server.probe_usage(self.conn), [])
        self.assertEqual(self.calls, [])

    def test_a_question_at_the_start_gets_no_answer(self):
        self.screens["claude"] = (["Do you trust the files in this folder?", "❯ 1. Yes, I trust this folder",
                                   "  2. No, exit"], self.CLAUDE_USAGE)
        got = server.probe_one(self.conn, "claude", Path(self.tmp.name))
        self.assertIn("asks first", got["error"])
        self.assertNotIn("/usage", self.typed["%usage-claude"])
        self.assertEqual(self.calls[-1][:3], ["kill-pane", "-t", "%usage-claude"])
        self.assertFalse(core.agent_limit_status(self.conn, "claude"))

    def test_read_now_reads_within_the_probe_interval_and_the_panel_shows_the_error(self):
        server.probe_usage(self.conn)
        self.assertEqual(server.probe_usage(self.conn), [])  # within usage_probe_every
        n = len([c for c in self.calls if c[0] in ("new-session", "new-window")])
        got = server.OPS["usage_read"](self.conn, {"agent": "claude-code"}, "mark")
        self.assertEqual(got, {"agent_type": "claude", "reading": False})  # PROBE_SYNC: it ended already
        self.assertEqual(len([c for c in self.calls if c[0] in ("new-session", "new-window")]), n + 1)
        # A failed reading: the panel says why; the next good one takes the line away.
        self.screens["antigravity"] = (["Sign in with Google to continue"], self.AGY_USAGE)
        with mock.patch.object(core._adapters().find("antigravity"), "detect",
                               {"idle": r"\? for shortcuts", "blocked": [r"Sign in"]}):
            server.usage_read_now(self.conn, "antigravity")
        u = server.agent_usage(self.conn, "antigravity")
        self.assertIn("asks first", u["probe_error"])
        self.assertTrue(u["can_read"])
        self.assertFalse(u["reading"])
        self.screens["antigravity"] = (self.AGY_IDLE, self.AGY_USAGE)
        server.usage_read_now(self.conn, "antigravity")
        self.assertIsNone(server.agent_usage(self.conn, "antigravity")["probe_error"])
        # An agent the probe cannot read: the refusal says what to do.
        with self.assertRaises(core.RiverError):
            server.usage_read_now(self.conn, "goose")

    def test_limit_stop_does_not_postpone_probe_and_probe_replaces_stale_stop(self):
        # #6547: a session's limit_stop updates agent_limits.updated_at without reading /usage; usage_probes_due
        # must check the last real reading's time so the scheduled probe still runs and replaces a stale stop
        # (e.g. after a plan upgrade).
        t0 = datetime(2026, 11, 21, 3, 0, 0, tzinfo=timezone.utc)
        cur = [t0]
        with mock.patch.object(core, "now", side_effect=lambda: cur[0]):
            core.record_agent_limit(self.conn, "antigravity", limited=False, source="probe", windows=[
                {"name": "Gemini models: weekly", "used_percent": 95.0, "remaining_percent": 5.0,
                 "resets_at": core.iso(t0 + timedelta(days=6))}])
            # 20m later: a session hits a weekly limit stop; updated_at moves to t0+20m.
            cur[0] = t0 + timedelta(minutes=20)
            core.record_agent_limit(self.conn, "antigravity", limited=True,
                                    resets_at=core.iso(t0 + timedelta(days=6)),
                                    reason="Individual quota reached.", source="limit_stop",
                                    session="agy-1", model="gemini-4-neon")
            # At t0+25m (5m after stop, 25m after probe): not due yet.
            cur[0] = t0 + timedelta(minutes=25)
            self.assertNotIn("antigravity", [a for a, _ in server.usage_probes_due(self.conn)])
            # At t0+31m (11m after stop, 31m after last real probe): due! The limit_stop must not delay it.
            cur[0] = t0 + timedelta(minutes=31)
            self.assertIn("antigravity", [a for a, _ in server.usage_probes_due(self.conn)])
            # Probe reads the upgraded plan's /usage and replaces the stale limit_stop row.
            self.screens["antigravity"] = (self.AGY_IDLE, [
                "└ Models & Quota", "GEMINI MODELS", "  Weekly Limit Remaining",
                "    [██████████████████████████████████████████████████] 99.80%",
                "    Refreshes in 167h 53m"])
            got = server.probe_usage(self.conn)
            self.assertIn("antigravity", got)
            st = core.agent_limit_status(self.conn, "antigravity")
            self.assertEqual(st["source"], "probe")
            self.assertIsNone(core.agent_is_limited(self.conn, "antigravity", model="gemini-4-neon"))

    def test_restart_waits_for_running_probe_and_orphaned_probe_panes_are_cleaned(self):
        # #6547: restart_blocked waits while a probe runs, and orphaned panes in maxpm-usage (dead owner PID,
        # stale untagged pane from a prior serve) are killed without touching another queue's or a live probe's pane.
        import os
        server.PROBE["running"], server.PROBE["now"] = True, "antigravity"
        self.assertIn("antigravity", server.restart_blocked(self.conn))
        server.PROBE["running"], server.PROBE["now"] = False, None
        self.assertIsNone(server.restart_blocked(self.conn))

        now_ts = int(core.now().timestamp())
        db = server._queue_db()
        panes = "\n".join([
            f"%dead|999999|{db}|{now_ts - 10}|{now_ts - 10}",
            f"%stale||{db}||{now_ts - server.PROBE_STALE_SEC - 5}",
            f"%mine|{os.getpid()}|{db}|{now_ts - 5}|{now_ts - 5}",
            f"%other|999999|/tmp/other-queue.db|{now_ts - 500}|{now_ts - 500}",
            f"%live|424242|{db}|{now_ts - 5}|{now_ts - 5}",
        ])
        killed = []

        def fake_tmux(args):
            if args[0] == "has-session":
                return ""
            if args[0] == "list-panes":
                return panes
            if args[0] == "kill-pane":
                killed.append(args[args.index("-t") + 1])
                return ""
            return ""

        with mock.patch.object(server, "TMUX_RUNNER", fake_tmux), \
             mock.patch.object(core, "pid_alive", side_effect=lambda p: p in (os.getpid(), 424242)):
            closed = server._clean_probe_panes()
        self.assertEqual(closed, ["%dead", "%stale", "%mine"])
        self.assertEqual(killed, ["%dead", "%stale", "%mine"])

    def test_start_probes_resets_running_if_connect_fails(self):
        # #6547: an exception in core.connect() inside _start_probes must not leave PROBE['running'] stuck True.
        with mock.patch.object(core, "connect", side_effect=RuntimeError("db locked")):
            server._start_probes([("antigravity", Path(self.tmp.name))])
        self.assertFalse(server.PROBE["running"])
        self.assertIsNone(server.PROBE["now"])
        self.assertIn("db locked", server.PROBE["error"]["antigravity"])


class AgentUsagePanel(unittest.TestCase):
    """#6387: a click on an agent in the usage list opens its usage: windows, model groups, why a limit, where and
    when it was read, and the sessions of the agent now."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _db(self.tmp.name)
        self.t0 = datetime(2026, 11, 20, 18, 10, 0, tzinfo=timezone.utc)
        p = mock.patch.object(core, "now", side_effect=lambda: self.t0)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)

    def _usage(self, gemini=0.0, claude=99.0):
        at = core.iso(self.t0 + timedelta(hours=1, minutes=30))
        return [{"name": n, "remaining_percent": r, "used_percent": round(100 - r, 1), "resets_at": at,
                 "resets_text": "in 1h 30m"}
                for n, r in (("Gemini models: 5-hour", gemini), ("Claude and GPT models: 5-hour", claude))]

    def test_a_usage_reading_clears_a_whole_agent_limit_from_before_and_names_the_group(self):
        # The row of 2026-11-20 17:52: a whole-agy limit stop recorded before 9d7c7b5a, no windows.
        core.record_agent_limit(self.conn, "antigravity", limited=True, reason="Individual quota reached.",
                                resets_at=core.iso(self.t0 + timedelta(hours=1, minutes=32)), source="limit_stop")
        self.assertTrue(core.agent_usage(self.conn, "antigravity")["limited"])
        core.record_agent_limit(self.conn, "antigravity", limited=True, windows=self._usage(), source="probe")
        u = core.agent_usage(self.conn, "antigravity")
        self.assertFalse(u["limited"])
        self.assertIsNone(u["reason"])
        self.assertEqual([(g["name"], g["full"], g["remaining_pct"]) for g in u["groups"]],
                         [("Gemini models", True, 0.0), ("Claude and GPT models", False, 99.0)])
        self.assertEqual(u["groups"][0]["resets_at"], "2026-11-20T19:40:00Z")
        self.assertEqual(u["source_text"], "/usage, read by maxpm serve in a hidden window")
        row = next(r for r in core.agent_limits_list(self.conn) if r["agent_type"] == "antigravity")
        self.assertEqual(row["groups"], u["groups"])  # the list row names the group too
        # An agent whose windows count every model has no groups.
        core.record_agent_limit(self.conn, "codex", windows=[{"name": "5-hour window", "used_percent": 1.0}],
                                source="log")
        self.assertEqual(core.agent_usage(self.conn, "codex")["groups"], [])

    def test_one_status_per_group_and_a_stop_is_no_window(self):
        # #6414, the agy panel of 2026-11-20: /usage at 10:22 read the Claude group at 2%, a session stopped at 10:46
        # with 'Individual quota reached', and the panel showed both as windows, the 2% as the truth.
        read_at = self.t0 + timedelta(minutes=12)
        self.t0 = read_at
        core.record_agent_limit(self.conn, "antigravity", limited=True, windows=self._usage(gemini=0.0, claude=97.9),
                                source="probe")
        self.t0 = read_at + timedelta(minutes=24)
        stop_reset = core.iso(self.t0 + timedelta(hours=3, minutes=19))
        core.record_agent_limit(self.conn, "antigravity", limited=True, resets_at=stop_reset,
                                reason="Individual quota reached. Resets in 3h19m.", source="limit_stop",
                                session="agy-7", model="claude-opus-5-5-high")
        u = core.agent_usage(self.conn, "antigravity")
        gem, cl = u["lines"]
        self.assertEqual((gem["name"], gem["limited"], gem["resets_at"], gem["stop"]),
                         ("Gemini models", True, core.iso(read_at + timedelta(hours=1, minutes=30)), None))
        self.assertEqual((cl["name"], cl["limited"], cl["resets_at"]), ("Claude and GPT models", True, stop_reset))
        self.assertEqual((cl["stop"]["session"], cl["stop"]["at"], cl["stop"]["message"]),
                         ("agy-7", core.iso(self.t0), "Individual quota reached. Resets in 3h19m."))
        # The windows are the reading's, with its time; the Claude window is older than the stop; no stop window.
        self.assertEqual([(w["name"], w["older"], w["read_at"]) for w in u["usage_windows"]],
                         [("Gemini models: 5-hour", False, core.iso(read_at)),
                          ("Claude and GPT models: 5-hour", True, core.iso(read_at))])
        self.assertEqual(u["read"]["at"], core.iso(read_at))
        self.assertEqual(u["read"]["source"], "probe")
        # A new reading takes the stop's place: its windows say what holds.
        self.t0 += timedelta(minutes=12)
        core.record_agent_limit(self.conn, "antigravity", limited=True, windows=self._usage(gemini=0.0, claude=0.0),
                                source="probe")
        u = core.agent_usage(self.conn, "antigravity")
        self.assertEqual([(l["limited"], l["stop"]) for l in u["lines"]], [(True, None), (True, None)])
        self.assertFalse(any(w["older"] for w in u["usage_windows"]))

    def test_a_stop_of_an_agent_without_groups_dims_the_windows_read_before_it(self):
        core.record_agent_limit(self.conn, "claude", windows=[{"name": "Current week (all models)", "used_percent": 29.0,
                                "remaining_percent": 71.0}], source="probe")
        read_at = core.iso(self.t0)
        self.t0 += timedelta(minutes=30)
        core.record_agent_limit(self.conn, "claude", limited=True, reason="You've hit your limit",
                                resets_at=core.iso(self.t0 + timedelta(hours=2)), source="limit_stop", session="cc-3")
        u = core.agent_usage(self.conn, "claude")
        (line,) = u["lines"]
        self.assertEqual((line["name"], line["limited"], line["stop"]["session"], line["stop"]["message"]),
                         ("Claude Code", True, "cc-3", "You've hit your limit"))
        self.assertEqual([(w["older"], w["read_at"]) for w in u["usage_windows"]], [(True, read_at)])
        # No limit: one line that can launch, and no stop.
        core.record_agent_limit(self.conn, "codex", windows=[{"name": "5-hour window", "used_percent": 4.0}],
                                source="log")
        (line,) = core.agent_usage(self.conn, "codex")["lines"]
        self.assertEqual((line["limited"], line["remaining_pct"], line["stop"]), (False, 96.0, None))

    def test_the_sessions_of_the_agent_now_with_their_models_and_items(self):
        for name, at, model in (("agy-1", "antigravity", "claude-opus-5-5-high"), ("agy-2", "antigravity", None),
                                ("cc-1", "claude-code", "opus")):
            core.register(self.conn, name)
            with core.tx(self.conn):
                self.conn.execute("UPDATE agents SET agent_type=?, model=? WHERE name=?", (at, model, name))
        it = core.item_add(self.conn, "app", "Build feature")
        core.claim(self.conn, it["id"], "agy-1")
        core.unregister(self.conn, "agy-2")  # an ended session is not in the list
        u = core.agent_usage(self.conn, "agy")
        self.assertFalse(u["known"])
        self.assertEqual([(s["name"], s["model"], [i["id"] for i in s["items"]]) for s in u["sessions"]],
                         [("agy-1", "claude-opus-5-5-high", [it["id"]])])
        self.assertEqual([s["name"] for s in core.agent_usage(self.conn, "claude")["sessions"]], ["cc-1"])
        with self.assertRaises(core.RiverError):
            core.agent_usage(self.conn, "no-such-agent")

    def test_the_route_answers(self):
        got = {}
        h = server.Handler.__new__(server.Handler)
        h.path = "/api/agent-usage?agent=codex"
        h._send = lambda code, body, *a, **k: got.update(code=code, body=body)
        from river.routes import sessions as rs
        with mock.patch.dict("os.environ", {"MAXPM_DB": str(Path(self.tmp.name) / "test.db")}):
            rs.agent_usage(h, "/api/agent-usage")
        self.assertEqual(got["code"], 200)
        self.assertEqual(got["body"]["agent_type"], "codex")
        self.assertIn("can_read", got["body"])


class ProviderSlots(unittest.TestCase):
    """#6361: agent providers in order, each with a limit of active sessions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _db(self.tmp.name)
        self.t0 = datetime(2026, 11, 20, 16, 0, 0, tzinfo=timezone.utc)
        p_now = mock.patch.object(core, "now", side_effect=lambda: self.t0)
        p_now.start()
        self.addCleanup(p_now.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)
        core.config_set(self.conn, "launch_agents", "Claude Code=@claude-code; Antigravity=@antigravity; Codex=@codex")
        core.config_set(self.conn, "agent_order", "antigravity max 2, claude-code")

    def _dispatch(self, item_id, agent=None):
        """The page's Dispatch of one item, with the terminal faked: the started session's name and agent type."""
        with mock.patch.object(server, "_can_open_terminal", return_value=None), \
             mock.patch.object(server, "_open_terminal", return_value={}):
            r = server.dispatch_item(self.conn, item_id, agent=agent, actor="mark")
        name = r["session_name"]
        return name, self.conn.execute("SELECT agent_type FROM agents WHERE name=?", (name,)).fetchone()[0]

    def test_parse_providers(self):
        e = lambda agent, model=None, effort=None, mx=None: {"agent": agent, "model": model, "effort": effort, "max": mx}
        self.assertEqual(core.parse_providers("antigravity max 2, claude-code"), [e("antigravity", mx=2), e("claude")])
        self.assertEqual(core.parse_providers("codex max=1 -> Claude Code max 3"), [e("codex", mx=1), e("claude", mx=3)])
        # One agent, an entry for each model group (mark 2026-11-20: agy's Claude models have their own 5-hour window).
        text = "antigravity model gemini-4-neon effort medium max 2, antigravity model claude-opus-5-5, claude-code model opus"
        got = core.parse_providers(text)
        self.assertEqual(got, [e("antigravity", "gemini-4-neon", "medium", 2), e("antigravity", "claude-opus-5-5"),
                               e("claude", "opus")])
        self.assertEqual(core.providers_text(got), text)
        self.assertEqual(core.parse_agent_order("antigravity max 0, codex"), ["antigravity", "codex"])
        for bad in ("antigravity max two", "antigravity max", "nonexistent max 2", "codex, codex max 1",
                    "antigravity model gemini-4-neon, antigravity model gemini-4-neon", "codex speed 2",
                    "codex effort Very-High"):
            with self.assertRaises(core.RiverError, msg=bad):
                core.config_set(self.conn, "agent_order", bad)

    def test_one_agent_with_an_entry_for_each_model_group(self):
        core.config_set(self.conn, "agent_order", "antigravity model gemini-4-neon effort medium max 1, "
                                                  "antigravity model claude-opus-5-5 effort high max 1, claude-code model opus")
        ids = [core.item_add(self.conn, "app", f"Build part {n}")["id"] for n in (1, 2, 3)]
        got = [self._dispatch(i) for i in ids]
        self.assertEqual([t for _, t in got], ["antigravity", "antigravity", "claude-code"])
        models = [self.conn.execute("SELECT model FROM agents WHERE name=?", (n,)).fetchone()[0] for n, _ in got]
        self.assertEqual(models, ["gemini-4-neon", "claude-opus-5-5", "opus"])
        provs = core.agent_providers(self.conn)
        self.assertEqual([(p["name"], p["used"], p["full"]) for p in provs],
                         [("Antigravity CLI (gemini-4-neon)", 1, True), ("Antigravity CLI (claude-opus-5-5)", 1, True),
                          ("Claude Code (opus)", 1, False)])
        self.assertEqual(provs[1]["run_effort"], "high")
        # A page state build counts the sessions from its agent statuses, with the same result (#6394).
        self.conn.execute("UPDATE agents SET stop_at=? WHERE name=?", (core.iso(self.t0), got[2][0]))  # it stops
        outside = core.provider_sessions(self.conn)
        self.assertEqual(sorted(outside), [("antigravity", "claude-opus-5-5"), ("antigravity", "gemini-4-neon")])
        tok = core.BUILD_MEMO.set({})
        try:
            self.assertEqual(sorted(core.provider_sessions(self.conn)), sorted(outside))
        finally:
            core.BUILD_MEMO.reset(tok)

    def test_a_model_group_out_of_usage_skips_only_its_entry(self):
        core.config_set(self.conn, "agent_order", "antigravity model gemini-4-neon, antigravity model claude-opus-5-5, "
                                                  "claude-code")
        core.record_agent_limit(self.conn, "antigravity", limited=True, source="screen", windows=[
            {"name": "Gemini models: 5-hour", "used_percent": 100.0, "remaining_percent": 0.0,
             "resets_at": core.iso(self.t0 + timedelta(hours=2))},
            {"name": "Claude and GPT models: 5-hour", "used_percent": 1.0, "remaining_percent": 99.0}])
        provs = core.agent_providers(self.conn)
        self.assertEqual([(p["limited"], p["remaining_pct"]) for p in provs[:2]], [(True, 0.0), (False, 99.0)])
        it = core.item_add(self.conn, "app", "Build feature")["id"]
        t = core.launch_target(self.conn, item=it)
        self.assertEqual((t["platform"], t["model"]), ("antigravity", "claude-opus-5-5"))
        self.assertIn("Gemini models: 5-hour at limit", t["fallback_why"])
        # The quota restart of #6348 takes the same list: the next entry after the one that stopped.
        self.assertEqual(core.fallback_agent(self.conn, "antigravity", model="gemini-4-neon")[2]["model"],
                         "claude-opus-5-5")

    def test_three_ready_items_two_on_antigravity_and_the_third_on_claude_code(self):
        ids = [core.item_add(self.conn, "app", f"Build part {n}")["id"] for n in (1, 2, 3)]
        got = [self._dispatch(i) for i in ids]
        self.assertEqual([t for _, t in got], ["antigravity", "antigravity", "claude-code"])
        provs = {p["agent"]: p for p in core.agent_providers(self.conn)}
        self.assertEqual((provs["antigravity"]["used"], provs["antigravity"]["full"]), (2, True))
        self.assertEqual((provs["claude"]["used"], provs["claude"]["max"]), (1, None))
        # A slot frees (a session ends): the next ready item takes it; the running Claude Code session stays.
        core.unregister(self.conn, got[0][0])
        nxt = core.item_add(self.conn, "app", "Build part 4")["id"]
        self.assertEqual(self._dispatch(nxt)[1], "antigravity")
        self.assertEqual(self.conn.execute("SELECT agent_type FROM agents WHERE name=?", (got[2][0],)).fetchone()[0],
                         "claude-code")

    def test_a_provider_out_of_usage_is_skipped(self):
        core.record_agent_limit(self.conn, "antigravity", limited=True, resets_at=core.iso(self.t0 + timedelta(hours=1)),
                                reason="Individual quota reached", source="limit_stop")
        it = core.item_add(self.conn, "app", "Build feature")["id"]
        t = core.launch_target(self.conn, item=it)
        self.assertEqual(t["platform"], "claude-code")
        self.assertIn("usage limit", t["fallback_why"])

    def test_explicit_agent_wins_and_all_full_refuses(self):
        core.config_set(self.conn, "agent_order", "antigravity max 1, claude-code max 1")
        a, b, c, d = (core.item_add(self.conn, "app", f"Build part {n}")["id"] for n in (1, 2, 3, 4))
        self.assertEqual(self._dispatch(a)[1], "antigravity")
        self.assertEqual(self._dispatch(b)[1], "claude-code")
        with self.assertRaises(core.RiverError) as cm:
            core.launch_target(self.conn, item=c)
        self.assertIn("1 of 1 session", str(cm.exception))
        self.assertIn("no fallback agent is available", str(cm.exception))
        # A person's --agent still wins over the limit.
        self.assertEqual(core.launch_target(self.conn, item=c, agent="Antigravity")["platform"], "antigravity")
        # serve's start of an item (start_fresh) waits too: the item stays in the queue.
        with mock.patch.object(server, "_can_open_terminal", return_value=None), \
             mock.patch.object(server, "_open_terminal", return_value={}):
            with self.assertRaises(core.RiverError):
                server.start_fresh(self.conn, d, "test")

    def test_a_sessions_explicit_agent_counts_against_its_max(self):
        """#6585: the manager followed the watch's hint '--agent "Antigravity CLI"' and ran 5 agy sessions where
        agent_order allowed 2. A session's --agent now waits at the limit; a person's still starts."""
        core.config_set(self.conn, "agent_order", "antigravity max 1, claude-code")
        core.register(self.conn, "manager-1")
        core.register(self.conn, "mark", human=True)
        a, b = (core.item_add(self.conn, "app", f"Build part {n}")["id"] for n in (1, 2))
        self.assertEqual(self._dispatch(a)[1], "antigravity")
        with self.assertRaises(core.RiverError) as cm:
            core.launch_target(self.conn, item=b, agent="Antigravity", actor="manager-1")
        self.assertIn("1 of 1 session", str(cm.exception))
        self.assertIn("Leave out --agent", str(cm.exception))
        # Without --agent the list picks the next provider with a free slot.
        self.assertEqual(core.launch_target(self.conn, item=b, actor="manager-1")["platform"], "claude-code")
        # A person (by name, or with no name) still starts the agent past its max.
        self.assertEqual(core.launch_target(self.conn, item=b, agent="Antigravity", actor="mark")["platform"],
                         "antigravity")
        self.assertEqual(core.launch_target(self.conn, item=b, agent="Antigravity")["platform"], "antigravity")

    def test_global_default_agent_gives_way_to_the_list_and_an_items_own_agent_does_not(self):
        core.config_set(self.conn, "default_agent", "codex")
        it = core.item_add(self.conn, "app", "Build feature")["id"]
        self.assertEqual(core.launch_target(self.conn, item=it)["platform"], "antigravity")
        own = core.item_add(self.conn, "app", "Draw the logo", models={"agent": "codex"})["id"]
        self.assertEqual(core.launch_target(self.conn, item=own)["platform"], "codex")
        # An item's own agent at its usage limit goes to the list.
        core.record_agent_limit(self.conn, "codex", limited=True, source="limit_stop")
        self.assertEqual(core.launch_target(self.conn, item=own)["platform"], "antigravity")

    def test_status_shows_each_provider_with_its_slots_and_place(self):
        core.config_set(self.conn, "agy_model", "gemini-4-neon")
        core.config_set(self.conn, "agy_effort", "medium")
        it = core.item_add(self.conn, "app", "Build feature")["id"]
        self._dispatch(it)
        st = core.status(self.conn)
        self.assertEqual([(p["pos"], p["agent"], p["used"], p["max"]) for p in st["providers"]],
                         [(1, "antigravity", 1, 2), (2, "claude", 0, None)])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.render_status(st)
        out = buf.getvalue()
        self.assertIn("Agent providers, in launch order (agent_order):", out)
        self.assertIn("1. Antigravity CLI (gemini-4-neon, medium): 1 of 2 sessions", out)
        self.assertIn("2. Claude Code: 0 sessions, no limit", out)
        self.assertEqual(core.state(self.conn)["providers"][0]["used"], 1)


class ModelRefused(unittest.TestCase):
    """#6429: four Codex sessions answered their first turn with a 400 'model is not supported' error and sat at
    their prompt for minutes with no alert; and two Dispatches of Codex ran different models."""

    # The error as Codex 0.159.1 printed it on 2026-11-20, wrapped as a pane shows it.
    SCREEN = ("■ {\"type\":\"error\",\"status\":400,\"error\":{\"type\":\"invalid_request_error\",\"message\":\"The\n"
              "  'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account.\"}}\n\n"
              "› Improve documentation in @filename\n\n  gpt-6.1-sol medium · ~/app\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = _db(self.tmp.name)
        self.t0 = datetime(2026, 11, 20, 19, 20, 0, tzinfo=timezone.utc)
        p_now = mock.patch.object(core, "now", side_effect=lambda: self.t0)
        p_now.start()
        self.addCleanup(p_now.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)
        self.addCleanup(server.LIMIT_SEEN.clear)
        core.register(self.conn, "mark", human=True)
        core.config_set(self.conn, "launch_agents", "Codex=@codex; Claude Code=@claude-code")
        core.config_set(self.conn, "agent_order", "codex model sol effort medium, claude-code")

    def _dispatch(self, item_id, agent=None):
        with mock.patch.object(server, "_can_open_terminal", return_value=None), \
             mock.patch.object(server, "_open_terminal", return_value={}):
            r = server.dispatch_item(self.conn, item_id, agent=agent, actor="mark")
        return r["session_name"], self.conn.execute(
            "SELECT agent_type, model FROM agents WHERE name=?", (r["session_name"],)).fetchone()

    def _watch(self, agent, text):
        launched = []

        def fake_kill(conn, name, reason, actor=None, by_serve=False):
            return core._killed(conn, name, reason, actor or "maxpm", pid=None, killed=True)

        screen = {"ended": False, "running": "codex", "tty": None, "target": f"maxpm:{agent}", "text": text}
        with mock.patch.object(server, "agent_terminals", return_value={agent: "%1"}), \
             mock.patch.object(server, "_pane_screen", return_value=screen), \
             mock.patch.object(server, "_pane_session_id", return_value=None), \
             mock.patch.object(server, "_pane_process", return_value=None), \
             mock.patch.object(server, "kill_agent", side_effect=fake_kill), \
             mock.patch.object(server, "_can_open_terminal", return_value=None), \
             mock.patch.object(server, "_open_terminal", side_effect=lambda t, env, r, c: launched.append((t, env))):
            return server.watch_prompts(self.conn), launched

    def test_codex_reads_the_refusal_over_wrapped_lines(self):
        got = agents.get("codex").model_refused(self.SCREEN.splitlines())
        self.assertEqual(got["model"], "gpt-6.1-sol")
        self.assertIn("not supported when using Codex with a ChatGPT account", got["message"])
        self.assertIsNone(agents.get("codex").model_refused(["› Improve documentation", "gpt-6.1-sol medium"]))
        self.assertIsNone(agents.get("claude").model_refused(self.SCREEN.splitlines()))

    def test_a_refused_first_turn_alerts_and_starts_the_item_on_the_next_agent(self):
        it = core.item_add(self.conn, "app", "Implement feature Y")
        name, (atype, model) = self._dispatch(it["id"])
        self.assertEqual((atype, model), ("codex", "sol"))
        res, launched = self._watch(name, self.SCREEN)
        rr = res["limit_restarts"][0]
        self.assertEqual((rr["agent"], rr["item_id"], rr["fallback"]), (name, it["id"], "Claude Code"))
        self.assertIsNotNone(self.conn.execute("SELECT stop_at FROM agents WHERE name=?", (name,)).fetchone()[0])
        self.assertEqual(core.item_show(self.conn, it["id"])["reserved_for"], rr["restarted"])
        self.assertEqual(len(launched), 1)
        alerts = [r["body"] for r in self.conn.execute(
            "SELECT body FROM messages WHERE kind='alert' AND to_agent='mark'")]
        self.assertEqual(len(alerts), 1)
        self.assertIn("refuses model gpt-6.1-sol", alerts[0])
        events = [r["change"] for r in self.conn.execute("SELECT change FROM events WHERE item_id=?", (it["id"],))]
        self.assertTrue(any("Codex refused model gpt-6.1-sol" in e for e in events))
        # The next launch skips the refused entry for an hour, and says why; after the hour Codex runs again.
        other = core.item_add(self.conn, "app", "Implement feature Z")
        provs = core.agent_providers(self.conn)
        self.assertIn("refused model gpt-6.1-sol", provs[0]["refused"])
        self.assertEqual(self._dispatch(other["id"])[1][0], "claude-code")
        self.t0 += core.MODEL_REFUSED_HOLD + timedelta(minutes=1)
        self.assertIsNone(core.agent_providers(self.conn)[0]["refused"])

    def test_a_session_at_work_is_not_read_as_refused(self):
        it = core.item_add(self.conn, "app", "Implement feature Y")
        name, _ = self._dispatch(it["id"])
        core.claim(self.conn, it["id"], name)
        res, launched = self._watch(name, self.SCREEN)
        self.assertNotIn("limit_restarts", res)
        self.assertEqual(launched, [])
        self.assertEqual(core.model_refusals(self.conn), {})

    def test_a_dispatch_that_names_codex_runs_the_model_of_its_entry(self):
        # A Dispatch that names Codex ran Codex's own default (GPT-6.1 Terra), the list's pick ran gpt-6.2-sol.
        plain = core.item_add(self.conn, "app", "Implement feature Y")
        opus = core.item_add(self.conn, "app", "Implement feature Z")
        core.item_edit(self.conn, opus["id"], models={"model": "opus"}, actor="mark")
        self.assertEqual(tuple(self._dispatch(plain["id"], agent="Codex")[1]), ("codex", "sol"))
        t = core.launch_target(self.conn, item=opus["id"], agent="Codex", actor="mark")
        self.assertEqual(tuple(self._dispatch(opus["id"], agent="Codex")[1]), ("codex", "sol"))
        self.assertIn("gpt-6.1-sol", t["command"])
        self.assertIn("model_reasoning_effort=medium", t["command"])


if __name__ == "__main__":
    unittest.main()
