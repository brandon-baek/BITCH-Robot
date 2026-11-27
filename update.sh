#!/bin/bash
# Pulls the latest BITCH code, links it into the home folder, and restarts BITCH's body.
set -e
cd ~/BITCH-Robot
git pull --ff-only -q
for f in actor.py character.py desk.py shows.py stage.py tests.py sounds.py music.py control.py brain.py macros.py desktop_chatgpt.py desktop_cloud.py face.py hardware.py center.py; do ln -sf ~/BITCH-Robot/$f ~/$f; done
# Run brain.py as a user service (starts with the desktop, restarts on a crash) instead of from a shell.
mkdir -p ~/.config/systemd/user
ln -sf ~/BITCH-Robot/pi/bitch-brain.service ~/.config/systemd/user/bitch-brain.service
systemctl --user daemon-reload
systemctl --user enable -q bitch-brain.service
# A copy started by hand from a shell would hold port 8765.
pkill -u "$USER" -f '^python3 .*/brain\.py' 2>/dev/null || true
systemctl --user restart bitch-brain.service
echo "BITCH updated: $(git log -1 --format=%s)"
echo "Body: $(systemctl --user is-active bitch-brain.service)   (logs: journalctl --user -u bitch-brain -f)"
