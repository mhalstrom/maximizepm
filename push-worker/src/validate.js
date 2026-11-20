const encoder = new TextEncoder();

export const MAX_BODY_BYTES = 8192;
export const MAX_CIPHERTEXT_BYTES = 3500;
export const MAX_COLLAPSE_ID_BYTES = 64;
export const MAX_TOKEN_LENGTH = 4096;

function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function utf8ByteLength(str) {
  return encoder.encode(str).byteLength;
}

function isPositiveInt(value) {
  return typeof value === "number" && Number.isInteger(value) && value > 0;
}

export function validatePushRequest(body) {
  if (!isPlainObject(body)) {
    return { ok: false, error: "Request body must be a JSON object" };
  }

  const { platform, token, ciphertext, collapse_id, sandbox, live_activity } = body;

  if (platform !== "ios" && platform !== "android") {
    return { ok: false, error: "platform must be 'ios' or 'android'" };
  }

  if (typeof token !== "string" || token.length === 0 || token.length > MAX_TOKEN_LENGTH) {
    return { ok: false, error: "token must be a non-empty string" };
  }
  if (/[\s\x00-\x1f\x7f]/.test(token)) {
    return { ok: false, error: "token must not contain whitespace or control characters" };
  }

  if (sandbox !== undefined) {
    if (typeof sandbox !== "boolean") {
      return { ok: false, error: "sandbox must be a boolean" };
    }
    if (sandbox && platform !== "ios") {
      return { ok: false, error: "sandbox is only supported for ios" };
    }
  }

  if (collapse_id !== undefined) {
    if (typeof collapse_id !== "string" || collapse_id.length === 0) {
      return { ok: false, error: "collapse_id must be a non-empty string" };
    }
    if (utf8ByteLength(collapse_id) > MAX_COLLAPSE_ID_BYTES) {
      return { ok: false, error: `collapse_id exceeds ${MAX_COLLAPSE_ID_BYTES} bytes` };
    }
  }

  let normalizedLiveActivity;
  if (live_activity !== undefined) {
    if (platform !== "ios") {
      return { ok: false, error: "live_activity is only supported for ios" };
    }
    if (!isPlainObject(live_activity)) {
      return { ok: false, error: "live_activity must be an object" };
    }
    const {
      event,
      content_state,
      timestamp,
      stale_date,
      dismissal_date,
      relevance_score,
      priority,
      attributes_type,
      attributes,
    } = live_activity;

    if (event !== "start" && event !== "update" && event !== "end") {
      return { ok: false, error: "live_activity.event must be 'start', 'update', or 'end'" };
    }
    if (!isPlainObject(content_state)) {
      return { ok: false, error: "live_activity.content_state must be an object" };
    }
    if (timestamp !== undefined && !isPositiveInt(timestamp)) {
      return { ok: false, error: "live_activity.timestamp must be a positive integer" };
    }
    if (stale_date !== undefined && !isPositiveInt(stale_date)) {
      return { ok: false, error: "live_activity.stale_date must be a positive integer" };
    }
    if (dismissal_date !== undefined && !isPositiveInt(dismissal_date)) {
      return { ok: false, error: "live_activity.dismissal_date must be a positive integer" };
    }
    if (
      relevance_score !== undefined &&
      (typeof relevance_score !== "number" || !Number.isFinite(relevance_score) || relevance_score < 0 || relevance_score > 100)
    ) {
      return { ok: false, error: "live_activity.relevance_score must be a number between 0 and 100" };
    }
    if (priority !== undefined && priority !== 5 && priority !== 10) {
      return { ok: false, error: "live_activity.priority must be 5 or 10" };
    }
    if (event === "start") {
      if (typeof attributes_type !== "string" || attributes_type.length === 0) {
        return { ok: false, error: "live_activity.attributes_type is required when event is 'start'" };
      }
      if (!isPlainObject(attributes)) {
        return { ok: false, error: "live_activity.attributes is required when event is 'start'" };
      }
    } else {
      if (attributes_type !== undefined && (typeof attributes_type !== "string" || attributes_type.length === 0)) {
        return { ok: false, error: "live_activity.attributes_type must be a non-empty string" };
      }
      if (attributes !== undefined && !isPlainObject(attributes)) {
        return { ok: false, error: "live_activity.attributes must be an object" };
      }
    }

    normalizedLiveActivity = {
      event,
      content_state,
      ...(timestamp !== undefined ? { timestamp } : {}),
      ...(stale_date !== undefined ? { stale_date } : {}),
      ...(dismissal_date !== undefined ? { dismissal_date } : {}),
      ...(relevance_score !== undefined ? { relevance_score } : {}),
      ...(priority !== undefined ? { priority } : {}),
      ...(attributes_type !== undefined ? { attributes_type } : {}),
      ...(attributes !== undefined ? { attributes } : {}),
    };
  }

  if (ciphertext === undefined) {
    if (!normalizedLiveActivity) {
      return { ok: false, error: "ciphertext is required" };
    }
  } else {
    if (typeof ciphertext !== "string" || ciphertext.length === 0) {
      return { ok: false, error: "ciphertext must be a non-empty string" };
    }
    if (utf8ByteLength(ciphertext) > MAX_CIPHERTEXT_BYTES) {
      return { ok: false, error: `ciphertext exceeds ${MAX_CIPHERTEXT_BYTES} bytes` };
    }
  }

  return {
    ok: true,
    value: {
      platform,
      token,
      ...(ciphertext !== undefined ? { ciphertext } : {}),
      ...(collapse_id !== undefined ? { collapse_id } : {}),
      sandbox: Boolean(sandbox),
      ...(normalizedLiveActivity ? { live_activity: normalizedLiveActivity } : {}),
    },
  };
}
