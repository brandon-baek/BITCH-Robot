#!/bin/bash
# What the BITCH app runs (started by mac/launcher.c, so macOS knows it as BITCH for camera and mic access).
cd ~/BITCH-Robot && git pull -q --ff-only 2>/dev/null
curl -s -m 1 localhost:11434 >/dev/null || (open -a Ollama 2>/dev/null; sleep 3)
pkill -f "[-]m core" 2>/dev/null; sleep 0.5                           # only one BITCH at a time
caffeinate -i ~/dc-venv/bin/python -m core --app >> ~/.bitch/log.txt 2>&1   # caffeinate: the Mac stays awake while BITCH runs
