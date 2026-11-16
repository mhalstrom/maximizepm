"""The page uses one set of shared UI parts (#5028, mark: 'we should really have common components for ui elements').

A view (river/static/views/*) makes no element that a part makes (river/static/components/README.md says which part
for what): no raw button (components/button.js; in HTML a <button> with the class btn or iconbtn), no dialog of its own
(components/dialog.js and the .dlg look), no literal color and no literal px size (the tokens of app.css). Comments do
not count, and a px size in an @media line does not count (a media query cannot read a token).

ALLOW holds what the views had when the guard came. It only shrinks: a count above it fails (use the part or the
token), and a count below it fails too, so the list goes down with each change (set the new, lower number).
"""
import re
import unittest
from pathlib import Path

VIEWS = Path(__file__).resolve().parent.parent / "river" / "static" / "views"

# file: {kind: count}. A file or a kind that is not here allows 0.
ALLOW = {}

HEX = r"#[0-9a-fA-F]{3,8}\b"


def strip_comments(text, suffix):
    if suffix == ".html":
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    if suffix == ".js":
        text = re.sub(r"(?m)(^|[\s;{}(,])//.*$", r"\1", text)
    return text


def counts(path):
    """{kind: count} of the raw elements in one view file."""
    suffix = path.suffix
    text = strip_comments(path.read_text(encoding="utf-8"), suffix)
    c = {"button": 0, "dialog": 0, "color": 0, "px": 0}
    if suffix == ".js":
        c["button"] = len(re.findall(r"<button\b", text)) + len(re.findall(r"createElement\(\s*['\"]button['\"]", text))
    elif suffix == ".html":
        for tag in re.findall(r"<button\b[^>]*>", text):
            cls = re.search(r"class=\"([^\"]*)\"", tag)
            if not cls or not set(cls.group(1).split()) & {"btn", "iconbtn"}:
                c["button"] += 1
    if suffix in (".js", ".html"):
        c["dialog"] = len(re.findall(r"<dialog\b|\.showModal\(|aria-modal|role=\\?[\"']dialog", text))
    else:
        c["dialog"] = sum(1 for rule in re.findall(r"\{[^{}]*\}", text)
                          if re.search(r"position:\s*fixed", rule) and re.search(r"inset:\s*0", rule))
    if suffix == ".css":
        c["color"] = len(re.findall(HEX, text)) + len(re.findall(r"\b(?:rgba?|hsla?)\(", text))
    else:
        # A hex color alone in a string ("#fff"), or after a color property; not an item number in a text ("#4955").
        at = {m.start(2) for m in re.finditer(r"(['\"`])(" + HEX + r")\1", text)}
        at |= {m.start(1) for m in re.finditer(r"(?:color|background|border|fill|stroke)[\w-]*\s*[:=]\s*['\"]?(" + HEX
                                               + ")", text)}
        c["color"] = len(at) + len(re.findall(r"\b(?:rgba?|hsla?)\(", text))
    c["px"] = sum(len(re.findall(r"(?<![\w-])\d*\.?\d+px\b", line)) for line in text.splitlines()
                  if not line.lstrip().startswith("@media"))
    return c


def all_counts():
    out = {}
    for p in sorted(VIEWS.iterdir()):
        if p.suffix in (".js", ".html", ".css"):
            got = {k: v for k, v in counts(p).items() if v}
            if got:
                out[p.name] = got
    return out


# A hover never moves the page (#5106, mark: 'this layout shift is really annoying ... ensure there are no layout shifts
# possible'). A :hover rule (also :not(:hover)) shows or hides a control only with opacity or visibility, and changes
# colors, never a property that sizes or places a box: the control keeps its place, and nothing beside it moves.
STATIC = VIEWS.parent
LAYOUT = re.compile(r"(?:^|;)\s*(display|width|height|(?:min|max)-(?:width|height)|margin[\w-]*|padding[\w-]*|font-size|"
                    r"font-weight|font|letter-spacing|line-height|gap|(?:row|column)-gap|flex[\w-]*|grid[\w-]*|position|"
                    r"inset|top|left|right|bottom|border(?:-(?:top|right|bottom|left))?(?:-width)?|outline-offset|"
                    r"content)\s*:", re.I)


