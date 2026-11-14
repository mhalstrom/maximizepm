#!/bin/sh
# MaximizePM with its own Python, for a computer that has no Python of its own: a Linux server or a Mac mini (#5873).
# It downloads a MaximizePM release and the Python that MaximizePM carries (python-build-standalone 3.13), checks
# both by sha256, puts them in ~/.local/share/maximizepm, and writes the maxpm command into ~/.local/bin.
# Run it again to update; the queue (~/.maximizepm) does not change.
#
#   curl -fsSL https://raw.githubusercontent.com/mhalstrom/maximizepm/main/install-standalone.sh | sh
#   sh install-standalone.sh [--version vX.Y.Z | --from <checkout>] [--dir DIR] [--bin-dir DIR] [--force]
#
# --version: that release (default: the latest). --from: the code of a checkout instead of a release.
# --force: replace a maxpm command in the bin folder that this script did not write.
set -eu

SLUG=mhalstrom/maximizepm
DIR="${XDG_DATA_HOME:-$HOME/.local/share}/maximizepm"
BIN_DIR="$HOME/.local/bin"
VERSION=""
FROM=""
FORCE=0
MARK="# MaximizePM launcher"
# Parts of the standard library that MaximizePM never uses (as desktop/scripts/fetch-python.js drops them).
DROP="test idlelib tkinter turtledemo ensurepip lib2to3"

die() { printf 'install-standalone.sh: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --version) [ $# -ge 2 ] || die "--version needs a version, for example v1.2.0"; VERSION="$2"; shift 2 ;;
    --from) [ $# -ge 2 ] || die "--from needs a folder"; FROM="$2"; shift 2 ;;
    --dir) [ $# -ge 2 ] || die "--dir needs a folder"; DIR="$2"; shift 2 ;;
    --bin-dir) [ $# -ge 2 ] || die "--bin-dir needs a folder"; BIN_DIR="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done
[ -z "$VERSION" ] || [ -z "$FROM" ] || die "use --version or --from, not both"
case "$DIR$BIN_DIR" in *"'"*) die "a folder name with a ' in it is not supported" ;; esac

for c in curl tar uname; do
  command -v "$c" >/dev/null 2>&1 || die "$c is needed and is not on this computer"
done
if command -v sha256sum >/dev/null 2>&1; then
  sha() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
  sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
  die "sha256sum or shasum is needed to check the downloads"
fi

case "$(uname -s)" in
  Linux) OS=linux ;;
  Darwin) OS=mac ;;
  *) die "MaximizePM runs on Linux and macOS (on Windows: inside WSL 2); this is $(uname -s)" ;;
esac
case "$(uname -m)" in
  x86_64|amd64) ARCH=x64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) die "no Python build for the processor $(uname -m); install Python 3.10 or later and use install.sh" ;;
esac
if [ "$OS" = linux ] && ldd --version 2>&1 | grep -qi musl; then
  die "this Linux uses musl (Alpine), and the Python build needs glibc; install Python 3.10 or later and use install.sh"
fi

fetch() {  # url file
  curl -fsSL --retry 3 -o "$2" "$1" || die "could not download $1"
}

# The maxpm command: one there that this script did not write (a link to a clone) stays, unless --force. The check
# comes before any change.
L="$BIN_DIR/maxpm"
OURS=0
if [ ! -L "$L" ] && grep -q "^$MARK" "$L" 2>/dev/null; then
  OURS=1
elif { [ -e "$L" ] || [ -L "$L" ]; } && [ "$FORCE" = 0 ]; then
  die "$L exists and is not the launcher of this install; move it away, or run again with --force"
fi

mkdir -p "$DIR"
WORK="$DIR/.install.$$"  # inside DIR, so the last moves are renames on one disk
rm -rf "$WORK"
mkdir "$WORK"
trap 'rm -rf "$WORK"' EXIT INT TERM

# 1. The code: a release's code archive (named with its sha256 in the release's manifest.json), or a checkout.
if [ -n "$FROM" ]; then
  [ -f "$FROM/bin/maxpm" ] && [ -d "$FROM/river" ] || die "$FROM is not a MaximizePM checkout (no bin/maxpm and river)"
  mkdir "$WORK/code"
  cp -R "$FROM/bin" "$FROM/river" "$WORK/code/"
  [ ! -d "$FROM/skills" ] || cp -R "$FROM/skills" "$WORK/code/"
  find "$WORK/code" -name __pycache__ -type d -prune -exec rm -rf {} +
  WHAT="the code of $FROM"
