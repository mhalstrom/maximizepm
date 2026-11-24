#!/usr/bin/env bash
# Build, sign, notarize, staple, verify and upload a MaximizePM macOS desktop release on this Mac (#7014).
#
# Runs the mac + draft-publish steps of .github/workflows/desktop-release.yml locally (no GitHub Actions),
# using the Developer ID Application identity in the login keychain and the Apple notarization key stored in
# maxpm secrets (apple-notary-key-p8, apple-notary-key-id, apple-notary-issuer).
#
# Usage:
#   desktop/scripts/release-local.sh --tag v1.7.0 [--sha <commit>] [--repo <owner/repo>] [--no-upload] [--out-dir <dir>]
#
# Never publishes a release: it only creates or updates a DRAFT GitHub release.
# Builds in a detached git worktree outside /tmp (RELEASE_BUILD_DIR, MAXPM_BUILD_DIR, or ~/.cache/maximizepm/release-build).
#
# Environment overrides:
#   SIGNING_IDENTITY   exact codesigning identity in the login keychain (default: first Developer ID Application)
#   RELEASE_BUILD_DIR  parent directory for the temporary build worktree (never /tmp)
#   GH                 gh binary (default: gh)
#   MAXPM_BIN          maxpm binary (default: <repo>/bin/maxpm if executable, else maxpm)
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: desktop/scripts/release-local.sh --tag <vX.Y.Z> [--sha <commit>] [--repo <owner/repo>] [--no-upload] [--out-dir <dir>]
EOF
  exit 2
}

TAG=""
SHA=""
REPO=""
NO_UPLOAD=0
OUT_DIR=""

while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="${2:-}"; shift 2 ;;
    --sha) SHA="${2:-}"; shift 2 ;;
    --repo) REPO="${2:-}"; shift 2 ;;
    --no-upload) NO_UPLOAD=1; shift ;;
    --out-dir) OUT_DIR="${2:-}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "release-local: unknown argument: $1" >&2; usage ;;
  esac
done

[ "$(uname -s)" = "Darwin" ] || { echo "release-local: macOS is required" >&2; exit 1; }
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "release-local: --tag must look like v1.2.3, not '${TAG}'" >&2; exit 2; }
VERSION="${TAG#v}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR/../.." rev-parse --show-toplevel)" || { echo "release-local: not in a git repository" >&2; exit 1; }

if [ -z "$SHA" ]; then
  SHA="$(git -C "$REPO_ROOT" rev-parse HEAD)"
fi
SHA="$(git -C "$REPO_ROOT" rev-parse --verify --quiet "${SHA}^{commit}")" \
  || { echo "release-local: commit not found: $SHA" >&2; exit 1; }

GH_BIN="${GH:-gh}"
if [ -n "${MAXPM_BIN:-}" ]; then
  MAXPM="$MAXPM_BIN"
elif [ -x "$REPO_ROOT/bin/maxpm" ]; then
  MAXPM="$REPO_ROOT/bin/maxpm"
else
  MAXPM="maxpm"
fi

GH_REPO_ARGS=()
[ -n "$REPO" ] && GH_REPO_ARGS=(-R "$REPO")

# Required tools
for cmd in git node npm python3 swiftc clang lipo codesign xcrun spctl jq rsync tar security; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "release-local: missing required tool: $cmd" >&2; exit 1; }
done
if [ "$NO_UPLOAD" = 0 ]; then
  command -v "$GH_BIN" >/dev/null 2>&1 || { echo "release-local: missing required tool: $GH_BIN" >&2; exit 1; }
fi

# Resolve Developer ID Application identity from the login keychain (not a .p12)
IDENTITIES="$(security find-identity -v -p codesigning 2>&1 || true)"
if [ -n "${SIGNING_IDENTITY:-}" ]; then
  grep -Fq "\"$SIGNING_IDENTITY\"" <<<"$IDENTITIES" \
    || { echo "release-local: signing identity not found in keychain: $SIGNING_IDENTITY" >&2; printf '%s\n' "$IDENTITIES" >&2; exit 1; }
