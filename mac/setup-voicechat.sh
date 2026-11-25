#!/bin/bash
set -euo pipefail

voice_home="${BITCH_HOME:-$HOME/.bitch}"
bundle="${BITCH_VOICECHAT_MODEL:-$voice_home/voicechat-int5}"
runtime="$voice_home/voicechat-runtime"
release="v0.0.29"
runtime_hash="ff426a060004dd39b3d00ad014a648747d48cb78b800672aa17221ada2453406"
model="aufklarer/VoiceChat-11B-Perception-MLX-int5"
revision="11c937c1826bb79abbaa210091fafaa14576b785"

if [[ "$(uname -s)" != Darwin || "$(uname -m)" != arm64 ]]; then
    echo "The bundled VoiceChat runtime requires an Apple Silicon Mac." >&2
    exit 1
fi
mkdir -p "$bundle" "$runtime"
if [[ ! -x "$runtime/speech" ]]; then
    archive=$(mktemp "$runtime/.speech-archive.XXXXXX")
    trap 'rm -f "$archive"' EXIT
    curl -fLsS --retry 3 "https://github.com/soniqo/speech-swift/releases/download/$release/speech-macos-arm64.tar.gz" -o "$archive"
    printf '%s  %s\n' "$runtime_hash" "$archive" | shasum -a 256 -c -
    tar -xzf "$archive" -C "$runtime" --exclude speech-server --exclude audio --exclude audio-server
    rm -f "$archive"
    trap - EXIT
fi
"$runtime/speech" voice-chat --help >/dev/null

hf_bin="${BITCH_HF_BIN:-$HOME/dc-venv/bin/hf}"
if [[ ! -x "$hf_bin" ]]; then
    hf_bin=$(command -v hf)
fi
HF_XET_CHUNK_CACHE_SIZE_BYTES=0 "$hf_bin" download "$model" \
    config.json encoder/config.json encoder/model.safetensors \
    llm/config.json llm/model.safetensors llm/special_tokens_map.json llm/tokenizer.json llm/tokenizer_config.json \
    tts/config.json tts/model.safetensors --revision "$revision" \
    --max-workers 4 --local-dir "$bundle"

printf 'VoiceChat ready in %s\nRestart the companion to pick it up. For a custom location set voicechat_model in the Mac settings file.\n' "$bundle"
