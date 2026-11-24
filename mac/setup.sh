#!/bin/bash
# One-time Mac setup for the Desk Creature companion.
set -e
echo "== Desk Creature: Mac setup =="
cd ~/BITCH-Robot && git pull -q
if [ ! -x ~/dc-venv/bin/python ]; then echo "-> creating Python environment"; python3 -m venv ~/dc-venv; fi
echo "-> installing speech recognition, mic and camera support (a few minutes)"
~/dc-venv/bin/pip install -q --upgrade pip
~/dc-venv/bin/pip install -q -r ~/BITCH-Robot/mac/requirements.txt
echo "-> installing the word voice (Kokoro)"
~/dc-venv/bin/pip install -q kokoro soundfile || echo "!! word voice install failed; creature babble will be used"
echo "-> downloading the face finder"
curl -fsSL -o ~/BITCH-Robot/mac/face_detection_yunet_2023mar.onnx https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx || true
if command -v ollama >/dev/null; then echo "-> downloading the brain model (qwen3.5:4b, ~3 GB)"; ollama pull qwen3.5:4b || ollama pull qwen3:4b-instruct
else echo "!! Ollama command not found. Open the Ollama app once, then run this again."; fi
echo "== done. Start it with:  cd ~/BITCH-Robot && ~/dc-venv/bin/python mac/companion.py   (opens the control room) =="
