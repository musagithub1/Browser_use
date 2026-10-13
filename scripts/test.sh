#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
if [[ ! -x .venv/bin/python ]]; then
  echo "Run ./scripts/setup.sh first to create .venv with Ubuntu's system GI bindings." >&2
  exit 1
fi

exec .venv/bin/python - "$@" <<'PY'
import sys

from jev_linux.environment import clean_editor_environment

# Clean Snap editor paths before test discovery can import native GI libraries.
clean_editor_environment()

import unittest

arguments = sys.argv[1:] or ["discover", "-s", "tests", "-v"]
unittest.main(module=None, argv=["unittest", *arguments])
PY
