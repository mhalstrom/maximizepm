import assert from "node:assert/strict";
import crypto from "node:crypto";
import { beforeEach, describe, it } from "node:test";

import { resetApnsCache, sendApns } from "../src/apns.js";
import { resetFcmCache, sendFcm } from "../src/fcm.js";
import { handleRequest } from "../src/index.js";
import { validatePushRequest } from "../src/validate.js";

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
  assert.equal(parts.length, 3);
  return {
    header: JSON.parse(base64UrlDecode(parts[0]).toString("utf8")),
    payload: JSON.parse(base64UrlDecode(parts[1]).toString("utf8")),
    signingInput: `${parts[0]}.${parts[1]}`,
    signature: base64UrlDecode(parts[2]),
  };
}

function makeApnsEnv(overrides = {}) {
  return {
    APNS_PRIVATE_KEY: ecPair.privateKey,
    APNS_KEY_ID: "KEY1234567",
    APNS_TEAM_ID: "TEAM123456",
    APNS_BUNDLE_ID: "com.toolscaled.maximizepm",
    ...overrides,
  };
}

function makeFcmEnv(overrides = {}) {
  return {
    FCM_PRIVATE_KEY: rsaPair.privateKey,
    FCM_CLIENT_EMAIL: "push@maximizepm-test.iam.gserviceaccount.com",
    FCM_PROJECT_ID: "maximizepm-test",
    ...overrides,
  };
}

describe("validatePushRequest", () => {
  it("accepts a valid iOS encrypted notice", () => {
    const res = validatePushRequest({
      platform: "ios",
      token: "abcdef0123456789",
      ciphertext: "ZW5jcnlwdGVk",
      collapse_id: "session-42",
    });
    assert.equal(res.ok, true);
    assert.deepEqual(res.value, {
      platform: "ios",
      token: "abcdef0123456789",
      ciphertext: "ZW5jcnlwdGVk",
      collapse_id: "session-42",
      sandbox: false,
    });
  });

  it("accepts a valid iOS Live Activity update without ciphertext", () => {
    const res = validatePushRequest({
      platform: "ios",
      token: "la-token-123",
      live_activity: {
        event: "update",
        content_state: { waitingCount: 3 },
        relevance_score: 80,
      },
    });
    assert.equal(res.ok, true);
    assert.equal(res.value.live_activity.event, "update");
    assert.deepEqual(res.value.live_activity.content_state, { waitingCount: 3 });
  });

  it("requires attributes_type and attributes for live_activity start", () => {
    const bad = validatePushRequest({
      platform: "ios",
      token: "la-token-123",
      live_activity: { event: "start", content_state: { waitingCount: 1 } },
    });
    assert.equal(bad.ok, false);

    const good = validatePushRequest({
      platform: "ios",
      token: "la-token-123",
      live_activity: {
        event: "start",
        content_state: { waitingCount: 1 },
        attributes_type: "SessionActivityAttributes",
        attributes: { host: "macbook" },
      },
    });
    assert.equal(good.ok, true);
  });

  it("rejects live_activity and sandbox on android", () => {
    assert.equal(
      validatePushRequest({
        platform: "android",
        token: "fcm-tok",
        ciphertext: "abc",
        sandbox: true,
      }).ok,
      false,
    );
    assert.equal(
      validatePushRequest({
        platform: "android",
        token: "fcm-tok",
        live_activity: { event: "update", content_state: {} },
      }).ok,
      false,
    );
  });

  it("rejects missing ciphertext, bad tokens, and oversized fields", () => {
    assert.equal(validatePushRequest({ platform: "ios", token: "abc" }).ok, false);
    assert.equal(validatePushRequest({ platform: "ios", token: "bad token", ciphertext: "abc" }).ok, false);
    assert.equal(validatePushRequest({ platform: "web", token: "abc", ciphertext: "abc" }).ok, false);
    assert.equal(
      validatePushRequest({ platform: "ios", token: "abc", ciphertext: "x".repeat(3501) }).ok,
      false,
    );
    assert.equal(
      validatePushRequest({ platform: "ios", token: "abc", ciphertext: "x", collapse_id: "c".repeat(65) }).ok,
      false,
    );
  });
});

