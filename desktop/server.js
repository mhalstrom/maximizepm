// Starts `maxpm serve` for the desktop app and stops it again. Plain Node, no Electron,
// so `npm test` can check it without downloading Electron.
const { spawn, execFileSync } = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");
const http = require("node:http");

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

// Start the server from the river folder (the repo, or the app's bundled copy). Resolves to
// { url, port, child, stop } once /api/state answers; rejects with a message a person can act on.
async function startRiver({ riverRoot, python, env = process.env, timeoutMs = 20000, find = findPython } = {}) {
  const own = python ? null : bundledPython(riverRoot);
  const py = python || own || find();
  if (!py) throw new Error("MaximizePM needs Python 3.10 or newer. Install it (for example: brew install python), then open the app again.");
  const [pyCmd, ...pyArgs] = Array.isArray(py) ? py : [py];
  const bin = path.join(riverRoot, "bin", "maxpm");
  if (!fs.existsSync(bin)) throw new Error(`MaximizePM is missing from ${riverRoot}`);
  const port = await freePort();
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

module.exports = { findPython, bundledPython, freePort, startRiver };
