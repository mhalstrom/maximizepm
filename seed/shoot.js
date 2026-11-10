// Takes the pictures for the README and maximizepm.com from the page, with the demo data of
// seed/screenshots.sh, and writes them to site/img/ (or to OUT). Uses the desktop app's Electron (no other
// packages), tmux, and ffmpeg:
//   cd desktop && npm ci && npx electron ../seed/shoot.js
// The server runs with a throwaway HOME (/tmp/.../alex), so no path of this computer shows in a picture.
// The page never clicks Start or Dispatch here: those open real terminal windows. The agents of the
// pictures are stand-ins in panes of a tmux server of its own (TMUX_TMPDIR), never this computer's tmux:
// they give the ! dialog and the alert for a prompt that waits, and maxpm view.
// A sandbox blocks the tmux socket: run this outside one.
const { app, BrowserWindow } = require("electron");
const { execFileSync, spawn } = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");

const repo = path.join(__dirname, "..");
const out = process.env.OUT || path.join(repo, "site", "img");
const tmp = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "river-shots-")));
const home = path.join(tmp, "alex");
const sock = fs.realpathSync(fs.mkdtempSync("/tmp/rs-"));  // short: a socket path has a length limit
const W = 1280, H = 820;
const COLS = 160, ROWS = 44;  // the terminal that runs maxpm view
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const tmuxExe = ["/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux"].find((p) => fs.existsSync(p)) || "tmux";

function freePort() {
  return new Promise((resolve) => { const s = net.createServer(); s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => resolve(p)); }); });
}

// A throwaway HOME whose login profile finds stand-in claude, codex and maxpm commands (not this Mac's).
function makeHome() {
  const bin = path.join(home, "bin");
  fs.mkdirSync(bin, { recursive: true });
  fs.mkdirSync(path.join(home, "code", "shop"), { recursive: true });
  for (const c of ["claude", "codex", "maxpm"]) fs.writeFileSync(path.join(bin, c), "#!/bin/sh\nexit 0\n", { mode: 0o755 });
  fs.writeFileSync(path.join(home, ".zprofile"), `export PATH="${bin}:$PATH"\n`);
}
const riverIn = (env) => (...a) => execFileSync("python3", [path.join(repo, "bin", "maxpm"), "-q", ...a], { env, encoding: "utf8" });

// maxpm serve on a database; returns its URL and a stop function.
const servers = [];
async function serve(env) {
  const port = await freePort();
  const srv = spawn("python3", [path.join(repo, "bin", "maxpm"), "serve", "--port", String(port)], { env, stdio: "ignore" });
  servers.push(srv);
  return { url: `http://127.0.0.1:${port}/`, stop: () => srv.kill() };
}

// What a stand-in agent shows in its pane: demo text in the look of an agent CLI.
const E = "\x1b[", R = E + "0m";
const tool = (name, arg, ...res) => [`${E}32m●${R} ${E}1m${name}${R}(${arg})`, ...res.map((x, i) => `${E}2m  ${i ? " " : "└"} ${x}${R}`), ""];
const busy = (text) => [`${E}33m✻ ${text}${R}`];
function screen(a) {
  return [`${E}2m>${R} go`, "",
    ...tool("Bash", "maxpm go", `You are MaximizePM agent ${a.name}. Role: WORKER.`, `YOUR ITEM #${a.id}: ${a.title}`),
    ...a.steps.flat()].join("\n");
}