describe("sendApns", () => {
  beforeEach(() => resetApnsCache());

  it("signs a valid ES256 JWT and sends an alert push", async () => {
    let captured;
    const fakeFetch = async (url, init) => {
      captured = { url, init };
      return new Response(null, { status: 200, headers: { "apns-id": "apns-uuid-1" } });
    };

    const res = await sendApns(
      {
        platform: "ios",
        token: "devtoken123",
        ciphertext: "Y2lwaGVydGV4dA",
        collapse_id: "item-6470",
        sandbox: false,
      },
      makeApnsEnv(),
      fakeFetch,
      1_700_000_000_000,
    );

    assert.equal(res.httpStatus, 200);
    assert.deepEqual(res.body, {
      ok: true,
      platform: "ios",
      provider_status: 200,
      provider_id: "apns-uuid-1",
    });

    assert.equal(captured.url, "https://api.push.apple.com/3/device/devtoken123");
    assert.equal(captured.init.headers["apns-push-type"], "alert");
    assert.equal(captured.init.headers["apns-topic"], "com.toolscaled.maximizepm");
    assert.equal(captured.init.headers["apns-collapse-id"], "item-6470");

    const auth = captured.init.headers.authorization;
    assert.ok(auth.startsWith("bearer "));
    const jwt = parseJwt(auth.slice("bearer ".length));
    assert.deepEqual(jwt.header, { alg: "ES256", kid: "KEY1234567" });
    assert.deepEqual(jwt.payload, { iss: "TEAM123456", iat: 1_700_000_000 });

    const validSig = crypto.verify(
      "sha256",
      Buffer.from(jwt.signingInput),
      { key: ecPair.publicKey, dsaEncoding: "ieee-p1363" },
      jwt.signature,
    );
    assert.equal(validSig, true);

    const sentBody = JSON.parse(captured.init.body);
    assert.equal(sentBody.aps["mutable-content"], 1);
    assert.equal(sentBody.ciphertext, "Y2lwaGVydGV4dA");
  });

  it("caches APNs JWT for 45 minutes and refreshes afterward", async () => {
    const tokens = [];
    const fakeFetch = async (_url, init) => {
      tokens.push(init.headers.authorization);
      return new Response(null, { status: 200 });
    };
    const payload = { platform: "ios", token: "tok", ciphertext: "abc", sandbox: false };
    const env = makeApnsEnv();

    await sendApns(payload, env, fakeFetch, 1_700_000_000_000);
    await sendApns(payload, env, fakeFetch, 1_700_000_000_000 + 20 * 60 * 1000);
    await sendApns(payload, env, fakeFetch, 1_700_000_000_000 + 46 * 60 * 1000);

    assert.equal(tokens[0], tokens[1]);
    assert.notEqual(tokens[1], tokens[2]);
  });

  it("builds a Live Activity push with liveactivity topic and aps fields", async () => {
    let captured;
    const fakeFetch = async (url, init) => {
      captured = { url, init };
      return new Response(null, { status: 200 });
    };

    const res = await sendApns(
      {
        platform: "ios",
        token: "la-tok",
        sandbox: true,
        live_activity: {
          event: "end",
          content_state: { waitingCount: 0 },
          dismissal_date: 1_700_000_500,
          priority: 5,
        },
      },
      makeApnsEnv(),
      fakeFetch,
      1_700_000_000_000,
    );

    assert.equal(res.httpStatus, 200);
    assert.equal(captured.url, "https://api.sandbox.push.apple.com/3/device/la-tok");
    assert.equal(captured.init.headers["apns-push-type"], "liveactivity");
    assert.equal(captured.init.headers["apns-topic"], "com.toolscaled.maximizepm.push-type.liveactivity");
    assert.equal(captured.init.headers["apns-priority"], "5");
    assert.deepEqual(JSON.parse(captured.init.body), {
      aps: {
        timestamp: 1_700_000_000,
        event: "end",
        "content-state": { waitingCount: 0 },
        "dismissal-date": 1_700_000_500,
      },
    });
  });

  it("marks 410 Unregistered and 400 BadDeviceToken as dead_token", async () => {
    const payload = { platform: "ios", token: "dead", ciphertext: "abc", sandbox: false };
    const env = makeApnsEnv();

    const r410 = await sendApns(
      payload,
      env,
      async () => new Response(JSON.stringify({ reason: "Unregistered" }), { status: 410 }),
    );
    assert.equal(r410.httpStatus, 410);
    assert.equal(r410.body.dead_token, true);
    assert.equal(r410.body.provider_status, 410);
    assert.equal(r410.body.reason, "Unregistered");

    const r400 = await sendApns(
      payload,
      env,
      async () => new Response(JSON.stringify({ reason: "BadDeviceToken" }), { status: 400 }),
    );
    assert.equal(r400.httpStatus, 410);
    assert.equal(r400.body.dead_token, true);
    assert.equal(r400.body.provider_status, 400);

    const r500 = await sendApns(
      payload,
      env,
      async () => new Response(JSON.stringify({ reason: "InternalServerError" }), { status: 500 }),
    );
    assert.equal(r500.httpStatus, 502);
    assert.equal(r500.body.dead_token, false);
  });

  it("returns 503 when APNs secrets are missing", async () => {
    const res = await sendApns({ platform: "ios", token: "t", ciphertext: "c", sandbox: false }, {});
    assert.equal(res.httpStatus, 503);
    assert.equal(res.body.error, "apns_not_configured");
  });
});

