"""The question dialogs of the agent CLIs other than Claude Code, read from a tmux pane and answered with the CLI's
keys (#6235). Claude Code's AskUserQuestion dialog is read in server._ask_dialog (#6234); this module reads the same
kind of dialog of Codex (request_user_input in Plan mode, request_user_input_async in Default mode), Copilot (ask_user), OpenCode (question) and Antigravity
CLI (ask_question, #6248). An adapter
names its form in DETECT['ask_form'] and the dialog's first line in DETECT['ask'].

Each form has read(rows, t): the dialog whose first line is rows[t], or None, as one dict for the page:
  {"tabs": [{"header", "done", "submit"}] (one at least), "current": the tab shown now (None: read from the colours),
   "question", "options": [{"n", "label", "detail", "checked": True/False for a box (None: no box), "chosen", "chat",
   "stop": its place in the list the arrows move in, "text": the option that takes the person's own text, "typed":
   the text in it}], "multi", "submit_stop", "at": the place of the marker (None: read from the colours), "preview",
   "review": [{"q", "a"}], "warn", "start", "rows"}
finish(ask, raw): what only the colours of the pane say (raw: the pane's text with its escape sequences).
answer(ask, pick, text, picks): (the keys, what was picked), with {"key": ...}, {"text": ...} and {"pause": True}
steps; the caller waits a moment before each text, while the CLI draws its input box, and at each pause.
tab(ask, n): the keys that show tab n.
Each was checked by hand on the CLI's own dialog on 2026-11-19: Codex 0.159.1, Copilot 1.0.110, OpenCode 2.2.7,
Antigravity CLI 1.5.13."""
from __future__ import annotations

import re

from .core import RiverError

_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_PLAIN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _move(a, b, more="Down", less="Up"):
    """The arrow keys from place a to place b of a list."""
    return [{"key": more if b > a else less}] * abs(b - a)


def _opt(n, label, stop, detail="", checked=None, text=False, typed=""):
    return {"n": n, "label": label, "detail": detail, "checked": checked, "chosen": False, "chat": False,
            "stop": stop, **({"text": True, "typed": typed} if text else {})}


def _dialog(tabs, question, opts, at, current, start, rows, review=(), warn=""):
    return {"tabs": tabs, "current": current, "question": question, "options": opts,
            "multi": any(o["checked"] is not None for o in opts), "submit_stop": None, "at": at, "preview": [],
            "review": list(review), "warn": warn, "start": start, "rows": rows}


def _box_of(ask, text):
    """The option that takes the person's own text, or a refusal when a text came and the question takes none."""
    box = next((o for o in ask["options"] if o.get("text")), None)
    if text and box is None:
        raise RiverError("this question takes no text: pick one of its options")
    return box


def _need_at(ask):
    if ask["at"] is None:
        raise RiverError("MaximizePM cannot see the marked option in the terminal; answer it there")
    return ask["at"]


def _one(ask, pick):
    """The option the person picked in a question that takes one answer."""
    opts = ask["options"]
    if not isinstance(pick, int) or not 0 <= pick < len(opts):
        raise RiverError("pick one of the question's options, or write your own answer")
    o = opts[pick]
    if o.get("text") and not o.get("typed"):
        raise RiverError("write your own answer in the text box")
    return o


def _want(picks):
    return {int(x) for x in (picks or []) if isinstance(x, int) or str(x).isdigit()}


def _toggles(ask, picks, key):
    """The keys that turn the boxes of a question with more answers to the person's picks: the arrows to each box
    that changes, then the key that turns it over. Returns (keys, the place of the marker after them, the labels)."""
    want, keys, pos = _want(picks), [], _need_at(ask)
    for n, o in enumerate(ask["options"]):
        if o["checked"] is not None and not o.get("text") and o["checked"] != (n in want):
            keys += _move(pos, o["stop"]) + [{"key": key}]
            pos = o["stop"]
    return keys, pos, [o["label"] for n, o in enumerate(ask["options"]) if n in want and not o.get("text")]


def _typed(text):
    """A text, then a pause before the next key: Codex takes characters that come fast as a paste, and an Enter
    right after them answered with an empty note (2026-11-19, Codex 0.159.1; 0.4 s later the note came)."""
    return [{"text": text}, {"pause": True}]


