"""Local Moonshine streaming speech, without saving or uploading microphone audio."""

from __future__ import annotations

from array import array
from collections import deque
from dataclasses import dataclass, field
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from typing import Callable


SAMPLE_RATE = 16000
CHUNK_BYTES = 1600  # 50 milliseconds of signed 16-bit mono PCM.
MODEL_FILES = (
    "adapter.ort", "cross_kv.ort", "decoder_kv.ort", "encoder.ort",
    "frontend.model.ort", "frontend.weights.ort", "streaming_config.json", "tokenizer.bin",
)
# A replacement recognizer can be created by Settings while an old native call
# is finishing. Keep model ownership exclusive across every recognizer instance
# so rapid Stop/Start cannot load two large models at once.
_NATIVE_SESSION_LOCK = threading.Lock()


def _native_transcriber(**kwargs):
    # Importing/loading the model happens on the worker, never on the UI thread.
    from moonshine_voice import ModelArch, Transcriber

    kwargs["model_arch"] = ModelArch(kwargs["model_arch"])
    return Transcriber(**kwargs)


class _AudioQueue:
    """Bound latency and memory without silently dropping dictated words."""

    MAX_CHUNKS = 64  # Stop explicitly if inference falls over 3.2 seconds behind.

    def __init__(self):
        self.condition = threading.Condition()
        self.chunks: deque[bytes] = deque()

    def put(self, chunk: bytes) -> bool:
        with self.condition:
            if len(self.chunks) >= self.MAX_CHUNKS:
                return False
            self.chunks.append(chunk)
            self.condition.notify_all()
            return True

    def take(self, stop_event: threading.Event) -> bytes | None:
        with self.condition:
            self.condition.wait_for(lambda: stop_event.is_set() or bool(self.chunks))
            if stop_event.is_set():
                return None
            # Small batches help the native stream catch up after an inference.
            return b"".join(self.chunks.popleft() for _ in range(min(8, len(self.chunks))))

    def clear(self):
        with self.condition:
            self.chunks.clear()
            self.condition.notify_all()


@dataclass
class _Session:
    generation: int
    stop_event: threading.Event = field(default_factory=threading.Event)
    ready: threading.Event = field(default_factory=threading.Event)
    queue: _AudioQueue = field(default_factory=_AudioQueue)
    process: subprocess.Popen | None = None
    worker: threading.Thread | None = None


class _TranscriptListener:
    """Native callable listener with an isolated transcript state per session."""

    def __init__(self, recognizer, session: _Session):
        self.recognizer = recognizer
        self.session = session
        self._lines: dict[int, tuple[str, bool, float]] = {}
        self._last_partial = ""
        self._completed: set[int] = set()
        self._silence_seconds = max(0.4, float(recognizer.config.get("speech_silence_seconds", 0.8)))

    def __call__(self, event):
        method = {
            "LineStarted": self.on_line_started,
            "LineUpdated": self.on_line_text_changed,
            "LineTextChanged": self.on_line_text_changed,
            "LineCompleted": self.on_line_completed,
            "Error": self.on_error,
        }.get(type(event).__name__)
        if method:
            method(event)

    def _emit(self, event, final: bool):
        line = event.line
        text = getattr(line, "text", "").strip()
        line_id = int(getattr(line, "line_id", 0))
        start_time = float(getattr(line, "start_time", 0.0))
        end_time = start_time + float(getattr(line, "duration", 0.0))
        with self.recognizer._lock:
            if not self.recognizer._current(self.session) or line_id in self._completed:
                return
            if line_id not in self._lines:
                self._maybe_final(start_time)
            self._lines[line_id] = (text, final, end_time)
            cumulative = self._text()
            if cumulative and cumulative != self._last_partial:
                self._last_partial = cumulative
                self.recognizer.on_transcript(cumulative, False)

    def _text(self) -> str:
        return " ".join(line[0] for _, line in sorted(self._lines.items()) if line[0])

    def _maybe_final(self, audio_seconds: float):
        if not self._lines or not all(line[1] for line in self._lines.values()):
            return
        end_time = max(line[2] for line in self._lines.values())
        if audio_seconds + 1e-6 < end_time + self._silence_seconds:
            return
        text = self._text()
        self._completed.update(self._lines)
        self._lines.clear()
        self._last_partial = ""
        # Native IDs increase. Bound duplicate tracking on long sessions.
        if len(self._completed) > 256:
            self._completed = set(sorted(self._completed)[-128:])
        if text:
            self.recognizer.on_transcript(text, True)

    def advance(self, audio_seconds: float):
        """Finalize only after an audio pause, joining short native VAD fragments."""
        with self.recognizer._lock:
            if self.recognizer._current(self.session):
                self._maybe_final(audio_seconds)

    def on_line_started(self, event):
        self._emit(event, False)

    def on_line_text_changed(self, event):
        self._emit(event, False)

    def on_line_completed(self, event):
        self._emit(event, True)

    def on_error(self, event):
        self.recognizer._fail(
            self.session, "Local speech recognition failed. Restart listening to try again."
        )


