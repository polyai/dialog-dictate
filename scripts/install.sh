#!/usr/bin/env bash
# Copyright (c) 2026 PolyAI Limited. All rights reserved.
# Install dialog-dictate on macOS:
#   curl -LsSf https://raw.githubusercontent.com/polyai/dialog-dictate/main/scripts/install.sh | bash
set -euo pipefail

main() {
  [ "$(uname -s)" = "Darwin" ] || { echo "dialog-dictate needs macOS." >&2; exit 1; }
  if ! command -v uv >/dev/null 2>&1; then
    echo "==> Installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh </dev/null
    export PATH="$HOME/.local/bin:$PATH"
  fi
  echo "==> Installing dictate"
  uv tool install --force git+https://github.com/polyai/dialog-dictate </dev/null
  uv tool update-shell </dev/null >/dev/null 2>&1 || true
  export PATH="$(uv tool dir --bin):$PATH"
  echo "==> Saving your API key"
  dictate setup </dev/tty || echo "Run \`dictate setup\` when you have a key."
  echo "==> Done. Run \`dictate\`, hold Right Command and speak."
}

main "$@"
