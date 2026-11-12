// The id of the app shell (#5601): a hash of what the .app carries apart from the MaximizePM code: its Electron
// files, its Electron version, its build settings and its Python. An Update of the page swaps only the code; when a
// release has another shell id than the running app (its manifest.json says it), the page offers the new .dmg.
// Usage: node scripts/shell-id.js [--write]  (--write: build/shell-id.txt, which the build copies into the app).
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const here = path.join(__dirname, "..");

function shellId() {
  const h = crypto.createHash("sha256");
  for (const f of ["main.js", "preload.js", "server.js", "scripts/fetch-python.js"]) {
    h.update(f + "\0" + fs.readFileSync(path.join(here, f)) + "\0");
  }
  const pkg = JSON.parse(fs.readFileSync(path.join(here, "package.json"), "utf8"));
  delete pkg.version;  // each release sets it from the tag
  h.update(JSON.stringify(pkg) + "\0");
  const lock = JSON.parse(fs.readFileSync(path.join(here, "package-lock.json"), "utf8"));
  h.update(String(((lock.packages || {})["node_modules/electron"] || {}).version || ""));
  return h.digest("hex").slice(0, 16);
}

if (require.main === module) {
  const id = shellId();
  if (process.argv.includes("--write")) {
    fs.mkdirSync(path.join(here, "build"), { recursive: true });
    fs.writeFileSync(path.join(here, "build", "shell-id.txt"), id + "\n");
  }
  console.log(id);
}

module.exports = { shellId };