def hover_shifts(path):
    """[(selector, property)] of the :hover rules of one style sheet that size or place a box."""
    text = strip_comments(path.read_text(encoding="utf-8"), ".css")
    out = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
        sel, body = " ".join(m.group(1).split()), m.group(2)
        if ":hover" in sel:
            out += [(sel, x.group(1)) for x in LAYOUT.finditer(body)]
    return out


class UiParts(unittest.TestCase):
    def test_a_hover_moves_nothing(self):
        errors = [f"{p.relative_to(STATIC)}: {sel} sets {prop} on hover: show or hide with opacity or visibility, "
                  f"and keep the box's size and place"
                  for p in sorted(STATIC.rglob("*.css")) if "vendor" not in p.parts for sel, prop in hover_shifts(p)]
        self.assertEqual(errors, [], "\n".join(errors))

    def test_the_hover_guard_sees_a_shift(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            css = Path(d, "x.css")
            css.write_text(".row:not(:hover) .pin { display: none; }\n.row:hover .end { opacity: 1; visibility: visible; }\n"
                           "@media (hover: hover) { .b:hover { padding-left: var(--sp-1); color: red; } }\n"
                           "/* .c:hover { width: 0 } */ .d:hover{border:1px solid}\n.e .f { display: none; }\n", encoding="utf-8")
            self.assertEqual([p for _, p in hover_shifts(css)], ["display", "padding-left", "border"])

    def test_views_use_the_parts_and_the_allow_list_only_shrinks(self):
        got = all_counts()
        what = {"button": "a raw button: use components/button.js (HTML: class btn or iconbtn)",
                "dialog": "a dialog of its own: use components/dialog.js (makeDialog, the .dlg look)",
                "color": "a literal color: use a color token of app.css (--ink, --muted, --warn, ...)",
                "px": "a literal px size: use a size token of app.css (--sp-*, --fs-*, --r-*)"}
        errors = []
        for name in sorted(set(got) | set(ALLOW)):
            for kind in what:
                n, allowed = got.get(name, {}).get(kind, 0), ALLOW.get(name, {}).get(kind, 0)
                if n > allowed:
                    errors.append(f"views/{name}: {n} {kind} (allowed {allowed}): {what[kind]}; "
                                  "see river/static/components/README.md")
                elif n < allowed:
                    errors.append(f"views/{name}: {kind} went down to {n}: set ALLOW[{name!r}][{kind!r}] = {n} in "
                                  "tests/test_ui_parts.py (the list only shrinks)")
        self.assertEqual(errors, [], "\n".join(errors))

    def test_the_guard_sees_each_kind(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            js, html, css = Path(d, "x.js"), Path(d, "x.html"), Path(d, "x.css")
            js.write_text('// <button> in a comment\nconst b = document.createElement("button");\n'
                          'el.innerHTML = `<button class="btn">x</button>`; el.style.color = "#ff0000";\n'
                          'const t = "see #4955"; d.setAttribute("role", "dialog"); x = `<div role="dialog">`;\n'
                          'url = "https://a.b/c"; w = "12px";\n', encoding="utf-8")
            html.write_text('<!-- <button> --><button class="btn">ok</button><button id="raw">raw</button>'
                            '<div style="margin:8px"></div>', encoding="utf-8")
            css.write_text("/* #fff 9px */ .a { color: #fff; margin: 8px; background: rgba(0,0,0,.4); }\n"
                           "@media (max-width: 600px) { .b { padding: 0; } }\n"
                           ".scrim { position: fixed; inset: 0; }\n", encoding="utf-8")
            self.assertEqual(counts(js), {"button": 2, "dialog": 1, "color": 1, "px": 1})
            self.assertEqual(counts(html), {"button": 1, "dialog": 0, "color": 0, "px": 1})
            self.assertEqual(counts(css), {"button": 0, "dialog": 1, "color": 2, "px": 1})


if __name__ == "__main__":
    unittest.main()


class TimeTexts(unittest.TestCase):
    def test_no_text_puts_ago_after_ago(self):
        # lib.js ago() ends with 'ago' itself; an ended session's hover read 'ended 3h ago ago' (#5395).
        bad = [f"{p.relative_to(STATIC)}:{n}" for p in sorted(STATIC.rglob("*.js")) if "vendor" not in p.parts
               for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
               if re.search(r"\bago\([^`]*?\)\)?\}?\s*ago\b", line)]
        self.assertEqual(bad, [])


class DataKeys(unittest.TestCase):
    def test_no_data_key_of_a_part_is_camel_case(self):
        # button({data: {credRevoke: 1}}) writes data-credRevoke; HTML lowers the name, so dataset.credRevoke was empty
        # and Revoke, Grant (#5829) and Retire a policy did nothing (#5833). A key is kebab-case: "cred-revoke".
        bad = [f"{p.relative_to(STATIC)}:{n}" for p in sorted(STATIC.rglob("*.js")) if "vendor" not in p.parts
               for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
               if re.search(r"data: *\{[^}]*\b[a-z]+[A-Z][A-Za-z]*\s*:", line)]
        self.assertEqual(bad, [])


class Dialogs(unittest.TestCase):
    def test_every_dialog_of_the_part_is_a_modal_dialog(self):
        # The page's keys and refreshes wait while a [role=dialog] shows; the End dialog had no role (#5403).
        text = (STATIC / "components" / "dialog.js").read_text(encoding="utf-8")
        self.assertIn('if (!el.hasAttribute("role")) el.setAttribute("role", "dialog");', text)
        self.assertIn('el.setAttribute("aria-modal", "true");', text)


class Selects(unittest.TestCase):
    def test_no_rule_takes_the_phone_arrow_off_a_select(self):
        # phone.css draws a select's arrow as a background image; a later or stronger rule with the background
        # shorthand on a select removes it (the session Settings rows, #5421). Only the base rule of app.css, which
        # phone.css follows, may use it; any other rule sets background-color.
        found = []
        for path in sorted(STATIC.rglob("*.css")):
            if "vendor" in path.parts:
                continue
            text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
            for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
                sel = " ".join(sel.split())
                names = [s.strip() for s in sel.split(",")]
                if not any(re.search(r"(^|[\s>])select\b(?![-\w])", s) for s in names):
                    continue
                if sel == "select, input, textarea":
                    continue
                if re.search(r"(^|;)\s*background\s*:", body):
                    found.append(f"{path.name}: {sel}")
        self.assertEqual(found, [])


class Tooltips(unittest.TestCase):
    """A tooltip names what the control does in a few words ('Dictate', 'Close finished panes'); an explanation, if
    one is needed, is visible help text or the guide (#5482). No title, aria-label or data-tip of the page has more
    than 40 characters of fixed text. The parts that a template fills in (${...}) do not count."""

    LIMIT = 40
    PATTERNS = [
        re.compile(r"""\b(?:title|aria-label|data-tip)=(["'])(.*?)\1""", re.S),
        re.compile(r"""\b(?:title|label|tip)\s*:\s*(["'`])(.*?)(?<!\\)\1""", re.S),
        re.compile(r"""\.title\s*=\s*(["'`])(.*?)(?<!\\)\1""", re.S),
        re.compile(r"""setAttribute\(\s*["'](?:title|aria-label)["']\s*,\s*(["'`])(.*?)(?<!\\)\1""", re.S),
    ]

    def test_no_tooltip_is_longer_than_40_characters(self):
        found = []
        for path in sorted(STATIC.rglob("*")):
            if path.suffix not in (".html", ".js") or "vendor" in path.parts or "canvaskit" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            for pat in self.PATTERNS:
                for m in pat.finditer(text):
                    fixed = re.sub(r"\$\{[^}]*\}", "", m.group(2))
                    if len(fixed) > self.LIMIT:
                        line = text.count("\n", 0, m.start()) + 1
                        found.append(f"{path.relative_to(STATIC)}:{line} ({len(fixed)}): {fixed[:60]}")
        self.assertEqual(found, [])

    # #5963 (policy p146, mark 2026-11-12): a tooltip names what the control does in 1 to 4 words; a part that a
    # template fills in (${...}) counts as one word. WORDS_ALLOW holds the tooltips of more words that the files had
    # when the rule came (#5963 shortened index.html and views/sessions.html). It only goes down: a count above it
    # fails (shorten the new tooltip), and a count below it fails too (set the new, lower number).
    WORDS = 4
    WORDS_ALLOW = {
        "app.js": 5, "components/agentActions.js": 2, "components/artLinks.js": 2, "components/askCard.js": 3,
        "components/bar.js": 1, "components/browserTool.js": 3, "components/canvasComments.js": 2,
        "components/canvasDoc.js": 2, "components/card.js": 3, "components/changesView.js": 4, "components/chat.js": 2,
        "components/drawer.js": 7, "components/endSession.js": 1, "components/itemPop.js": 2,
        "components/itemRow.js": 1, "components/itemView.js": 1, "components/panZoom.js": 2,
        "components/reportDialog.js": 1, "components/settingsDialog.js": 3, "components/sidePanel.js": 5,
        "components/targetHooks.js": 1, "components/taskContext.js": 1, "components/waitDialog.js": 2,
        "views/artifacts.html": 2, "views/browsers.html": 1, "views/cost.js": 2, "views/done.js": 1, "views/goals.js": 2,
        "views/graph.html": 1, "views/health.html": 1, "views/health.js": 2, "views/ops.html": 1, "views/ops.js": 3,
        "views/overview.html": 3, "views/overview.js": 3, "views/plan.html": 2, "views/plan.js": 1,
        "views/projects.js": 1, "views/sessions-work.js": 6, "views/sessions.js": 4, "views/status.js": 1,
        "views/targets.js": 6, "views/terminal.html": 1, "views/you-needs.js": 2, "views/you.html": 2, "views/you.js": 4,
    }

    def test_a_tooltip_has_at_most_4_words(self):
        counts, long = {}, {}
        for path in sorted(STATIC.rglob("*")):
            if path.suffix not in (".html", ".js") or "vendor" in path.parts or "canvaskit" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            name = str(path.relative_to(STATIC))
            for pat in self.PATTERNS:
                for m in pat.finditer(text):
                    fixed = re.sub(r"\$\{[^}]*\}", "X", m.group(2))
                    if len(fixed.split()) > self.WORDS:
                        counts[name] = counts.get(name, 0) + 1
                        long.setdefault(name, []).append(f"line {text.count(chr(10), 0, m.start()) + 1}: {fixed[:60]}")
        over = {f: (n, long[f]) for f, n in counts.items() if n > self.WORDS_ALLOW.get(f, 0)}
        under = {f: (counts.get(f, 0), n) for f, n in self.WORDS_ALLOW.items() if counts.get(f, 0) < n}
        self.assertEqual(over, {}, "tooltips of more than 4 words (shorten them)")
        self.assertEqual(under, {}, "fewer long tooltips than WORDS_ALLOW says: set the lower number")


class ProductName(unittest.TestCase):
    """#5963 (policy p93, mark 2026-10-03): text that maxpm prints or the page shows calls the product MaximizePM and
    the command maxpm, never river (the package's old name). A sentence (3 words or more) that holds the word river is
    refused; the package name in code (import river, river/, river.core) is no sentence."""

    WORD = re.compile(r"(?<![\w./-])[Rr]iver(?![\w./-])")

    def sentences(self, text):
        return len(text.split()) >= 3 and "import " not in text and self.WORD.search(text)

    def test_no_text_for_a_person_says_river(self):
        import ast
        root = STATIC.parent
        found = []
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docs = {id(n.body[0].value) for n in ast.walk(tree)
                    if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body
                    and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
            for n in ast.walk(tree):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs \
                        and self.sentences(n.value):
                    found.append(f"{path.relative_to(root)}:{n.lineno}: {n.value[:60]}")
        for path in sorted(STATIC.rglob("*")):
            if path.suffix not in (".html", ".js") or "vendor" in path.parts:
                continue
            text = strip_comments(path.read_text(encoding="utf-8"), path.suffix)
            for m in re.finditer(r"""(["'`])((?:(?!\1).)*?)\1""", text):
                if self.sentences(m.group(2)):
                    found.append(f"static/{path.relative_to(STATIC)}: {m.group(2)[:60]}")
        self.assertEqual(found, [])
