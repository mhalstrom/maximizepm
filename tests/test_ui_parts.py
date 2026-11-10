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
ALLOW = {
    "sessions.css": {"color": 10, "px": 375},
    "sessions.html": {"button": 1},
    "sessions.js": {"button": 9, "px": 1},
}

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
    text = strip_comments(path.read_text(), suffix)
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


class UiParts(unittest.TestCase):
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
                          'url = "https://a.b/c"; w = "12px";\n')
            html.write_text('<!-- <button> --><button class="btn">ok</button><button id="raw">raw</button>'
                            '<div style="margin:8px"></div>')
            css.write_text("/* #fff 9px */ .a { color: #fff; margin: 8px; background: rgba(0,0,0,.4); }\n"
                           "@media (max-width: 600px) { .b { padding: 0; } }\n"
                           ".scrim { position: fixed; inset: 0; }\n")
            self.assertEqual(counts(js), {"button": 2, "dialog": 1, "color": 1, "px": 1})
            self.assertEqual(counts(html), {"button": 1, "dialog": 0, "color": 0, "px": 1})
            self.assertEqual(counts(css), {"button": 0, "dialog": 1, "color": 2, "px": 1})


if __name__ == "__main__":
    unittest.main()
