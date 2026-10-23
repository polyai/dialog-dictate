#!/usr/bin/env bash
# Copyright (c) 2026 PolyAI Limited. All rights reserved.
# Install dialog-dictate on macOS:
#   curl -LsSf https://raw.githubusercontent.com/polyai/dialog-dictate/v0.4.0/scripts/install.sh | bash
set -euo pipefail

# Bump with each release.
DICTATE_VERSION="v0.4.0"
UV_VERSION="0.12.23"

main() {
  [ "$(uname -s)" = "Darwin" ] || { echo "dialog-dictate needs macOS." >&2; exit 1; }
  if ! command -v uv >/dev/null 2>&1; then
    echo "==> Installing uv $UV_VERSION"
    curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" | sh </dev/null
    export PATH="$HOME/.local/bin:$PATH"
  fi
  echo "==> Installing dictate $DICTATE_VERSION"
  uv tool install --force "git+https://github.com/polyai/dialog-dictate@$DICTATE_VERSION" </dev/null
  uv tool update-shell </dev/null >/dev/null 2>&1 || true
  export PATH="$(uv tool dir --bin):$PATH"
  echo "==> Saving your API key"
  dictate setup </dev/tty || echo "Run \`dictate setup\` when you have a key."
  echo "==> Done. Run \`dictate\`, hold your hotkey and speak."
}

main "$@"
