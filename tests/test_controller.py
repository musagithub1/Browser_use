import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from jev_linux.config import DEFAULTS
from jev_linux.controller import Controller, build_request
from jev_linux.notes import create_note
from jev_linux.spans import text_candidates, url_candidates


class Backend:
    def __init__(self):
        self.calls = []

    def apps(self):
        return [{"id": "notes.desktop", "name": "Text Editor", "aliases": ["notes"]}]

    def snapshot(self):
        return {"focused_app": "Text Editor", "windows": [], "elements": [{"id": "e1", "name": "New note", "role": "text", "editable": True, "focused": True}]}

    def launch_app(self, app):
        self.calls.append(("launch", app))
        return "Opened Text Editor"

    def type_text(self, text, target):
        self.calls.append(("type", text, target))
        return "Inserted text"

    def press_key(self, key):
        self.calls.append(("key", key))
        return "Pressed " + key

    def close(self):
        pass


class Client:
    def __init__(self, decide=None):
        self.custom = decide
        self.calls = []

    def decide(self, state, questions):
        self.calls.append(state)
        action = self.custom(state) if self.custom else ("launch_app" if not state["completed_steps"] else "type_text" if len(state["completed_steps"]) == 1 else "done")
        answers = {}
        picks = {"action": action, "app": "notes.desktop", "target": "e1", "text": "s0", "text_mode": "dictation", "key": "Delete", "url": "none", "direction": "none"}
        for key, question in questions.items():
            answers[key] = {"noul": 0 if key == "sensitive" else 1} if question["type"] == "noul" else {"choice": picks[key], "confidence": 1}
        return {"answers": answers, "latency_ms": 1}

    def close(self):
        pass


def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Expected asynchronous result did not arrive")


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.events = []
        self.controller = None

    def tearDown(self):
        if self.controller:
            self.controller.close()

    def start(self, client):
        self.controller = Controller(self.backend, {**DEFAULTS, "memory_enabled": False}, client=client, debounce=0)
        self.controller.set_event_handler(self.events.append)
        return self.controller

    def test_partial_launch_then_final_dictation_never_duplicates_launch(self):
        controller = self.start(Client())
        controller.submit("open notes and type hello", final=False)
        eventually(lambda: len(self.backend.calls) == 1)
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")])
        controller.submit("open notes and type hello world", final=True)
        eventually(lambda: len(self.backend.calls) == 2)
        self.assertEqual(self.backend.calls[-1], ("type", "hello world", "e1"))
        eventually(lambda: any(e["message"] == "Done." for e in self.events))
        self.assertEqual(len(self.backend.calls), 2)

    def test_partial_dictation_does_not_type(self):
        client = Client(lambda state: "type_text")
        controller = self.start(client)
        controller.submit("type hello", final=False)
        eventually(lambda: len(client.calls) == 1)
        self.assertEqual(self.backend.calls, [])

    def test_cancel_discards_an_inflight_decision(self):
        entered, release = threading.Event(), threading.Event()
        def blocking(state):
            entered.set()
            release.wait(2)
            return "launch_app"
        controller = self.start(Client(blocking))
        controller.submit("open notes", final=True)
        self.assertTrue(entered.wait(1))
        controller.cancel()
        release.set()
        time.sleep(0.05)
        self.assertEqual(self.backend.calls, [])

    def test_delete_key_waits_for_confirmation_and_cancel_discards_it(self):
        controller = self.start(Client(lambda state: "press_key"))
        controller.submit("press delete", final=True)
        eventually(lambda: any(e["type"] == "confirmation" for e in self.events))
        self.assertEqual(self.backend.calls, [])
        controller.cancel()
        controller.confirm()
        time.sleep(0.05)
        self.assertEqual(self.backend.calls, [])

    def test_confirmation_executes_once(self):
        controller = self.start(Client(lambda state: "done" if state["completed_steps"] else "press_key"))
        controller.submit("press delete", final=True)
        eventually(lambda: any(e["type"] == "confirmation" for e in self.events))
        controller.confirm()
        eventually(lambda: len(self.backend.calls) == 1)
        controller.confirm()
        time.sleep(0.05)
        self.assertEqual(self.backend.calls, [("key", "Delete")])

    def test_explicit_background_terminal_target_requires_confirmation(self):
        def terminal_snapshot():
            return {"focused_app": "Text Editor", "elements": [{"id": "e1", "app": "Terminal", "name": "Command", "role": "text", "editable": True}]}
        self.backend.snapshot = terminal_snapshot
        controller = self.start(Client(lambda state: "type_text"))
        controller.submit("type hello", final=True)
        eventually(lambda: any(e["type"] == "confirmation" for e in self.events))
        self.assertEqual(self.backend.calls, [])

    def test_new_command_submitted_during_error_event_is_preserved(self):
        def fail_first(state):
            if state["user_command"] == "first command":
                raise RuntimeError("Provider failed")
            return "done" if state["completed_steps"] else "launch_app"
        controller = self.start(Client(fail_first))
        def handler(event):
            self.events.append(event)
            if event["type"] == "error":
                controller.submit("open notes", final=True)
        controller.set_event_handler(handler)
        controller.submit("first command", final=True)
        eventually(lambda: len(self.backend.calls) == 1)
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")])

    def test_low_confidence_note_content_does_not_become_blank_note(self):
        controller = self.start(Client())
        state, questions, candidates = build_request("create a note containing hello", True, self.backend.snapshot(), self.backend.apps(), [])
        answers = Client().decide(state, questions)["answers"]
        answers["text"]["confidence"] = 0.1
        self.assertIsNone(controller._arguments("create_note", answers, candidates, state["desktop"]))

    def test_error_from_old_partial_does_not_discard_final_transcript(self):
        entered, release = threading.Event(), threading.Event()
        def fail_partial(state):
            if not state["speech_final"]:
                entered.set()
                release.wait(2)
                raise RuntimeError("Old request failed")
            return "done" if state["completed_steps"] else "launch_app"
        controller = self.start(Client(fail_partial))
        controller.submit("open notes", final=False)
        self.assertTrue(entered.wait(1))
        controller.submit("open notes please", final=True)
        release.set()
        eventually(lambda: len(self.backend.calls) == 1)
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")])
        self.assertFalse(any(e["type"] == "error" for e in self.events))

    def test_punctuated_stop_from_new_recognizer_cancels_pending_command(self):
        controller = self.start(Client())
        controller.submit("Stop.", final=True)
        self.assertIsNone(controller._active)
        self.assertEqual(self.backend.calls, [])


class ContentTests(unittest.TestCase):
    def test_exact_dictation_candidates_and_spoken_url(self):
        self.assertIn("hello world", text_candidates("open notes and type hello world").values())
        self.assertIn("hello", text_candidates('type "hello" into the search box').values())
        self.assertIn("https://example.com", url_candidates("open example dot com").values())
        self.assertIn("hello", text_candidates("type hello and press enter").values())

    def test_new_notes_never_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            first = create_note(directory, "Original", "../note")
            second = create_note(directory, "Next", "../note")
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_text(), "Original\n")
            self.assertTrue(first.resolve().is_relative_to(__import__('pathlib').Path(directory).resolve()))


if __name__ == "__main__":
    unittest.main()
