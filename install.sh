#!/bin/sh
# MaximizePM setup: put `maxpm` on PATH and install the Claude Code skills.
# Usage: ./install.sh [--bin-dir DIR] [--no-skills]
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"
SKILLS=1
while [ $# -gt 0 ]; do
  case "$1" in
    --bin-dir) BIN_DIR="$2"; shift 2 ;;
    --no-skills) SKILLS=0; shift ;;
    -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

# No Python 3.10 here (a new Mac has 3.9): name the installer that brings its own Python (#5).
# The printed command is one to copy and paste: the checkout's path goes in single quotes (a space in it, #10).
quoted() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
no_python() {
  echo "$1" >&2
  echo "or install MaximizePM with its own Python, with no Python on this computer:" >&2
  echo "  sh $(quoted "$ROOT/install-standalone.sh") --from $(quoted "$ROOT")" >&2
  exit 1
}
command -v python3 >/dev/null || no_python "python3 is required (3.10 or later)"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || no_python "python3 3.10 or later is required; this one is $(python3 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null)"

mkdir -p "$BIN_DIR"
ln -sf "$ROOT/bin/maxpm" "$BIN_DIR/maxpm"
echo "linked $BIN_DIR/maxpm -> $ROOT/bin/maxpm"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "note: $BIN_DIR is not on PATH; add it to your shell profile" ;;
esac

if [ "$SKILLS" = 1 ] && [ -d "$HOME/.claude" ]; then
  mkdir -p "$HOME/.claude/skills"
  for s in maxpm maxpm-manager; do
    ln -sfn "$ROOT/skills/$s" "$HOME/.claude/skills/$s"
    echo "linked ~/.claude/skills/$s"
  done
  # The planner guide is gone (#4978): remove its link, which would point at nothing.
  if [ -L "$HOME/.claude/skills/maxpm-planner" ]; then
    rm "$HOME/.claude/skills/maxpm-planner"
    echo "removed ~/.claude/skills/maxpm-planner"
  fi
fi

cat <<'NEXT'

Next, in each project folder:
  maxpm init --description "what this project covers and what context helps"
  maxpm add <project> "first item"
Then open an agent in that folder and say: go
Watch the queue:  maxpm serve --open
NEXT
