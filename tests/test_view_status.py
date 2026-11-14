"""The status inbox of the Sessions tab (#5852): every session in one of four states (needs you, done, working,
idle), one list of all projects' sessions by state, and a push notice for the states the person picks
(notify_status)."""
import unittest
from unittest import mock

from river import core, sessions
from river.routes import sessions as session_routes
from tests.test_view_sessions import Base, claude_line, drive_page, DRIVE_HEAD


def card(**kw):
    c = {"state": "idle", "state_label": "idle: at its prompt", "waiting": [], "last": [{"role": "assistant"}],
         "at": "2026-11-08T10:00:05.500Z"}
    c.update(kw)
    return c


class StatusOf(unittest.TestCase):
    def test_a_prompt_a_question_or_a_badge_needs_you(self):
        self.assertEqual(sessions.status_of(card(state="prompt")), "needs_you")
        self.assertEqual(sessions.status_of(card(state="answer")), "needs_you")
        self.assertEqual(sessions.status_of(card(waiting=[{"kind": "question"}])), "needs_you")

    def test_a_turn_that_runs_or_background_work_is_working(self):
        for st in ("working", "starting", "stopping"):
            self.assertEqual(sessions.status_of(card(state=st)), "working", st)
        # Its turn ended, and the harness wakes it when its background command ends.
        self.assertEqual(sessions.status_of(card(state_label="idle: waits on npm test")), "working")

    def test_a_turn_end_after_the_last_look_is_done_and_a_seen_one_is_idle(self):
        seen = core._ts("2026-11-08T10:00:06Z")
        self.assertEqual(sessions.status_of(card()), "done")  # never seen
        self.assertEqual(sessions.status_of(card(), seen), "idle")
        self.assertEqual(sessions.status_of(card(at="2026-11-08T10:01:00Z"), seen), "done")  # a later turn ended
        self.assertEqual(sessions.status_of(card(state="expert")), "done")  # an expert wrote to you
        self.assertEqual(sessions.status_of(card(state_label="idle: waits for work")), "done")  # maxpm wait after a task

    def test_a_session_with_no_turn_or_one_that_ended_is_idle(self):
        self.assertEqual(sessions.status_of(card(last=[])), "idle")
        self.assertEqual(sessions.status_of(card(state="ended")), "idle")
        self.assertEqual(sessions.status_of(card(state="gone")), "idle")


class Inbox(Base):
    def cards(self):
        return sessions.views(self.c, {}, self.root, self.croot)

    def test_the_card_has_its_state_and_project_and_a_look_moves_done_to_idle(self):
        (c,) = self.cards()
        self.assertEqual((c["status"], c["project"]), ("working", "site"))  # a tool runs, on an item of site
        self.write(claude_line("assistant", [{"type": "text", "text": "Done."}], "end_turn", at="2026-11-08T10:05:00Z"))
        self.assertEqual(self.cards()[0]["status"], "done")
        core.session_seen(self.c, "ag", "mark")
        self.assertEqual(self.cards()[0]["status"], "idle")
        # A later turn ends: done again, until the person types in its chat.
        self.write(claude_line("user", "and the footer") + claude_line(
            "assistant", [{"type": "text", "text": "Footer done."}], "end_turn", at="2099-01-01T00:00:00Z"))
        self.assertEqual(self.cards()[0]["status"], "done")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET person_said_at='2099-01-01T00:00:10Z' WHERE name='ag'")
        self.assertEqual(self.cards()[0]["status"], "idle")

    def test_the_project_comes_from_an_expert_or_the_item_it_finished_last(self):
        core.register(self.c, "ex")
        with core.tx(self.c):
            self.c.execute("UPDATE agents SET expert_project='site' WHERE name='ex'")
        a = core.agent_status(self.c, "ex")
        self.assertEqual(sessions._project_of(self.c, a, [], None), "site")
        core.project_add(self.c, "docs")
        it = core.item_add(self.c, "docs", "readme")["id"]
        core.register(self.c, "w2")
        core.claim(self.c, it, "w2")
        core.done(self.c, it, "x", actor="w2")
        self.assertEqual(sessions._project_of(self.c, core.agent_status(self.c, "w2"), [], None), "docs")
        self.assertIsNone(sessions._project_of(self.c, core.agent_status(self.c, "mark"), [], None))

    def test_session_seen_needs_a_known_session(self):
        with self.assertRaises(core.RiverError):
            core.session_seen(self.c, "nobody")


class Setting(Base):
    def test_notify_status_is_off_or_a_list_of_the_four_states(self):
        self.assertEqual(core.setting(self.c, "notify_status"), "off")
        core.config_set(self.c, "notify_status", "needs_you,done")
        core.config_set(self.c, "notify_status", "idle", project="site")
        core.config_set(self.c, "notify_status", "off")
        for bad in ("on", "done,later", ","):
            with self.assertRaisesRegex(core.RiverError, "notify_status is off or a comma list"):
                core.config_set(self.c, "notify_status", bad)


