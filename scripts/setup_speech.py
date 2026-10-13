#!/usr/bin/env python3
"""Install and validate fully local English Moonshine streaming speech.

Only the publisher's model files are downloaded. No microphone is opened and
no audio or credentials are sent to any service. Cached model downloads are
reused and checked against the official native catalog before activation.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jev_linux.config import DATA_DIR, load_config, save_config


class Progress:
    """Keep model-download progress readable, including cached files."""

    def __init__(self) -> None:
        self._last_time = 0.0
        self._last_name = ""

    def __call__(self, fraction: float, filename: str) -> None:
        now = time.monotonic()
        if filename != self._last_name or now - self._last_time >= 2 or fraction >= 1:
            print(f"{fraction:6.1%}  {Path(filename).name}", flush=True)
            self._last_name = filename
            self._last_time = now


def verify_assets(cache_root: Path, architecture) -> int:
    """Check fresh and cached files using the publisher's sizes and CRC32C.

    Moonshine 0.1.5 skips CRC32C on its cached-file fast path. Checking it here
    also catches damage to a previously installed model before using it.
    """
    from moonshine_voice.download_file import crc32c_file
    from moonshine_voice.moonshine_api import moonshine_get_stt_dependencies_string

    manifest = json.loads(moonshine_get_stt_dependencies_string("en", {"model_arch": int(architecture)}))
    total = 0
    files = 0
    for group in manifest["groups"]:
        root = cache_root / group["base_url"].removeprefix("https://")
        for asset in group["files"]:
            path = (root / asset["name"]).resolve()
            if not path.is_relative_to(cache_root.resolve()):
                raise RuntimeError("The model catalog contains an invalid local file path.")
            expected_size = asset["size"]
            if not path.is_file() or path.stat().st_size != expected_size:
                raise RuntimeError(f"Model file is missing or incomplete: {asset['name']}. Run setup again.")
            if asset.get("checksum_type") != "crc32c" or not asset.get("checksum"):
                raise RuntimeError(f"The publisher did not supply the required checksum for {asset['name']}.")
            checksum = crc32c_file(path)
            if checksum is None:
                raise RuntimeError("Checksum verification needs google-crc32c. Install the project requirements and try again.")
            if checksum != asset["checksum"]:
                raise RuntimeError(f"Model checksum failed for {asset['name']}. Preserve that file elsewhere and run setup again.")
            total += expected_size
            files += 1
    if not files:
        raise RuntimeError("The publisher returned an empty speech model manifest.")
    return total


def install_speech(model: str = "small", *, write_config: bool = True) -> Path:
    try:
        from moonshine_voice import ModelArch, Transcriber, get_model_for_language
        import google_crc32c  # noqa: F401: required for the publisher's checksum helper
    except ImportError:
        raise RuntimeError("Install the project requirements in .venv, then run .venv/bin/python scripts/setup_speech.py.") from None

    architectures = {"small": ModelArch.SMALL_STREAMING, "medium": ModelArch.MEDIUM_STREAMING}
    if model not in architectures:
        raise ValueError("Choose the small or medium English speech model.")
    architecture = architectures[model]
    cache_root = DATA_DIR / "moonshine"
    cache_root.mkdir(parents=True, exist_ok=True)
    lock_path = cache_root / ".setup.lock"
    with lock_path.open("a") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another speech setup is running. Try again after it finishes.") from None

        print(f"Installing Moonshine {model.title()} Streaming for local English speech.", flush=True)
        if model == "medium":
            print("Medium uses more memory (about 812 MiB measured on this computer). Small is the default for reliable desktop use.", flush=True)
        print("Reusing cached assets; missing downloads can resume on the next run.", flush=True)
        model_path, installed_architecture = get_model_for_language(
            "en", architecture, cache_root=cache_root, on_progress=Progress()
        )
        if installed_architecture != architecture:
            raise RuntimeError("The downloaded speech architecture does not match the selected model.")
        model_path = Path(model_path).resolve()
        if not model_path.is_relative_to(cache_root.resolve()):
            raise RuntimeError("The downloaded speech model is outside the local model folder.")
        print("Checking model file sizes and publisher checksums…", flush=True)
        size = verify_assets(cache_root, architecture)
        print("Validating the native CPU speech runtime…", flush=True)
        started = time.monotonic()
        with Transcriber(model_path, model_arch=architecture, update_interval=0.5,
                         options={"ort_providers": "CPU"}):
            pass
        elapsed = time.monotonic() - started

        if write_config:
            config = load_config()
            config.update({
                "speech_engine": "moonshine",
                "speech_model_path": str(model_path),
                "speech_model_arch": int(architecture),
                "speech_language": "en",
                "speech_update_interval": 0.5,
            })
            save_config(config)
        print(f"Ready: {model.title()} English · {size / 1024**2:.1f} MiB · native load {elapsed:.2f} s", flush=True)
        print(f"Model: {model_path}", flush=True)
        if write_config:
            print("Private configuration updated. Restart Jev Linux to use local English streaming speech.", flush=True)
        else:
            print("Configuration unchanged (--no-config).", flush=True)
        return model_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Install fully local English streaming speech for Jev Linux.")
    parser.add_argument("--model", choices=("small", "medium"), default="small",
                        help="small: recommended desktop memory use; medium: more memory, optional accuracy upgrade")
    parser.add_argument("--no-config", action="store_true", help="Download and validate without changing the selected speech settings")
    args = parser.parse_args()
    try:
        install_speech(args.model, write_config=not args.no_config)
    except KeyboardInterrupt:
        print("Speech setup stopped. Cached download progress is retained.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"Speech setup failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
