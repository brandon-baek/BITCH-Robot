#!/bin/bash
# Pulls the latest Desk Creature code and links it into the home folder.
set -e
cd ~/BITCH-Robot
git pull --ff-only -q
for f in desk.py shows.py tests.py sounds.py; do ln -sf ~/BITCH-Robot/$f ~/$f; done
echo "Desk Creature updated: $(git log -1 --format=%s)"
