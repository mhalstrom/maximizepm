import { sendApns } from "./apns.js";
import { sendFcm } from "./fcm.js";
import { MAX_BODY_BYTES, validatePushRequest } from "./validate.js";

function jsonResponse(body, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
      ...extraHeaders,
    },
  });
}

function clientIp(request) {
  const cfIp = request.headers.get("CF-Connecting-IP");
  if (cfIp) return cfIp.trim();
  const xff = request.headers.get("X-Forwarded-For");
  if (xff) {
    const first = xff.split(",")[0].trim();
    if (first) return first;
  }
  return "unknown";
}

async function readBoundedText(request, maxBytes) {
  const contentLength = request.headers.get("content-length");
  if (contentLength !== null) {
    const parsed = Number(contentLength);
    if (Number.isFinite(parsed) && parsed > maxBytes) {
      return { ok: false, status: 413, error: `Request body exceeds ${maxBytes} bytes` };
    }
  }
  const buf = await request.arrayBuffer();
  if (buf.byteLength > maxBytes) {
    return { ok: false, status: 413, error: `Request body exceeds ${maxBytes} bytes` };
  }
  return { ok: true, text: new TextDecoder().decode(buf) };
}

export async function handleRequest(request, env, fetchImpl = fetch) {
  const url = new URL(request.url);

  if (url.pathname === "/health") {
    if (request.method !== "GET") {
      return jsonResponse({ ok: false, error: "Method not allowed" }, 405, { Allow: "GET" });
    }
    return jsonResponse({ ok: true }, 200);
  }

  if (url.pathname !== "/push") {
    return jsonResponse({ ok: false, error: "Not found" }, 404);
  }

  if (request.method !== "POST") {
    return jsonResponse({ ok: false, error: "Method not allowed" }, 405, { Allow: "POST" });
  }

  if (env && env.RATE_LIMITER && typeof env.RATE_LIMITER.limit === "function") {
    const { success } = await env.RATE_LIMITER.limit({ key: clientIp(request) });
    if (!success) {
      return jsonResponse({ ok: false, error: "rate_limited" }, 429);
    }
  }

  const raw = await readBoundedText(request, MAX_BODY_BYTES);
  if (!raw.ok) {
    return jsonResponse({ ok: false, error: raw.error }, raw.status);
  }

  let parsed;
  try {
    parsed = JSON.parse(raw.text);
  } catch {
    return jsonResponse({ ok: false, error: "Invalid JSON body" }, 400);
  }

  const validated = validatePushRequest(parsed);
  if (!validated.ok) {
    return jsonResponse({ ok: false, error: validated.error }, 400);
  }

  const payload = validated.value;
  const result =
    payload.platform === "ios"
      ? await sendApns(payload, env || {}, fetchImpl)
      : await sendFcm(payload, env || {}, fetchImpl);

  return jsonResponse(result.body, result.httpStatus);
}

export default {
  async fetch(request, env) {
    return handleRequest(request, env, fetch);
  },
};