async function main() {
  fs.mkdirSync(out, { recursive: true });
  makeHome();
  const base = { ...process.env, HOME: home, SHELL: "/bin/zsh", TZ: "UTC", LANG: "en_US.UTF-8", LC_ALL: "en_US.UTF-8", TMUX_TMPDIR: sock };
  for (const k of ["MAXPM_AGENT", "MAXPM_FOCUS", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "MAXPM_DB", "TMUX", "TMUX_PANE"]) delete base[k];
  const tmux = (...a) => execFileSync(tmuxExe, ["-u", "-f", "/dev/null", ...a], { env: base, encoding: "utf8" }).replace(/\n$/, "");
  const outer = (...a) => tmux("-L", "outer", ...a);
  // The tmux server of the pictures is a new one: this must find no server.
  let other = null;
  try { other = tmux("list-sessions"); } catch (e) { /* no server: right */ }
  if (other !== null) throw new Error("a tmux server answers on the socket of the pictures: " + other);

  // The window is never shown (it would take the focus), so the page would read document.hidden as true, and the
  // Sessions tab reads nothing while the page is hidden: the preload says the page is visible.
  const preload = path.join(tmp, "visible.js");
  fs.writeFileSync(preload, `Object.defineProperty(document, "hidden", { get: () => false });
Object.defineProperty(document, "visibilityState", { get: () => "visible" });\n`);
  const win = new BrowserWindow({ width: W, height: H, show: false, paintWhenInitiallyHidden: true,
    webPreferences: { backgroundThrottling: false, contextIsolation: false, sandbox: false, preload } });
  const js = (code) => win.webContents.executeJavaScript(code);
  let url;
  const ready = async () => { for (let i = 0; i < 50; i++) { try { await win.loadURL(url); return; } catch (e) { await sleep(200); } } };
  const until = async (code, tries = 50) => { for (let i = 0; i < tries; i++) { if (await js(code)) return true; await sleep(200); } return false; };

  // Open the page on a link (graph?as=alex) in a theme, wait until it has drawn, run a step, take a picture.
  async function open(path, theme = "light") {
    await js(`localStorage.setItem("river.theme", ${JSON.stringify(theme)})`);
    await win.loadURL("about:blank");
    await win.loadURL(new URL(path || "", url).href);
    await until(`!!document.querySelector("#work .row, #work .card, #work > *")`);
    await sleep(1200);
    await tidyPaths();
  }
  // Paths: the throwaway HOME reads as ~, like on a person's own computer. Again before each picture: the chat,
  // the side panel and the lists load after the page.
  const tidyPaths = () => js(`(() => { const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); let n;
      while ((n = w.nextNode())) n.nodeValue = n.nodeValue.split(${JSON.stringify(home)}).join("~").split(${JSON.stringify(os.homedir())}).join("~");
      // A field the page fills again (the side panel's address) gets ~ too: the value setter of this page does it.
      if (!window.__tidyValue) {
        window.__tidyValue = true;
        const d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
        Object.defineProperty(HTMLInputElement.prototype, "value", { configurable: true, get() { return d.get.call(this); },
          set(v) { d.set.call(this, String(v).split(${JSON.stringify(home)}).join("~")); } });
      }
      for (const e of document.querySelectorAll("input, textarea")) e.value = e.value.split(${JSON.stringify(home)}).join("~"); })()`);
  async function shot(name, selector, pad = 0) {
    await tidyPaths();  // first: the text with ~ is what the box measures
    let rect;
    if (selector) {
      const r = await js(`(() => { const e = document.querySelector(${JSON.stringify(selector)}); if (!e) return null;
        e.scrollIntoView({ block: "start" }); const b = e.getBoundingClientRect(); return { x: b.x, y: b.y, width: b.width, height: b.height }; })()`);
      if (!r) throw new Error(`${name}: nothing matches ${selector}`);
      await sleep(200);
      rect = { x: Math.max(0, Math.floor(r.x - pad)), y: Math.max(0, Math.floor(r.y - pad)),
        width: Math.ceil(r.width + 2 * pad), height: Math.ceil(Math.min(r.height + 2 * pad, H)) };
    }
    await tidyPaths();  // again: a field the page filled after the box was measured
    const img = await win.webContents.capturePage(rect);
    fs.writeFileSync(path.join(out, name), img.toPNG());
    console.log("wrote", name, JSON.stringify(img.getSize()));
  }
  const frames = [];
  async function frame() { await tidyPaths(); const img = await win.webContents.capturePage(); frames.push(img.toPNG()); }
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;");
  // A terminal window with this HTML as its text.
  const termPage = (inner, font = "13px/1.45") => "data:text/html;charset=utf-8," + encodeURIComponent(`<!doctype html><body style="margin:0;background:#1e1f24">
    <div id="t" style="margin:24px;border-radius:10px;background:#15161a;box-shadow:0 8px 30px #0008;overflow:hidden;width:${W - 48}px">
    <div style="height:28px;background:#2a2b31;display:flex;align-items:center;gap:8px;padding-left:12px">
    <i style="width:12px;height:12px;border-radius:6px;background:#ff5f57"></i><i style="width:12px;height:12px;border-radius:6px;background:#febc2e"></i><i style="width:12px;height:12px;border-radius:6px;background:#28c840"></i></div>
    <pre style="margin:0;padding:16px 18px;color:#d8dae0;font:${font} Menlo,monospace;white-space:pre-wrap">${inner}</pre></div></body>`);

  // The setup guide on a first start: only the person is known yet. It opens by itself.
  const first = { ...base, MAXPM_DB: path.join(tmp, "first.db") };
  riverIn(first)("register", "alex", "--human");
  let s1 = await serve(first); url = s1.url; await ready();
  await open("?as=alex");
  await shot("setup-guide.png", "#setup .box", 0);
  s1.stop();

  // Everything else: the demo data, with the guide dismissed.
  // The default database of the throwaway HOME, so the go briefing prints no QUEUE line about MAXPM_DB.
  const env = { ...base };
  const river = riverIn(env);
  river("config", "set", "setup_done", "on");  // first, so the Recent panel shows only the demo work
  execFileSync("sh", [path.join(repo, "seed", "screenshots.sh")], { env, stdio: "ignore" });
  // The briefing picture: before the page pictures, so the page keeps the state the seed made.
  const brief = river("--as", "web-agent", "go", "--project", "website");

  // Two more agents at work, so maxpm view has four panes.
  const add = (project, title, touches) => +river("--json", "--as", "alex", "add", project, title, "--doer", "ai", "--touches", touches).match(/"id":\s*(\d+)/)[1];
  const extra = [
    { name: "search-agent", note: "storefront work", project: "website", title: "Add search to the product list", touches: "web/search/",
      steps: [tool("Read", "web/products/List.tsx", "Read 97 lines"), tool("Write", "web/search/SearchBox.tsx", "Wrote 58 lines"),
        tool("Bash", "npm test -- SearchBox", "6 passed"), busy("Connecting the search box to the product list… (48s)")] },
    { name: "email-agent", note: "backend work", project: "backend", title: "Send an email when an order ships", touches: "api/email.py",
      steps: [tool("Read", "api/email.py", "Read 44 lines"), tool("Update", "api/email.py", "Added 31 lines"),
        tool("Bash", "pytest tests/test_email.py", "1 failed, 7 passed"), busy("Fixing the test for the subject line… (1m 05s)")] },
  ];
  for (const a of extra) {
    river("register", a.name, "--note", a.note);
    a.id = add(a.project, a.title, a.touches);
    river("--as", a.name, "claim", String(a.id));
  }
  const agents = [
    { name: "api-agent", id: 2, title: "Build the orders API",
      steps: [tool("Read", "api/orders.md", "Read 61 lines"), tool("Write", "api/orders.py", "Wrote 142 lines"),
        tool("Bash", "pytest tests/test_orders.py", "12 passed in 0.84s"), busy("Adding pages to GET /orders… (2m 14s)")] },
    { name: "web-agent", id: 10, title: "Resize the product photos",
      steps: [tool("Bash", "node web/scripts/resize.js web/public/products", "24 photos: 4000 px to 1600 px"),
        [`${E}32m●${R} ${E}1mBash${R}(git commit -am "Resize the product photos")`, "",
          `  ${E}1mDo you want to proceed?${R}`, `  ${E}36m❯ 1. Yes${R}`, "    2. Yes, and don't ask again for git commit", "    3. No, and tell the agent what to do differently"]] },
    ...extra,
  ];

  // The agents in tmux, as maxpm launch --tmux makes them: a window each in the session maxpm, with the
  // session's name and the agent's name on the pane. A stand-in shows the screen and stays.
  const standin = path.join(tmp, "agent.py");
  fs.writeFileSync(standin, `import signal, sys, time
text = open(sys.argv[1]).read()
def draw(*_):
    sys.stdout.write("\\x1b[2J\\x1b[H\\x1b[?25l" + text.replace("\\n", "\\r\\n"))
    sys.stdout.flush()
signal.signal(signal.SIGWINCH, draw)
draw()
while True:
    time.sleep(60)
`);
  const cwd = path.join(home, "code", "shop");
  agents.forEach((a, i) => {
    const file = path.join(tmp, a.name + ".txt");
    fs.writeFileSync(file, screen(a));
    const name = `#${a.id} ${a.title}`.slice(0, 45);
    const make = i ? ["new-window", "-d", "-t", "=maxpm:"] : ["new-session", "-d", "-s", "maxpm", "-x", String(COLS), "-y", String(ROWS)];
    const pane = tmux(...make, "-n", name, "-c", cwd, "-P", "-F", "#{pane_id}", `exec python3 ${standin} ${file}`);
    // The status line shows the time only: the default also shows the host name.
    if (!i) { tmux("set-option", "-t", "=maxpm:", "default-size", `${COLS}x${ROWS}`); tmux("set-option", "-g", "status-right", " %H:%M "); }
    tmux("set-option", "-p", "-t", pane, "@maxpm_name", name);
    tmux("set-option", "-p", "-t", pane, "@maxpm_agent", a.name);
  });
  console.log(river("view"));  // side by side, as a person's maxpm view leaves them
  // The Sessions and Artifacts tabs: the projects' folder, and what two of the agents said and wrote there.
  for (const p of ["website", "backend"]) river("--as", "alex", "project", "path", p, cwd);
  execFileSync("python3", [path.join(repo, "seed", "transcripts.py"), cwd], { env, stdio: "ignore" });

  // The prompt alert comes after a short wait here.
  river("config", "set", "prompt_wait", "1s");
  river("config", "set", "notify_interval", "2s");
  // The stand-in agent commands do nothing: a session that serve starts by itself (the review of the release, a fresh
  // session for an answer) would show in the Sessions tab as one that never connected.
  river("config", "set", "auto_review", "off");
  river("config", "set", "fresh_sessions", "off");
  const s2 = await serve(env); url = s2.url; await ready();
  await open("?as=alex");
  if (!await until(`/waits on a prompt/.test((document.querySelector("#needsYou") || {}).textContent || "")`, 150)) throw new Error("no prompt alert on the page");

  // Plan > Overview in both themes, and the You view of Sessions (the first page while something waits on alex),
  // with the alert's row open: its Open session button shows (#5102).
  for (const theme of ["light", "dark"]) {
    await open("plan/overview?as=alex", theme);
    await shot(`plan-${theme}.png`);
    await open("?as=alex", theme);
    await until(`/waits on a prompt/.test((document.querySelector("#needsYou") || {}).textContent || "")`);
    await js(`[...document.querySelectorAll("#needsYou .nyl")].find(e => /waits on a prompt/.test(e.textContent)).click()`);
    await sleep(300);
    await shot(`needs-you-${theme}.png`, "#needsYouPanel", 8);
  }
  // The Sessions tab: the ! dialog opens by itself for the prompt that waits in web-agent's terminal; then its
  // chat, with a file it wrote open in the side panel.
  const dlgOpen = () => until(`!document.querySelector("#waitDlg").classList.contains("hidden")`);
  await open("sessions/web-agent?as=alex");
  if (!await dlgOpen()) {
    console.error(await js(`fetch("api/sessions").then(r => r.json()).then(j => JSON.stringify({ hidden: document.hidden, path: location.pathname,
      title: document.querySelector("#sessTitle").textContent, dialogs: [...document.querySelectorAll('#setup:not(.hidden), [id$="Dlg"]:not(.hidden)')].map(e => e.id),
      sessions: j.sessions.map(s => [s.name, s.state, (s.waiting || []).map(w => w.kind + ":" + (w.sig || "").slice(0, 8))]) }))`));
    throw new Error("the ! dialog did not open");
  }
  await sleep(400); await shot("sessions-prompt.png");
  const photos = path.join(cwd, "docs", "product-photos.md");
  for (const theme of ["light", "dark"]) {
    await open("sessions/web-agent?as=alex", theme);
    await dlgOpen(); await js(`document.querySelector("#waitDismiss").click()`);
    await js(`import("/components/sidePanel.js").then(m => m.openInPanel(${JSON.stringify(photos)}))`);
    await until(`(document.querySelector("#spBody .sp-md") || {}).textContent > ""`);
    await sleep(600); await shot(`sessions-${theme}.png`);
  }
  await open("artifacts?as=alex");
  await until(`document.querySelectorAll("#artList .art-row").length >= 4`);
  await shot("artifacts.png", "#sessArts", 8);
  // The views of Plan (#5102): Done, Overview (the capacity numbers), Graph (with the step bars), Releases, and Goals
  // with the scope of one goal; then the scope goes back to all projects for the pictures after them.
  await open("plan/done?as=alex"); await sleep(600); await shot("done.png", "#plan-done", 8);
  await open("settings?as=alex"); await sleep(600); await shot("settings.png");  // the window: the tab is taller
  await open("plan/overview?as=alex"); await shot("parallel-work.png", "#plan-overview", 8);
  await open("plan/graph?as=alex"); await sleep(1500); await shot("graph.png", "#plan-graph", 8);
  await open("plan/releases?as=alex"); await shot("release.png", "#plan-releases", 8);
  await open("plan/goals?project=website&goal=checkout-launch&as=alex");
  await sleep(600); await shot("goals.png", "#plan-goals", 8);
  await js(`localStorage.removeItem("river.scope")`);  // the page remembers the scope: the pictures after this one show all work

  // The tour: the You view; an agent's session on the Sessions tab, first with the ! dialog for its prompt, then its
  // chat; an item, Artifacts, and Plan: Overview, Items, Goals, Graph, Releases, Done. Two frames each.
  for (const h of ["?as=alex", "sessions/web-agent?as=alex", "sessions/web-agent?as=alex&item=4", "artifacts?as=alex",
                   "plan/overview?as=alex", "plan/items?as=alex", "plan/goals?as=alex", "plan/graph?as=alex",
                   "plan/releases?as=alex", "plan/done?as=alex"]) {
    await open(h); if (h.includes("graph")) await sleep(1500);
    if (h.startsWith("sessions")) {
      if (!await dlgOpen()) throw new Error("the ! dialog did not open for the tour");
      await sleep(400); await frame(); await frame();
      await js(`document.querySelector("#waitDismiss").click()`);
      await sleep(1500);
    }
    await frame(); await frame();
  }
  const fd = path.join(tmp, "frames"); fs.mkdirSync(fd);
  frames.forEach((f, i) => fs.writeFileSync(path.join(fd, `f${String(i).padStart(3, "0")}.png`), f));
  execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-framerate", "0.8", "-i", path.join(fd, "f%03d.png"),
    "-vf", "scale=960:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=none",
    "-loop", "0", path.join(out, "board-tour.gif")]);
  console.log("wrote board-tour.gif");

  // maxpm view: the command runs in a terminal of COLS x ROWS (a pane of a second tmux server), and that
  // terminal's screen, with its colours, is the picture.
  outer("new-session", "-d", "-s", "outer", "-x", String(COLS), "-y", String(ROWS), "-c", cwd,
    `env -u TMUX -u TMUX_PANE python3 ${path.join(repo, "bin", "maxpm")} view`, ";", "set-option", "-g", "status", "off");
  await sleep(2500);
  const view = outer("capture-pane", "-p", "-e", "-t", "outer");
  await open("?as=alex");
  const viewHtml = await js(`import("/components/terminalDialog.js").then(m => m.ansiToHtml(${JSON.stringify(view)}))`);
  await win.loadURL(termPage(viewHtml, "12px/1.2"));
  await sleep(500); await shot("maxpm-view.png", "#t", 0);

  // The go briefing an agent reads, drawn as a terminal window.
  await win.loadURL(termPage(`<span style="color:#8bd5a0">~/code/shop $</span> maxpm go\n${esc(brief.trim())}`));
  await sleep(500); await shot("go-briefing.png", "#t", 0);

  s2.stop();
  for (const t of [outer, tmux]) { try { t("kill-server"); } catch (e) { /* none */ } }
  fs.rmSync(tmp, { recursive: true, force: true });
  fs.rmSync(sock, { recursive: true, force: true });
}

app.whenReady().then(main).then(() => app.quit(), (e) => {
  console.error(e);
  for (const srv of servers) srv.kill();
  // Only the servers of the pictures: each by the path of its own socket.
  const dir = path.join(sock, "tmux-" + os.userInfo().uid);
  for (const name of ["outer", "default"]) { try { execFileSync(tmuxExe, ["-S", path.join(dir, name), "kill-server"], { stdio: "ignore" }); } catch (x) { /* none */ } }
  app.exit(1);
});
