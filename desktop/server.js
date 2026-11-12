// Starts `maxpm serve` for the desktop app and stops it again. Plain Node, no Electron,
// so `npm test` can check it without downloading Electron.
const { spawn, execFileSync } = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");
const http = require("node:http");
const os = require("node:os");

// Each candidate is a command and its first arguments. An app started from the Dock gets a short
// PATH, so on macOS look in the usual places too.
const PYTHONS = [["python3"], ["/opt/homebrew/bin/python3"], ["/usr/local/bin/python3"], ["/usr/bin/python3"]];

// The first Python that is 3.10 or newer, as [command, ...args], or null.
function findPython(candidates = PYTHONS) {
  for (const c of candidates) {
    const [cmd, ...args] = Array.isArray(c) ? c : [c];
    try {
      const out = execFileSync(cmd, [...args, "-c", "import sys; print(int(sys.version_info >= (3, 10)))"],
        { encoding: "utf8", timeout: 5000, stdio: ["ignore", "pipe", "ignore"] });
      if (out.trim() === "1") return [cmd, ...args];
    } catch (e) { /* not there, or too old */ }
  }
  return null;
}

// The Python a packaged app carries (scripts/fetch-python.js puts it in river-app/python), or null:
// then the app looks for one on the Mac (npm start in the repo).
function bundledPython(riverRoot) {
  const exe = path.join(riverRoot, "python", "bin", "python3");
  return fs.existsSync(exe) ? [exe] : null;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.unref();
    s.on("error", reject);
    s.listen(0, "127.0.0.1", () => { const { port } = s.address(); s.close(() => resolve(port)); });
  });
}

function get(url) {
  return new Promise((resolve) => {
    const req = http.get(url, (res) => { res.resume(); resolve(res.statusCode); });
    req.on("error", () => resolve(0));
    req.setTimeout(1000, () => { req.destroy(); resolve(0); });
  });
}

function getJson(url) {
  return new Promise((resolve) => {
    const req = http.get(url, (res) => {
      let b = "";
      res.on("data", (d) => { b += d; });
      res.on("end", () => { try { resolve(res.statusCode === 200 ? JSON.parse(b) : null); } catch (e) { resolve(null); } });
    });
    req.on("error", () => resolve(null));
    req.setTimeout(1000, () => { req.destroy(); resolve(null); });
  });
}

// Whether a port on 127.0.0.1 is free to listen on.
function portFree(port) {
  return new Promise((resolve) => {
    const s = net.createServer();
    s.unref();
    s.once("error", () => resolve(false));
    s.listen(port, "127.0.0.1", () => s.close(() => resolve(true)));
  });
}

const real = (p) => { try { return fs.realpathSync(p); } catch (e) { return path.resolve(p); } };

// The queue of this app and its serve (#5618): {db, port (the setting serve_port), running: {port, pid} or null},
// read by the Python that the app runs, from the code it runs.
function queueInfo({ riverRoot, python, env = process.env }) {
  const [cmd, ...args] = python;
  const code = "import json, sys; sys.path.insert(0, sys.argv[1]); from river import core; c = core.connect(); "
    + "print(json.dumps({'db': str(core.db_path()), 'port': int(core.setting(c, 'serve_port')), "
    + "'running': core.running_serve(c)}))";
  const out = execFileSync(cmd, [...args, "-c", code, riverRoot], { encoding: "utf8", timeout: 20000, env,
    stdio: ["ignore", "pipe", "pipe"] });
  return JSON.parse(out.trim().split("\n").pop());
}

// A maxpm serve that answers for this queue already (the login serve, or one a person started from a terminal):
// {url, port}, or null. It must name the same queue in GET /api/queue.
async function runningServe(info) {
  const ports = [...new Set([info.running && info.running.port, info.port].filter(Boolean))];
  for (const port of ports) {
    const q = await getJson(`http://127.0.0.1:${port}/api/queue`);
    if (q && q.db && real(q.db) === real(info.db)) return { url: `http://127.0.0.1:${port}/`, port };
  }
  return null;
}

// The app's own folder for versions of the code (#5601): versions/<version>, which an Update of the page unpacks
// (river/release.py), current (the folder the app and the maxpm launcher run) and trying (a version that an Update
// started and that has not answered yet).
function appDir(env = process.env) {
  return env.MAXPM_APP_DIR || path.join(os.homedir(), ".maximizepm", "app");
}

// The version a code folder says it is (river/__init__.py), or null.
function codeVersion(root) {
  try {
    const m = fs.readFileSync(path.join(root, "river", "__init__.py"), "utf8").match(/__version__\s*=\s*"(\d+)\.(\d+)\.(\d+)"/);
    return m ? [+m[1], +m[2], +m[3]] : null;
  } catch (e) { return null; }
}
const cmp = (a, b) => (a[0] - b[0]) || (a[1] - b[1]) || (a[2] - b[2]);

// A version that failed: the app does not start it again. Only a folder in versions/, never the app's own copy.
function markBad(root, dir) {
  const rel = path.relative(path.join(dir, "versions"), root);
  if (!root || rel.startsWith("..") || path.isAbsolute(rel) || !fs.existsSync(root)) return false;
  fs.writeFileSync(path.join(root, ".bad"), new Date().toISOString() + "\n");
  return true;
}

function firstLine(file) {
  try { return fs.readFileSync(file, "utf8").split("\n")[0].trim() || null; } catch (e) { return null; }
}

