#!/bin/bash
# One-time Mac setup for the Desk Creature companion.
set -e
echo "== Desk Creature: Mac setup =="
cd ~/BITCH-Robot && git pull -q
if [ ! -x ~/dc-venv/bin/python ]; then echo "-> creating Python environment"; python3 -m venv ~/dc-venv; fi
echo "-> installing speech recognition, mic and camera support (a few minutes)"
~/dc-venv/bin/pip install -q --upgrade pip
~/dc-venv/bin/pip install -q -r ~/BITCH-Robot/mac/requirements.txt
if command -v ollama >/dev/null; then echo "-> downloading the brain model (qwen3.5:4b, ~3 GB)"; ollama pull qwen3.5:4b || ollama pull qwen3:4b-instruct
else echo "!! Ollama command not found. Open the Ollama app once, then run this again."; fi
if [ "$1" == "--words" ]; then
  if command -v brew >/dev/null; then brew install espeak-ng; fi
  ~/dc-venv/bin/pip install -q kokoro soundfile
fi
echo "== done. Start it with:  cd ~/BITCH-Robot && ~/dc-venv/bin/python mac/companion.py =="
