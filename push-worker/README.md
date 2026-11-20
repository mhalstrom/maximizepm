# MaximizePM Push Worker

Stateless Cloudflare Worker that relays encrypted push notices from `maxpm serve` to Apple Push Notification service (APNs) and Firebase Cloud Messaging (FCM).

It has no database, no KV, no R2, and stores no tokens or notices. Each request carries the push token and the encrypted notice; the Worker signs the provider auth token from Cloudflare Worker secrets, forwards the push, and returns the provider status so `maxpm serve` learns when a token is dead.

## Route

### `POST /push`

JSON body:

```json
{
  "platform": "ios",
  "token": "<APNs or FCM device token>",
  "ciphertext": "<base64-encoded encrypted notice>",
  "collapse_id": "session-123",
  "sandbox": false
}
```

For an iOS Live Activity push (`apns-push-type: liveactivity`), pass `live_activity` (with `platform: "ios"`):

```json
{
  "platform": "ios",
  "token": "<ActivityKit push token>",
  "collapse_id": "live-1",
  "live_activity": {
    "event": "update",
    "content_state": { "waitingCount": 2 }
  }
}
```

Responses:

- `200`: `{ "ok": true, "platform": "ios", "provider_status": 200, "provider_id": "..." }`
- `410`: `{ "ok": false, "platform": "ios", "provider_status": 410, "dead_token": true, "reason": "Unregistered" }`
- `400` / `413`: invalid request body
- `429`: per-IP rate limit exceeded (or provider rate limit)
- `502`: upstream provider error (`provider_status` and `reason` included)
- `503`: provider secrets not configured

### `GET /health`

Returns `200 { "ok": true }`.

## Secrets

Set via `wrangler secret put` (never committed to this repository):

- APNs: `APNS_PRIVATE_KEY` (`.p8` PEM), `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_BUNDLE_ID`
- FCM: `FCM_PRIVATE_KEY` (service account PEM), `FCM_CLIENT_EMAIL`, `FCM_PROJECT_ID`

## Local development and tests

```sh
cd push-worker
npm install
npm test                  # unit tests (no network)
npm run test:integration  # end-to-end against wrangler dev + fake APNs/FCM providers
```
