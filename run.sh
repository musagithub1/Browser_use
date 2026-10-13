#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"
# Editors packaged as Snaps can export incompatible GTK module paths.
for module_var in GTK_PATH GIO_MODULE_DIR GDK_PIXBUF_MODULE_FILE; do
  if [[ "${!module_var:-}" == /snap/* || "${!module_var:-}" == */snap/* ]]; then
    unset "$module_var"
  fi
done
if [[ ! -x .venv/bin/python ]]; then
  echo "Run ./scripts/setup.sh first."
  exit 1
fi
exec .venv/bin/python -m jev_linux "$@"