def _rub(typed):
    """The keys that delete the text already in an input box (the CLI keeps it when the box opens again)."""
    return [{"key": "BSpace"}] * len(typed or "")


# Codex (request_user_input, Plan mode). One question at a time, under 'Question 1/2 (2 unanswered)' ('Question
# 2/2' with no count when each question has an answer):
#     Which color for the button?
#     › 1. Red (Recommended)  Use red for the button.
#       2. Green              Use green for the button.
#       4. None of the above  Optionally, add details in notes (tab)
#     tab to add notes | enter to submit answer | ←/→ to navigate questions | esc to interrupt
# Enter answers the marked option and shows the next question ('enter to submit all' on the last one). Tab opens the
# notes of the marked option ('› Add notes' under the list; 'tab or esc to clear notes'): the own answer is 'None of
# the above' with a note. Left and Right show another question. No question with more answers.
# Enter on the last question while another one has no answer asks first: 'Submit with unanswered questions?',
# '1 unanswered question', '› 1. Proceed  Submit with 1 unanswered question', '2. Go back  Return to the first
# unanswered question', 'Press enter to confirm or esc to go back'.
#
# The question of Default mode (request_user_input_async, #6258, Codex 0.159.1, measured 2026-11-20): Codex asks it while
# its turn goes on and puts it under 'Queued follow-up inputs', closed: '? 1 question · 12s' and 'shift+← to answer'
# (DETECT['ask_open'] opens it). Open, it shows one question at a time, with '1 of 2' over it while more than one
# is left, the options and a last one 'Other', and a key line at the end:
#     Which color for the button?
#     › 1. Red
#       2. Green
#       3. Other
#     enter submit   ctrl+] skip   shift+→ main prompt   shift+← next question
# Enter answers the marked option, and the next question shows at once, open. A text typed on 'Other' takes the place
# of its label ('› 3. Blue'), and Enter sends it. The key line is the anchor: the form is read upwards from it.
class Codex:
    anchor = (r"^\s*(?:Question \d+/\d+(?: \(\d+ unanswered\))?|Submit with unanswered questions\?)\s*$"
              r"|^\s*enter submit\s+ctrl\+\] skip\b")
    _OPT = re.compile(r"^\s*(›\s*)?(\d+)\.\s+(.*?)(?:\s{2,}(\S.*))?$")
    _HINT = re.compile(r"\benter to submit\b|\btab to add notes\b|\bclear notes\b|\bPress enter to confirm\b")
    _QUEUED = re.compile(r"^\s*enter submit\s+ctrl\+\] skip\b")
    _OF = re.compile(r"^\s*(\d+) of (\d+)\s*$")

    def _queued(self, rows, t):
        """The open question of Default mode (request_user_input_async), read upwards from its key line rows[t]."""
        opts, n = [], t - 1
        while n >= 0 and (o := self._OPT.match(rows[n])):
            opts.insert(0, o)
            n -= 1
        question, k, total = [], 1, 1
        while n >= 0 and rows[n].strip() and not rows[n].lstrip().startswith(("•", "›", "└")):
            if (m := self._OF.match(rows[n])):
                k, total = int(m.group(1)), int(m.group(2))
                break
            question.insert(0, rows[n].strip())
            n -= 1
        if len(opts) < 2 or not question:
            return None
        out, at = [], None
        for i, o in enumerate(opts):
            if o.group(1):
                at = i
            label, last = o.group(3).strip(), i == len(opts) - 1
            # The last option takes the person's own text: 'Other', or the text typed on it.
            out.append(_opt(int(o.group(2)), label, i, (o.group(4) or "").strip(), text=last,
                            typed="" if not last or label == "Other" else label))
        header = f"Question {k} of {total}" if total > 1 else "Question"
        ask = _dialog([{"header": header, "done": False, "submit": False}], " ".join(question), out, at, 0, n + 1,
                      rows)
        ask["notes"], ask["queued"] = False, True
        return ask

    def read(self, rows, t):
        if self._QUEUED.match(rows[t]):
            return self._queued(rows, t)
        m = re.search(r"Question (\d+)/(\d+)", rows[t])
        k, total = (int(m.group(1)), int(m.group(2))) if m else (1, 0)
        question, opts, at, notes, end = [] if m else [rows[t].strip()], [], None, None, False
        for x in rows[t + 1:]:
            st = x.strip()
            o = self._OPT.match(x)
            if o:
                if o.group(1):
                    at = len(opts)
                label = o.group(3).strip()
                opts.append(_opt(int(o.group(2)), label, len(opts), (o.group(4) or "").strip(),
                                 text=label.startswith("None of the above")))
            elif self._HINT.search(st):
                end = True
                break
            elif not opts:
                question.append(st)
            elif st.startswith("›"):
                notes = st[1:].strip()  # the notes box of the marked option
            elif opts:
                opts[-1]["detail"] = (opts[-1]["detail"] + " " + st).strip()  # a description on more lines
        if not opts or not end:
            return None
        if notes is not None and notes != "Add notes" and at is not None and opts[at].get("text"):
            opts[at]["typed"] = notes
        if not m:  # the question before a submit with unanswered questions: Proceed or Go back, no text
            ask = _dialog([{"header": "Submit", "done": False, "submit": True}], question[0], opts, at, 0, t, rows,
                          warn=" ".join(question[1:]))
            ask["notes"] = False
            return ask
        tabs = [{"header": f"Question {n}", "done": False, "submit": False} for n in range(1, total + 1)]
        ask = _dialog(tabs, " ".join(question), opts, at, k - 1, t, rows)
        ask["notes"] = notes is not None  # the notes box is open: Tab closes it first
        return ask

    def finish(self, ask, raw):
        pass

    def answer(self, ask, pick, text, picks):
        at, box = _need_at(ask), _box_of(ask, text)
        if ask.get("queued"):  # Default mode: the text goes on 'Other' in place of its label (or of a text there)
            if text:
                return _move(at, box["stop"]) + _rub(box.get("typed")) + _typed(text) + [{"key": "Enter"}], text
            o = _one(ask, pick)
            return _move(at, o["stop"]) + [{"key": "Enter"}], o.get("typed") or o["label"]
        if text and ask.get("notes") and at == box["stop"]:  # its notes box is open: the new text in place of the old
            return _rub(box.get("typed")) + _typed(text) + [{"key": "Enter"}], text
        # An open notes box closes first (Tab), and a moment passes: a second Tab right after it was lost.
        first = [{"key": "Tab"}, {"pause": True}] if ask.get("notes") else []
        if text:
            return first + _move(at, box["stop"]) + [{"key": "Tab"}, *_typed(text), {"key": "Enter"}], text
        o = _one(ask, pick)
        if o.get("text"):
            return first + _move(at, o["stop"]) + [{"key": "Tab"}, *_typed(o["typed"]), {"key": "Enter"}], o["typed"]
        return first + _move(at, o["stop"]) + [{"key": "Enter"}], o["label"]

    def tab(self, ask, n):
        return _move(ask["current"], n, "Right", "Left")


