// Builds the tmux the packaged app carries (#6237), so a person who installs the app needs no tmux: every agent
// session starts in a tmux pane. Pinned source releases (by sha256) of tmux, libevent, utf8proc and jemalloc; the
// three libraries are linked in statically, so the program needs only the libraries of macOS (libncurses, libSystem).
// tmux 3.8 on macOS asks for jemalloc, because the calloc of macOS does not always zero memory (its configure).
// Run before electron-builder (npm run dist does). Usage: node scripts/build-tmux.js [mac-arm64] [mac-x64]
// (default: both; a Mac with either chip builds both). Needs the Xcode command line tools (clang, make).
// Result: build/tmux/<os>-<arch>/tmux with bin/tmux and LICENSES/, which electron-builder copies to river-app/tmux.
// The default TERM inside a pane is screen-256color: macOS before 14 has no tmux-256color in its terminfo.
const { execFileSync } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const SOURCES = {
  tmux: { url: "https://github.com/tmux/tmux/releases/download/3.8/tmux-3.8.tar.gz", dir: "tmux-3.8",
    sha256: "e79c699c7e949dccd0a4a125e17b8d1261e311b16979dbc8eb34542f3966d82e", license: "COPYING" },
  libevent: { url: "https://github.com/libevent/libevent/releases/download/release-2.1.13-stable/libevent-2.1.13-stable.tar.gz",
    dir: "libevent-2.1.13-stable", sha256: "f7e9383b8c0baa81b687e5b5eecc01beefaf1b19b64151d95ed61647fe7a315c", license: "LICENSE" },
  utf8proc: { url: "https://github.com/JuliaStrings/utf8proc/archive/refs/tags/v2.12.0.tar.gz", dir: "utf8proc-2.12.0",
    sha256: "f564011d38b2888d583d510b08e69ffa15aa117155db1b9b49ef1dfe1fa25111", license: "LICENSE.md" },
  jemalloc: { url: "https://github.com/jemalloc/jemalloc/releases/download/5.3.1/jemalloc-5.3.1.tar.bz2", dir: "jemalloc-5.3.1",
    sha256: "3826bc80232f22ed5c4662f3034f799ca316e819103bdc7bb99018a421706f92", license: "COPYING" },
};
// jemalloc's page size, fixed because a build for the other chip cannot measure it: 16 KB, the page of Apple silicon.
// A larger page than the system's works (Intel Macs and Rosetta have 4 KB); a smaller one stops the program.
const LG_PAGE = 14;
const ARCHES = { "mac-arm64": { arch: "arm64", host: "aarch64-apple-darwin" },
  "mac-x64": { arch: "x86_64", host: "x86_64-apple-darwin" } };
const MIN_MACOS = "12.0";  // the oldest macOS that Electron 45 runs on
// The stamp of a finished build: the pins and the flags; a change of either builds again.
const STAMP = crypto.createHash("sha256").update(JSON.stringify([SOURCES, MIN_MACOS, "screen-256color", LG_PAGE])).digest("hex");

const top = path.join(__dirname, "..", "build", "tmux");

function sh(cmd, args, opts) {
  execFileSync(cmd, args, { stdio: ["ignore", "inherit", "inherit"], ...opts });
}

// Each source archive once, checked against its pin.
function source(name) {
  const s = SOURCES[name], file = path.join(top, "src", path.basename(s.url));
  if (!fs.existsSync(file)) {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    console.log(`tmux: ${s.url}`);
    // curl: on every Mac and CI runner, and it follows the proxy settings.
    fs.writeFileSync(file + ".part", execFileSync("curl", ["-fsSL", "--retry", "3", s.url], { maxBuffer: 64 * 1024 * 1024 }));
    fs.renameSync(file + ".part", file);
  }
  const got = crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
  if (got !== s.sha256) { fs.rmSync(file); throw new Error(`sha256 mismatch for ${s.url}: got ${got}, want ${s.sha256}`); }
  return file;
}

