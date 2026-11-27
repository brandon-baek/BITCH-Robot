#!/bin/zsh
# Try a chat model for BITCH, live: replies scroll by as they're made, with the time left.
#   testing/eval_candidate.sh MODEL [NAME]
# Three parts on BITCH's real model server (llama-server): conversation and personality (~5-10 min),
# tools (~2 min), and who-was-that-for in a busy room (~1 min). Results land in testing/model_reports/.
M="$1"; T="${2:-candidate}"
cd "$(dirname "$0")/.."
export OLLAMA_URL=${OLLAMA_URL:-http://127.0.0.1:11436} PYTHONUNBUFFERED=1
out=testing/model_reports/runs; mkdir -p $out
print -P "%B== $M: conversation and personality%b"
~/dc-venv/bin/python -m testing.eval_models "$M" 2>&1 | grep --line-buffered -v -i warn | tee $out/$T-chat.txt
print -P "%B== $M: tools%b"
BITCH_MODEL="$M" BITCH_HOME=$(mktemp -d) ~/dc-venv/bin/python -m testing.eval_tools 2>&1 | grep --line-buffered -v -i warn | tee $out/$T-tools.txt
print -P "%B== $M: busy room%b"
BITCH_MODEL="$M" BITCH_HOME=$(mktemp -d) ~/dc-venv/bin/python -m testing.eval_group 2>&1 | grep --line-buffered -v -i warn | tee $out/$T-room.txt
print -P "%B== $M: summary%b"
tail -1 $out/$T-chat.txt; grep passed $out/$T-tools.txt; grep correct $out/$T-room.txt