# Copilot (ask_user). Under 'Copilot needs information.': its message, a tab line when it asks more than one question
# ('  Which color for the button?  [Which size?]', the shown one in brackets), the question as a label with ':', the
# options with no number (the marked one '❯'; a question with more answers has '•' and '✓' boxes, Space turns one
# over), 'Other (type your answer)', and the key line. On 'Other' an input box shows under the dialog ('❯ Type your
# answer...'): the text goes there. Enter answers (a question with boxes: all its ticks) and shows the next question;
# Tab and Shift+Tab show another one.
class Copilot:
    anchor = r"^\s*Copilot needs information\.?\s*$"
    _OPT = re.compile(r"^\s{0,2}([❯ ])\s(?:([•✓])\s)?(\S.*?)\s*$")
    _HINT = re.compile(r"↑/↓ select\b.*\benter accept\b")
    _OTHER = re.compile(r"^Other \(type your answer(?: below)?\)$")

    def read(self, rows, t):
        lines = [x for x in rows[t + 1:] if x.strip()]
        h = next((n for n, x in enumerate(lines) if self._HINT.search(x)), None)
        if h is None:
            return None
        n = h
        while n > 0 and self._OPT.match(lines[n - 1]):
            n -= 1
        if n == 0 or n == h:
            return None
        label, head = lines[n - 1].strip(), [x.strip() for x in lines[:n - 1]]
        opts, at = [], None
        for x in lines[n:h]:
            m = self._OPT.match(x)
            if m.group(1) == "❯":
                at = len(opts)
            other = bool(self._OTHER.match(m.group(3)))
            opts.append(_opt(None, m.group(3), len(opts), checked=None if other or not m.group(2) else m.group(2) == "✓",
                             text=other))
        box = next((o for o in opts if o.get("text")), None)
        typed = next((x.strip()[1:].strip() for x in lines[h + 1:] if x.strip().startswith("❯")), "")
        if box is not None and typed != "Type your answer...":
            box["typed"] = typed
        tabs, current = [{"header": label.rstrip(":"), "done": False, "submit": False}], 0
        bar = next((x for x in head if re.search(r"\[[^\]]+\]", x) and re.search(r"\S\s{2,}\S", x)), None)
        if bar:
            parts = re.split(r"\s{2,}", bar.strip())
            tabs = [{"header": p.strip("[]"), "done": False, "submit": False} for p in parts]
            current = next((i for i, p in enumerate(parts) if p.startswith("[") and p.endswith("]")), None)
        message = " ".join(x for x in head if x != bar)
        warn = message if message and message.rstrip(":") != label.rstrip(":") else ""
        return _dialog(tabs, label.rstrip(":").strip(), opts, at, current, t, rows, warn=warn)

    def finish(self, ask, raw):
        pass

    def answer(self, ask, pick, text, picks):
        at, box = _need_at(ask), _box_of(ask, text)
        if ask["multi"]:
            keys, pos, picked = _toggles(ask, picks, "Space")
            if text:
                keys += _move(pos, box["stop"]) + _rub(box.get("typed")) + _typed(text)
            return keys + [{"key": "Enter"}], ", ".join(picked + ([text] if text else [])) or "nothing"
        if text:
            return _move(at, box["stop"]) + _rub(box.get("typed")) + _typed(text) + [{"key": "Enter"}], text
        o = _one(ask, pick)
        return _move(at, o["stop"]) + [{"key": "Enter"}], o.get("typed") or o["label"]

    def tab(self, ask, n):
        return _move(ask["current"], n, "Tab", "BTab")


