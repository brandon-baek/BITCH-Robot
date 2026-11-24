#!/bin/bash
# One-time Mac setup for BITCH. Safe to run again any time (it also updates).
set -e
echo "== BITCH: Mac setup =="
cd ~/BITCH-Robot && git pull -q || true

# Python 3.12 (Kokoro, the natural word voice, does not support 3.13+ yet). uv fetches it, no Homebrew needed.
UV=$(command -v uv || echo ~/.local/bin/uv)
if [ ! -x "$UV" ]; then echo "-> installing uv (Python manager)"; curl -LsSf https://astral.sh/uv/install.sh | sh; UV=~/.local/bin/uv; fi
PYV=$(~/dc-venv/bin/python -c 'import sys;print(sys.version_info[1])' 2>/dev/null || echo 0)
if [ "$PYV" != "12" ]; then echo "-> making a Python 3.12 environment"; rm -rf ~/dc-venv; "$UV" venv -q --seed --python 3.12 ~/dc-venv; fi
PIP="$UV pip install -q --python $HOME/dc-venv/bin/python"

echo "-> installing ears, eyes and the app window (a few minutes)"
$PIP -r ~/BITCH-Robot/mac/requirements.txt
echo "-> installing the word voice (Kokoro)"
$PIP kokoro soundfile || echo "!! Kokoro failed; BITCH will use the Mac's own voice for words"

echo "-> downloading the face finder and face recogniser"
Z=https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models
cd ~/BITCH-Robot/mac
[ -s face_detection_yunet_2023mar.onnx ] || curl -fsSL -o face_detection_yunet_2023mar.onnx $Z/face_detection_yunet/face_detection_yunet_2023mar.onnx || true
[ -s face_recognition_sface_2021dec.onnx ] || curl -fsSL -o face_recognition_sface_2021dec.onnx $Z/face_recognition_sface/face_recognition_sface_2021dec.onnx || true

if command -v ollama >/dev/null; then
  echo "-> downloading the brains: fast (qwen3.5:4b, ~3 GB), deep (qwen3.5:9b, ~7 GB) and memory search (nomic-embed-text)"
  ollama pull qwen3.5:4b || true; ollama pull qwen3.5:9b || echo "!! the deep brain did not download; BITCH will use the fast one for everything"
  ollama pull nomic-embed-text || true
else echo "!! Ollama command not found. Open the Ollama app once, then run this again."; fi

echo "-> making the BITCH app"
APP=~/Applications/BITCH.app; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" ~/.bitch
cat > "$APP/Contents/Info.plist" <<'PL'
<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>BITCH</string><key>CFBundleDisplayName</key><string>BITCH</string>
<key>CFBundleIdentifier</key><string>com.brandonbaek.bitch</string><key>CFBundleVersion</key><string>1</string>
<key>CFBundleExecutable</key><string>BITCH</string><key>CFBundleIconFile</key><string>bitch</string><key>CFBundlePackageType</key><string>APPL</string>
<key>NSCameraUsageDescription</key><string>BITCH watches with the camera so it can look at you and recognise faces. Nothing leaves this Mac.</string>
<key>NSMicrophoneUsageDescription</key><string>BITCH listens so you can talk to it. Speech is turned into text on this Mac.</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PL
cat > "$APP/Contents/MacOS/BITCH" <<'SH'
#!/bin/bash
cd ~/BITCH-Robot && git pull -q --ff-only 2>/dev/null
curl -s -m 1 localhost:11434 >/dev/null || (open -a Ollama 2>/dev/null; sleep 3)
pkill -f "[-]m core" 2>/dev/null; sleep 0.5                           # only one BITCH at a time
exec caffeinate -i ~/dc-venv/bin/python -m core --app >> ~/.bitch/log.txt 2>&1   # caffeinate: the Mac stays awake while BITCH runs
SH
chmod +x "$APP/Contents/MacOS/BITCH"
I=/tmp/bitch.iconset; rm -rf $I; mkdir $I
for s in 16 32 128 256 512; do sips -z $s $s ~/BITCH-Robot/mac/icon.png --out $I/icon_${s}x${s}.png >/dev/null; sips -z $((s*2)) $((s*2)) ~/BITCH-Robot/mac/icon.png --out $I/icon_${s}x${s}@2x.png >/dev/null; done
iconutil -c icns $I -o "$APP/Contents/Resources/bitch.icns" || true
codesign --force -s - "$APP" 2>/dev/null || true
touch "$APP"
echo "== done. Open BITCH from your Applications folder (or Spotlight: BITCH). =="
echo "   It will ask for camera and microphone access the first time: allow both."
echo "   On your phone (same Wi-Fi): http://$(scutil --get LocalHostName).local:8780  (pair with the code in BITCH > More)"
