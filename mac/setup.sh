#!/bin/bash
# One-time Mac setup for BITCH. Run again to refresh the launcher and dependencies.
set -e
echo "== BITCH: Mac setup =="
cd ~/BITCH-Robot

# Python 3.12 (Kokoro, the natural word voice, does not support 3.13+ yet). uv fetches it, no Homebrew needed.
UV=$(command -v uv || echo ~/.local/bin/uv)
if [ ! -x "$UV" ]; then echo "-> installing uv (Python manager)"; curl -LsSf https://astral.sh/uv/install.sh | sh; UV=~/.local/bin/uv; fi
PYV=$(~/dc-venv/bin/python -c 'import sys;print(sys.version_info[1])' 2>/dev/null || echo 0)
if [ "$PYV" != "12" ]; then echo "-> making a Python 3.12 environment"; "$UV" venv --clear -q --seed --python 3.12 ~/dc-venv; fi
PIP="$UV pip install -q --python $HOME/dc-venv/bin/python"

echo "-> installing ears, eyes and the app window (a few minutes)"
$PIP -r ~/BITCH-Robot/mac/requirements.txt
echo "-> installing the word voice (Kokoro)"
$PIP kokoro soundfile || echo "!! Kokoro failed; BITCH will use the Mac's own voice for words"

echo "-> downloading the face detector"
Z=https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models
cd ~/BITCH-Robot/mac
[ -s face_detection_yunet_2023mar.onnx ] || curl -fsSL -o face_detection_yunet_2023mar.onnx $Z/face_detection_yunet/face_detection_yunet_2023mar.onnx || true
[ -s face_recognition_sface_2021dec.onnx ] || curl -fsSL -o face_recognition_sface_2021dec.onnx "$Z/face_recognition_sface/face_recognition_sface_2021dec.onnx" || true
VOICE_MODEL=3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx
if [ ! -s "$VOICE_MODEL" ]; then
  if curl -fsSL -o "$VOICE_MODEL.part" "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/$VOICE_MODEL" &&
     printf '%s  %s\n' c4947caf150b8502fdd75bbe4089b971ab1ec42e90ee3ba547faca2cde44d9ee "$VOICE_MODEL.part" | shasum -a 256 -c; then
    mv "$VOICE_MODEL.part" "$VOICE_MODEL"
  else
    echo "!! Voice recognition model unavailable"
  fi
fi

POSE_MODEL=pose_landmarker_lite.task
if [ ! -s "$POSE_MODEL" ]; then
  if curl -fsSL -o "$POSE_MODEL.part" "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/$POSE_MODEL" &&
     printf '%s  %s\n' 59929e1d1ee95287735ddd833b19cf4ac46d29bc7afddbbf6753c459690d574a "$POSE_MODEL.part" | shasum -a 256 -c; then
    mv "$POSE_MODEL.part" "$POSE_MODEL"
  else
    echo "!! Pointing recognition model unavailable"
  fi
fi

HAND_MODEL=hand_landmarker.task
if [ ! -s "$HAND_MODEL" ]; then
  if curl -fsSL -o "$HAND_MODEL.part" "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/$HAND_MODEL" &&
     printf '%s  %s\n' fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1 "$HAND_MODEL.part" | shasum -a 256 -c; then
    mv "$HAND_MODEL.part" "$HAND_MODEL"
  else
    echo "!! Finger tracking model unavailable"
  fi
fi


if command -v ollama >/dev/null; then
  echo "-> downloading the local conversation model"
  open -a Ollama 2>/dev/null || true
  for i in {1..10}; do
    if ollama list >/dev/null 2>&1; then break; fi
    sleep 0.5
  done
  # The conversation brain (about 18 GB, needs 32 GB of RAM) and the small model for quick camera checks.
  ollama pull codecraftersllc/ornith-1.5-35b-a3b-abliterated || echo "!! When Ollama is running, run: ollama pull codecraftersllc/ornith-1.5-35b-a3b-abliterated"
  ollama pull qwen3.5:4b-q4_K_M || echo "!! When Ollama is running, run: ollama pull qwen3.5:4b-q4_K_M"
else echo "!! Ollama command not found. Open the Ollama app once, then run this again."; fi

echo "-> making the BITCH app"
APP=~/Applications/BITCH.app; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" ~/.bitch
cat > "$APP/Contents/Info.plist" <<'PL'
<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>BITCH</string><key>CFBundleDisplayName</key><string>BITCH</string>
<key>CFBundleIdentifier</key><string>com.brandonbaek.bitch</string><key>CFBundleVersion</key><string>1</string>
<key>CFBundleExecutable</key><string>BITCH</string><key>CFBundleIconFile</key><string>bitch</string><key>CFBundlePackageType</key><string>APPL</string>
<key>NSCameraUsageDescription</key><string>BITCH watches with the camera so it can look toward you. Video is processed on your Mac.</string>
<key>NSMicrophoneUsageDescription</key><string>BITCH listens so you can talk to it. Speech is turned into text on this Mac.</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PL
clang -O2 -o "$APP/Contents/MacOS/BITCH" ~/BITCH-Robot/mac/launcher.c   # a real program, so macOS can grant it the camera and mic
chmod +x "$APP/Contents/MacOS/BITCH"
tmp=$(mktemp -d "${TMPDIR:-/tmp}/bitch.XXXXXX")
I="$tmp/bitch.iconset"; mkdir "$I"
for s in 16 32 128 256 512; do sips -z $s $s ~/BITCH-Robot/mac/icon.png --out "$I"/icon_${s}x${s}.png >/dev/null; sips -z $((s*2)) $((s*2)) ~/BITCH-Robot/mac/icon.png --out "$I"/icon_${s}x${s}@2x.png >/dev/null; done
iconutil -c icns $I -o "$APP/Contents/Resources/bitch.icns" || true
codesign --force -s - "$APP" 2>/dev/null || true
touch "$APP"
echo "== done. Open BITCH from your Applications folder (or Spotlight: BITCH). =="
echo "   It will ask for camera and microphone access the first time: allow both."
echo "   On your phone (same Wi-Fi): http://$(scutil --get LocalHostName).local:8780  (follow Connections > Pair a phone or another computer)"
