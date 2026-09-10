#!/usr/bin/env bash
# Trunk Tap -- one-command setup.
#
#   ./install.sh            set up a local venv, deps, whisper backend, config
#   ./install.sh --docker   skip the venv; verify Docker and print that path
#   ./install.sh --help     show usage
#
# Env knobs:
#   TRUNK_TAP_SKIP_WHISPER=1   skip the whisper-backend install/check step
#
# Safe to re-run: every step is idempotent.
set -euo pipefail
cd "$(dirname "$0")"

MODE=local
for arg in "$@"; do
  case "$arg" in
    --docker) MODE=docker ;;
    --help|-h)
      sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# ---- pretty output ------------------------------------------------------------
if [ -t 1 ]; then
  C_STEP=$'\033[1;34m'; C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'
  C_ERR=$'\033[1;31m';  C_DIM=$'\033[2m';   C_OFF=$'\033[0m'
else
  C_STEP=""; C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_OFF=""
fi
step() { printf '\n%s==> %s%s\n' "$C_STEP" "$*" "$C_OFF"; }
ok()   { printf '    %s[ok]%s %s\n'   "$C_OK"   "$C_OFF" "$*"; }
warn() { printf '    %s[!!]%s %s\n'   "$C_WARN" "$C_OFF" "$*"; }
err()  { printf '    %s[xx]%s %s\n'   "$C_ERR"  "$C_OFF" "$*" >&2; }
note() { printf '    %s%s%s\n'        "$C_DIM"  "$*"     "$C_OFF"; }

# ---- platform -----------------------------------------------------------------
step "Detecting platform"
OS="$(uname -s)"        # Darwin | Linux | ...
ARCH="$(uname -m)"      # arm64/aarch64 | x86_64 | ...
case "$OS" in
  Darwin) PLATFORM="macOS" ;;
  Linux)  PLATFORM="Linux" ;;
  *)      PLATFORM="$OS" ;;
esac
ok "$PLATFORM ($ARCH)"

# ---- config bootstrap (shared by local and docker modes) ----------------------
step "Bootstrapping config"
for f in systems coverage_targets; do
  if [ ! -f "config/$f.json" ]; then
    cp "config/$f.example.json" "config/$f.json"
    ok "created config/$f.json from the example -- edit it for your systems"
  else
    ok "config/$f.json already exists"
  fi
done
note "config/*.json is gitignored: your local presets stay out of the repo."

# ---- docker mode --------------------------------------------------------------
if [ "$MODE" = "docker" ]; then
  step "Checking Docker"
  if ! command -v docker >/dev/null 2>&1; then
    err "docker not found on PATH"
    note "Install Docker Desktop (macOS) or Docker Engine (Linux), then re-run."
    exit 1
  fi
  if ! docker compose version >/dev/null 2>&1; then
    err "'docker compose' (v2 plugin) not available"
    note "Update Docker, or install the compose plugin, then re-run."
    exit 1
  fi
  ok "$(docker --version)"
  ok "$(docker compose version)"
  step "You're ready (Docker path)"
  cat <<EOF

    Build and boot everything (dashboard + SQLite + whisper.cpp):

        docker compose up -d --build

    Then open http://localhost:5544 and see README.md, section
    "Point SDRTrunk at it", to connect your scanner.
EOF
  exit 0
fi

# ---- python -------------------------------------------------------------------
step "Checking Python"
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  err "python3 not found"
  case "$OS" in
    Darwin) note "Install it with:  brew install python@3.12" ;;
    Linux)  note "Install it with:  sudo apt update && sudo apt install python3 python3-venv" ;;
  esac
  exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  err "python3 >= 3.10 required (found $("$PY" --version 2>&1))"
  case "$OS" in
    Darwin) note "Upgrade with:  brew install python@3.12" ;;
    Linux)  note "Upgrade with:  sudo apt update && sudo apt install python3 python3-venv" ;;
  esac
  exit 1
fi
ok "$("$PY" --version 2>&1)"

# ---- venv + deps (reuse scripts/start.sh bootstrap; don't fork it) ------------
step "Setting up .venv and installing dependencies"
if [ -d .venv ]; then
  ok ".venv already exists -- skipping creation"
  note "Delete .venv and re-run to rebuild from scratch."
else
  TRUNK_TAP_SETUP_ONLY=1 ./scripts/start.sh
  ok ".venv created and requirements.txt installed"
fi

# ---- whisper backend ----------------------------------------------------------
if [ "${TRUNK_TAP_SKIP_WHISPER:-0}" = "1" ]; then
  step "Whisper backend"
  warn "skipped (TRUNK_TAP_SKIP_WHISPER=1)"
else
  step "Setting up a Whisper transcription backend"
  VPY=./.venv/bin/python
  if "$VPY" -c 'import mlx_whisper' 2>/dev/null; then
    ok "mlx-whisper available (Apple Silicon GPU/ANE backend)"
  elif { [ -n "${WHISPER_CPP_BIN:-}" ] && command -v "$WHISPER_CPP_BIN" >/dev/null 2>&1 \
         && [ -n "${WHISPER_CPP_MODEL:-}" ] && [ -f "$WHISPER_CPP_MODEL" ]; }; then
    ok "whisper.cpp available (\$WHISPER_CPP_BIN + \$WHISPER_CPP_MODEL set)"
  elif "$VPY" -c 'import whisper' 2>/dev/null; then
    ok "openai-whisper available (CPU fallback)"
  elif [ "$OS" = "Darwin" ] && [ "$ARCH" = "arm64" ]; then
    # requirements.txt already asks for mlx-whisper on this platform; this is
    # the safety net if the environment marker didn't match (e.g. old pip).
    note "installing mlx-whisper into .venv ..."
    ./.venv/bin/pip install 'mlx-whisper>=0.4'
    ok "mlx-whisper installed"
  else
    warn "no Whisper backend found -- calls will sit in transcribe_state='pending'"
    note "until you install one. Options:"
    note ""
    note "  whisper.cpp (fast CPU, recommended off Apple Silicon):"
    note "    build whisper.cpp (https://github.com/ggml-org/whisper.cpp), then:"
    note "      export WHISPER_CPP_BIN=/path/to/whisper-cli   # 'main' before v1.7"
    note "      export WHISPER_CPP_MODEL=/path/to/ggml-small.en.bin"
    note ""
    note "  openai-whisper (pure pip, slow CPU fallback):"
    note "    ./.venv/bin/pip install openai-whisper"
    note "    # optional model override: export SDRTD_WHISPER_PY_MODEL=base"
    note ""
    note "  Docker handles all of this for you:  docker compose up -d --build"
  fi
fi

# ---- done ---------------------------------------------------------------------
step "You're ready"
cat <<EOF

    Start the dashboard:

        ./scripts/start.sh           # add --fresh for a clean DB

    Then open http://127.0.0.1:5544

    Next: edit config/systems.json for your radio systems, then see
    README.md, section "Point SDRTrunk at it", to connect your scanner.
EOF
if command -v docker >/dev/null 2>&1; then
  note "Zero-install alternative:  docker compose up -d --build"
fi