# OpenCode (question). Under its title 'Questions': a tab line when it asks more than one question or one with more
# answers ('Button color  Size  Submit'; the shown one in bold), the question, the numbered options with a
# description under each ('[ ]' and '[✓]' boxes in a question with more answers, Enter turns one over), 'Type your
# own answer' last (after a text: the text), and the key line. The marked option shows only by the colour of its
# number. Enter answers and shows the next tab; on the own answer it opens an input in its place ('enter confirm
# esc close'). The Submit tab lists the answers ('Size: Large') over 'enter submit'. Tab and Shift+Tab show
# another tab. Every line has the frame '┃' of the chat on its left.
class OpenCode:
    anchor = r"^\s*┃\s+Questions\s*$"
    _OPT = re.compile(r"^\s*(\d+)\.\s+(?:\[([ ✓])\]\s+)?(\S.*)$")
    _HINT = re.compile(r"\besc (?:dismiss|close)\b")
    _OWN = "Type your own answer"

    @staticmethod
    def _clean(x):
        x = re.sub(r"^\s*┃", "", x)
        return re.sub(r"(\S)\s{6,}\S.*$", r"\1", x).rstrip()  # the footer of the chat on the right

    def read(self, rows, t):
        lines = [self._clean(x) for x in rows[t + 1:]]
        lines = [x for x in lines if x.strip()]
        h = next((n for n, x in enumerate(lines) if self._HINT.search(x)), None)
        if h is None or h == 0:
            return None
        hint, body = lines[h], lines[:h]
        tabs, current = None, None
        parts = re.split(r"\s{2,}", body[0].strip())
        if len(parts) > 1 and parts[-1] == "Submit":
            tabs = [{"header": p, "done": False, "submit": p == "Submit"} for p in parts]
            body = body[1:]
        opts, question = [], []
        for x in body:
            m = self._OPT.match(x)
            if m:
                opts.append(_opt(int(m.group(1)), m.group(3).strip(), len(opts),
                                 checked=None if m.group(2) is None else m.group(2) == "✓"))
            elif opts:
                opts[-1]["detail"] = (opts[-1]["detail"] + " " + x.strip()).strip()
            else:
                question.append(x.strip())
        if not opts:
            if not tabs or "enter submit" not in hint:
                return None
            review = [dict(zip(("q", "a"), (y.strip() for y in q.split(":", 1)))) for q in question if ":" in q]
            ask = _dialog(tabs, "Review your answers", [_opt(None, "Submit answers", 0)], 0, len(tabs) - 1, t, rows,
                          review=review)
            ask["typing"] = False
            return ask
        own = opts[-1]
        if own["label"] == self._OWN or (not own["detail"] and any(o["detail"] for o in opts[:-1])):
            own.update(text=True, typed="" if own["label"] == self._OWN else own["label"])
            if own["checked"] is not None and own["typed"] == "":
                own["checked"] = False
        tabs = tabs or [{"header": " ".join(question)[:40], "done": False, "submit": False}]
        ask = _dialog(tabs, " ".join(question), opts, None, 0 if len(tabs) == 1 else None, t, rows)
        ask["typing"] = "esc close" in hint  # the input of the own answer is open
        return ask

    @staticmethod
    def _parts(line):
        """[(the foreground colour, text)] of a line with its escape sequences."""
        out, fg, at = [], None, 0
        for m in _SGR.finditer(line + "\x1b[m"):
            if line[at:m.start()]:
                out.append((fg, line[at:m.start()]))
            codes = m.group(1)
            if codes.startswith("38;"):
                fg = codes[3:]
            elif codes in ("", "0", "39"):
                fg = None
            at = m.end()
        return out

    def finish(self, ask, raw):
        """The marked option and the tab shown now, from the colours: the marked option's label has the colour of
        its number (2026-11-19: both orange; the others a grey number and a white label), the tab in bold."""
        lines = (raw or "").splitlines()
        top = next((n for n in range(len(lines) - 1, -1, -1) if re.match(self.anchor, _PLAIN.sub("", lines[n]))), None)
        if top is None:
            return
        marked = []
        for x in lines[top + 1:]:
            parts = [(c, s) for c, s in self._parts(re.sub(r"^.*?┃", "", x)) if s.strip()]
            num = next((i for i, (_, s) in enumerate(parts) if re.fullmatch(r"\s*\d+\.\s*", s)), None)
            if num is None:
                continue
            label = next((p for p in parts[num + 1:] if not re.fullmatch(r"\s*\[[ ✓]\]\s*", p[1])), None)
            if label and label[0] == parts[num][0]:
                marked.append(int(parts[num][1].strip().rstrip(".")))
        if ask["at"] is None and len(marked) == 1:
            ask["at"] = next((o["stop"] for o in ask["options"] if o["n"] == marked[0]), None)
        if ask["current"] is None:
            bar = next((x for x in lines[top + 1:top + 4] if "Submit" in _PLAIN.sub("", x)), "")
            bold = re.search(r"\x1b\[1m((?:\x1b\[[0-9;]*m)*)([^\x1b]+)", bar)
            if bold:
                ask["current"] = next((i for i, x in enumerate(ask["tabs"]) if x["header"] == bold.group(2).strip()),
                                      None)

    def answer(self, ask, pick, text, picks):
        if ask["question"] == "Review your answers" and not any(o.get("text") for o in ask["options"]):
            return [{"key": "Enter"}], "Submit answers"
        first = [{"key": "Escape"}] if ask.get("typing") else []  # close an open input: the arrows move again
        at, box = _need_at(ask), _box_of(ask, text)
        own = (lambda pos, t: _move(pos, box["stop"]) + [{"key": "Enter"}] + _rub(box.get("typed"))
               + _typed(t) + [{"key": "Enter"}])
        if ask["multi"]:
            keys, pos, picked = _toggles(ask, picks, "Enter")
            if text:
                keys += own(pos, text)
            nxt = [{"key": "Tab"}] if len(ask["tabs"]) > 1 else []  # the next tab: a question or Submit
            return first + keys + nxt, ", ".join(picked + ([text] if text else [])) or "nothing"
        if text:
            return first + own(at, text), text
        o = _one(ask, pick)
        if o.get("text"):  # the own answer given before: its input opens again with the text in it
            return first + own(at, o["typed"]), o["typed"]
        return first + _move(at, o["stop"]) + [{"key": "Enter"}], o["label"]

    def tab(self, ask, n):
        return _move(ask["current"], n, "Tab", "BTab")


