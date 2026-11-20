import { importPkcs8Rs256, keyFingerprint, signRs256Jwt } from "./crypto.js";

const FCM_BASE = "https://fcm.googleapis.com";
const FCM_TOKEN_URL = "https://oauth2.googleapis.com/token";
const FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging";

// Isolate-local OAuth2 access token cache only (no user or notice state).
let cachedAccessToken = null;

export function resetFcmCache() {
  cachedAccessToken = null;
}

export async function getFcmAccessToken(env, fetchImpl = fetch, nowMs = Date.now()) {
  const tokenUrl = env.FCM_TOKEN_URL || FCM_TOKEN_URL;
  const fp = await keyFingerprint(env.FCM_PRIVATE_KEY);
  const cacheKey = `${env.FCM_CLIENT_EMAIL}:${tokenUrl}:${fp}`;

  if (cachedAccessToken && cachedAccessToken.cacheKey === cacheKey && nowMs < cachedAccessToken.expiresAtMs) {
    return { ok: true, accessToken: cachedAccessToken.accessToken };
  }

  const nowSec = Math.floor(nowMs / 1000);
  const privateKey = await importPkcs8Rs256(env.FCM_PRIVATE_KEY);
  const assertion = await signRs256Jwt(
    { alg: "RS256", typ: "JWT" },
    {
      iss: env.FCM_CLIENT_EMAIL,
      scope: FCM_SCOPE,
      aud: tokenUrl,
      iat: nowSec,
      exp: nowSec + 3600,
    },
    privateKey,
  );

  const formBody = new URLSearchParams({
    grant_type: "urn:ietf:params:oauth:grant-type:jwt-bearer",
    assertion,
  });

  let res;
  try {
    res = await fetchImpl(tokenUrl, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: formBody.toString(),
    });
  } catch {
    return { ok: false, status: 0, reason: "oauth_unreachable" };
  }

  if (!res.ok) {
    return { ok: false, status: res.status, reason: "oauth_token_error" };
  }

  let data;
  try {
    data = await res.json();
  } catch {
    return { ok: false, status: res.status, reason: "oauth_invalid_response" };
  }

  if (!data || typeof data.access_token !== "string" || !data.access_token) {
    return { ok: false, status: res.status, reason: "oauth_missing_token" };
  }

  const expiresInSec = typeof data.expires_in === "number" ? data.expires_in : 3600;
  const ttlSec = Math.max(60, expiresInSec - 300);
  cachedAccessToken = {
    cacheKey,
    accessToken: data.access_token,
    expiresAtMs: nowMs + ttlSec * 1000,
  };
  return { ok: true, accessToken: data.access_token };
}

export function buildFcmRequest(payload, env, accessToken) {
  const baseUrl = (env.FCM_BASE_URL || FCM_BASE).replace(/\/+$/, "");
  const url = `${baseUrl}/v1/projects/${encodeURIComponent(env.FCM_PROJECT_ID)}/messages:send`;

  const android = { priority: "HIGH" };
  if (payload.collapse_id) {
    android.collapse_key = payload.collapse_id;
  }

  const body = {
    message: {
      token: payload.token,
      data: {
        ciphertext: payload.ciphertext,
      },
      android,
    },
  };

  return {
    url,
    headers: {
      authorization: `Bearer ${accessToken}`,
      "content-type": "application/json",
    },
    body: JSON.stringify(body),
  };
}

function extractFcmError(data) {
  const err = data && data.error;
  if (!err || typeof err !== "object") {
    return { code: undefined, message: "" };
  }
  let code;
  if (Array.isArray(err.details)) {
    for (const detail of err.details) {
      if (detail && typeof detail.errorCode === "string") {
        code = detail.errorCode;
        break;
      }
    }
  }
  if (!code && typeof err.status === "string") {
    code = err.status;
  }
  return { code, message: typeof err.message === "string" ? err.message : "" };
}

export async function sendFcm(payload, env, fetchImpl = fetch, nowMs = Date.now()) {
  if (!env.FCM_PRIVATE_KEY || !env.FCM_CLIENT_EMAIL || !env.FCM_PROJECT_ID) {
    return {
      httpStatus: 503,
      body: { ok: false, platform: "android", error: "fcm_not_configured" },
    };
  }

  const tokenResult = await getFcmAccessToken(env, fetchImpl, nowMs);
  if (!tokenResult.ok) {
    return {
      httpStatus: 502,
      body: {
        ok: false,
        platform: "android",
        provider_status: tokenResult.status,
        dead_token: false,
        reason: tokenResult.reason,
      },
    };
  }

  const req = buildFcmRequest(payload, env, tokenResult.accessToken);
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
      body: { ok: false, platform: "android", provider_status: 0, dead_token: false, reason: "provider_unreachable" },
    };
  }

  let data = null;
  try {
    data = await res.json();
  } catch {
    // Non-JSON response body from provider.
  }

  if (res.status === 200) {
    return {
      httpStatus: 200,
      body: {
        ok: true,
        platform: "android",
        provider_status: 200,
        ...(data && typeof data.name === "string" ? { provider_id: data.name } : {}),
      },
    };
  }

  const { code, message } = extractFcmError(data);
  const deadToken =
    res.status === 404 ||
    code === "UNREGISTERED" ||
    code === "SENDER_ID_MISMATCH" ||
    (res.status === 400 && code === "INVALID_ARGUMENT" && /registration token/i.test(message));

  const httpStatus = deadToken ? 410 : (res.status === 429 ? 429 : 502);
  return {
    httpStatus,
    body: {
      ok: false,
      platform: "android",
      provider_status: res.status,
      dead_token: deadToken,
      ...(code ? { reason: code } : {}),
    },
  };
}