class MoonshineVoiceRecognizer:
    """Continuously capture audio while Moonshine emits changing and final text.

    Callbacks run on worker threads. A fresh native stream is created on each
    start. ``stop`` immediately closes the microphone and invalidates callbacks;
    the native model is safely freed by its owner after any current call returns.
    A replacement session waits cancellably until the previous model is freed.
    ``transcriber_factory`` has the constructor signature of Moonshine's
    ``Transcriber`` and allows tests to run without a real model or microphone.
    """

    def __init__(
        self,
        config: dict,
        on_transcript: Callable[[str, bool], None],
        on_status: Callable[[str], None],
        *,
        transcriber_factory=None,
    ):
        self.config = dict(config)
        self.on_transcript = on_transcript
        self.on_status = on_status
        self._factory = transcriber_factory or _native_transcriber
        self._lock = threading.RLock()
        self._generation = 0
        self._running = False
        self._session: _Session | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def _current(self, session: _Session) -> bool:
        return session.generation == self._generation and not session.stop_event.is_set()

    def _status(self, session: _Session, message: str):
        with self._lock:
            if self._current(session):
                self.on_status(message)

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return False
            model = Path(self.config.get("speech_model_path", "")).expanduser()
            if not model.is_dir() or any(
                not (model / name).is_file() or (model / name).stat().st_size == 0
                for name in MODEL_FILES
            ):
                self.on_status("Error: Moonshine speech model is missing or incomplete. Choose its folder in Settings.")
                return False
            if shutil.which("arecord") is None:
                self.on_status("Error: Microphone capture needs arecord. Install alsa-utils.")
                return False
            self._generation += 1
            session = _Session(self._generation)
            self._session = session
            self._running = True
            self._thread = threading.Thread(
                target=self._capture, args=(session,), name="jev-microphone", daemon=True
            )
            self._thread.start()
            return True

    def stop(self):
        with self._lock:
            self._generation += 1
            self._running = False
            session, self._session = self._session, None
            if session:
                session.stop_event.set()
                session.queue.clear()
        if session:
            self._terminate(session.process)
        self.on_status("Microphone off")

    @staticmethod
    def _terminate(process):
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

    def _fail(self, session: _Session, message: str):
        with self._lock:
            if not self._current(session):
                return
            self.on_status(f"Error: {message}")
            session.stop_event.set()
            session.ready.set()
            session.queue.clear()
            self._running = False
        self._terminate(session.process)

    def _capture(self, session: _Session):
        process = None
        try:
            session.worker = threading.Thread(
                target=self._transcribe, args=(session,), name="jev-local-speech", daemon=True
            )
            session.worker.start()
            while not session.ready.wait(0.1):
                if session.stop_event.is_set():
                    return
            if session.stop_event.is_set():
                return
            process = subprocess.Popen(
                ["arecord", "-q", "-D", os.environ.get("JEV_AUDIO_DEVICE", "default"),
                 "-t", "raw", "-f", "S16_LE", "-r", "16000", "-c", "1"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            with self._lock:
                if not self._current(session):
                    self._terminate(process)
                    return
                session.process = process
            self._status(session, "Listening · Moonshine · English · local")
            pending = b""
            while not session.stop_event.is_set():
                chunk = process.stdout.read(CHUNK_BYTES)
                if not chunk:
                    if not session.stop_event.is_set():
                        self._fail(session, "Microphone capture stopped. Check Ubuntu Sound input settings.")
                    break
                if session.stop_event.is_set():
                    break
                pending += chunk
                while len(pending) >= CHUNK_BYTES and not session.stop_event.is_set():
                    frame, pending = pending[:CHUNK_BYTES], pending[CHUNK_BYTES:]
                    if not session.queue.put(frame):
                        self._fail(
                            session,
                            "Local speech recognition cannot keep up. Listening stopped; restart to try again.",
                        )
                        break
        except Exception:
            if not session.stop_event.is_set():
                self._fail(session, "Microphone capture failed. Check Ubuntu Sound input settings.")
        finally:
            session.stop_event.set()
            session.queue.clear()
            self._terminate(process)
            if process is not None and process.stdout is not None:
                process.stdout.close()
            with self._lock:
                if session.generation == self._generation:
                    self._running = False

    def _transcribe(self, session: _Session):
        transcriber = None
        stream_started = False
        owns_native_session = False
        try:
            if session.stop_event.is_set():
                return
            owns_native_session = _NATIVE_SESSION_LOCK.acquire(blocking=False)
            if not owns_native_session:
                self._status(session, "Waiting for previous speech session to stop…")
                while not session.stop_event.is_set():
                    if _NATIVE_SESSION_LOCK.acquire(timeout=0.1):
                        owns_native_session = True
                        break
            if session.stop_event.is_set() or not owns_native_session:
                return
            self._status(session, "Loading accurate local Moonshine model…")
            transcriber = self._factory(
                model_path=str(Path(self.config["speech_model_path"]).expanduser()),
                model_arch=int(self.config.get("speech_model_arch", 4)),
                update_interval=max(0.5, float(self.config.get("speech_update_interval", 0.5))),
                options={"log_api_calls": "false", "log_ort_run": "false",
                         "log_output_text": "false", "return_audio_data": "false",
                         "save_input_wav_path": "", "ort_providers": "CPU"},
            )
            if session.stop_event.is_set():
                return
            listener = _TranscriptListener(self, session)
            transcriber.add_listener(listener)
            transcriber.start()
            stream_started = True
            session.ready.set()
            audio_seconds = 0.0
            while not session.stop_event.is_set():
                pcm = session.queue.take(session.stop_event)
                if pcm is None:
                    break
                samples = array("h", pcm)
                if sys.byteorder != "little":
                    samples.byteswap()
                transcriber.add_audio([sample / 32768.0 for sample in samples], sample_rate=SAMPLE_RATE)
                audio_seconds += len(samples) / SAMPLE_RATE
                listener.advance(audio_seconds)
        except ImportError:
            self._fail(session, "Moonshine Voice is missing. Run the setup script to install local speech recognition.")
        except Exception:
            self._fail(session, "Local speech recognition failed. Check the Moonshine model folder in Settings.")
        finally:
            # stop() flushes native final events. Invalidate before calling it so
            # an unfinished spoken instruction never executes after user stop.
            session.stop_event.set()
            session.ready.set()
            session.queue.clear()
            if transcriber is not None:
                try:
                    transcriber.remove_all_listeners()
                    if stream_started:
                        transcriber.stop()
                    stream = getattr(transcriber, "_default_stream", None)
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
                finally:
                    try:
                        transcriber.close()
                    except Exception:
                        pass
            if owns_native_session:
                _NATIVE_SESSION_LOCK.release()
