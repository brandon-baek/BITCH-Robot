#!/bin/bash
# Start the installed companion without changing the checkout.
set -e
cd ~/BITCH-Robot
mkdir -p ~/.bitch
open -a Ollama 2>/dev/null || true
exec caffeinate -i ~/dc-venv/bin/python -m core --app >> ~/.bitch/log.txt 2>&1