else
  if [ -z "$VERSION" ]; then
    fetch "https://api.github.com/repos/$SLUG/releases/latest" "$WORK/latest.json"
    VERSION=$(sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' "$WORK/latest.json" | head -n 1)
    [ -n "$VERSION" ] || die "GitHub named no latest release of $SLUG"
  fi
  case "$VERSION" in v*) ;; *) VERSION="v$VERSION" ;; esac
  base="https://github.com/$SLUG/releases/download/$VERSION"
  fetch "$base/manifest.json" "$WORK/manifest.json"
  name=$(sed -n 's/.*"name": *"\([^"/]*\)".*/\1/p' "$WORK/manifest.json" | head -n 1)
  want=$(sed -n 's/.*"sha256": *"\([0-9a-f]\{64\}\)".*/\1/p' "$WORK/manifest.json" | head -n 1)
  [ -n "$name" ] && [ -n "$want" ] || die "the manifest.json of $VERSION names no code archive"
  fetch "$base/$name" "$WORK/code.tar.gz"
  [ "$(sha "$WORK/code.tar.gz")" = "$want" ] || die "$name does not have the sha256 that manifest.json gives"
  mkdir "$WORK/x"
  tar -xzf "$WORK/code.tar.gz" -C "$WORK/x"
  for d in "$WORK"/x/*/; do mv "$d" "$WORK/code"; break; done
  [ -f "$WORK/code/bin/maxpm" ] || die "$name holds no bin/maxpm"
  WHAT="MaximizePM $VERSION"
fi
PINS="$WORK/code/river/python-builds.txt"
[ -f "$PINS" ] || die "$WHAT is older than this install way (no river/python-builds.txt); pick a newer --version"

# 2. The Python of the pins, unless DIR has that one already.
line=$(grep "^$OS-$ARCH " "$PINS" || true)
[ -n "$line" ] || die "river/python-builds.txt has no Python for $OS-$ARCH"
file=$(echo "$line" | cut -d' ' -f2)
pysha=$(echo "$line" | cut -d' ' -f3)
release=$(sed -n 's/^release \([0-9]*\)$/\1/p' "$PINS")
if [ "$(cat "$DIR/python/SHA256" 2>/dev/null || true)" = "$pysha" ] && [ -x "$DIR/python/bin/python3" ]; then
  echo "Python: $DIR/python is current"
else
  url="https://github.com/astral-sh/python-build-standalone/releases/download/$release/$(echo "$file" | sed 's/+/%2B/g')"
  echo "Python: $url"
  fetch "$url" "$WORK/python.tar.gz"
  [ "$(sha "$WORK/python.tar.gz")" = "$pysha" ] || die "$file does not have the sha256 of river/python-builds.txt"
  tar -xzf "$WORK/python.tar.gz" -C "$WORK"
  for lib in "$WORK"/python/lib/python3.*; do
    for d in $DROP; do rm -rf "${lib:?}/$d"; done
  done
  echo "$pysha" > "$WORK/python/SHA256"
  rm -rf "$DIR/python.old"
  [ ! -d "$DIR/python" ] || mv "$DIR/python" "$DIR/python.old"
  mv "$WORK/python" "$DIR/python"
  rm -rf "$DIR/python.old"
fi
PY="$DIR/python/bin/python3"

# 3. The new code must start with this Python before it replaces the old.
got=$(MAXPM_LOCAL=1 "$PY" -S "$WORK/code/bin/maxpm" --version 2>&1) || die "$WHAT does not start with $PY: $got"
UPDATE=0
rm -rf "$DIR/code.old"
if [ -d "$DIR/code" ]; then mv "$DIR/code" "$DIR/code.old"; UPDATE=1; fi
mv "$WORK/code" "$DIR/code"
rm -rf "$DIR/code.old"

# 4. The maxpm command (checked at the start).
if [ -e "$L" ] || [ -L "$L" ]; then
  [ "$OURS" = 1 ] || echo "replaces $L (--force)"
  rm -f "$L"  # never write through a link into a clone's bin/maxpm
fi
mkdir -p "$BIN_DIR"
cat > "$L" <<EOF
#!/bin/sh
$MARK: runs the MaximizePM that install-standalone.sh put in $DIR.
# install-standalone.sh rewrites it; delete it and $DIR to remove MaximizePM.
exec '$PY' -S '$DIR/code/bin/maxpm' "\$@"
EOF
chmod 755 "$L"

echo "Installed: $got ($WHAT) in $DIR"
echo "The maxpm command: $L"
if [ -d "$HOME/.claude" ] && [ -d "$DIR/code/skills" ]; then
  MAXPM_LOCAL=1 "$L" skills install || echo "note: the Claude Code skills were not linked; run: maxpm skills install"
fi
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "note: $BIN_DIR is not on PATH; add this line to your shell profile: export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac
if [ "$UPDATE" = 1 ]; then
  echo "If maxpm serve runs, start it again on the new code: maxpm serve --restart"
else
  cat <<'NEXT'

Next:
  maxpm serve --install-login    maxpm serve at login, and again after a crash
  maxpm phone --on               the page on your phone and laptop over Tailscale
NEXT
fi
