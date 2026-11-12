// The manifest.json of a release (#5601), for the Update of the desktop app (river/release.py): the version, the code
// archive with its sha256, and the shell id of the app that the release builds.
// Usage: node scripts/manifest.js <version> <code archive> > manifest.json
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { shellId } = require("./shell-id");

const [version, archive] = process.argv.slice(2);
if (!/^\d+\.\d+\.\d+$/.test(version || "") || !archive) {
  console.error("usage: node scripts/manifest.js <version like 1.2.3> <code archive>");
  process.exit(2);
}
const data = fs.readFileSync(archive);
process.stdout.write(JSON.stringify({
  version,
  code: { name: path.basename(archive), sha256: crypto.createHash("sha256").update(data).digest("hex"), size: data.length },
  shell: shellId(),
}, null, 2) + "\n");