function buildOne(key) {
  const a = ARCHES[key];
  if (!a) throw new Error(`no tmux build for ${key}; known: ${Object.keys(ARCHES).join(", ")}`);
  const out = path.join(top, key), stamp = path.join(out, "STAMP");
  if (fs.existsSync(stamp) && fs.readFileSync(stamp, "utf8").trim() === STAMP) {
    console.log(`tmux ${key}: already there`);
    return;
  }
  const work = path.join(top, "work", key), prefix = path.join(work, "prefix");
  fs.rmSync(work, { recursive: true, force: true });
  fs.mkdirSync(prefix, { recursive: true });
  for (const name of Object.keys(SOURCES)) sh("tar", ["-xf", source(name)], { cwd: work });  // .tar.gz and .tar.bz2
  const flags = `-arch ${a.arch} -mmacosx-version-min=${MIN_MACOS} -O2`;
  const env = { ...process.env, CC: "clang", CFLAGS: flags, MACOSX_DEPLOYMENT_TARGET: MIN_MACOS,
    // No pkg-config: the libraries come from the prefix only, never from Homebrew's.
    PKG_CONFIG: "false", PKG_CONFIG_PATH: "", CPPFLAGS: `-I${prefix}/include`, LDFLAGS: `-arch ${a.arch} -L${prefix}/lib` };
  const host = ["--build=" + (process.arch === "arm64" ? "aarch64" : "x86_64") + "-apple-darwin", "--host=" + a.host];

  const u = path.join(work, SOURCES.utf8proc.dir);
  sh("make", ["libutf8proc.a", `CC=clang`, `CFLAGS=${flags}`], { cwd: u, env });
  fs.mkdirSync(path.join(prefix, "lib"), { recursive: true });
  fs.mkdirSync(path.join(prefix, "include"), { recursive: true });
  fs.copyFileSync(path.join(u, "libutf8proc.a"), path.join(prefix, "lib", "libutf8proc.a"));
  fs.copyFileSync(path.join(u, "utf8proc.h"), path.join(prefix, "include", "utf8proc.h"));

  const e = path.join(work, SOURCES.libevent.dir);
  sh("./configure", [...host, `--prefix=${prefix}`, "--disable-shared", "--enable-static", "--disable-openssl",
    "--disable-samples", "--disable-libevent-regress", "--disable-debug-mode", "--disable-dependency-tracking"], { cwd: e, env });
  sh("make", ["-j8", "install"], { cwd: e, env });
  // Only the static libraries are in the prefix, so -levent_core and -lutf8proc link them in.
  for (const f of fs.readdirSync(path.join(prefix, "lib"))) if (f.endsWith(".dylib")) fs.rmSync(path.join(prefix, "lib", f));

  // jemalloc: only the static library and the headers (its docs need tools a Mac may not have). On macOS it registers
  // itself as the default malloc zone, so malloc and calloc of tmux and its libraries use it.
  const j = path.join(work, SOURCES.jemalloc.dir);
  sh("./configure", [...host, `--prefix=${prefix}`, `--with-lg-page=${LG_PAGE}`, "--disable-cxx", "--disable-shared",
    "--disable-dependency-tracking"], { cwd: j, env });
  sh("make", ["-j8", "build_lib_static"], { cwd: j, env });
  sh("make", ["install_lib_static", "install_include"], { cwd: j, env });

  const t = path.join(work, SOURCES.tmux.dir);
  // tmux finds jemalloc only with pkg-config: these two variables stand in for it (the static library by its path).
  sh("./configure", [...host, `--prefix=${prefix}`, "--enable-utf8proc", "--enable-jemalloc", "--with-TERM=screen-256color",
    "--disable-dependency-tracking"], { cwd: t, env: { ...env, JEMALLOC_CFLAGS: `-I${prefix}/include`,
      JEMALLOC_LIBS: path.join(prefix, "lib", "libjemalloc.a") } });
  sh("make", ["-j8"], { cwd: t, env });

  // The program may use only the libraries every Mac has.
  const exe = path.join(t, "tmux");
  const libs = execFileSync("otool", ["-L", exe], { encoding: "utf8" }).split("\n").slice(1).map((l) => l.trim().split(" ")[0]).filter(Boolean);
  const outside = libs.filter((l) => !l.startsWith("/usr/lib/") && !l.startsWith("/System/"));
  if (outside.length) throw new Error(`tmux ${key} links libraries a Mac does not have: ${outside.join(", ")}`);
  const kind = execFileSync("lipo", ["-archs", exe], { encoding: "utf8" }).trim();
  if (kind !== a.arch) throw new Error(`tmux ${key} is built for ${kind}, not ${a.arch}`);
  // The build for this Mac's own chip starts (jemalloc's page size, the libraries): tmux -V prints its version.
  if ((process.arch === "arm64" ? "arm64" : "x86_64") === a.arch) {
    const v = execFileSync(exe, ["-V"], { encoding: "utf8" }).trim();
    if (!v.startsWith("tmux ")) throw new Error(`tmux ${key} -V printed ${v}`);
    console.log(`tmux ${key}: ${v} starts`);
  }

  fs.rmSync(out, { recursive: true, force: true });
  const bin = path.join(out, "tmux", "bin"), lic = path.join(out, "tmux", "LICENSES");
  fs.mkdirSync(bin, { recursive: true });
  fs.mkdirSync(lic, { recursive: true });
  fs.copyFileSync(exe, path.join(bin, "tmux"));
  fs.chmodSync(path.join(bin, "tmux"), 0o755);
  for (const [name, s] of Object.entries(SOURCES)) fs.copyFileSync(path.join(work, s.dir, s.license), path.join(lic, `${name}-${s.license}`));
  fs.writeFileSync(path.join(out, "tmux", "VERSION"), Object.values(SOURCES).map((s) => s.dir).join("\n") + "\n");
  fs.writeFileSync(stamp, STAMP + "\n");
  fs.rmSync(work, { recursive: true, force: true });
  console.log(`tmux ${key}: ready in ${path.relative(process.cwd(), path.join(out, "tmux"))}`);
}

if (require.main === module) {
  try {
    const keys = process.argv.slice(2).length ? process.argv.slice(2) : Object.keys(ARCHES);
    for (const k of keys) buildOne(k);
  } catch (e) { console.error(e.message); process.exit(1); }
}

module.exports = { SOURCES, ARCHES };