describe("sendFcm", () => {
  beforeEach(() => resetFcmCache());

  it("signs an RS256 OAuth2 assertion, exchanges it, and sends an FCM data message", async () => {
    const calls = [];
    const fakeFetch = async (url, init) => {
      calls.push({ url, init });
      if (url.includes("oauth2.googleapis.com")) {
        const params = new URLSearchParams(init.body);
        assert.equal(params.get("grant_type"), "urn:ietf:params:oauth:grant-type:jwt-bearer");
        const jwt = parseJwt(params.get("assertion"));
        assert.deepEqual(jwt.header, { alg: "RS256", typ: "JWT" });
        assert.equal(jwt.payload.iss, "push@maximizepm-test.iam.gserviceaccount.com");
        assert.equal(jwt.payload.scope, "https://www.googleapis.com/auth/firebase.messaging");

        const validSig = crypto.verify("sha256", Buffer.from(jwt.signingInput), rsaPair.publicKey, jwt.signature);
        assert.equal(validSig, true);

        return new Response(JSON.stringify({ access_token: "ya29.test", expires_in: 3600 }), { status: 200 });
      }
      return new Response(JSON.stringify({ name: "projects/maximizepm-test/messages/m1" }), { status: 200 });
    };

    const res = await sendFcm(
      {
        platform: "android",
        token: "fcm-device-token",
        ciphertext: "Y2lwaGVydGV4dA",
        collapse_id: "session-9",
      },
      makeFcmEnv(),
      fakeFetch,
      1_700_000_000_000,
    );

    assert.equal(res.httpStatus, 200);
    assert.deepEqual(res.body, {
      ok: true,
      platform: "android",
      provider_status: 200,
      provider_id: "projects/maximizepm-test/messages/m1",
    });

    assert.equal(calls.length, 2);
    assert.equal(calls[1].url, "https://fcm.googleapis.com/v1/projects/maximizepm-test/messages:send");
    assert.equal(calls[1].init.headers.authorization, "Bearer ya29.test");
    assert.deepEqual(JSON.parse(calls[1].init.body), {
      message: {
        token: "fcm-device-token",
        data: { ciphertext: "Y2lwaGVydGV4dA" },
        android: { priority: "HIGH", collapse_key: "session-9" },
      },
    });
  });

  it("caches the FCM OAuth2 access token across calls", async () => {
    let oauthCalls = 0;
    const fakeFetch = async (url) => {
      if (url.includes("oauth2")) {
        oauthCalls += 1;
        return new Response(JSON.stringify({ access_token: "tok-1", expires_in: 3600 }), { status: 200 });
      }
      return new Response(JSON.stringify({ name: "m1" }), { status: 200 });
    };
    const payload = { platform: "android", token: "t", ciphertext: "c" };
    const env = makeFcmEnv();

    await sendFcm(payload, env, fakeFetch, 1_700_000_000_000);
    await sendFcm(payload, env, fakeFetch, 1_700_000_000_000 + 10 * 60 * 1000);
    assert.equal(oauthCalls, 1);

    await sendFcm(payload, env, fakeFetch, 1_700_000_000_000 + 56 * 60 * 1000);
    assert.equal(oauthCalls, 2);
  });

  it("maps 404 UNREGISTERED and invalid registration token to dead_token", async () => {
    const env = makeFcmEnv();
    const payload = { platform: "android", token: "dead", ciphertext: "c" };

    const makeFetch = (status, errBody) => async (url) => {
      if (url.includes("oauth2")) {
        return new Response(JSON.stringify({ access_token: "tok", expires_in: 3600 }), { status: 200 });
      }
      return new Response(JSON.stringify(errBody), { status });
    };

    const r404 = await sendFcm(
      payload,
      env,
      makeFetch(404, {
        error: {
          code: 404,
          status: "NOT_FOUND",
          details: [{ "@type": "type.googleapis.com/google.firebase.fcm.v1.FcmError", errorCode: "UNREGISTERED" }],
        },
      }),
    );
    assert.equal(r404.httpStatus, 410);
    assert.equal(r404.body.dead_token, true);
    assert.equal(r404.body.reason, "UNREGISTERED");

    const r400 = await sendFcm(
      payload,
      env,
      makeFetch(400, {
        error: {
          code: 400,
          status: "INVALID_ARGUMENT",
          message: "The registration token is not a valid FCM registration token",
        },
      }),
    );
    assert.equal(r400.httpStatus, 410);
    assert.equal(r400.body.dead_token, true);
  });
});

