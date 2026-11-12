// Smoke test: the app's server starter runs maxpm serve from this repo and stops it again.
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const http = require("node:http");
const { findPython, startRiver } = require("../server");

const repo = path.join(__dirname, "..", "..");
const fetchJson = (url) => new Promise((resolve, reject) => {
  http.get(url, (res) => { let b = ""; res.on("data", (d) => { b += d; }); res.on("end", () => resolve(JSON.parse(b))); }).on("error", reject);
});

test("finds a Python 3.10+", () => {
  const py = findPython();
  assert.ok(Array.isArray(py) && py.length >= 1, "Python 3.10 or newer is on this machine");
  assert.strictEqual(findPython(["/no/such/python", ["/no/such/py", "-3"]]), null);
});

test("starts maxpm serve on a free port, in desktop mode, and stops it", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "river-desktop-"));
  const r = await startRiver({ riverRoot: repo, env: { ...process.env, MAXPM_DB: path.join(dir, "t.db") } });
  try {
    const st = await fetchJson(r.url + "api/state");
    assert.strictEqual(st.desktop, true);
    const up = await fetchJson(r.url + "api/update");
    assert.strictEqual(up.git, false);  // the page hides its git Update button
    assert.strictEqual(up.desktop, true);
  } finally {
    await r.stop();
  }
  assert.ok(r.child.exitCode !== null || r.child.signalCode, "the server process ended");
  fs.rmSync(dir, { recursive: true, force: true });
});

test("says clearly when Python is missing", async () => {
  await assert.rejects(startRiver({ riverRoot: repo, find: () => null }), /needs Python 3\.10 or newer/);
});

test("a packaged app starts MaximizePM with the Python it carries, without looking for one", async () => {
  // A copy of the app's river-app folder: river and bin from the repo, python/bin/python3 = this machine's Python.
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "river-app-"));
  fs.symlinkSync(path.join(repo, "river"), path.join(root, "river"));
  fs.symlinkSync(path.join(repo, "bin"), path.join(root, "bin"));
  fs.mkdirSync(path.join(root, "python", "bin"), { recursive: true });
  const real = require("node:child_process").execFileSync(findPython()[0], ["-c", "import sys; print(sys.executable)"], { encoding: "utf8" }).trim();
  fs.symlinkSync(real, path.join(root, "python", "bin", "python3"));
  const r = await startRiver({ riverRoot: root, find: () => null, env: { ...process.env, MAXPM_DB: path.join(root, "t.db") } });
  try {
    assert.strictEqual((await fetchJson(r.url + "api/state")).desktop, true);
    assert.strictEqual(r.child.spawnargs[0], path.join(root, "python", "bin", "python3"));
  } finally {
    await r.stop();
    fs.rmSync(root, { recursive: true, force: true });
  }
});
