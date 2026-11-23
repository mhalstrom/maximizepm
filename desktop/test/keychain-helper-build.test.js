// Build, manifest, and signing config checks for the macOS Keychain helper (#5838).
const test = require("node:test");
const assert = require("node:assert");
const { execFileSync, spawnSync } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const desktopDir = path.join(__dirname, "..");
const buildScript = path.join(desktopDir, "scripts", "build-keychain-helper.sh");
const manifestScript = path.join(desktopDir, "scripts", "manifest.js");

test("signed.config.js enables hardened runtime and skips re-signing the Keychain helper", () => {
  const cfg = require("../signed.config.js");
  assert.strictEqual(cfg.mac.hardenedRuntime, true);
  assert.strictEqual(cfg.forceCodeSigning, true);
  assert.strictEqual(cfg.mac.identity, undefined);
  assert.ok(Array.isArray(cfg.mac.signIgnore) && cfg.mac.signIgnore.length > 0, "mac.signIgnore is configured");
  const samplePath = "/tmp/MaximizePM.app/Contents/Resources/river-app/river/bin/maxpm-keychain-helper";
  assert.ok(
    cfg.mac.signIgnore.some((pat) => new RegExp(pat).test(samplePath)),
    "mac.signIgnore matches river/bin/maxpm-keychain-helper",
  );
});

test("manifest.js records keychain_helper metadata when provided and stays backward-compatible without it", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "maxpm-manifest-"));
  try {
    const codeArchive = path.join(dir, "MaximizePM-1.6.0-code.tar.gz");
    const helperArchive = path.join(dir, "MaximizePM-1.6.0-keychain-helper-macos.tar.gz");
    fs.writeFileSync(codeArchive, "code-bytes");
    fs.writeFileSync(helperArchive, "helper-bytes");

    const plain = JSON.parse(execFileSync(process.execPath, [manifestScript, "1.6.0", codeArchive], { encoding: "utf8" }));
    assert.strictEqual(plain.version, "1.6.0");
    assert.strictEqual(plain.keychain_helper, undefined);

    const withHelper = JSON.parse(execFileSync(
      process.execPath,
      [manifestScript, "1.6.0", codeArchive, helperArchive, "CWYV4R338W"],
      { encoding: "utf8" },
    ));
    assert.deepStrictEqual(withHelper.keychain_helper, {
      name: "MaximizePM-1.6.0-keychain-helper-macos.tar.gz",
      sha256: crypto.createHash("sha256").update("helper-bytes").digest("hex"),
      size: Buffer.byteLength("helper-bytes"),
      identifier: "com.maximizepm.keychain-helper",
      team_id: "CWYV4R338W",
    });

    const unsigned = JSON.parse(execFileSync(
      process.execPath,
      [manifestScript, "1.6.0", codeArchive, helperArchive, ""],
      { encoding: "utf8" },
    ));
    assert.strictEqual(unsigned.keychain_helper.team_id, null);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test("build-keychain-helper.sh builds a universal binary with stable identifier and rejects invalid signatures", { skip: process.platform !== "darwin" }, () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "maxpm-kh-build-"));
  try {
    const helper = path.join(dir, "maxpm-keychain-helper");
    execFileSync(buildScript, ["build", helper], { stdio: "pipe" });
    execFileSync(buildScript, ["verify", helper], { stdio: "pipe" });

    // Ad-hoc build must fail --signed verification (no Developer ID / TeamIdentifier / runtime).
    const signedCheck = spawnSync(buildScript, ["verify", "--signed", helper], { encoding: "utf8" });
    assert.notStrictEqual(signedCheck.status, 0, "ad-hoc helper must fail --signed verify");

    // Re-signing with a wrong identifier must fail verify.
    const wrongId = path.join(dir, "wrong-id");
    fs.copyFileSync(helper, wrongId);
    fs.chmodSync(wrongId, 0o755);
    execFileSync("codesign", ["--force", "--sign", "-", "--identifier", "com.example.other", wrongId], { stdio: "pipe" });
    const wrongIdCheck = spawnSync(buildScript, ["verify", wrongId], { encoding: "utf8" });
    assert.notStrictEqual(wrongIdCheck.status, 0, "wrong identifier must fail verify");

    // Usage check on built universal binary exits 64.
    const run = spawnSync(helper, [], { encoding: "utf8" });
    assert.strictEqual(run.status, 64);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
