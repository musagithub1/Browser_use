"""Install the official small English Vosk model, with cached HTTP resume.

Interrupted downloads stay under ~/.cache/jev-linux/models. The next run
requests only missing bytes. A successful install is checked by Vosk itself.
"""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jev_linux.config import DATA_DIR


MODEL_NAME = "vosk-model-small-en-us-0.15"
MODEL_URL = f"https://alphacephei.com/vosk/models/{MODEL_NAME}.zip"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "jev-linux/models"
CHUNK_SIZE = 64 * 1024


def validate_model(target: Path) -> None:
    from vosk import Model, SetLogLevel

    required = ("am/final.mdl", "conf/model.conf", "conf/mfcc.conf", "graph/Gr.fst", "graph/HCLr.fst")
    if not all((target / filename).is_file() for filename in required):
        raise RuntimeError("The speech model is incomplete.")
    SetLogLevel(-1)
    model = Model(str(target))
    del model


def archive_valid(archive: Path) -> bool:
    try:
        with zipfile.ZipFile(archive) as model_zip:
            return model_zip.testzip() is None
    except (OSError, zipfile.BadZipFile):
        return False


def preserve_prefix(source: Path, destination: Path) -> None:
    """Read a known prefix without changing a previous downloader's file."""
    source_size = source.stat().st_size
    if destination.exists() and destination.stat().st_size >= source_size:
        return
    temporary = destination.with_suffix(".adopt")
    try:
        with source.open("rb") as incoming, temporary.open("wb") as outgoing:
            remaining = source_size
            while remaining:
                chunk = incoming.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    raise RuntimeError("The source download changed while reading it. Try again after it stops.")
                outgoing.write(chunk)
                remaining -= len(chunk)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def download(archive: Path, *, deadline: float) -> Path:
    complete = archive.with_suffix("")
    if complete.is_file() and archive_valid(complete):
        print("Using the verified cached archive.", flush=True)
        return complete
    if archive.is_file() and archive_valid(archive):
        os.replace(archive, complete)
        return complete

    request = urllib.request.Request(MODEL_URL, method="HEAD")
    with urllib.request.urlopen(request, timeout=20) as response:
        total = int(response.headers["Content-Length"])
        etag = response.headers.get("ETag")
    if total <= 0 or total > 200 * 1024 * 1024:
        raise RuntimeError("The model server returned an unexpected archive size.")
    initial = archive.stat().st_size if archive.exists() else 0
    if initial > total:
        raise RuntimeError("The cached archive is too large. Preserve it elsewhere and rerun this installer.")
    print(f"Downloading official speech model: {total / 1024**2:.1f} MiB; {initial / 1024**2:.1f} MiB cached.", flush=True)
    started = time.monotonic()
    last_progress = 0.0
    for attempt in range(5):
        offset = archive.stat().st_size if archive.exists() else 0
        if offset == total:
            break
        headers = {"User-Agent": "Jev-Linux-model-installer/1.0", "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
            if etag:
                headers["If-Range"] = etag
        request = urllib.request.Request(MODEL_URL, headers=headers)
        try:
            if time.monotonic() >= deadline:
                raise TimeoutError("Download time limit reached.")
            with urllib.request.urlopen(request, timeout=20) as response:
                if offset:
                    matched = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                    if (response.status != 206 or not matched
                            or int(matched[1]) != offset or int(matched[3]) != total):
                        raise RuntimeError("The server refused a safe resume. The cached prefix was preserved.")
                with archive.open("ab" if offset else "wb") as stream:
                    os.chmod(archive, 0o600)
                    downloaded = offset
                    while True:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Download time limit reached.")
                        chunk = response.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > total:
                            raise RuntimeError("The model response exceeded the expected archive size.")
                        stream.write(chunk)
                        now = time.monotonic()
                        if now - last_progress >= 2:
                            rate = (downloaded - initial) / max(now - started, 0.1) / 1024
                            print(f"{downloaded / total:6.1%}  {downloaded / 1024**2:5.1f}/{total / 1024**2:.1f} MiB  {rate:5.0f} KiB/s", flush=True)
                            last_progress = now
            if archive.stat().st_size == total:
                break
            raise OSError("The download ended before all bytes arrived.")
        except (urllib.error.URLError, OSError) as error:
            if attempt == 4 or time.monotonic() >= deadline:
                raise RuntimeError(
                    "Download interrupted. Your progress is cached; run this installer again to resume."
                ) from error
            print(f"Connection interrupted; resuming cached bytes (attempt {attempt + 2}/5).", flush=True)
            time.sleep(min(2 ** attempt, 8, max(0, deadline - time.monotonic())))

    if not archive_valid(archive):
        raise RuntimeError("The downloaded archive failed its ZIP integrity check. It was preserved for inspection.")
    os.replace(archive, complete)
    return complete


def install(archive: Path, target: Path) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".model-install-", dir=DATA_DIR) as temporary:
        staging = Path(temporary)
        with zipfile.ZipFile(archive) as model_zip:
            expanded_size = 0
            for item in model_zip.infolist():
                destination = (staging / item.filename).resolve()
                if not destination.is_relative_to(staging.resolve()):
                    raise RuntimeError("Invalid model archive path.")
                if not item.filename.startswith(f"{MODEL_NAME}/"):
                    raise RuntimeError("The archive contains an unexpected model directory.")
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError("The model archive unexpectedly contains symbolic links.")
                expanded_size += item.file_size
                if expanded_size > 500 * 1024 * 1024:
                    raise RuntimeError("The model archive is unexpectedly large after extraction.")
            model_zip.extractall(staging)
        extracted = staging / MODEL_NAME
        validate_model(extracted)
        if target.exists():
            backup = target.with_name(f"{MODEL_NAME}.incomplete-{time.time_ns()}")
            target.rename(backup)
            print(f"Preserved the previous incomplete installation at {backup}.", flush=True)
        os.replace(extracted, target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume-from", type=Path, help="Read an existing partial archive into the resumable cache.")
    parser.add_argument("--timeout", type=float, default=900, help="Maximum download time in seconds (default: 900).")
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 3600:
        parser.error("--timeout must be between 0 and 3600 seconds")
    target = DATA_DIR / MODEL_NAME
    if target.is_dir():
        try:
            validate_model(target)
            print(f"Speech model already installed and verified: {target}", flush=True)
            return 0
        except Exception:
            print("Previous model installation is incomplete; preparing a verified replacement.", flush=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (CACHE_DIR / ".download.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("A model installer is already running. Wait for it to finish.", file=sys.stderr)
            return 1
        try:
            partial = CACHE_DIR / f"{MODEL_NAME}.zip.part"
            if args.resume_from:
                preserve_prefix(args.resume_from, partial)
            archive = download(partial, deadline=time.monotonic() + args.timeout)
            install(archive, target)
            validate_model(target)
        except (Exception, KeyboardInterrupt) as error:
            print(f"Speech model installation stopped: {error}", file=sys.stderr)
            print(f"Cached downloads remain in {CACHE_DIR}. Rerun this command to resume.", file=sys.stderr)
            return 1
    print(f"Local speech model installed and verified: {target}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
