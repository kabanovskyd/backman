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
echo 'export PATH=$PWD/.venv/bin:$PATH' >> ~/.bashrc
source ~/.bashrc