// The app's own tmux (#6237): the build runs before each package, and the package puts it where maxpm looks.
const test = require("node:test");
const assert = require("node:assert");
const pkg = require("../package.json");
const { SOURCES, ARCHES, jemallocEnv } = require("../scripts/build-tmux");

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

test("tmux links all of jemalloc, so its zone code replaces the calloc of macOS", () => {
  const env = jemallocEnv("/p");
  assert.strictEqual(env.JEMALLOC_LIBS, "-Wl,-force_load,/p/lib/libjemalloc.a");
  assert.strictEqual(env.JEMALLOC_CFLAGS, "-I/p/include");
});