# Antigravity CLI (ask_question, #6248). One question at a time, under 'Question 1/2: Which size?' (the whole question
# on that line), the numbered options ('> ' marks the selected one; '[ ]' and '[x]' boxes when it takes more answers;
# '✓' on the answer given before, after the person came back), 'Write-in...' last, and the key line
# ('↑/↓ Navigate · → Next · enter Select · esc Skip'; 'space Toggle · enter Submit All' on the last question with
# boxes). Enter answers and shows the next question (on the last one it sends all); on Write-in it opens a box under
# the list ('Your answer:', the typed text, 'enter Submit · esc Back'). Left and Right show another question. Checked by
# hand on agy 1.5.13, 2026-11-19 (tests/fixtures/agy/pane_ask_*).
class Antigravity:
    anchor = r"^\s*Question \d+/\d+: "
    _OPT = re.compile(r"^\s*(>\s*)?(\d+)\.\s+(?:\[([ x])\]\s+)?(✓\s+)?(\S.*?)\s*$")
    _HINT = re.compile(r"↑/↓ Navigate\b|\benter Submit\b")

    def read(self, rows, t):
        m = re.search(r"Question (\d+)/(\d+): (.*)$", rows[t])
        if not m:
            return None
        k, total, question = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        opts, at, typed, typing, end = [], None, [], False, False
        for x in rows[t + 1:]:
            st = x.strip()
            o = None if typing else self._OPT.match(x)
            if o:
                if o.group(1):
                    at = len(opts)
                label = o.group(5).strip()
                opt = _opt(int(o.group(2)), label, len(opts), checked=None if o.group(3) is None else o.group(3) == "x",
                           text=label.startswith("Write-in"))
                opt["chosen"] = bool(o.group(4))
                opts.append(opt)
            elif self._HINT.search(st):
                end = True
                break
            elif st == "Your answer:":
                typing = True
            elif typing and st:
                typed.append(st)
            elif question and not opts and st:
                question += " " + st  # a long question goes on over more lines
        if not opts or not end:
            return None
        box = next((o for o in opts if o.get("text")), None)
        if box is not None and typed:
            box["typed"] = " ".join(typed)
        tabs = [{"header": f"Question {n}", "done": False, "submit": False} for n in range(1, total + 1)]
        ask = _dialog(tabs, question, opts, at, k - 1, t, rows)
        ask["typing"] = typing  # the Write-in box is open: Enter sends its text
        return ask

    def finish(self, ask, raw):
        pass

    def answer(self, ask, pick, text, picks):
        at, box = _need_at(ask), _box_of(ask, text)
        if ask.get("typing"):  # the box of the own answer is open: the new text in place of the old
            if not text:
                raise RiverError("the question waits for your own answer: write it")
            return _rub(box.get("typed")) + _typed(text) + [{"key": "Enter"}], text
        own = (lambda pos, t: _move(pos, box["stop"]) + [{"key": "Enter"}, {"pause": True}] + _typed(t)
               + [{"key": "Enter"}])
        if ask["multi"]:
            keys, pos, picked = _toggles(ask, picks, "Space")
            keys += own(pos, text) if text else [{"key": "Enter"}]
            return keys, ", ".join(picked + ([text] if text else [])) or "nothing"
        if text:
            return own(at, text), text
        o = _one(ask, pick)
        return _move(at, o["stop"]) + [{"key": "Enter"}], o["label"]

    def tab(self, ask, n):
        return _move(ask["current"], n, "Right", "Left")


