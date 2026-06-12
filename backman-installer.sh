#!/bin/bash

if ! command -v uv &> /dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Source the env file uv's installer adds so the binary is available immediately
    [ -f "$HOME/.local/bin" ] && source "$HOME/.local/bin"
    export PATH="$HOME/.local/bin:$PATH"
else
    echo "uv installed at $(which uv)"
fi

uv venv
uv tool install -e .
backman_path=$(find . -name "backman" -type f)
export PATH=$PWD/.venv/bin:$PATH
echo 'export PATH=$PWD/.venv/bin:$PATH' >> "$HOME/.${SHELL##*/}rc"
source "$HOME/.${SHELL##*/}rc"

if ! command -v gcloud >/dev/null 2>&1; then
  echo "[ERROR]: gcloud is not installed or not on PATH"
  exit 1
fi

ACTIVE_ACCOUNT=$(gcloud config get-value account 2>/dev/null)
if [[ -z "$ACTIVE_ACCOUNT" || "$ACTIVE_ACCOUNT" == "(unset)" ]]; then
  echo "[ERROR]: no active gcloud account. Run \`gcloud auth login\` to activate your account."
  exit 1
fi