// The code folder to start: the newest of the app's own copy (bundle) and the good versions in dir/versions. A
// version that an Update tried and that never answered (dir/trying is still there) is bad first. Writes dir/current,
// which the maxpm launcher reads.
function pickRoot({ bundle, dir }) {
  const tried = firstLine(path.join(dir, "trying"));
  if (tried) { markBad(tried, dir); fs.rmSync(path.join(dir, "trying"), { force: true }); }
  let best = { root: bundle, v: codeVersion(bundle) || [0, 0, 0] };
  let names = [];
  try { names = fs.readdirSync(path.join(dir, "versions")); } catch (e) { /* no updates yet */ }
  for (const name of names) {
    const root = path.join(dir, "versions", name);
    if (name.startsWith(".") || fs.existsSync(path.join(root, ".bad")) || !fs.existsSync(path.join(root, "bin", "maxpm"))) continue;
    const v = codeVersion(root);
    if (v && cmp(v, best.v) > 0) best = { root, v };
  }
  try {
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, ".current.tmp"), best.root + "\n");
    fs.renameSync(path.join(dir, ".current.tmp"), path.join(dir, "current"));
  } catch (e) { /* the launcher then runs the bundle */ }
  return best.root;
}

// Start the server from the river folder (the repo, the app's bundled copy, or a version in the app's folder).
// pythonRoot: the folder that holds the app's own Python (the bundle), when it is not riverRoot. Resolves to
// { url, port, child, stop } once /api/state answers; rejects with a message a person can act on.
async function startRiver({ riverRoot, pythonRoot, python, env = process.env, timeoutMs = 20000, find = findPython,
  port: want } = {}) {
  const own = python ? null : bundledPython(pythonRoot || riverRoot);
  const py = python || own || find();
  if (!py) throw new Error("MaximizePM needs Python 3.10 or newer. Install it (for example: brew install python), then open the app again.");
  const [pyCmd, ...pyArgs] = Array.isArray(py) ? py : [py];
  const bin = path.join(riverRoot, "bin", "maxpm");
  if (!fs.existsSync(bin)) throw new Error(`MaximizePM is missing from ${riverRoot}`);
  // The queue's serve_port when it is free (#5618), else any free port: serve names the port it took in
  // <queue>-serve.json, where every maxpm command reads it.
  const port = want && await portFree(want) ? want : await freePort();
  const child = spawn(pyCmd, [...pyArgs, bin, "serve", "--port", String(port)],
    { cwd: riverRoot, env: { ...env, MAXPM_DESKTOP: "1", PYTHONFAULTHANDLER: "1",
      // The app's own Python writes no .pyc files into the app bundle (they would break its signature).
      ...(own ? { PYTHONDONTWRITEBYTECODE: "1", PYTHONNOUSERSITE: "1" } : {}) }, stdio: ["ignore", "pipe", "pipe"] });
  let log = "";
  child.stdout.on("data", (d) => { log += d; });
  child.stderr.on("data", (d) => { log += d; });
  const url = `http://127.0.0.1:${port}/`;
  const stop = () => new Promise((resolve) => {
    if (child.exitCode !== null || child.signalCode) return resolve();
    child.once("exit", () => resolve());
    child.kill("SIGTERM");
    setTimeout(() => { if (child.exitCode === null) child.kill("SIGKILL"); }, 3000).unref();
  });
  const until = Date.now() + timeoutMs;
  while (Date.now() < until) {
    if (child.exitCode !== null) throw new Error(`maxpm serve stopped (exit ${child.exitCode}):\n${log.trim().slice(-800)}`);
    if (await get(url + "api/state") === 200) return { url, port, child, stop };
    await new Promise((r) => setTimeout(r, 200));
  }
  // A server that hangs: SIGABRT makes Python (PYTHONFAULTHANDLER) print where each thread is.
  if (child.exitCode === null) {
    await new Promise((resolve) => { child.once("exit", resolve); child.kill("SIGABRT"); setTimeout(resolve, 2000).unref(); });
  }
  await stop();
  throw new Error(`maxpm serve did not answer on ${url} within ${timeoutMs / 1000}s:\n${log.trim().slice(0, 4000)}`);
}

// The app's server (#5618): a serve that answers for the queue already is opened as it is (the app is then only a
// window, and quitting it leaves that serve running: stop does nothing); else the app starts its own, on the queue's
// serve_port when it is free. Resolves to { url, port, child (null for a serve the app did not start), stop, external }.
async function openRiver(opts = {}) {
  const own = opts.python ? null : bundledPython(opts.pythonRoot || opts.riverRoot);
  const py = opts.python || own || (opts.find || findPython)();
  let info = null;
  if (py) {
    try { info = queueInfo({ riverRoot: opts.riverRoot, python: Array.isArray(py) ? py : [py], env: opts.env || process.env }); }
    catch (e) { /* the code does not load: startRiver says why */ }
  }
  const running = info && await runningServe(info);
  if (running) return { ...running, child: null, external: true, stop: () => Promise.resolve() };
  const r = await startRiver({ ...opts, port: info ? info.port : undefined });
  return { ...r, external: false };
}

module.exports = { findPython, bundledPython, freePort, portFree, startRiver, openRiver, runningServe, queueInfo,
  appDir, codeVersion, markBad, pickRoot };
