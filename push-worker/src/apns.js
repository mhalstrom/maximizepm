import { importPkcs8EcdsaP256, keyFingerprint, signEs256Jwt } from "./crypto.js";

const APNS_PROD_BASE = "https://api.push.apple.com";
const APNS_SANDBOX_BASE = "https://api.sandbox.push.apple.com";
const APNS_JWT_TTL_SEC = 45 * 60; // Refresh every 45m (Apple allows 20m..60m)
const DEAD_TOKEN_REASONS = new Set(["BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered"]);

// Isolate-local auth token cache only (no user or notice state).
let cachedJwt = null;

export function resetApnsCache() {
  cachedJwt = null;
}

export async function getApnsJwt(env, nowMs = Date.now()) {
  const nowSec = Math.floor(nowMs / 1000);
  const fp = await keyFingerprint(env.APNS_PRIVATE_KEY);
  const cacheKey = `${env.APNS_KEY_ID}:${env.APNS_TEAM_ID}:${fp}`;

  if (cachedJwt && cachedJwt.cacheKey === cacheKey && nowSec - cachedJwt.iat < APNS_JWT_TTL_SEC) {
    return cachedJwt.token;
  }

  const privateKey = await importPkcs8EcdsaP256(env.APNS_PRIVATE_KEY);
  const token = await signEs256Jwt(
    { alg: "ES256", kid: env.APNS_KEY_ID },
    { iss: env.APNS_TEAM_ID, iat: nowSec },
    privateKey,
  );
  cachedJwt = { cacheKey, iat: nowSec, token };
  return token;
}

export function buildApnsRequest(payload, env, jwt, nowMs = Date.now()) {
  const baseUrl = payload.sandbox
    ? (env.APNS_SANDBOX_BASE_URL || env.APNS_BASE_URL || APNS_SANDBOX_BASE)
    : (env.APNS_BASE_URL || APNS_PROD_BASE);
  const url = `${baseUrl.replace(/\/+$/, "")}/3/device/${encodeURIComponent(payload.token)}`;
  const nowSec = Math.floor(nowMs / 1000);

  const headers = {
    authorization: `bearer ${jwt}`,
    "content-type": "application/json",
  };

  let body;
  if (payload.live_activity) {
    const la = payload.live_activity;
    headers["apns-push-type"] = "liveactivity";
    headers["apns-topic"] = `${env.APNS_BUNDLE_ID}.push-type.liveactivity`;
    headers["apns-priority"] = String(la.priority ?? 10);

    const aps = {
      timestamp: la.timestamp ?? nowSec,
      event: la.event,
      "content-state": la.content_state,
    };
    if (la.stale_date !== undefined) aps["stale-date"] = la.stale_date;
    if (la.dismissal_date !== undefined) aps["dismissal-date"] = la.dismissal_date;
    if (la.relevance_score !== undefined) aps["relevance-score"] = la.relevance_score;
    if (la.attributes_type !== undefined) aps["attributes-type"] = la.attributes_type;
    if (la.attributes !== undefined) aps.attributes = la.attributes;

    body = { aps };
    if (payload.ciphertext !== undefined) {
      body.ciphertext = payload.ciphertext;
    }
  } else {
    headers["apns-push-type"] = "alert";
    headers["apns-topic"] = env.APNS_BUNDLE_ID;
    headers["apns-priority"] = "10";
    body = {
      aps: {
        "mutable-content": 1,
        alert: {
          title: "MaximizePM",
          body: "New notice",
        },
        sound: "default",
      },
      ciphertext: payload.ciphertext,
    };
  }

  if (payload.collapse_id) {
    headers["apns-collapse-id"] = payload.collapse_id;
  }

  return { url, headers, body: JSON.stringify(body) };
}

export async function sendApns(payload, env, fetchImpl = fetch, nowMs = Date.now()) {
  if (!env.APNS_PRIVATE_KEY || !env.APNS_KEY_ID || !env.APNS_TEAM_ID || !env.APNS_BUNDLE_ID) {
    return {
      httpStatus: 503,
      body: { ok: false, platform: "ios", error: "apns_not_configured" },
    };
  }

  const jwt = await getApnsJwt(env, nowMs);
  const req = buildApnsRequest(payload, env, jwt, nowMs);

  let res;
  try {
    res = await fetchImpl(req.url, {
      method: "POST",
      headers: req.headers,
      body: req.body,
    });
  } catch {
    return {
      httpStatus: 502,
      body: { ok: false, platform: "ios", provider_status: 0, dead_token: false, reason: "provider_unreachable" },
    };
  }

  const apnsId = res.headers.get("apns-id");
  if (res.status === 200) {
    return {
      httpStatus: 200,
      body: {
        ok: true,
        platform: "ios",
        provider_status: 200,
        ...(apnsId ? { provider_id: apnsId } : {}),
      },
    };
  }

  let reason;
  try {
    const data = await res.json();
    if (data && typeof data.reason === "string") {
      reason = data.reason;
    }
  } catch {
    // Non-JSON error body from provider.
  }

  const deadToken =
    res.status === 410 ||
    (res.status === 400 && reason !== undefined && DEAD_TOKEN_REASONS.has(reason));

  const httpStatus = deadToken ? 410 : (res.status === 429 ? 429 : 502);
  return {
    httpStatus,
    body: {
      ok: false,
      platform: "ios",
      provider_status: res.status,
      dead_token: deadToken,
      ...(reason ? { reason } : {}),
    },
  };
}
