"""Streaming lifecycle checks with synthetic PCM and a fake native transcriber."""

from array import array
from collections import deque
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

from jev_linux.moonshine_voice import (
    CHUNK_BYTES, MODEL_FILES, MoonshineVoiceRecognizer, _AudioQueue,
    _Session, _TranscriptListener,
)


QUIET = b"\0" * CHUNK_BYTES
SPEECH = array("h", [2000, -2000] * 400).tobytes()


def _event(text, line_id=1, start_time=0.0, duration=0.05):
    return types.SimpleNamespace(line=types.SimpleNamespace(
        text=text, line_id=line_id, start_time=start_time, duration=duration,
    ))


class _AudioStream:
    def __init__(self, process):
        self.process = process
        self.chunks = deque()
        self.condition = threading.Condition()
        self.closed = False

    def add(self, frames):
        with self.condition:
            self.chunks.extend(frames)
            self.condition.notify_all()

    def read(self, size):
        with self.condition:
            self.condition.wait_for(lambda: self.chunks or self.process.exited.is_set())
            return self.chunks.popleft() if self.chunks else b""

    def close(self):
        self.closed = True


class _CaptureProcess:
    def __init__(self):
        self.exited = threading.Event()
        self.stdout = _AudioStream(self)
        self.terminated = False

    def poll(self):
        return 0 if self.exited.is_set() else None

    def terminate(self):
        self.terminated = True
        with self.stdout.condition:
            self.exited.set()
            self.stdout.condition.notify_all()

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        self.exited.wait(timeout)
        return 0


class _Transcriber:
    def __init__(self):
        self.kwargs = None
        self.listener = None
        self.saved_listener = None
        self.events = deque()
        self.calls = []
        self.decoding = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.started = False
        self.stopped = False
        self.closed = False
        self.error = None

    def add_listener(self, listener):
        self.listener = self.saved_listener = listener

    def remove_all_listeners(self):
        self.listener = None

    def start(self):
        self.started = True

    def add_audio(self, samples, sample_rate):
        self.calls.append((samples, sample_rate))
        self.decoding.set()
        self.release.wait(timeout=3)
        if self.error:
            raise self.error
        while self.events:
            name, event = self.events.popleft()
            getattr(self.saved_listener, name)(event)

    def stop(self):
        self.stopped = True
        # Native stop can flush the remaining words; they must be discarded.
        self.saved_listener.on_line_completed(_event("unfinished flushed command", line_id=99))
        self.saved_listener.advance(100.0)

    def close(self):
        self.closed = True


class QueueAndListenerTests(unittest.TestCase):
    def setUp(self):
        self.transcripts = []
        self.statuses = []
        self.voice = MoonshineVoiceRecognizer(
            {"speech_silence_seconds": 0.8},
            lambda text, final: self.transcripts.append((text, final)), self.statuses.append,
        )
        self.voice._generation = 1
        self.voice._running = True
        self.session = _Session(1)
        self.listener = _TranscriptListener(self.voice, self.session)

    def test_audio_queue_bounds_backlog_and_preserves_sample_order(self):
        queue = _AudioQueue()
        for index in range(queue.MAX_CHUNKS):
            self.assertTrue(queue.put(bytes([index])))
        self.assertFalse(queue.put(b"overflow"))
        self.assertEqual(queue.take(threading.Event()), bytes(range(8)))
        self.assertEqual(len(queue.chunks), queue.MAX_CHUNKS - 8)

    def test_stop_discards_queued_audio_and_wakes_worker(self):
        queue = _AudioQueue()
        queue.put(SPEECH)
        stop = threading.Event()
        stop.set()
        queue.clear()
        self.assertIsNone(queue.take(stop))
        self.assertEqual(len(queue.chunks), 0)

    def test_duplicate_partials_and_complete_lines_wait_for_pause(self):
        self.listener.on_line_started(_event("open"))
        self.listener.on_line_text_changed(_event("open"))
        self.listener.on_line_text_changed(_event("open notes"))
        self.listener.on_line_completed(_event("open notes"))
        self.listener.advance(0.8)
        self.assertEqual(self.transcripts, [("open", False), ("open notes", False)])
        self.listener.advance(0.85)
        self.listener.on_line_completed(_event("open notes"))
        self.listener.advance(1.0)
        self.assertEqual(self.transcripts[-1], ("open notes", True))
        self.assertEqual(sum(final for _, final in self.transcripts), 1)

    def test_short_pause_fragments_form_one_complete_instruction(self):
        self.listener.on_line_completed(_event("open notes", line_id=1, duration=1.0))
        self.listener.advance(1.2)
        self.listener.on_line_started(_event("and create a note", line_id=2, start_time=1.3, duration=0.1))
        self.listener.on_line_completed(_event("and create a note", line_id=2, start_time=1.3, duration=1.0))
        self.listener.advance(3.1)
        self.assertEqual(self.transcripts[-1], ("open notes and create a note", True))
        self.assertEqual(sum(final for _, final in self.transcripts), 1)
        self.assertIn(("open notes and create a note", False), self.transcripts)

    def test_separate_instructions_keep_order(self):
        self.listener.on_line_completed(_event("open notes", line_id=1, duration=1.0))
        self.listener.on_line_started(_event("open terminal", line_id=2, start_time=2.0, duration=0.1))
        self.listener.on_line_completed(_event("open terminal", line_id=2, start_time=2.0, duration=1.0))
        self.listener.advance(3.8)
        self.assertEqual(
            [text for text, final in self.transcripts if final], ["open notes", "open terminal"]
        )

    def test_blank_text_and_stale_callbacks_are_discarded(self):
        self.listener.on_line_completed(_event(""))
        self.listener.advance(5.0)
        self.session.stop_event.set()
        self.listener.on_line_completed(_event("stale command", line_id=2))
        self.listener.advance(10.0)
        self.assertEqual(self.transcripts, [])

    def test_native_callable_event_dispatch(self):
        changed_type = type("LineTextChanged", (), {})
        event = changed_type()
        event.line = _event("open notes").line
        self.listener(event)
        self.assertEqual(self.transcripts, [("open notes", False)])


class MoonshineVoiceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.model_path = directory.name
        for name in MODEL_FILES:
            (Path(self.model_path) / name).write_bytes(b"synthetic model fixture")
        self.condition = threading.Condition()
        self.transcripts = []
        self.statuses = []
        self.transcribers = []
        self.processes = []
        self.sessions = []
        self.capture_threads = []
        self.plans = deque()
        self.enterContext(patch("jev_linux.moonshine_voice.shutil.which", return_value="/usr/bin/arecord"))
        self.capture = self.enterContext(patch("jev_linux.moonshine_voice.subprocess.Popen", side_effect=self._capture))
        self.voice = MoonshineVoiceRecognizer(
            {"speech_model_path": self.model_path, "speech_model_arch": 4,
             "speech_update_interval": 0.5, "speech_silence_seconds": 0.8},
            self._transcript, self._status, transcriber_factory=self._factory,
        )
        self.addCleanup(self._cleanup)

    def _factory(self, **kwargs):
        transcriber = self.plans.popleft() if self.plans else _Transcriber()
        transcriber.kwargs = kwargs
        self.transcribers.append(transcriber)
        return transcriber

    def _capture(self, command, **kwargs):
        self.assertIn("16000", command)
        self.assertIn("S16_LE", command)
        self.assertIn("1", command)
        process = _CaptureProcess()
        self.processes.append(process)
        return process

    def _transcript(self, text, final):
        with self.condition:
            self.transcripts.append((text, final))
            self.condition.notify_all()

    def _status(self, message):
        with self.condition:
            self.statuses.append(message)
            self.condition.notify_all()

    def _wait(self, predicate):
        with self.condition:
            reached = self.condition.wait_for(predicate, timeout=3)
        self.assertTrue(reached, f"Transcripts: {self.transcripts}; statuses: {self.statuses}")

    def _start(self):
        previous = len(self.processes)
        self.assertTrue(self.voice.start())
        self.sessions.append(self.voice._session)
        self.capture_threads.append(self.voice._thread)
        self._wait(lambda: len(self.processes) > previous and "Listening" in self.statuses[-1])
        return self.processes[-1], self.transcribers[-1]

    def _cleanup(self):
        self.voice.stop()
        for transcriber in self.transcribers:
            transcriber.release.set()
        for thread in self.capture_threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        for session in self.sessions:
            if session.worker:
                session.worker.join(timeout=3)
                self.assertFalse(session.worker.is_alive())

    def test_native_constructor_and_pcm_conversion(self):
        process, transcriber = self._start()
        self.assertFalse(self.voice.start())
        transcriber.events.append(("on_line_text_changed", _event("open notes")))
        pcm = array("h", [-32768, 32767] + [0] * 798).tobytes()
        process.stdout.add([pcm])
        self._wait(lambda: len(self.transcripts) == 1)
        self.assertEqual(transcriber.kwargs["model_arch"], 4)
        self.assertEqual(transcriber.kwargs["update_interval"], 0.5)
        self.assertEqual(transcriber.kwargs["options"]["log_output_text"], "false")
        self.assertEqual(transcriber.kwargs["options"]["return_audio_data"], "false")
        samples, rate = transcriber.calls[0]
        self.assertEqual(rate, 16000)
        self.assertEqual(samples[0], -1.0)
        self.assertAlmostEqual(samples[1], 32767 / 32768)

    def test_partial_and_final_after_pause(self):
        process, transcriber = self._start()
        transcriber.events.append(("on_line_started", _event("open notes")))
        process.stdout.add([SPEECH])
        self._wait(lambda: len(self.transcripts) == 1)
        transcriber.events.append(("on_line_completed", _event("open notes")))
        process.stdout.add([QUIET] * 20)
        self._wait(lambda: len(self.transcripts) == 2)
        self.assertEqual(self.transcripts, [("open notes", False), ("open notes", True)])

    def test_silence_emits_no_transcript(self):
        process, transcriber = self._start()
        process.stdout.add([QUIET] * 20)
        self.assertTrue(transcriber.decoding.wait(timeout=3))
        self.voice.stop()
        self.assertEqual(self.transcripts, [])

    def test_stop_discards_native_flush_and_closes_model(self):
        process, transcriber = self._start()
        transcriber.events.append(("on_line_text_changed", _event("unfinished speech")))
        process.stdout.add([SPEECH])
        self._wait(lambda: len(self.transcripts) == 1)
        self.voice.stop()
        self.sessions[-1].worker.join(timeout=3)
        self.capture_threads[-1].join(timeout=3)
        self.assertEqual(self.transcripts, [("unfinished speech", False)])
        self.assertTrue(process.terminated)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(transcriber.stopped)
        self.assertTrue(transcriber.closed)
        self.assertFalse(self.voice.running)

    def test_restart_discards_inflight_native_callback(self):
        old_transcriber = _Transcriber()
        old_transcriber.release.clear()
        old_transcriber.events.append(("on_line_completed", _event("stale command")))
        self.plans.append(old_transcriber)
        old_process, _ = self._start()
        old_process.stdout.add([SPEECH])
        self.assertTrue(old_transcriber.decoding.wait(timeout=3))
        self.voice.stop()
        self.assertFalse(old_transcriber.closed, "Native inference must finish before freeing the model")
        self.assertTrue(self.voice.start())
        self.sessions.append(self.voice._session)
        self.capture_threads.append(self.voice._thread)
        self._wait(lambda: "Waiting for previous" in self.statuses[-1])
        self.assertEqual(len(self.transcribers), 1, "Restart must not overlap native models")
        old_transcriber.release.set()
        self._wait(lambda: len(self.processes) == 2 and "Listening" in self.statuses[-1])
        new_process, new_transcriber = self.processes[-1], self.transcribers[-1]
        new_transcriber.events.append(("on_line_completed", _event("fresh command")))
        new_process.stdout.add([QUIET] * 20)
        self._wait(lambda: any(final for _, final in self.transcripts))
        self.sessions[0].worker.join(timeout=3)
        self.assertEqual([text for text, final in self.transcripts if final], ["fresh command"])
        self.assertTrue(self.voice.running)
        self.assertTrue(old_transcriber.closed)

    def test_replacement_instance_waits_until_previous_model_close_finishes(self):
        process, old_transcriber = self._start()
        closing = threading.Event()
        allow_close = threading.Event()
        self.addCleanup(allow_close.set)

        def close():
            closing.set()
            allow_close.wait(timeout=3)
            old_transcriber.closed = True

        old_transcriber.close = close
        self.voice.stop()
        self.assertTrue(closing.wait(timeout=3))
        self.assertTrue(process.terminated, "Stopping capture must not wait for native close")
        replacement_loaded = threading.Event()
        replacement_waiting = threading.Event()
        replacement = _Transcriber()

        def factory(**kwargs):
            self.assertTrue(old_transcriber.closed, "Previous model must be freed before loading another")
            replacement_loaded.set()
            return replacement

        def status(message):
            if "Waiting for previous" in message:
                replacement_waiting.set()
            self._status(message)

        replacement_voice = MoonshineVoiceRecognizer(
            self.voice.config, self._transcript, status, transcriber_factory=factory,
        )
        self.addCleanup(replacement_voice.stop)
        self.assertTrue(replacement_voice.start())
        replacement_session = replacement_voice._session
        self.sessions.append(replacement_session)
        self.capture_threads.append(replacement_voice._thread)
        self.assertTrue(replacement_waiting.wait(timeout=3))
        self.assertFalse(replacement_loaded.is_set())
        self.assertEqual(len(self.processes), 1, "Waiting sessions must not open another microphone")
        allow_close.set()
        self.assertTrue(replacement_loaded.wait(timeout=3))
        self._wait(lambda: len(self.processes) == 2 and "Listening" in self.statuses[-1])
        replacement_voice.stop()
        replacement_session.worker.join(timeout=3)
        self.assertTrue(replacement.closed)

    def test_stop_cancels_waiting_start_without_loading_or_opening_capture(self):
        process, old_transcriber = self._start()
        old_transcriber.release.clear()
        process.stdout.add([SPEECH])
        self.assertTrue(old_transcriber.decoding.wait(timeout=3))
        self.voice.stop()
        waiting = threading.Event()
        loaded = threading.Event()

        def factory(**kwargs):
            loaded.set()
            return _Transcriber()

        replacement_voice = MoonshineVoiceRecognizer(
            self.voice.config, self._transcript,
            lambda message: waiting.set() if "Waiting for previous" in message else None,
            transcriber_factory=factory,
        )
        self.addCleanup(replacement_voice.stop)
        self.assertTrue(replacement_voice.start())
        replacement_session = replacement_voice._session
        self.sessions.append(replacement_session)
        self.capture_threads.append(replacement_voice._thread)
        self.assertTrue(waiting.wait(timeout=3))
        replacement_voice.stop()
        replacement_session.worker.join(timeout=1)
        replacement_voice._thread.join(timeout=1)
        self.assertFalse(replacement_session.worker.is_alive(), "Waiting startup must remain cancellable")
        self.assertFalse(replacement_voice._thread.is_alive())
        self.assertFalse(loaded.is_set())
        self.assertEqual(len(self.processes), 1)
        old_transcriber.release.set()

    def test_stop_during_loading_never_starts_microphone(self):
        loading = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        transcriber = _Transcriber()

        def factory(**kwargs):
            loading.set()
            release.wait(timeout=3)
            return transcriber

        self.voice._factory = factory
        self.assertTrue(self.voice.start())
        session = self.voice._session
        thread = self.voice._thread
        self.sessions.append(session)
        self.capture_threads.append(thread)
        self.assertTrue(loading.wait(timeout=3))
        self.voice.stop()
        release.set()
        session.worker.join(timeout=3)
        thread.join(timeout=3)
        self.capture.assert_not_called()
        self.assertTrue(transcriber.closed)
        self.assertFalse(transcriber.started)

    def test_error_is_scrubbed_and_stops_capture(self):
        process, transcriber = self._start()
        transcriber.error = RuntimeError("SECRET recorded text /tmp/private.wav")
        process.stdout.add([SPEECH])
        self._wait(lambda: any(message.startswith("Error:") for message in self.statuses))
        self.sessions[-1].worker.join(timeout=3)
        self.assertFalse(self.voice.running)
        self.assertTrue(process.terminated)
        self.assertFalse(any("SECRET" in message or "private.wav" in message for message in self.statuses))
        self.assertEqual(self.transcripts, [])

    def test_decoder_delay_does_not_block_microphone_and_backlog_is_bounded(self):
        transcriber = _Transcriber()
        transcriber.release.clear()
        self.plans.append(transcriber)
        process, _ = self._start()
        process.stdout.add([SPEECH])
        self.assertTrue(transcriber.decoding.wait(timeout=3))
        process.stdout.add([SPEECH] * 65)
        self._wait(lambda: any("cannot keep up" in message for message in self.statuses))
        self.assertFalse(self.voice.running)
        self.assertTrue(process.terminated)
        self.assertLessEqual(len(self.sessions[-1].queue.chunks), 64)
        transcriber.release.set()

    def test_missing_or_incomplete_model_reports_settings_error(self):
        (Path(self.model_path) / "tokenizer.bin").unlink()
        self.assertFalse(self.voice.start())
        self.assertIn("Settings", self.statuses[-1])
        self.capture.assert_not_called()

    def test_missing_recorder_reports_install_action(self):
        with patch("jev_linux.moonshine_voice.shutil.which", return_value=None):
            self.assertFalse(self.voice.start())
        self.assertIn("alsa-utils", self.statuses[-1])
        self.capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
