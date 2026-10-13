"""Streaming, local speech recognition without recording audio to disk."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from typing import Callable


def speech_model_name(config):
    if config.get("speech_engine", "moonshine") == "vosk":
        return "Vosk · English · local"
    size = "Medium" if int(config.get("speech_model_arch", 4)) == 5 else "Small"
    return f"Moonshine {size} Streaming · English · local"


def create_voice_recognizer(config, on_transcript, on_status):
    if config.get("speech_engine", "moonshine") == "vosk":
        return VoiceRecognizer(config.get("vosk_model_path", ""), on_transcript, on_status)
    from .moonshine_voice import MoonshineVoiceRecognizer
    return MoonshineVoiceRecognizer(config, on_transcript, on_status)


class VoiceRecognizer:
    """Read the default ALSA microphone and emit changing Vosk hypotheses.

    Callbacks run on a worker thread. ``start`` is nonblocking; ``stop`` closes
    the capture process immediately and discards the unfinished utterance.
    Each start gets a new recognizer and capture process, so buffered audio
    cannot carry over between listening sessions.
    """

    def __init__(
        self,
        model_path: str,
        on_transcript: Callable[[str, bool], None],
        on_status: Callable[[str], None],
    ) -> None:
        self.model_path = str(Path(model_path).expanduser()) if model_path else ""
        self.on_transcript = on_transcript
        self.on_status = on_status
        self._lock = threading.RLock()
        self._model_lock = threading.Lock()
        self._generation = 0
        self._stop_event = threading.Event()
        self._process: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._model = None
        self._running = False

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def _status(self, generation: int, message: str) -> None:
        with self._lock:
            if generation == self._generation:
                self.on_status(message)

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return False
            if not self.model_path or not Path(self.model_path).is_dir():
                self.on_status("Error: Speech model folder is missing. Choose it in Settings.")
                return False
            if shutil.which("arecord") is None:
                self.on_status("Error: Microphone capture needs arecord. Install alsa-utils.")
                return False
            self._generation += 1
            generation = self._generation
            stop_event = threading.Event()
            self._stop_event = stop_event
            self._running = True
            self._thread = threading.Thread(
                target=self._listen,
                args=(generation, stop_event),
                name="jev-microphone",
                daemon=True,
            )
            self._thread.start()
            return True

    def stop(self) -> None:
        with self._lock:
            self._generation += 1
            self._running = False
            self._stop_event.set()
            process = self._process
            self._process = None
        self._terminate(process)
        self.on_status("Microphone off")

    @staticmethod
    def _terminate(process: subprocess.Popen | None) -> None:
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=0.4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=0.4)
        except (OSError, ProcessLookupError):
            pass

    def _listen(self, generation: int, stop_event: threading.Event) -> None:
        process = None
        try:
            try:
                from vosk import KaldiRecognizer, Model, SetLogLevel
            except ImportError as exc:
                raise RuntimeError("Vosk is missing. Run the setup script to install speech recognition.") from exc
            SetLogLevel(-1)
            self._status(generation, "Loading local speech model…")
            with self._model_lock:
                if stop_event.is_set():
                    return
                if self._model is None:
                    self._model = Model(self.model_path)
            if stop_event.is_set():
                return
            recognizer = KaldiRecognizer(self._model, 16000)
            recognizer.SetWords(True)
            # ALSA diagnostics go to an anonymous temporary file, never audio.
            # A pipe for stderr could fill while the audio reader is blocked.
            with tempfile.TemporaryFile() as diagnostics:
                command = [
                    "arecord", "-q", "-D", os.environ.get("JEV_AUDIO_DEVICE", "default"),
                    "-t", "raw", "-f", "S16_LE", "-r", "16000", "-c", "1",
                ]
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=diagnostics)
                with self._lock:
                    if generation != self._generation or stop_event.is_set():
                        self._terminate(process)
                        return
                    self._process = process
                self._status(generation, "Listening · local speech recognition")
                last_partial = ""
                while not stop_event.is_set():
                    # 125 ms of mono PCM keeps first-word feedback responsive.
                    chunk = process.stdout.read(4000)
                    if not chunk:
                        if stop_event.is_set():
                            break
                        process.wait(timeout=1)
                        diagnostics.seek(0)
                        detail = diagnostics.read(4096).decode("utf-8", errors="replace").strip()
                        raise RuntimeError(
                            "Microphone capture stopped. Check Ubuntu Sound input settings."
                            + (f" {detail}" if detail else "")
                        )
                    if stop_event.is_set():
                        break
                    final = bool(recognizer.AcceptWaveform(chunk))
                    result = json.loads(recognizer.Result() if final else recognizer.PartialResult())
                    transcript = result.get("text" if final else "partial", "").strip()
                    with self._lock:
                        if generation != self._generation or stop_event.is_set():
                            break
                        if final:
                            if transcript:
                                self.on_transcript(transcript, True)
                            last_partial = ""
                        elif transcript and transcript != last_partial:
                            last_partial = transcript
                            self.on_transcript(transcript, False)
        except Exception as exc:
            if not stop_event.is_set():
                message = str(exc).strip() or type(exc).__name__
                self._status(generation, f"Error: {message}")
        finally:
            self._terminate(process)
            if process is not None and process.stdout is not None:
                process.stdout.close()
            with self._lock:
                if generation == self._generation:
                    self._process = None
                    self._running = False
