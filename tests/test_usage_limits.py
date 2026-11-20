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
        self.assertEqual(core.setting(self.conn, "agent_order"), "antigravity, claude-code")
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
        # Codex has no reading: it shows too, as not known (mark 2026-11-20: also with no session of the agent)
        self.assertEqual([r["agent_type"] for r in st["agent_limits"]], ["claude", "codex", "antigravity"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.render_status(st)
        out = buf.getvalue()
        self.assertIn("Agent usage:", out)
        self.assertIn("Antigravity CLI: AT LIMIT", out)
        self.assertIn("Claude Code: 65% remaining", out)

        # Page state carries agent_limits
        pst = core.state(self.conn)
        self.assertEqual(len(pst["agent_limits"]), 3)
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
        self.assertEqual(lim["reason"], "Gemini models: 5-hour at 0% left")
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
        self.assertIn("Gemini models: 5-hour at 0% left", str(ctx.exception))


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
                     "PROBE": {"running": False, "at": {}}, "CLAUDE_JSON": str(claude_json)}.items():
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
        self.assertTrue(rows["antigravity"]["limited"])
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

    def test_no_probe_while_a_session_runs_when_off_or_without_a_trusted_folder(self):
        core.register(self.conn, "cc1")
        self.conn.execute("UPDATE agents SET agent_type='claude-code' WHERE name='cc1'")
        self.conn.commit()
        self.assertEqual([a for a, _ in server.usage_probes_due(self.conn)], ["antigravity"])
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


if __name__ == "__main__":
    unittest.main()
