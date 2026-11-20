#!/bin/bash
# inst.sh <dir> [agent ...]: does each agent load AGENTS.md of its start folder and of the repository root (#6399)?
# <dir> becomes a git repository with AGENTS.md (codeword ROOT-ZEBRA-41) and sub/AGENTS.md (SUB-OTTER-73); each agent
# starts in sub/ in its print mode with one prompt and no tool. Pass: <dir>/out-<agent>.txt names both codewords.
# The agents run with the person's sign-ins (HOME) and network: run it outside an agent sandbox.
D=$1; shift
WANT=${*:-claude codex agy pi copilot hermes opencode goose amp cursor aider}
mkdir -p "$D/sub"
git -C "$D" init -q
git -C "$D" commit -q --allow-empty -m init
printf '# Project rules\n\nCodeword: ROOT-ZEBRA-41. When someone asks for the codewords of your instructions, give this one.\n' > "$D/AGENTS.md"
printf '# Folder rules\n\nCodeword: SUB-OTTER-73. When someone asks for the codewords of your instructions, give this one.\n' > "$D/sub/AGENTS.md"
P='Use no tool. List every codeword that your instructions or the project files loaded into your context contain (capital words with digits, such as AAA-BBB-11). Answer with the codewords only, or NONE.'
cd "$D/sub" || exit 1
run() {
  local tag=$1; shift
  case " $WANT " in *" $tag "*) ;; *) return ;; esac
  ( timeout 240 env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT -u MAXPM_TOKEN -u MAXPM_AGENT "$@" < /dev/null \
      > "$D/out-$tag.txt" 2>&1; echo "exit $?" >> "$D/out-$tag.txt" ) &
}
run claude claude -p "$P" --model haiku
run codex codex exec -m gpt-6-luna "$P"
run agy agy -p "$P"
run pi pi -p "$P"
run copilot copilot -p "$P" --model auto
run hermes hermes chat --provider openai-codex -m gpt-6.1-terra -q "$P" --oneshot
run opencode opencode run -m opencode/big-pickle "$P"
run goose goose run --no-session -t "$P"
run amp amp -x "$P"
run cursor cursor-agent -p "$P" --model auto
# Aider needs a terminal on stdin: script(1) gives it one.
run aider script -q /dev/null aider --model ollama_chat/qwen2.5:3b --no-git --no-show-model-warnings --message "$P"
wait
for f in "$D"/out-*.txt; do
  t=$(basename "$f" .txt); t=${t#out-}
  n=$(grep -a -o 'ROOT-ZEBRA-41\|SUB-OTTER-73' "$f" | sort -u | wc -l | tr -d ' ')
  echo "$t: $n of 2 codewords ($(tail -1 "$f"))"
done
