#!/bin/bash
# Start the installed companion without changing the checkout.
set -e
cd ~/BITCH-Robot
mkdir -p ~/.bitch
# BITCH has its own background Ollama (a launch agent on port 11436), so the Ollama app never needs to open.
curl -s -m 1 http://127.0.0.1:11436/api/version >/dev/null || launchctl kickstart "gui/$(id -u)/com.brandonbaek.bitch.ollama" 2>/dev/null || true
exec caffeinate -i ~/dc-venv/bin/python -m core --app >> ~/.bitch/log.txt 2>&1
