#!/bin/sh
# Fetch the whisper model on first boot, then start the dashboard.
# Knobs:
#   WHISPER_MODEL_NAME    ggml model file name (default ggml-small.en.bin)
#   WHISPER_CPP_MODEL_URL full URL override for the model download
#   WHISPER_CPP_MODEL     full path override (skip download if file exists)
#   PORT                  listen port (default 5544)
# Extra args are passed through to app.py (e.g. --no-tail --no-whisper).
set -e

MODEL_NAME="${WHISPER_MODEL_NAME:-ggml-small.en.bin}"
MODEL_URL="${WHISPER_CPP_MODEL_URL:-https://huggingface.co/ggerganov/whisper.cpp/resolve/main/${MODEL_NAME}}"
export WHISPER_CPP_MODEL="${WHISPER_CPP_MODEL:-${SDRTD_DATA_DIR:-/data}/models/${MODEL_NAME}}"

if [ ! -f "$WHISPER_CPP_MODEL" ]; then
    mkdir -p "$(dirname "$WHISPER_CPP_MODEL")"
    echo "[boot] downloading whisper model $MODEL_NAME"
    curl -fL --retry 3 "$MODEL_URL" -o "$WHISPER_CPP_MODEL.tmp"
    mv "$WHISPER_CPP_MODEL.tmp" "$WHISPER_CPP_MODEL"
fi

exec python app.py --host 0.0.0.0 --port "${PORT:-5544}" "$@"
