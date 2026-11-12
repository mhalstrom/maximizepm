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

// The Update of the page (#5601) puts newer code in the app's folder; the app starts the newest good version.
test("the app starts the newest good version of the code, and never one that failed", () => {
  const { pickRoot, markBad } = require("../server");
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "river-versions-"));
  const code = (root, v) => {
    fs.mkdirSync(path.join(root, "river"), { recursive: true });
    fs.mkdirSync(path.join(root, "bin"), { recursive: true });
    fs.writeFileSync(path.join(root, "river", "__init__.py"), `__version__ = "${v}"\n`);
    fs.writeFileSync(path.join(root, "bin", "maxpm"), "");
    return root;
  };
  try {
    const bundle = code(path.join(tmp, "bundle"), "1.0.0"), dir = path.join(tmp, "app");
    assert.strictEqual(pickRoot({ bundle, dir }), bundle);  // no updates yet
    assert.strictEqual(fs.readFileSync(path.join(dir, "current"), "utf8").trim(), bundle);
    const v11 = code(path.join(dir, "versions", "1.1.0"), "1.1.0"), v12 = code(path.join(dir, "versions", "1.2.0"), "1.2.0");
    code(path.join(dir, "versions", "1.10.0-half"), "1.10.0");
    fs.rmSync(path.join(dir, "versions", "1.10.0-half", "bin"), { recursive: true });  // no bin/maxpm: skipped
    assert.strictEqual(pickRoot({ bundle, dir }), v12);
    // An Update to 1.2.0 that never answered (trying is still there): 1.2.0 is bad, 1.1.0 starts.
    fs.writeFileSync(path.join(dir, "trying"), v12 + "\n");
    assert.strictEqual(pickRoot({ bundle, dir }), v11);
    assert.ok(fs.existsSync(path.join(v12, ".bad")) && !fs.existsSync(path.join(dir, "trying")));
    assert.strictEqual(fs.readFileSync(path.join(dir, "current"), "utf8").trim(), v11);
    // A new app with newer code than the versions: its own copy wins.
    fs.writeFileSync(path.join(bundle, "river", "__init__.py"), '__version__ = "2.0.0"\n');
    assert.strictEqual(pickRoot({ bundle, dir }), bundle);
    assert.strictEqual(markBad(bundle, dir), false);  // the app's own copy is never marked bad
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("a version in the app's folder starts with the Python of the app's bundle", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "river-split-"));
  const bundle = path.join(tmp, "bundle");
  fs.mkdirSync(path.join(bundle, "python", "bin"), { recursive: true });
  const real = require("node:child_process").execFileSync(findPython()[0], ["-c", "import sys; print(sys.executable)"], { encoding: "utf8" }).trim();
  fs.symlinkSync(real, path.join(bundle, "python", "bin", "python3"));
  const r = await startRiver({ riverRoot: repo, pythonRoot: bundle, find: () => null,
    env: { ...process.env, MAXPM_DB: path.join(tmp, "t.db") } });
  try {
    assert.strictEqual(r.child.spawnargs[0], path.join(bundle, "python", "bin", "python3"));
    assert.strictEqual((await fetchJson(r.url + "api/update")).release.current.split(".").length, 3);
  } finally {
    await r.stop();
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("the shell id ignores the version of package.json", () => {
  const { shellId } = require("../scripts/shell-id");
  assert.match(shellId(), /^[0-9a-f]{16}$/);
});

// #5618: the app opens a serve that answers for its queue already, and else starts its own on serve_port (or a free
// port), which every maxpm command then finds in <queue>-serve.json.
const { openRiver, freePort: freePortOf } = require("../server");
const net = require("node:net");
const setPort = (db, port) => require("node:child_process").execFileSync(findPython()[0],
  [path.join(repo, "bin", "maxpm"), "config", "set", "serve_port", String(port)],
  { env: { ...process.env, MAXPM_DB: db, MAXPM_LOCAL: "1", MAXPM_AGENT: "" }, stdio: "ignore" });

test("the app opens a serve that runs for its queue, and quitting it leaves that serve running", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "river-open-"));
  const env = { ...process.env, MAXPM_DB: path.join(dir, "q.db") };
  const login = await startRiver({ riverRoot: repo, env });  // the login serve (random port, its serve file names it)
  try {
    const r = await openRiver({ riverRoot: repo, env });
    assert.strictEqual(r.external, true);
    assert.strictEqual(r.port, login.port);
    assert.strictEqual(r.child, null);
    await r.stop();
    assert.strictEqual(login.child.exitCode, null, "the serve the app did not start still runs");
    // Another queue's serve is not opened: that app starts its own.
    const other = { ...process.env, MAXPM_DB: path.join(dir, "other.db") };
    const o = await openRiver({ riverRoot: repo, env: other });
    try {
      assert.strictEqual(o.external, false);
      assert.notStrictEqual(o.port, login.port);
    } finally { await o.stop(); }
  } finally {
    await login.stop();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("alone, the app starts its serve on serve_port, or on a free port that its serve file names", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "river-own-"));
  const db = path.join(dir, "q.db"), env = { ...process.env, MAXPM_DB: db };
  const want = await freePortOf();
  setPort(db, want);
  let r = await openRiver({ riverRoot: repo, env });
  try {
    assert.strictEqual(r.external, false);
    assert.strictEqual(r.port, want);
    assert.strictEqual(JSON.parse(fs.readFileSync(db + "-serve.json", "utf8")).port, want);
  } finally { await r.stop(); }
  // serve_port taken by another program: a free port, named in the serve file.
  const blocker = net.createServer().listen(want, "127.0.0.1");
  await new Promise((res) => blocker.once("listening", res));
  r = await openRiver({ riverRoot: repo, env });
  try {
    assert.strictEqual(r.external, false);
    assert.notStrictEqual(r.port, want);
    assert.strictEqual(JSON.parse(fs.readFileSync(db + "-serve.json", "utf8")).port, r.port);
  } finally {
    await r.stop();
    blocker.close();
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
