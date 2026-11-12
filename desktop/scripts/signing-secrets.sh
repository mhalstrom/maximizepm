#!/bin/bash
# Store the signing secrets of the desktop release (#5615) in GitHub, for .github/workflows/desktop-release.yml.
# Prints only the names of the secrets, never a value.
#
#   desktop/scripts/signing-secrets.sh <developer-id .cer> <AuthKey_XXXX.p8> <key id> <issuer id> [owner/repo]
#
# <developer-id .cer>: the Developer ID Application certificate that Apple made from the request
#   ~/.maximizepm/signing/developer-id.csr (its private key, developer-id.key, never leaves this Mac).
# <AuthKey_XXXX.p8>, <key id>, <issuer id>: an App Store Connect API key for the notary service
#   (Users and Access, Integrations, Team Keys; role Developer).
# The script makes a .p12 of the certificate and the key with a new random password, sets APPLE_CERT_P12 (base64),
# APPLE_CERT_PASSWORD, APPLE_API_KEY_P8, APPLE_API_KEY_ID and APPLE_API_ISSUER with gh secret set, and deletes the
# .p12. Needs gh, signed in with a right to set the repository's secrets.
set -euo pipefail

cer=${1:?usage: signing-secrets.sh <developer-id .cer> <AuthKey_XXXX.p8> <key id> <issuer id> [owner/repo]}
p8=${2:?the .p8 file of the App Store Connect API key}
key_id=${3:?the key id of the API key}
issuer=${4:?the issuer id of the API key}
repo=${5:-mhalstrom/maximizepm}
key=${MAXPM_SIGNING_KEY:-$HOME/.maximizepm/signing/developer-id.key}
# The system's LibreSSL: it writes the .p12 in the older form that the security command of macOS imports.
ssl=/usr/bin/openssl
[ -x "$ssl" ] || ssl=openssl

for f in "$cer" "$p8" "$key"; do [ -f "$f" ] || { echo "no file $f" >&2; exit 1; }; done
command -v gh >/dev/null || { echo "gh is not installed" >&2; exit 1; }

tmp=$(mktemp -d "${TMPDIR:-/tmp}/maxpm-signing.XXXXXX")
trap 'rm -rf "$tmp"' EXIT
chmod 700 "$tmp"

# Apple gives the certificate in DER form; a PEM one works too.
if ! "$ssl" x509 -inform der -in "$cer" -out "$tmp/cert.pem" 2>/dev/null; then
  "$ssl" x509 -in "$cer" -out "$tmp/cert.pem"
fi
subject=$("$ssl" x509 -in "$tmp/cert.pem" -noout -subject)
case "$subject" in
  *"Developer ID Application"*) ;;
  *) echo "this is not a Developer ID Application certificate: $subject" >&2; exit 1 ;;
esac
# The certificate must belong to the private key of the request.
if [ "$("$ssl" x509 -in "$tmp/cert.pem" -noout -modulus)" != "$("$ssl" rsa -in "$key" -noout -modulus 2>/dev/null)" ]; then
  echo "the certificate does not belong to $key: make it from ~/.maximizepm/signing/developer-id.csr" >&2
  exit 1
fi

pass=$("$ssl" rand -base64 30)
legacy=()
"$ssl" version | grep -q '^OpenSSL 3' && legacy=(-legacy)
PASS="$pass" "$ssl" pkcs12 -export ${legacy[@]+"${legacy[@]}"} -inkey "$key" -in "$tmp/cert.pem" -out "$tmp/cert.p12" \
  -name "Developer ID Application" -passout env:PASS
# The .p12 must open with its password, as electron-builder opens it.
PASS="$pass" "$ssl" pkcs12 -in "$tmp/cert.p12" -noout -passin env:PASS ${legacy[@]+"${legacy[@]}"}

base64 < "$tmp/cert.p12" | tr -d '\n' | gh secret set APPLE_CERT_P12 -R "$repo"
printf '%s' "$pass" | gh secret set APPLE_CERT_PASSWORD -R "$repo"
gh secret set APPLE_API_KEY_P8 -R "$repo" < "$p8"
printf '%s' "$key_id" | gh secret set APPLE_API_KEY_ID -R "$repo"
printf '%s' "$issuer" | gh secret set APPLE_API_ISSUER -R "$repo"
echo "set in $repo: APPLE_CERT_P12 APPLE_CERT_PASSWORD APPLE_API_KEY_P8 APPLE_API_KEY_ID APPLE_API_ISSUER"
echo "the next release tag builds a signed and notarized app"
