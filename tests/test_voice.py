"""Repeatable speech lifecycle checks without audio devices or a Vosk model."""

import json
import tempfile
import threading
import types
import unittest
from collections import deque
from unittest.mock import patch

from jev_linux.voice import VoiceRecognizer


def _chunk(text, final=False):
    return json.dumps({"text": text, "final": final}).encode()


class _Recognizer:
    instances = []

    def __init__(self, model, sample_rate):
        self.model = model
        self.sample_rate = sample_rate
        self.result = {}
        self.instances.append(self)

    def SetWords(self, enabled):
        pass

    def AcceptWaveform(self, chunk):
        self.result = json.loads(chunk)
        return self.result["final"]

    def Result(self):
        return json.dumps({"text": self.result["text"]})

    def PartialResult(self):
        return json.dumps({"partial": self.result["text"]})


class _AudioStream:
    def __init__(self, process, chunks):
        self.process = process
        self.chunks = deque(chunks)
        self.closed = False

    def read(self, size):
        if self.chunks:
            return self.chunks.popleft()
        self.process.exited.wait()
        return b""

    def close(self):
        self.closed = True


class _CaptureProcess:
    def __init__(self, chunks):
        self.exited = threading.Event()
        self.stdout = _AudioStream(self, chunks)
        self.terminated = False

    def poll(self):
        return 0 if self.exited.is_set() else None

    def terminate(self):
        self.terminated = True
        self.exited.set()

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        self.exited.wait(timeout)
        return 0


class VoiceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.model_path = directory.name
        self.condition = threading.Condition()
        self.transcripts = []
        self.statuses = []
        self.models = []
        self.processes = []
        self.capture_plans = deque()
        _Recognizer.instances = []

        def model(path):
            result = object()
            self.models.append(result)
            return result

        self.vosk = types.SimpleNamespace(
            Model=model, KaldiRecognizer=_Recognizer, SetLogLevel=lambda _: None
        )
        self.enterContext(patch.dict("sys.modules", {"vosk": self.vosk}))
        self.enterContext(patch("jev_linux.voice.shutil.which", return_value="/usr/bin/arecord"))
        self.capture = self.enterContext(patch("jev_linux.voice.subprocess.Popen", side_effect=self._capture))
        self.voice = VoiceRecognizer(self.model_path, self._transcript, self._status)
        self.addCleanup(self._stop)

    def _capture(self, command, **kwargs):
        self.assertIn("16000", command)
        self.assertIn("S16_LE", command)
        process = _CaptureProcess(self.capture_plans.popleft())
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

    def _wait_for_transcripts(self, count):
        with self.condition:
            reached = self.condition.wait_for(lambda: len(self.transcripts) >= count, timeout=2)
        self.assertTrue(reached, f"Transcripts: {self.transcripts}; statuses: {self.statuses}")

    def _stop(self):
        self.voice.stop()
        if self.voice._thread is not None:
            self.voice._thread.join(timeout=2)
            self.assertFalse(self.voice._thread.is_alive(), "Speech worker did not exit after stop")

    def test_changed_partials_and_endpoint_restart_with_fresh_capture(self):
        self.capture_plans.extend([
            [_chunk(""), _chunk("open"), _chunk("open"), _chunk("open notes"), _chunk("open notes", True)],
            [_chunk("create"), _chunk("create a note", True)],
        ])
        self.assertTrue(self.voice.start())
        self.assertFalse(self.voice.start(), "Starting twice should not create two microphone captures")
        self._wait_for_transcripts(3)
        self._stop()
        self.assertEqual(self.transcripts, [("open", False), ("open notes", False), ("open notes", True)])
        self.assertFalse(self.voice.running)
        self.assertTrue(self.processes[0].terminated)
        self.assertTrue(self.processes[0].stdout.closed)

        self.assertTrue(self.voice.start())
        self._wait_for_transcripts(5)
        self._stop()
        self.assertEqual(self.transcripts[-2:], [("create", False), ("create a note", True)])
        self.assertEqual(len(self.models), 1, "Restart should reuse the loaded model")
        self.assertEqual(len(self.processes), 2, "Restart should replace the audio capture")
        self.assertEqual(len(_Recognizer.instances), 2, "Restart should replace the recognition stream")

    def test_stop_discards_unfinished_speech(self):
        self.capture_plans.append([_chunk("unfinished command")])
        self.assertTrue(self.voice.start())
        self._wait_for_transcripts(1)
        self._stop()
        self.assertEqual(self.transcripts, [("unfinished command", False)])
        self.assertFalse(any(final for _, final in self.transcripts))
        self.assertFalse(any(message.startswith("Error:") for message in self.statuses))
        self.assertTrue(self.processes[0].terminated)

    def test_stop_during_model_loading_never_opens_microphone(self):
        loading = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def model(path):
            loading.set()
            release.wait(timeout=2)
            return object()

        self.vosk.Model = model
        self.assertTrue(self.voice.start())
        self.assertTrue(loading.wait(timeout=2))
        self.voice.stop()
        release.set()
        self.voice._thread.join(timeout=2)
        self.assertFalse(self.voice._thread.is_alive())
        self.capture.assert_not_called()
        self.assertEqual(self.transcripts, [])
        self.assertFalse(self.voice.running)

    def test_restart_while_decoding_discards_old_pending_transcript(self):
        decoding = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        class SlowRecognizer(_Recognizer):
            def __init__(self, model, sample_rate):
                super().__init__(model, sample_rate)
                self.first_session = len(_Recognizer.instances) == 1

            def AcceptWaveform(self, chunk):
                if self.first_session:
                    decoding.set()
                    release.wait(timeout=2)
                return super().AcceptWaveform(chunk)

        self.vosk.KaldiRecognizer = SlowRecognizer
        self.capture_plans.extend([
            [_chunk("stale final", True)],
            [_chunk("fresh final", True)],
        ])
        self.assertTrue(self.voice.start())
        self.assertTrue(decoding.wait(timeout=2))
        old_thread = self.voice._thread
        self.voice.stop()
        self.assertTrue(self.voice.start())
        self._wait_for_transcripts(1)
        release.set()
        old_thread.join(timeout=2)
        self.assertFalse(old_thread.is_alive())
        self._stop()
        self.assertEqual(self.transcripts, [("fresh final", True)])
        self.assertTrue(self.processes[0].terminated)

    def test_missing_model_reports_actionable_error(self):
        self.voice.model_path = self.model_path + "/missing"
        self.assertFalse(self.voice.start())
        self.assertIn("Settings", self.statuses[-1])
        self.capture.assert_not_called()

    def test_missing_recorder_reports_actionable_error(self):
        with patch("jev_linux.voice.shutil.which", return_value=None):
            self.assertFalse(self.voice.start())
        self.assertIn("alsa-utils", self.statuses[-1])
        self.capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
