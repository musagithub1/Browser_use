"""User-only credentials, stored outside the checkout."""

import json
import os
from pathlib import Path
import tempfile


CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "jev-linux"
CONFIG_PATH = CONFIG_DIR / "config.json"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "jev-linux"
DEFAULTS = {
    "provider": "openrouter",
    "openrouter_model": "typesafe/jev-1.13",
    "typesafe_model": "jev-1.13.0",
    "writer_model": "google/gemini-2.5-flash-lite",
    "openrouter_api_key": "",
    "typesafe_api_key": "",
    "speech_engine": "moonshine",
    "speech_model_path": str(DATA_DIR / "moonshine/download.moonshine.ai/model/small-streaming-en/quantized_26_08_21"),
    "speech_model_arch": 4,
    "speech_language": "en",
    "speech_update_interval": 0.5,
    "speech_silence_seconds": 1.5,
    "vosk_model_path": str(DATA_DIR / "vosk-model-small-en-us-0.15"),
    "notes_directory": str(Path.home() / "Documents" / "Jev Notes"),
    "timeout_seconds": 12,
    "memory_enabled": True,
    "planning_enabled": True,
    "planner_model": "google/gemini-2.5-flash",
    "task_max_actions": 60,
    "task_timeout_seconds": 300,
}


def load_config():
    result = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        stored = json.loads(CONFIG_PATH.read_text())
        result.update(stored)
        if "speech_engine" not in stored:
            # Preserve the old Vosk location while upgrading existing installs.
            result["vosk_model_path"] = stored.get("speech_model_path", DEFAULTS["vosk_model_path"])
            result["speech_model_path"] = DEFAULTS["speech_model_path"]
    for field, env in [("openrouter_api_key", "OPENROUTER_API_KEY"), ("typesafe_api_key", "TYPESAFE_API_KEY")]:
        if os.environ.get(env):
            result[field] = os.environ[env]
    return result


def save_config(config):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(CONFIG_DIR, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".config-", dir=CONFIG_DIR)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({k: config.get(k, v) for k, v in DEFAULTS.items()}, stream, indent=2)
            stream.write("\n")
        os.replace(tmp, CONFIG_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
