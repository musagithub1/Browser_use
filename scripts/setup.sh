#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
/usr/bin/python3 - <<'PY' || {
from jev_linux.environment import clean_editor_environment
clean_editor_environment()
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Atspi", "2.0")
from gi.repository import Gtk, Atspi, Gio
PY
  echo 'Install Ubuntu desktop dependencies: sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-atspi-2.0 alsa-utils python3-venv'
  exit 1
}

if [[ -e .venv || -L .venv ]]; then
  if [[ ! -d .venv || -L .venv || ! -f .venv/pyvenv.cfg || ! -x .venv/bin/python ]]; then
    echo 'The existing .venv is incomplete or is not a project virtual environment. Preserve it elsewhere before running setup again.'
    exit 1
  fi
  .venv/bin/python - <<'PY' || {
from pathlib import Path
import sys
if sys.prefix == sys.base_prefix or Path(sys.prefix).resolve() != Path(".venv").resolve():
    raise SystemExit("The existing Python does not belong to this project's .venv.")
from jev_linux.environment import clean_editor_environment
clean_editor_environment()
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Atspi", "2.0")
from gi.repository import Gtk, Atspi, Gio
PY
    echo 'The existing .venv needs Ubuntu system GI bindings. Preserve it elsewhere and rerun setup; existing settings and models will be retained.'
    exit 1
  }
  echo 'Updating dependencies in the existing .venv.'
elif command -v uv >/dev/null 2>&1; then
  uv venv --python /usr/bin/python3 --system-site-packages .venv
else
  /usr/bin/python3 -m venv --system-site-packages .venv
fi

if command -v uv >/dev/null 2>&1; then
  uv pip install --python .venv/bin/python -r requirements.txt
else
  .venv/bin/python -m pip install -r requirements.txt
fi

speech_setup_args=()
if .venv/bin/python - <<'PY'
from jev_linux.config import CONFIG_PATH
raise SystemExit(0 if CONFIG_PATH.exists() else 1)
PY
then
  speech_setup_args+=(--no-config)
  echo 'Keeping the existing speech selection and settings.'
fi
.venv/bin/python scripts/setup_speech.py "${speech_setup_args[@]}"
.venv/bin/python scripts/install_launcher.py
echo 'Ready. Run ./run.sh or open Jev Linux from your app launcher.'
