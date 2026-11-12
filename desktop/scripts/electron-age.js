// The release check of the app's Electron (#5623): an old Electron carries an old Chromium, which misses its
// security fixes. Prints the Electron of package-lock.json and the newest stable one, and a GitHub warning when
// the app's is more than MAX_BEHIND major versions behind. It never stops a release: a failed lookup only says so.
// Usage: node scripts/electron-age.js
const fs = require("node:fs");
const path = require("node:path");

const MAX_BEHIND = 2;
const LATEST = "https://registry.npmjs.org/electron/latest";

const major = (v) => parseInt(String(v).replace(/^v/, ""), 10);

// How many major versions `current` is behind `latest` (0 when it is the same or newer).
function behind(current, latest) {
  return Math.max(0, major(latest) - major(current));
}

function lockedVersion(dir = path.join(__dirname, "..")) {
  const lock = JSON.parse(fs.readFileSync(path.join(dir, "package-lock.json"), "utf8"));
  return ((lock.packages || {})["node_modules/electron"] || {}).version || "";
}

// The text for the release log: a ::warning:: line when the Electron is too old, else a plain line.
function report(current, latest) {
  const n = behind(current, latest);
  if (n > MAX_BEHIND) {
    return `::warning::The app ships Electron ${current}, ${n} major versions behind ${latest}: raise it in desktop/package.json `
      + `(npm install --save-dev electron@^${major(latest)}) and run the desktop tests`;
  }
  return `Electron ${current} (newest stable ${latest}, ${n} major versions behind; the limit is ${MAX_BEHIND})`;
}

async function main() {
  const current = lockedVersion();
  let latest;
  try {
    const r = await fetch(LATEST, { signal: AbortSignal.timeout(15000) });
    latest = (await r.json()).version;
  } catch (e) {
    console.log(`Electron ${current}; the newest stable version could not be read (${e.message})`);
    return;
  }
  console.log(report(current, latest));
}

if (require.main === module) main();

module.exports = { behind, lockedVersion, report, MAX_BEHIND };