describe("handleRequest", () => {
  it("handles /health, method checks, 404, body size limit, and per-IP rate limiting", async () => {
    const health = await handleRequest(new Request("https://push.example/health"), {});
    assert.equal(health.status, 200);
    assert.deepEqual(await health.json(), { ok: true });

    const badMethod = await handleRequest(new Request("https://push.example/push", { method: "GET" }), {});
    assert.equal(badMethod.status, 405);

    const notFound = await handleRequest(new Request("https://push.example/other", { method: "POST" }), {});
    assert.equal(notFound.status, 404);

    let seenKey;
    const blocked = await handleRequest(
      new Request("https://push.example/push", {
        method: "POST",
        headers: { "CF-Connecting-IP": "203.0.113.5" },
        body: JSON.stringify({ platform: "ios", token: "t", ciphertext: "c" }),
      }),
      {
        RATE_LIMITER: {
          async limit({ key }) {
            seenKey = key;
            return { success: false };
          },
        },
      },
    );
    assert.equal(seenKey, "203.0.113.5");
    assert.equal(blocked.status, 429);
    assert.deepEqual(await blocked.json(), { ok: false, error: "rate_limited" });

    const tooBig = await handleRequest(
      new Request("https://push.example/push", {
        method: "POST",
        body: "a".repeat(9000),
      }),
      {},
    );
    assert.equal(tooBig.status, 413);
  });
});
