// Downloads the Python the packaged app carries (python-build-standalone, CPython 3.13), so the app
// starts on a Mac with no Python of its own. Pinned by release and sha256; run before electron-builder
// (npm run dist does). Usage: node scripts/fetch-python.js [mac-arm64] [mac-x64] (default: both).
// Result: build/python/<os>-<arch>/python (with bin/python3), which electron-builder copies to river-app/python.
// The pins are in river/python-builds.txt, which install-standalone.sh reads too (#5873).
const { execFileSync } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const PINS = path.join(__dirname, "..", "..", "river", "python-builds.txt");

// river/python-builds.txt: "release <tag>", then "<os>-<arch> <file> <sha256>" lines; # starts a comment.
function readPins(text) {
  let release = null;
  const builds = {};
  for (const raw of text.split("\n")) {
    const words = raw.replace(/#.*/, "").trim().split(/\s+/).filter(Boolean);
    if (words[0] === "release" && words.length === 2) release = words[1];
    else if (words.length === 3 && /^[0-9a-f]{64}$/.test(words[2])) builds[words[0]] = { file: words[1], sha256: words[2] };
    else if (words.length) throw new Error(`${PINS}: a line it does not know: ${raw}`);
  }
  if (!release) throw new Error(`${PINS} names no release`);
  return { release, builds };
}

const { release: RELEASE, builds: BUILDS } = readPins(fs.readFileSync(PINS, "utf8"));
// Parts of the standard library river never uses (tests, the Tk GUI and IDLE, pip bootstrap).
const DROP = ["test", "idlelib", "tkinter", "turtledemo", "ensurepip", "lib2to3"];

const out = path.join(__dirname, "..", "build", "python");

function fetchOne(arch) {
  const b = BUILDS[arch];
  if (!b) throw new Error(`no Python build for ${arch}; known: ${Object.keys(BUILDS).join(", ")}`);
  const dir = path.join(out, arch), stamp = path.join(dir, "SHA256");
  if (fs.existsSync(stamp) && fs.readFileSync(stamp, "utf8").trim() === b.sha256) {
    console.log(`python ${arch}: already there`);
    return;
  }
  const url = `https://github.com/astral-sh/python-build-standalone/releases/download/${RELEASE}/${encodeURIComponent(b.file)}`;
  console.log(`python ${arch}: ${url}`);
  // curl: on every Mac and CI runner, and it follows the proxy settings.
  const data = execFileSync("curl", ["-fsSL", "--retry", "3", url], { maxBuffer: 200 * 1024 * 1024 });
  const got = crypto.createHash("sha256").update(data).digest("hex");
  if (got !== b.sha256) throw new Error(`sha256 mismatch for ${b.file}: got ${got}, want ${b.sha256}`);
  fs.rmSync(dir, { recursive: true, force: true });
  fs.mkdirSync(dir, { recursive: true });
  const tgz = path.join(dir, b.file);
  fs.writeFileSync(tgz, data);
  execFileSync("tar", ["-xzf", b.file], { cwd: dir });
  fs.rmSync(tgz);
  // The standard library is python/lib/python3.13; Tcl/Tk is beside it in python/lib.
  const lib = path.join(dir, "python", "lib",
    fs.readdirSync(path.join(dir, "python", "lib")).find((n) => /^python3\.\d+$/.test(n)));
  for (const d of DROP) fs.rmSync(path.join(lib, d), { recursive: true, force: true });
  for (const n of fs.readdirSync(path.join(dir, "python", "lib"))) {
    if (/^(tcl|tk|itcl|thread)\d/.test(n)) fs.rmSync(path.join(dir, "python", "lib", n), { recursive: true, force: true });
  }
  fs.writeFileSync(stamp, b.sha256 + "\n");
  console.log(`python ${arch}: ready in ${path.relative(process.cwd(), dir)}`);
}

try {
  const arches = process.argv.slice(2).length ? process.argv.slice(2) : Object.keys(BUILDS).filter((a) => a.startsWith("mac-"));
  for (const a of arches) fetchOne(a);
} catch (e) { console.error(e.message); process.exit(1); }
