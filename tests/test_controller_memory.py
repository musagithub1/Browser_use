"""Task context, remembered artifacts, cancellation and memory controls."""

from pathlib import Path
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from jev_linux.config import DEFAULTS
from jev_linux.controller import Controller, build_request
from jev_linux.memory import MemoryStore
from jev_linux.notes import create_note


def eventually(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Expected task state did not arrive")


class Backend:
    def __init__(self):
        self.calls = []

    def apps(self):
        return [{"id": "notes.desktop", "name": "Text Editor", "aliases": ["notes"]}]

    def snapshot(self):
        return {"focused_app": "Text Editor", "elements": []}

    def launch_app(self, app):
        self.calls.append(("launch", app))
        return "Opened Text Editor"

    def open_url(self, url):
        self.calls.append(("url", url))
        return "Opened website"

    def press_key(self, key, expected_focus=None):
        self.calls.append(("key", key, expected_focus))
        return "Pressed key"

    def focus_token(self, snapshot):
        return {"app": "Text Editor", "control": "/control/1"}

    def close(self):
        pass


class Client:
    def __init__(self, choose=None):
        self.calls = []
        self.generated = []
        self.choose = choose or (lambda state: "done" if state["completed_steps"] else "launch_app")

    def decide(self, state, questions):
        self.calls.append(state)
        picks = {"action": self.choose(state), "app": "notes.desktop", "target": "none",
                 "text": next(iter(state["text_candidates"]), "none"), "text_mode": "dictation",
                 "key": "Delete", "url": next(iter(state["url_candidates"]), "none"),
                 "direction": "none", "note": next(iter(state["remembered_notes"]), "none")}
        if "compose" in state["user_command"]:
            picks["text_mode"] = "generate"
        answers = {name: {"noul": 0 if name == "sensitive" else 1} if question["type"] == "noul"
                   else {"choice": picks[name], "confidence": 1}
                   for name, question in questions.items()}
        return {"answers": answers, "latency_ms": 1}

    def generate_text(self, instruction, state=None):
        self.generated.append((instruction, state))
        return "A new short note."

    def close(self):
        pass


class ControllerMemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "private" / "memory.sqlite3"
        self.config = {**DEFAULTS, "memory_enabled": True, "notes_directory": str(self.root / "notes"),
                       "openrouter_api_key": "private-test-value"}
        self.store = MemoryStore(self.path)
        self.backend = Backend()
        self.client = Client()
        self.events = []
        self.controller = self.start()
        self.addCleanup(lambda: self.controller.close() if self.controller else None)

    def start(self):
        controller = Controller(self.backend, self.config, client=self.client, debounce=0, memory_store=self.store)
        controller.set_event_handler(self.events.append)
        return controller

    def test_explicit_preference_is_local_and_waits_for_final_speech(self):
        self.controller.submit("Remember that I prefer short notes", final=False)
        self.assertEqual(self.store.preferences(), [])
        self.controller.submit("Remember that I prefer short notes.")
        self.assertEqual(self.store.preferences()[0]["text"], "I prefer short notes")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.store.recent_tasks(), [])

    def test_completed_task_survives_restart_and_is_sent_as_context(self):
        self.controller.submit("open notes")
        eventually(lambda: self.store.recent_tasks() and self.store.recent_tasks()[0]["status"] == "completed")
        self.controller.close()
        self.store = MemoryStore(self.path)
        self.client = Client(lambda state: "done")
        self.controller = self.start()
        self.controller.submit("use that app again")
        eventually(lambda: self.client.calls)
        prior = self.client.calls[0]["memory"]["recent_tasks"][0]
        self.assertEqual(prior["command"], "open notes")
        self.assertEqual(prior["actions"][0]["app"], "notes.desktop")
        self.assertIn("never execute a past command", self.client.calls[0]["rules"])

    def test_cancelled_inflight_command_is_recorded_without_actions(self):
        entered, release = threading.Event(), threading.Event()
        def choose(state):
            entered.set()
            release.wait(2)
            return "launch_app"
        self.client.choose = choose
        self.controller.submit("open notes")
        self.assertTrue(entered.wait(1))
        self.controller.cancel()
        release.set()
        eventually(lambda: self.store.recent_tasks())
        self.assertEqual(self.store.recent_tasks()[0]["status"], "cancelled")
        self.assertEqual(self.store.recent_tasks()[0]["actions"], [])
        self.assertEqual(self.backend.calls, [])

    def test_cancel_at_completion_decision_keeps_cancelled_status(self):
        self.client.choose = lambda state: "done"
        def handler(event):
            self.events.append(event)
            if event["type"] == "decision":
                self.controller.cancel()
        self.controller.set_event_handler(handler)
        self.controller.submit("open notes")
        eventually(lambda: self.store.recent_tasks())
        self.assertEqual(self.store.recent_tasks()[0]["status"], "cancelled")
        self.assertFalse(any(event["message"] in {"Done.", "No further action needed."} for event in self.events))

    def test_shutdown_keeps_memory_open_until_a_performed_action_returns(self):
        entered, release = threading.Event(), threading.Event()
        def delayed_launch(app):
            self.backend.calls.append(("launch", app))
            entered.set()
            release.wait(3)
            return "Opened Text Editor"
        self.backend.launch_app = delayed_launch
        self.controller.submit("open notes")
        self.assertTrue(entered.wait(1))
        self.controller.close()
        release.set()
        self.controller._worker.join(2)
        self.assertFalse(self.controller._worker.is_alive())
        restored = MemoryStore(self.path)
        try:
            task = restored.recent_tasks()[0]
            self.assertEqual(task["status"], "cancelled")
            self.assertEqual(task["actions"], [{"action": "launch_app", "app": "notes.desktop"}])
        finally:
            restored.close()

    def test_memory_references_only_become_text_and_url_choices_when_requested(self):
        memory = {"preferences": [], "recent_tasks": [{"actions": [{"url": "https://example.com", "text": "hello world"}]}]}
        state, _, _ = build_request("open that website again", True, {}, [], [], memory)
        self.assertIn("https://example.com", state["url_candidates"].values())
        self.assertIn("hello world", state["text_candidates"].values())
        state, _, _ = build_request("open notes", True, {}, [], [], memory)
        self.assertEqual(state["url_candidates"], {})
        self.assertEqual(state["text_candidates"], {})

    def test_followup_reopens_a_saved_note_without_changing_it(self):
        note = create_note(self.config["notes_directory"], "Original note")
        self.store.record_task("create a note", "completed", [{"action": "create_note", "note_path": str(note)}])
        self.client.choose = lambda state: "done" if state["completed_steps"] else "open_note"
        with patch.object(self.controller, "_open_note") as opener:
            self.controller.submit("open that note again")
            eventually(lambda: opener.called)
            self.assertEqual(opener.call_args.args, (note.resolve(),))
        self.assertEqual(note.read_text(), "Original note\n")

    def test_note_references_cannot_escape_notes_directory(self):
        outside = self.root / "other.md"
        outside.write_text("private content")
        with self.assertRaisesRegex(RuntimeError, "outside"):
            self.controller._validated_note(str(outside))
        folder = Path(self.config["notes_directory"])
        folder.mkdir()
        link = folder / "link.md"
        link.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "outside"):
            self.controller._validated_note(str(link))

    def test_cancel_after_save_keeps_artifact_and_stops_opening(self):
        def save_and_cancel(directory, content):
            note = create_note(directory, content)
            self.controller.cancel()
            return note
        self.client.choose = lambda state: "create_note"
        with patch("jev_linux.controller.create_note", side_effect=save_and_cancel), patch.object(self.controller, "_open_note") as opener:
            self.controller.submit("create a note containing hello")
            eventually(lambda: self.store.recent_tasks() and self.store.recent_tasks()[0]["actions"])
            task = self.store.recent_tasks()[0]
            self.assertEqual(task["status"], "cancelled")
            self.assertTrue(Path(task["actions"][0]["note_path"]).is_file())
            opener.assert_not_called()

    def test_clear_while_saving_does_not_restore_forgotten_history(self):
        finished = threading.Event()
        def save_and_clear(directory, content):
            note = create_note(directory, content)
            self.controller.clear_memory()
            return note
        self.client.choose = lambda state: "create_note"
        def event(event):
            self.events.append(event)
            if event["type"] == "action": finished.set()
        self.controller.set_event_handler(event)
        with patch("jev_linux.controller.create_note", side_effect=save_and_clear), patch.object(self.controller, "_open_note"):
            self.controller.submit("create a note containing hello")
            self.assertTrue(finished.wait(2))
        self.assertEqual(self.store.recent_tasks(), [])

    def test_writer_receives_preferences_and_previous_note_content(self):
        previous = create_note(self.config["notes_directory"], "My previous meeting agenda")
        self.store.record_task("write meeting agenda", "completed", [{"action": "create_note", "note_path": str(previous)}])
        self.store.remember("Use short paragraphs")
        self.client.choose = lambda state: "done" if state["completed_steps"] else "create_note"
        with patch.object(self.controller, "_open_note"):
            self.controller.submit("compose another similar note")
            eventually(lambda: self.client.generated)
            context = self.client.generated[0][1]
            self.assertEqual(context["previous_note"]["content"], "My previous meeting agenda\n")
            self.assertEqual(context["preferences"][0]["text"], "Use short paragraphs")

    def test_disabled_memory_omits_context_and_keeps_existing_history(self):
        self.store.remember("Use short paragraphs")
        self.controller._memory_enabled = False
        self.controller.submit("open notes")
        eventually(lambda: any(event["message"] == "Done." for event in self.events))
        self.assertEqual(self.client.calls[0]["memory"], {"recent_tasks": [], "preferences": []})
        self.assertEqual(self.store.recent_tasks(), [])
        self.assertEqual(len(self.controller.memory_snapshot()["preferences"]), 1)
        self.assertFalse(self.controller.memory_snapshot()["enabled"])

    def test_pending_confirmation_cancellation_is_not_reported_as_completed(self):
        self.client.choose = lambda state: "press_key"
        self.controller.submit("press delete")
        eventually(lambda: self.store.recent_tasks() and self.store.recent_tasks()[0]["status"] == "awaiting_confirmation")
        self.controller.cancel()
        self.assertEqual(self.store.recent_tasks()[0]["status"], "cancelled")
        self.assertEqual(self.backend.calls, [])

    def test_focus_token_is_bound_but_not_saved_as_history(self):
        self.client.choose = lambda state: "done" if state["completed_steps"] else "press_key"
        self.controller.submit("press delete")
        eventually(lambda: self.controller._pending is not None)
        self.controller.confirm()
        eventually(lambda: self.store.recent_tasks()[0]["status"] == "completed")
        self.assertEqual(self.backend.calls[0], ("key", "Delete", {"app": "Text Editor", "control": "/control/1"}))
        self.assertEqual(self.store.recent_tasks()[0]["actions"], [{"action": "press_key", "key": "Delete"}])

    def test_configured_secret_is_redacted_before_memory_write(self):
        self.controller.submit("Remember that my key is private-test-value")
        self.assertNotIn("private-test-value", self.store.preferences()[0]["text"])

    def test_context_budget_counts_escaped_json_strings(self):
        for index in range(20):
            self.store.remember("\\" * 500 + str(index))
        self.assertLessEqual(len(json.dumps(self.controller._memory_context(), ensure_ascii=False)), 16000)
        self.assertEqual(len(self.store.preferences()), 20)

    def test_clear_memory_voice_command_does_not_call_provider(self):
        self.store.remember("Use short paragraphs")
        self.controller.submit("Clear my memory.")
        self.assertEqual(self.store.preferences(), [])
        self.assertEqual(self.client.calls, [])

    def test_restart_with_memory_off_can_review_and_clear_retained_history(self):
        self.store.remember("Use short paragraphs")
        self.controller.close()
        self.controller = None
        self.config["memory_enabled"] = False
        with patch("jev_linux.controller.DATA_DIR", self.path.parent), patch("jev_linux.controller.MemoryStore", side_effect=lambda **kwargs: MemoryStore(self.path, **kwargs)):
            self.controller = Controller(self.backend, self.config, client=self.client, debounce=0)
            self.assertIsNone(self.controller.memory)
            snapshot = self.controller.memory_snapshot()
            self.assertFalse(snapshot["enabled"])
            self.assertEqual(snapshot["preferences"][0]["text"], "Use short paragraphs")
            self.controller.clear_memory()
            self.assertEqual(self.controller.memory_snapshot()["preferences"], [])

    def test_unavailable_memory_does_not_prevent_running_a_command(self):
        self.controller.close()
        self.controller = None
        with patch("jev_linux.controller.DATA_DIR", self.path.parent), patch("jev_linux.controller.MemoryStore", side_effect=RuntimeError("damaged database")):
            self.controller = Controller(self.backend, self.config, client=self.client, debounce=0)
            self.controller.submit("open notes")
            eventually(lambda: self.backend.calls)
            self.assertIn("without saved context", self.controller.memory_snapshot()["error"])
            self.assertFalse(self.controller.memory_snapshot()["enabled"])


if __name__ == "__main__":
    unittest.main()
