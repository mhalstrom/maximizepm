// The release check of the app's Electron version (#5623).
const test = require("node:test");
const assert = require("node:assert");
const { behind, lockedVersion, report, MAX_BEHIND } = require("../scripts/electron-age");

test("behind counts major versions, never below 0", () => {
  assert.strictEqual(behind("38.8.6", "45.3.1"), 7);
  assert.strictEqual(behind("45.3.1", "45.3.1"), 0);
  assert.strictEqual(behind("46.0.0", "45.3.1"), 0);
});

test("the report warns only beyond the limit", () => {
  assert.match(report("38.8.6", "45.3.1"), /^::warning::.*Electron 38\.8\.6, 7 major versions behind 45\.3\.1/);
  assert.doesNotMatch(report(`${45 - MAX_BEHIND}.0.0`, "45.3.1"), /::warning::/);
});

test("the lock holds an Electron the check can read", () => {
  assert.match(lockedVersion(), /^\d+\.\d+\.\d+$/);
});