class Push(Base):
    def setUp(self):
        super().setUp()
        from river import notify
        self.notify, self.sent = notify, []
        notify.register_channel("fake", lambda conn: lambda t, b, u: self.sent.append((t, b, u)))
        self.addCleanup(notify.ADAPTERS.pop, "fake", None)
        core.config_set(self.c, "notify_channels", "fake")
        session_routes.STATUS_LAST["by"] = None
        self.addCleanup(session_routes.STATUS_LAST.update, by=None)
        p = mock.patch.object(core, "transcripts_root", lambda: self.root)
        p.start()
        self.addCleanup(p.stop)

    def end_turn(self, at="2026-11-08T10:05:00Z"):
        self.write(claude_line("assistant", [{"type": "text", "text": "Done."}], "end_turn", at=at))

    def test_a_session_that_enters_a_chosen_state_sends_one_notice(self):
        core.config_set(self.c, "notify_status", "done")
        self.assertEqual(session_routes.status_push(self.c), [])  # the first pass only records
        self.assertEqual(session_routes.status_push(self.c), [])  # no change
        self.end_turn()
        self.assertEqual(session_routes.status_push(self.c), [{"agent": "ag", "status": "done"}])
        ((title, body, url),) = self.sent
        self.assertEqual(title, "MaximizePM: header is done")
        self.assertIn("(site)", body)
        self.assertTrue(url.endswith("/sessions/ag"), url)
        self.assertEqual(session_routes.status_push(self.c), [])  # it stays done: no second notice
        core.session_seen(self.c, "ag", "mark")
        self.assertEqual(session_routes.status_push(self.c), [])  # idle is not chosen
        self.assertEqual(len(self.sent), 1)

    def test_the_projects_value_wins_and_off_sends_nothing(self):
        core.config_set(self.c, "notify_status", "done")
        core.config_set(self.c, "notify_status", "off", project="site")
        session_routes.status_push(self.c)
        self.end_turn()
        self.assertEqual(session_routes.status_push(self.c), [])
        self.assertEqual(self.sent, [])

    def test_off_everywhere_reads_no_session_and_the_next_pass_only_records(self):
        session_routes.STATUS_LAST["by"] = {"ag": "working"}
        with mock.patch.object(session_routes, "cards", side_effect=AssertionError("read")):
            self.assertEqual(session_routes.status_push(self.c), [])
        self.assertIsNone(session_routes.STATUS_LAST["by"])
        core.config_set(self.c, "notify_status", "done")
        self.end_turn()
        self.assertEqual(session_routes.status_push(self.c), [])  # records: the session was done before the setting

    def test_needs_you_skips_a_session_whose_question_went_out_already(self):
        core.config_set(self.c, "notify_status", "needs_you")
        session_routes.status_push(self.c)
        core.send(self.c, "question", "Which color for the header?", to="mark", item=self.it, actor="ag")
        core.sync_needs_you(self.c)
        self.assertEqual(session_routes.cards(self.c)[0]["status"], "needs_you")
        self.assertEqual(session_routes.status_push(self.c), [])
        self.assertEqual(self.sent, [])


class Page(Base):
    """The Status row of the list and the Status view, on the real page at desktop and phone width (p57)."""

    DRIVER = DRIVE_HEAD + r"""
try {
  await until(() => q("#sessStatusRow .srow-status"), "the Status row");
  // ag's turn ended before the page opened: its chat is open, so the page marks it seen and it moves to Idle.
  await until(() => /1 idle/.test(q("#sessStatusRow .sub").textContent), "ag seen: 1 idle");
  q("#sessStatusRow .srow-main").click();
  await until(() => !q("#sessStatus").classList.contains("hidden") && q("#statusList .st-group"), "the Status view");
  const groups = [...document.querySelectorAll("#statusList .st-gh > span:first-child")].map(x => x.textContent);
  const idle = q('#statusList [data-group="idle"] [data-status-go="ag"]');
  check(idle, "ag in Idle");
  const wide = document.documentElement.scrollWidth <= window.innerWidth;
  const rowBox = idle.getBoundingClientRect();
  q('#statusList [data-status-push="done"]').click();
  await until(() => q('#statusList [data-status-push="done"]').getAttribute("aria-pressed") === "true", "Push of Done on");
  const url = location.pathname;
  q('#statusList [data-status-go="ag"]').click();  // the list drew again after Push: the row of now
  await until(() => !q("#sessMain").classList.contains("hidden") && q("#sessStatus").classList.contains("hidden"), "ag's chat");
  post({ groups, wide, url, rowH: rowBox.height, rowW: rowBox.width, win: window.innerWidth });
} catch (e) { post({ error: String(e.stack || e) }); }
"""

    def run_at(self, size):
        self.write(claude_line("assistant", [{"type": "text", "text": "Done."}], "end_turn", at="2026-11-08T10:05:00Z"))
        got = drive_page(self, self.DRIVER, size=size)
        self.assertEqual(got["groups"], ["Needs you", "Done", "Working", "Idle"])
        self.assertTrue(got["wide"], "no sideways scroll of the page")
        self.assertEqual(got["url"], "/status")
        self.assertGreaterEqual(got["rowH"], 44, got)  # a tap target on a phone
        self.assertLessEqual(got["rowW"], got["win"], got)
        self.assertEqual(core.setting(self.c, "notify_status"), "done")
        self.assertIsNotNone(core.agent_status(self.c, "ag")["seen_at"])

    def test_desktop(self):
        self.run_at("1500,900")

    def test_phone(self):
        self.run_at("390,844")


if __name__ == "__main__":
    unittest.main()
