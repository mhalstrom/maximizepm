// The app's own tmux (#6237): the build runs before each package, and the package puts it where maxpm looks.
const test = require("node:test");
const assert = require("node:assert");
const pkg = require("../package.json");
const { SOURCES, ARCHES, jemallocEnv, weakImports, NEWER_THAN_MIN } = require("../scripts/build-tmux");

test("both package steps build tmux for both chips", () => {
  for (const step of ["predist", "predist:signed"]) {
    assert.match(pkg.scripts[step], /node scripts\/build-tmux\.js mac-arm64 mac-x64/, step);
  }
  assert.deepStrictEqual(Object.keys(ARCHES).sort(), ["mac-arm64", "mac-x64"]);
});

test("the package copies the build to river-app/tmux, where core.bundled_tmux finds tmux/bin/tmux", () => {
  const entry = pkg.build.extraResources.find((r) => r.to === "river-app/tmux");
  assert.ok(entry, "no extraResources entry for river-app/tmux");
  assert.strictEqual(entry.from, "build/tmux/${os}-${arch}/tmux");
});

test("every source is pinned by sha256 and ships its license", () => {
  assert.deepStrictEqual(Object.keys(SOURCES).sort(), ["jemalloc", "libevent", "tmux", "utf8proc"]);
  for (const [name, s] of Object.entries(SOURCES)) {
    assert.match(s.sha256, /^[0-9a-f]{64}$/, name);
    assert.ok(s.license, name);
  }
});

test("configure may not use pipe2 or dup3, and the build stops on any other unguarded weak import (#6240)", () => {
  assert.deepStrictEqual(NEWER_THAN_MIN, { ac_cv_func_pipe2: "no", ac_cv_func_dup3: "no" });
  // nm -um of the tmux that stopped at its first server start on macOS 26.
  const nm = [
    "                 (undefined) external _malloc (from libSystem)",
    "                 (undefined) weak external _malloc_default_purgeable_zone (from libSystem)",
    "                 (undefined) weak external _pipe2 (from libSystem)",
  ].join("\n");
  assert.deepStrictEqual(weakImports(nm), ["pipe2"]);
  assert.deepStrictEqual(weakImports(nm.split("\n").slice(0, 2).join("\n")), []);
});

test("tmux links all of jemalloc, so its zone code replaces the calloc of macOS", () => {
  const env = jemallocEnv("/p");
  assert.strictEqual(env.JEMALLOC_LIBS, "-Wl,-force_load,/p/lib/libjemalloc.a");
  assert.strictEqual(env.JEMALLOC_CFLAGS, "-I/p/include");
});