else
  SIGNING_IDENTITY="$(sed -n 's/.*"\(Developer ID Application: [^"]*\)".*/\1/p' <<<"$IDENTITIES" | sed -n '1p')"
  [ -n "$SIGNING_IDENTITY" ] \
    || { echo "release-local: no Developer ID Application identity found in login keychain" >&2; printf '%s\n' "$IDENTITIES" >&2; exit 1; }
fi
CSC_NAME_VAL="${SIGNING_IDENTITY#Developer ID Application: }"
echo "release-local: tag=$TAG sha=$SHA identity=$SIGNING_IDENTITY"

# Fail fast if the release is already published on GitHub
if [ "$NO_UPLOAD" = 0 ]; then
  draft_state="$("$GH_BIN" release view "$TAG" "${GH_REPO_ARGS[@]}" --json isDraft --jq .isDraft 2>/dev/null || true)"
  if [ "$draft_state" = "false" ]; then
    echo "release-local: the release $TAG is published already; this script changes only a draft" >&2
    exit 1
  fi
fi

# Build directory outside /tmp
BUILD_BASE="${RELEASE_BUILD_DIR:-${MAXPM_BUILD_DIR:-${HOME}/.cache/maximizepm/release-build}}"
case "$BUILD_BASE" in
  /tmp|/tmp/*|/private/tmp|/private/tmp/*)
    echo "release-local: refusing to build in /tmp ($BUILD_BASE); set RELEASE_BUILD_DIR elsewhere" >&2
    exit 1
    ;;
esac
mkdir -p "$BUILD_BASE"
WORK="$(mktemp -d "$BUILD_BASE/release-${TAG}-XXXXXX")"
WT="$WORK/tree"
TMP_DIR="$WORK/tmp"
KEY_FILE="$TMP_DIR/AuthKey.p8"
mkdir -p "$TMP_DIR"
chmod 700 "$TMP_DIR"

cleanup() {
  rm -f "$KEY_FILE"
  if [ -d "$WT" ]; then
    git -C "$REPO_ROOT" worktree remove --force "$WT" >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
  git -C "$REPO_ROOT" worktree prune >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "release-local: checking out $SHA into $WT"
git -C "$REPO_ROOT" worktree add --quiet --detach "$WT" "$SHA"

# Verify river/__init__.py matches the tag version before building
grep -qx "__version__ = \"$VERSION\"" "$WT/river/__init__.py" \
  || { echo "release-local: river/__init__.py does not say version $VERSION" >&2; exit 1; }

# Fetch Apple notarization .p8 key into a mode-0600 temp file via maxpm secret (never printed)
echo "release-local: fetching apple-notary-key-p8 via maxpm secret"
(umask 077 && "$MAXPM" secret pipe apple-notary-key-p8 -- sh -c 'cat > "$1"' _ "$KEY_FILE")
chmod 600 "$KEY_FILE"
[ -s "$KEY_FILE" ] || { echo "release-local: apple-notary-key-p8 is empty" >&2; exit 1; }

# Install dependencies, check Electron age, run smoke test, set package version from tag
echo "release-local: npm ci"
(cd "$WT/desktop" && npm ci)

echo "release-local: checking Electron age"
(cd "$WT/desktop" && node scripts/electron-age.js)

unset_agent_env() {
  local v
  for v in $(compgen -e); do
    case "$v" in
      MAXPM_*|CLAUDECODE|CLAUDE_CODE_ENTRYPOINT|CLAUDE_SESSION_ID|CODEX_THREAD_ID|CODEX_SANDBOX|\
      ANTIGRAVITY_AGENT|ANTIGRAVITY_CONVERSATION_ID|GROK_SESSION_ID|COPILOT_SESSION_ID|\
      OPENCODE_SESSION_ID|PI_SESSION_ID|HERMES_SESSION_ID|CURSOR_AGENT|GOOSE_SESSION_ID)
        unset "$v" ;;
    esac
  done
}

echo "release-local: running desktop smoke test"
(
  unset_agent_env
  cd "$WT/desktop" && npm test
)

echo "release-local: setting desktop version to $VERSION"
(cd "$WT/desktop" && npm version --no-git-tag-version --allow-same-version "$VERSION")

# Dictation helper (arm64 + x64 universal binary)
echo "release-local: building dictation helper (arm64 + x64)"
mkdir -p "$WT/river/bin"
swiftc -O -target arm64-apple-macos26.0 "$WT/river/dictate/main.swift" -o "$TMP_DIR/dictate-arm64"
swiftc -O -target x86_64-apple-macos26.0 "$WT/river/dictate/main.swift" -o "$TMP_DIR/dictate-x64"
lipo -create "$TMP_DIR/dictate-arm64" "$TMP_DIR/dictate-x64" -output "$WT/river/bin/maxpm-dictate"
codesign --force --sign - "$WT/river/bin/maxpm-dictate"
lipo -info "$WT/river/bin/maxpm-dictate"

# Keychain helper (universal binary, signed with login keychain Developer ID and hardened runtime)
echo "release-local: building and signing keychain helper"
"$WT/desktop/scripts/build-keychain-helper.sh" build "$WT/river/bin/maxpm-keychain-helper"
lipo -info "$WT/river/bin/maxpm-keychain-helper"
codesign --force --sign "$SIGNING_IDENTITY" \
  --identifier "com.maximizepm.keychain-helper" \
  --options runtime --timestamp \
  "$WT/river/bin/maxpm-keychain-helper"
"$WT/desktop/scripts/build-keychain-helper.sh" verify --signed "$WT/river/bin/maxpm-keychain-helper"

# Build, sign, notarize and staple the .app bundles and build signed .dmg files (arm64 + x64)
echo "release-local: running npm run dist:signed (arm64 + x64)"
(
  cd "$WT/desktop"
  "$MAXPM" secret run apple-notary-key-id --var APPLE_API_KEY_ID -- \
    "$MAXPM" secret run apple-notary-issuer --var APPLE_API_ISSUER -- \
    env CSC_IDENTITY_AUTO_DISCOVERY=true \
        CSC_NAME="$CSC_NAME_VAL" \
        APPLE_API_KEY="$KEY_FILE" \
        npm run dist:signed
)

# Notarize and staple each .dmg
for d in "$WT/desktop/out"/*.dmg; do
  echo "release-local: notarizing and stapling $(basename "$d")"
  "$MAXPM" secret run apple-notary-key-id --var APPLE_API_KEY_ID -- \
    "$MAXPM" secret run apple-notary-issuer --var APPLE_API_ISSUER -- \
    sh -c 'xcrun notarytool submit "$1" --key "$2" --key-id "$APPLE_API_KEY_ID" --issuer "$APPLE_API_ISSUER" --wait --timeout 30m' \
    _ "$d" "$KEY_FILE"
  xcrun stapler staple "$d"
done

# Remove the .p8 key file as soon as notarization finishes
rm -f "$KEY_FILE"

# Verify apps and DMGs (same checks as desktop-release.yml)
echo "release-local: verifying signed apps and DMGs"
for a in "$WT/desktop/out"/mac*/"MaximizePM.app"; do
  codesign --verify --deep --strict "$a" && echo "signature OK: $a"
  helper="$a/Contents/Resources/river-app/river/bin/maxpm-keychain-helper"
  app_info="$(codesign -dvv "$a" 2>&1)"
  grep -q "^Authority=Developer ID Application" <<<"$app_info" \
    || { echo "release-local: not signed with Developer ID Application: $a" >&2; printf '%s\n' "$app_info" >&2; exit 1; }
  "$WT/desktop/scripts/build-keychain-helper.sh" verify --signed "$helper"
  xcrun stapler validate "$a"
  spctl --assess --type execute -vv "$a"
done

for d in "$WT/desktop/out"/*.dmg; do
  xcrun stapler validate "$d"
  spctl --assess --type open --context context:primary-signature -vv "$d"
done

# Verify the app starts MaximizePM from its own bundle with its own Python
echo "release-local: verifying bundled app start check"
(
  unset_agent_env
  cd "$WT/desktop"
  MAXPM_DB="$TMP_DIR/q.db" node -e '
    const { startRiver } = require("./server");
    startRiver({ riverRoot: process.argv[1], find: () => null }).then(async (r) => {
      console.log("MaximizePM answers on", r.url, "with", r.child.spawnargs[0]);
      await r.stop();
    }).catch((e) => { console.error(e.message); process.exit(1); });' \
    "$WT/desktop/out/mac-arm64/MaximizePM.app/Contents/Resources/river-app"
)

# Code archive, standalone signed Keychain helper asset, and manifest.json
echo "release-local: building code archive, keychain helper asset, and manifest.json"
CODE_STAGE="$TMP_DIR/code/maximizepm-$VERSION"
mkdir -p "$CODE_STAGE"
APP_RIVER="$WT/desktop/out/mac-arm64/MaximizePM.app/Contents/Resources/river-app"
cp -R "$APP_RIVER/bin" "$CODE_STAGE/bin"
rsync -a --exclude '__pycache__' --exclude '*.pyc' "$APP_RIVER/river" "$CODE_STAGE/"
[ -f "$APP_RIVER/skills/maxpm/SKILL.md" ] || { echo "release-local: the app carries no agent guides" >&2; exit 1; }
cp -R "$APP_RIVER/skills" "$CODE_STAGE/skills"
[ -x "$CODE_STAGE/river/bin/maxpm-dictate" ] || { echo "release-local: the dictation helper is missing" >&2; exit 1; }
[ -x "$CODE_STAGE/river/bin/maxpm-keychain-helper" ] || { echo "release-local: the keychain helper is missing" >&2; exit 1; }

CODE_ARCHIVE="$WT/desktop/out/MaximizePM-$VERSION-code.tar.gz"
tar -C "$TMP_DIR/code" -czf "$CODE_ARCHIVE" "maximizepm-$VERSION"

HELPER_ASSET="$WT/desktop/out/MaximizePM-$VERSION-keychain-helper-macos.tar.gz"
tar -C "$APP_RIVER/river/bin" -czf "$HELPER_ASSET" maxpm-keychain-helper

helper_info="$(codesign -dv "$APP_RIVER/river/bin/maxpm-keychain-helper" 2>&1)"
TEAM_ID="$(awk -F= '/^TeamIdentifier=/{print $2; exit}' <<<"$helper_info")"
MANIFEST="$WT/desktop/out/manifest.json"
(cd "$WT/desktop" && node scripts/manifest.js "$VERSION" "$CODE_ARCHIVE" "$HELPER_ASSET" "$TEAM_ID" > "$MANIFEST")
[ "$(jq -r .shell "$MANIFEST")" = "$(cat "$WT/desktop/build/shell-id.txt")" ] \
  || { echo "release-local: the shell id differs from the app's" >&2; exit 1; }
cat "$MANIFEST"

# Code archive start check with the app's own Python
echo "release-local: verifying code archive start check"
UNPACK_DIR="$TMP_DIR/unpack"
mkdir -p "$UNPACK_DIR"
tar -C "$UNPACK_DIR" -xzf "$CODE_ARCHIVE"
APP_PY="$APP_RIVER/python/bin/python3"
PYTHONPATH= "$APP_PY" -c 'import sys; sys.path.insert(0, sys.argv[1]); from river import release; why = release.start_check(sys.argv[1], sys.argv[2]); print(why or "start check OK"); sys.exit(1 if why else 0)' \
  "$UNPACK_DIR/maximizepm-$VERSION" "$VERSION"

ASSETS=(
  "$WT/desktop/out/MaximizePM-$VERSION-arm64.dmg"
  "$WT/desktop/out/MaximizePM-$VERSION-x64.dmg"
  "$CODE_ARCHIVE"
  "$HELPER_ASSET"
  "$MANIFEST"
)
for f in "${ASSETS[@]}"; do
  [ -f "$f" ] || { echo "release-local: missing expected release asset: $f" >&2; exit 1; }
done

if [ -n "$OUT_DIR" ]; then
  mkdir -p "$OUT_DIR"
  cp -f "${ASSETS[@]}" "$OUT_DIR/"
  echo "release-local: copied release assets to $OUT_DIR"
fi

if [ "$NO_UPLOAD" = 1 ]; then
  echo "release-local: --no-upload set; skipped GitHub draft release upload"
  exit 0
fi

# Upload to the DRAFT GitHub Release (never a published one)
echo "release-local: uploading assets to draft release $TAG"
sed -e "s/{TAG}/${TAG}/g" -e "s/{VERSION}/${VERSION}/g" "$WT/desktop/release-notes.md" > "$TMP_DIR/notes.md"
draft_state="$("$GH_BIN" release view "$TAG" "${GH_REPO_ARGS[@]}" --json isDraft --jq .isDraft 2>/dev/null || true)"
if [ "$draft_state" = "false" ]; then
  echo "release-local: the release $TAG is published already; this script changes only a draft" >&2
  exit 1
elif [ "$draft_state" = "true" ]; then
  "$GH_BIN" release upload "$TAG" "${ASSETS[@]}" --clobber "${GH_REPO_ARGS[@]}"
  "$GH_BIN" release edit "$TAG" --target "$SHA" "${GH_REPO_ARGS[@]}"
else
  "$GH_BIN" release create "$TAG" "${ASSETS[@]}" --draft --title "MaximizePM ${TAG}" \
    --notes-file "$TMP_DIR/notes.md" --target "$SHA" "${GH_REPO_ARGS[@]}"
fi
echo "release-local: uploaded signed and notarized draft release $TAG at $SHA"
