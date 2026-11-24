#!/bin/bash
# Build, sign, and verify the macOS MaximizePM Keychain helper (#5838, #5839).
#
#   build-keychain-helper.sh build <output>
#   build-keychain-helper.sh sign <binary> <cert.p12>            (reads APPLE_CERT_PASSWORD from env)
#   build-keychain-helper.sh verify [--signed] [--team ID] <binary>
#
# The helper must keep the same signing identifier (com.maximizepm.keychain-helper) and Developer ID
# team ID (CWYV4R338W) in every release because macOS Keychain ACLs trust that team + identifier.
# Hardened runtime is enabled with no extra entitlements (no disable-library-validation or allow-jit).
set -euo pipefail

IDENTIFIER="com.maximizepm.keychain-helper"
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
REPO_DIR=$(cd "$SCRIPT_DIR/../.." && pwd)
SOURCE="$REPO_DIR/desktop/keychain-helper/keychain-helper.c"

usage() {
  echo "usage: build-keychain-helper.sh build <output> | sign <binary> <cert.p12> | verify [--signed] [--team ID] <binary>" >&2
  exit 2
}

cmd_build() {
  local out=${1:?missing output path}
  [ -f "$SOURCE" ] || { echo "missing source: $SOURCE" >&2; exit 1; }
  mkdir -p "$(dirname "$out")"
  clang -O2 -std=c11 -arch arm64 -arch x86_64 -mmacosx-version-min=12.0 \
    -Wall -Wextra -Werror -Wno-deprecated-declarations \
    -framework Security -framework CoreFoundation \
    "$SOURCE" -o "$out"
  chmod 755 "$out"
  codesign --force --sign - --identifier "$IDENTIFIER" "$out"
}

cmd_sign() {
  local binary=${1:?missing binary path}
  local p12=${2:?missing .p12 path}
  [ -x "$binary" ] || { echo "keychain helper is not executable: $binary" >&2; exit 1; }
  [ -f "$p12" ] || { echo "missing .p12 file: $p12" >&2; exit 1; }
  : "${APPLE_CERT_PASSWORD:?APPLE_CERT_PASSWORD must be set}"

  local tmp kc kc_pass identity
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/maxpm-kh-sign.XXXXXX")
  kc="$tmp/sign.keychain-db"
  cleanup() {
    security delete-keychain "$kc" >/dev/null 2>&1 || true
    rm -rf "$tmp"
  }
  trap cleanup EXIT
  chmod 700 "$tmp"

  kc_pass=$(/usr/bin/openssl rand -hex 24)
  security create-keychain -p "$kc_pass" "$kc"
  security set-keychain-settings -lut 21600 "$kc"
  security unlock-keychain -p "$kc_pass" "$kc"
  security import "$p12" -k "$kc" -P "$APPLE_CERT_PASSWORD" -T /usr/bin/codesign -T /usr/bin/security >/dev/null
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$kc_pass" "$kc" >/dev/null

  identity=$(security find-identity -v -p codesigning "$kc" | awk '/Developer ID Application:/ { print $2; exit }')
  [ -n "$identity" ] || { echo "no Developer ID Application identity found in $p12" >&2; exit 1; }

  codesign --force --sign "$identity" --keychain "$kc" \
    --identifier "$IDENTIFIER" \
    --options runtime --timestamp \
    "$binary"

  cmd_verify --signed "$binary"
}

cmd_verify() {
  local signed=0
  local team=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --signed) signed=1; shift ;;
      --team) team=${2:?missing team id}; shift 2 ;;
      -*) usage ;;
      *) break ;;
    esac
  done
  local binary=${1:?missing binary path}
  [ -x "$binary" ] || { echo "keychain helper is missing or not executable: $binary" >&2; exit 1; }

  local archs
  archs=$(lipo -archs "$binary")
  case " $archs " in
    *" arm64 "*) ;;
    *) echo "keychain helper lacks arm64 slice: $archs" >&2; exit 1 ;;
  esac
  case " $archs " in
    *" x86_64 "*) ;;
    *) echo "keychain helper lacks x86_64 slice: $archs" >&2; exit 1 ;;
  esac

  codesign --verify --strict -v "$binary"

  local info
  info=$(codesign -dvv "$binary" 2>&1)
  if ! printf '%s\n' "$info" | grep -qx "Identifier=$IDENTIFIER"; then
    echo "unexpected keychain helper identifier (expected $IDENTIFIER):" >&2
    printf '%s\n' "$info" >&2
    exit 1
  fi

  local ent
  ent=$(codesign -d --entitlements - "$binary" 2>&1 || true)
  if printf '%s\n' "$ent" | grep -q "disable-library-validation"; then
    echo "keychain helper must not carry disable-library-validation entitlement" >&2
    exit 1
  fi

  if [ "$signed" = 1 ]; then
    if ! printf '%s\n' "$info" | grep -q "^Authority=Developer ID Application"; then
      echo "keychain helper is not signed with Developer ID Application:" >&2
      printf '%s\n' "$info" >&2
      exit 1
    fi
    local got_team
    got_team=$(printf '%s\n' "$info" | sed -n 's/^TeamIdentifier=//p' | head -1)
    if ! printf '%s\n' "$got_team" | grep -Eq '^[A-Z0-9]{10}$'; then
      echo "keychain helper has no valid TeamIdentifier: ${got_team:-<empty>}" >&2
      exit 1
    fi
    if [ -n "$team" ] && [ "$got_team" != "$team" ]; then
      echo "keychain helper TeamIdentifier is $got_team, expected $team" >&2
      exit 1
    fi
    if ! printf '%s\n' "$info" | grep -Eq '^CodeDirectory .*flags=.*\(runtime\)'; then
      echo "keychain helper does not have hardened runtime enabled:" >&2
      printf '%s\n' "$info" >&2
      exit 1
    fi
  fi
}

[ $# -ge 1 ] || usage
sub=$1
shift
case "$sub" in
  build) cmd_build "$@" ;;
  sign) cmd_sign "$@" ;;
  verify) cmd_verify "$@" ;;
  *) usage ;;
esac