# Hermes Agent (clarify tool, #6681). A framed box titled '╭─ Hermes Agent needs your input ───╮', '1 question' or
# 'N questions', then one status line per question ('✓' answered with its locked answer indented below, '▸' active,
# '·' pending), and under the active question its numbered choices ('❯' marks the selected one; '[x]'/'[ ]' boxes
# when multi_select), with 'Other (type your answer)' / 'Other (type below)' last, then '╰───╯' and the key line
# ('↑/↓ to select, Enter to lock, Tab next question'). Enter locks the choice or opens the freetext composer on Other
# (then type + Enter); Space toggles a checkbox; Tab / Shift+Tab move between questions. Checked against Hermes v0.22.7
# (hermes_cli/cli_tui_mixin.py _get_clarify_display_fragments and tests/fixtures/hermes/pane_blocked_clarify.txt).
class Hermes:
    anchor = r"^\s*╭─\s*Hermes Agent needs your input\b"
    _COUNT = re.compile(r"^\d+ questions?$")
    _Q = re.compile(r"^([✓▸·])\s+(\S.*)$")
    _OPT = re.compile(r"^\s*(❯\s*)?(?:\[([ x])\]\s+)?(\d+)\.\s+(\S.*?)\s*$")
    _OTHER = re.compile(r"^Other(?:\s*\(type (?:your answer|below)\)|:\s*(.*))$")

    @staticmethod
    def _unbox(line):
        s = line.strip()
        if s.startswith("│"):
            s = s[1:]
        if s.endswith("│"):
            s = s[:-1]
        return s.rstrip()

    def read(self, rows, t):
        body = []
        closed = False
        for x in rows[t + 1:]:
            st = x.strip()
            if st.startswith("╰"):
                closed = True
                break
            if st.startswith("│"):
                body.append(self._unbox(x))
        if not closed or not body:
            return None
        tabs, current, question, opts, at, typing = [], None, "", [], None, False
        in_active = False
        for raw in body:
            st = raw.strip()
            if not st or self._COUNT.match(st):
                continue
            qm = self._Q.match(st)
            if qm:
                mark, qtext = qm.group(1), qm.group(2).strip()
                in_active = (mark == "▸")
                if in_active:
                    current = len(tabs)
                    question = qtext
                tabs.append({"header": qtext[:40], "done": mark == "✓", "submit": False})
                continue
            if in_active:
                om = self._OPT.match(raw)
                if om:
                    if om.group(1):
                        at = len(opts)
                    label = om.group(4).strip()
                    oth = self._OTHER.match(label)
                    is_other = bool(oth)
                    typed = (oth.group(1) or "").strip() if oth and oth.group(1) is not None else ""
                    if is_other and "type below" in label.lower():
                        typing = True
                    opts.append(_opt(int(om.group(3)), label, len(opts),
                                     checked=None if om.group(2) is None else om.group(2) == "x",
                                     text=is_other, typed=typed))
                elif not opts and question:
                    question += " " + st
        if not opts or current is None:
            return None
        ask = _dialog(tabs, question, opts, at, current, t, rows)
        ask["typing"] = typing
        return ask

    def finish(self, ask, raw):
        pass

    def answer(self, ask, pick, text, picks):
        at, box = _need_at(ask), _box_of(ask, text)
        if ask.get("typing"):
            if not text:
                raise RiverError("the question waits for your own answer: write it")
            return _rub(box.get("typed")) + _typed(text) + [{"key": "Enter"}], text
        own = (lambda pos, t: _move(pos, box["stop"]) + [{"key": "Enter"}, {"pause": True}] + _typed(t)
               + [{"key": "Enter"}])
        if ask["multi"]:
            keys, pos, picked = _toggles(ask, picks, "Space")
            keys += own(pos, text) if text else [{"key": "Enter"}]
            return keys, ", ".join(picked + ([text] if text else [])) or "nothing"
        if text:
            return own(at, text), text
        o = _one(ask, pick)
        return _move(at, o["stop"]) + [{"key": "Enter"}], o.get("typed") or o["label"]

    def tab(self, ask, n):
        return _move(ask["current"], n, "Tab", "BTab")


FORMS = {"codex": Codex(), "copilot": Copilot(), "opencode": OpenCode(), "antigravity": Antigravity(),
         "hermes": Hermes()}
