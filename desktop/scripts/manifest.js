// The manifest.json of a release (#5601), for the Update of the desktop app (river/release.py) and terminal installs
// of the signed Keychain helper (#5838): the version, the code archive with its sha256, optional keychain_helper
// archive metadata (name, sha256, size, identifier, team_id), and the shell id of the app that the release builds.
// Usage: node scripts/manifest.js <version> <code archive> [keychain helper archive] [team id] > manifest.json
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { shellId } = require("./shell-id");

const KEYCHAIN_HELPER_IDENTIFIER = "com.maximizepm.keychain-helper";

const [version, archive, helperArchive, teamId] = process.argv.slice(2);
if (!/^\d+\.\d+\.\d+$/.test(version || "") || !archive) {
  console.error("usage: node scripts/manifest.js <version like 1.2.3> <code archive> [keychain helper archive] [team id]");
  process.exit(2);
}
const data = fs.readFileSync(archive);
const out = {
  version,
  code: { name: path.basename(archive), sha256: crypto.createHash("sha256").update(data).digest("hex"), size: data.length },
  shell: shellId(),
};
if (helperArchive) {
  const hdata = fs.readFileSync(helperArchive);
  out.keychain_helper = {
    name: path.basename(helperArchive),
    sha256: crypto.createHash("sha256").update(hdata).digest("hex"),
    size: hdata.length,
    identifier: KEYCHAIN_HELPER_IDENTIFIER,
    team_id: teamId || null,
  };
}
process.stdout.write(JSON.stringify(out, null, 2) + "\n");
