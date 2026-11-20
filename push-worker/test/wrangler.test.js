import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { after, before, describe, it } from "node:test";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const workerDir = path.resolve(__dirname, "..");

const TEST_TIMEOUT_MS = 60_000;
const STARTUP_TIMEOUT_MS = 30_000;
const FETCH_TIMEOUT_MS = 10_000;

const ecPair = crypto.generateKeyPairSync("ec", {
  namedCurve: "prime256v1",
  publicKeyEncoding: { type: "spki", format: "pem" },
  privateKeyEncoding: { type: "pkcs8", format: "pem" },
});

const rsaPair = crypto.generateKeyPairSync("rsa", {
  modulusLength: 2048,
  publicKeyEncoding: { type: "spki", format: "pem" },
  privateKeyEncoding: { type: "pkcs8", format: "pem" },
});

function base64UrlDecode(str) {
  const padded = str.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (str.length % 4)) % 4);
  return Buffer.from(padded, "base64");
}

function parseJwt(token) {
  const parts = token.split(".");
  return {
    header: JSON.parse(base64UrlDecode(parts[0]).toString("utf8")),
    payload: JSON.parse(base64UrlDecode(parts[1]).toString("utf8")),
    signingInput: `${parts[0]}.${parts[1]}`,
    signature: base64UrlDecode(parts[2]),
  };
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

async function waitForHealth(baseUrl, proc, timeoutMs = STARTUP_TIMEOUT_MS) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (proc.exitCode !== null || proc.signalCode !== null) {
      throw new Error(
        `wrangler dev exited early (code=${proc.exitCode}, signal=${proc.signalCode})`,
      );
    }
    try {
      const res = await fetch(`${baseUrl}/health`, {
        signal: AbortSignal.timeout(2_000),
      });
      if (res.status === 200) return;
    } catch {
      // Not ready yet.
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error(`wrangler dev did not become healthy within ${timeoutMs}ms`);
}

async function stopProcess(proc, graceMs = 5_000) {
  if (!proc || proc.exitCode !== null || proc.signalCode !== null) return;
  const exited = new Promise((resolve) => proc.once("exit", resolve));
  proc.kill("SIGTERM");
  const timer = setTimeout(() => {
    if (proc.exitCode === null && proc.signalCode === null) {
      proc.kill("SIGKILL");
    }
  }, graceMs);
  timer.unref();
  await Promise.race([
    exited,
    new Promise((resolve) => setTimeout(resolve, graceMs + 2_000).unref()),
  ]);
  clearTimeout(timer);
}

describe("wrangler dev integration with fake APNs and FCM providers", { timeout: TEST_TIMEOUT_MS }, () => {
  let providerServer;
  let providerPort;
  let wranglerProc;
  let wranglerPort;
  let tmpDir;
  const recorded = { apns: [], oauth: [], fcm: [] };

  before(
    async () => {
      providerServer = http.createServer(async (req, res) => {
        const bodyText = await readBody(req);

        if (req.url.startsWith("/3/device/")) {
          const token = decodeURIComponent(req.url.slice("/3/device/".length));
          const auth = req.headers.authorization || "";
          const jwt = parseJwt(auth.replace(/^bearer /i, ""));
          const sigOk = crypto.verify(
            "sha256",
            Buffer.from(jwt.signingInput),
            { key: ecPair.publicKey, dsaEncoding: "ieee-p1363" },
            jwt.signature,
          );
          recorded.apns.push({
            token,
            sigOk,
            headers: req.headers,
            body: JSON.parse(bodyText),
          });

          if (!sigOk) {
            res.writeHead(403, { "content-type": "application/json" });
            res.end(JSON.stringify({ reason: "InvalidProviderToken" }));
            return;
          }
          if (token === "dead-ios-token") {
            res.writeHead(410, { "content-type": "application/json" });
            res.end(JSON.stringify({ reason: "Unregistered" }));
            return;
          }
          res.writeHead(200, { "apns-id": "fake-apns-id-1" });
          res.end();
          return;
        }

        if (req.url === "/oauth/token") {
          const params = new URLSearchParams(bodyText);
          const jwt = parseJwt(params.get("assertion"));
          const sigOk = crypto.verify("sha256", Buffer.from(jwt.signingInput), rsaPair.publicKey, jwt.signature);
          recorded.oauth.push({ sigOk, payload: jwt.payload });
          if (!sigOk) {
            res.writeHead(401, { "content-type": "application/json" });
            res.end(JSON.stringify({ error: "invalid_grant" }));
            return;
          }
          res.writeHead(200, { "content-type": "application/json" });
          res.end(JSON.stringify({ access_token: "fake-fcm-access-token", expires_in: 3600 }));
          return;
        }

        if (req.url === "/v1/projects/maximizepm-test/messages:send") {
          const auth = req.headers.authorization || "";
          const body = JSON.parse(bodyText);
          recorded.fcm.push({ auth, body });
          if (auth !== "Bearer fake-fcm-access-token") {
            res.writeHead(401, { "content-type": "application/json" });
            res.end(JSON.stringify({ error: { code: 401, status: "UNAUTHENTICATED" } }));
            return;
          }
          if (body.message.token === "dead-android-token") {
            res.writeHead(404, { "content-type": "application/json" });
            res.end(
              JSON.stringify({
                error: {
                  code: 404,
                  status: "NOT_FOUND",
                  details: [{ "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", errorCode: "UNREGISTERED" }],
                },
              }),
            );
            return;
          }
          res.writeHead(200, { "content-type": "application/json" });
          res.end(JSON.stringify({ name: "projects/maximizepm-test/messages/fake-1" }));
          return;
        }

        res.writeHead(404);
        res.end();
      });

      await new Promise((resolve) => providerServer.listen(0, "127.0.0.1", resolve));
      providerPort = providerServer.address().port;

      // Pick a free port for wrangler dev.
      const probe = http.createServer();
      await new Promise((resolve) => probe.listen(0, "127.0.0.1", resolve));
      wranglerPort = probe.address().port;
      await new Promise((resolve) => probe.close(resolve));

      tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "maxpm-push-worker-"));
      const varsFile = path.join(tmpDir, "test.vars");
      const providerBase = `http://127.0.0.1:${providerPort}`;
      const envLines = [
        `APNS_PRIVATE_KEY=${JSON.stringify(ecPair.privateKey)}`,
        `APNS_KEY_ID="KEY1234567"`,
        `APNS_TEAM_ID="TEAM123456"`,
        `APNS_BUNDLE_ID="com.toolscaled.maximizepm"`,
        `APNS_BASE_URL="${providerBase}"`,
        `FCM_PRIVATE_KEY=${JSON.stringify(rsaPair.privateKey)}`,
        `FCM_CLIENT_EMAIL="push@maximizepm-test.iam.gserviceaccount.com"`,
        `FCM_PROJECT_ID="maximizepm-test"`,
        `FCM_BASE_URL="${providerBase}"`,
        `FCM_TOKEN_URL="${providerBase}/oauth/token"`,
      ];
      fs.writeFileSync(varsFile, envLines.join("\n") + "\n", "utf8");

      const wranglerBin = path.join(workerDir, "node_modules", ".bin", "wrangler");
      wranglerProc = spawn(
        wranglerBin,
        [
          "dev",
          "--local",
          "--show-interactive-dev-session=false",
          "--ip",
          "127.0.0.1",
          "--port",
          String(wranglerPort),
          "--inspector-port",
          "0",
          "--env-file",
          varsFile,
          "--persist-to",
          path.join(tmpDir, "state"),
        ],
        {
          cwd: workerDir,
          stdio: ["ignore", "pipe", "pipe"],
          env: {
            ...process.env,
            HOME: tmpDir,
            XDG_CONFIG_HOME: tmpDir,
            WRANGLER_LOG_PATH: path.join(tmpDir, "wrangler.log"),
            CI: "true",
            WRANGLER_SEND_METRICS: "false",
          },
        },
      );

      let stdoutLog = "";
      let stderrLog = "";
      wranglerProc.stdout.on("data", (d) => {
        stdoutLog += d.toString();
      });
      wranglerProc.stderr.on("data", (d) => {
        stderrLog += d.toString();
      });

      try {
        await waitForHealth(`http://127.0.0.1:${wranglerPort}`, wranglerProc, STARTUP_TIMEOUT_MS);
      } catch (err) {
        await stopProcess(wranglerProc);
        throw new Error(`${err.message}\nwrangler stdout:\n${stdoutLog}\nwrangler stderr:\n${stderrLog}`);
      }
    },
    { timeout: STARTUP_TIMEOUT_MS + 15_000 },
  );

  after(
    async () => {
      await stopProcess(wranglerProc);
      if (providerServer) {
        providerServer.closeAllConnections?.();
        await new Promise((resolve) => providerServer.close(resolve));
      }
      if (tmpDir) {
        fs.rmSync(tmpDir, { recursive: true, force: true });
      }
    },
    { timeout: 15_000 },
  );

  it(
    "relays an iOS alert notice and verifies the ES256 signature at fake APNs",
    { timeout: TEST_TIMEOUT_MS },
    async () => {
      const res = await fetch(`http://127.0.0.1:${wranglerPort}/push`, {
        method: "POST",
        headers: { "content-type": "application/json", "CF-Connecting-IP": "198.51.100.1" },
        body: JSON.stringify({
          platform: "ios",
          token: "ios-live-token",
          ciphertext: "ZW5jcnlwdGVkLWlvcw",
          collapse_id: "sess-1",
        }),
        signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      });
      assert.equal(res.status, 200);
      assert.deepEqual(await res.json(), {
        ok: true,
        platform: "ios",
        provider_status: 200,
        provider_id: "fake-apns-id-1",
      });

      const hit = recorded.apns.at(-1);
      assert.equal(hit.sigOk, true);
      assert.equal(hit.token, "ios-live-token");
      assert.equal(hit.headers["apns-push-type"], "alert");
      assert.equal(hit.headers["apns-collapse-id"], "sess-1");
      assert.equal(hit.body.ciphertext, "ZW5jcnlwdGVkLWlvcw");
    },
  );

  it("relays an iOS Live Activity push", { timeout: TEST_TIMEOUT_MS }, async () => {
    const res = await fetch(`http://127.0.0.1:${wranglerPort}/push`, {
      method: "POST",
      headers: { "content-type": "application/json", "CF-Connecting-IP": "198.51.100.2" },
      body: JSON.stringify({
        platform: "ios",
        token: "ios-la-token",
        live_activity: {
          event: "update",
          content_state: { waitingCount: 2 },
        },
      }),
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    });
    assert.equal(res.status, 200);
    const hit = recorded.apns.at(-1);
    assert.equal(hit.sigOk, true);
    assert.equal(hit.headers["apns-push-type"], "liveactivity");
    assert.equal(hit.headers["apns-topic"], "com.toolscaled.maximizepm.push-type.liveactivity");
    assert.deepEqual(hit.body.aps["content-state"], { waitingCount: 2 });
  });

  it("reports a dead iOS token with HTTP 410 and dead_token: true", { timeout: TEST_TIMEOUT_MS }, async () => {
    const res = await fetch(`http://127.0.0.1:${wranglerPort}/push`, {
      method: "POST",
      headers: { "content-type": "application/json", "CF-Connecting-IP": "198.51.100.3" },
      body: JSON.stringify({
        platform: "ios",
        token: "dead-ios-token",
        ciphertext: "ZW5jcnlwdGVk",
      }),
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    });
    assert.equal(res.status, 410);
    assert.deepEqual(await res.json(), {
      ok: false,
      platform: "ios",
      provider_status: 410,
      dead_token: true,
      reason: "Unregistered",
    });
  });

  it("relays an Android notice via FCM after OAuth2 token exchange", { timeout: TEST_TIMEOUT_MS }, async () => {
    const res = await fetch(`http://127.0.0.1:${wranglerPort}/push`, {
      method: "POST",
      headers: { "content-type": "application/json", "CF-Connecting-IP": "198.51.100.4" },
      body: JSON.stringify({
        platform: "android",
        token: "android-live-token",
        ciphertext: "ZW5jcnlwdGVkLWFuZHJvaWQ",
        collapse_id: "sess-2",
      }),
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    });
    assert.equal(res.status, 200);
    assert.deepEqual(await res.json(), {
      ok: true,
      platform: "android",
      provider_status: 200,
      provider_id: "projects/maximizepm-test/messages/fake-1",
    });

    assert.equal(recorded.oauth.at(-1).sigOk, true);
    assert.deepEqual(recorded.fcm.at(-1).body, {
      message: {
        token: "android-live-token",
        data: { ciphertext: "ZW5jcnlwdGVkLWFuZHJvaWQ" },
        android: { priority: "HIGH", collapse_key: "sess-2" },
      },
    });
  });

  it("reports a dead Android token with HTTP 410 and dead_token: true", { timeout: TEST_TIMEOUT_MS }, async () => {
    const res = await fetch(`http://127.0.0.1:${wranglerPort}/push`, {
      method: "POST",
      headers: { "content-type": "application/json", "CF-Connecting-IP": "198.51.100.5" },
      body: JSON.stringify({
        platform: "android",
        token: "dead-android-token",
        ciphertext: "ZW5jcnlwdGVk",
      }),
      signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
    });
    assert.equal(res.status, 410);
    assert.deepEqual(await res.json(), {
      ok: false,
      platform: "android",
      provider_status: 404,
      dead_token: true,
      reason: "UNREGISTERED",
    });
  });

  it("enforces the per-IP Cloudflare rate limiting binding", { timeout: TEST_TIMEOUT_MS }, async () => {
    let saw429 = false;
    for (let i = 0; i < 65; i++) {
      const res = await fetch(`http://127.0.0.1:${wranglerPort}/push`, {
        method: "POST",
        headers: { "content-type": "application/json", "CF-Connecting-IP": "198.51.100.99" },
        body: JSON.stringify({
          platform: "ios",
          token: "ios-live-token",
          ciphertext: "ZW5jcnlwdGVk",
        }),
        signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
      });
      if (res.status === 429) {
        assert.deepEqual(await res.json(), { ok: false, error: "rate_limited" });
        saw429 = true;
        break;
      }
      assert.equal(res.status, 200);
    }
    assert.equal(saw429, true, "expected 429 after exceeding per-IP rate limit");
  });
});
