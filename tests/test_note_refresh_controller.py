"""Only current-task, complete append evidence can trigger native reload."""

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock

from jev_linux.controller import Controller
from jev_linux.notes import append_note, read_note


class NoteRefreshControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "Brief.md"
        self.path.write_text("Original brief.\n")
        self.before = {"source": "note", **read_note(self.path, self.root)}
        self.added = "Day 1: compare evidence."
        append_note(self.path, self.added, self.root, expected_revision=self.before["revision"])
        self.after = {"source": "note", **read_note(self.path, self.root),
                      "appended_text": self.added, "previous_revision": self.before["revision"]}
        self.utterance = SimpleNamespace(version=0, plan=None, completed=[
            {"action": "append_note", "arguments": {"note_path": str(self.path), "text": self.added}}],
            task_data=[self.before, self.after])
        self.controller = Controller.__new__(Controller)
        self.controller.config = {"notes_directory": str(self.root)}
        self.controller.backend = SimpleNamespace(refresh_note=Mock(return_value=True))
        self.controller._current = Mock(return_value=True)
        self.controller._open_note = Mock()
        self.controller._record_action = Mock()

    def open(self):
        self.controller._execute(self.utterance, "open_note", {"note_path": str(self.path)}, "open")

    def test_full_linked_evidence_is_dispatched_and_recorded_as_verified(self):
        self.open()
        self.controller.backend.refresh_note.assert_called_once_with(
            str(self.path), self.before, self.after, str(self.root))
        result = self.controller._record_action.call_args.args[-1]
        self.assertIn("verified updated text in Text Editor", result)

    def test_missing_or_truncated_current_task_source_refuses_reload_and_completion(self):
        self.utterance.task_data = [self.after]
        with self.assertRaisesRegex(RuntimeError, "evidence is unavailable"):
            self.open()
        self.utterance.task_data = [self.before, self.after]
        self.before["truncated"] = True
        with self.assertRaisesRegex(RuntimeError, "evidence is unavailable"):
            self.open()
        self.controller.backend.refresh_note.assert_not_called()
        self.controller._open_note.assert_not_called()
        self.controller._record_action.assert_not_called()

    def test_previous_task_append_does_not_trigger_reload(self):
        self.utterance.completed = []
        self.open()
        self.controller.backend.refresh_note.assert_not_called()
        self.assertEqual(self.controller._record_action.call_args.args[-1], f"Opened saved note: {self.path}")

    def test_refresh_rejection_or_unverified_result_is_not_recorded_as_opened(self):
        self.controller.backend.refresh_note.return_value = False
        with self.assertRaisesRegex(RuntimeError, "not verified"):
            self.open()
        self.controller._record_action.assert_not_called()
        self.controller.backend.refresh_note.side_effect = RuntimeError("Unsaved editor input")
        with self.assertRaisesRegex(RuntimeError, "Unsaved editor input"):
            self.open()
        self.controller._record_action.assert_not_called()

    def test_backend_without_refresh_preserves_existing_open_behavior(self):
        self.controller.backend = SimpleNamespace()
        self.utterance.task_data = []
        self.open()
        self.controller._open_note.assert_called_once_with(self.path)
        self.assertEqual(self.controller._record_action.call_args.args[-1], f"Opened saved note: {self.path}")

    def test_append_readback_records_its_source_revision(self):
        self.path.write_text(self.before["content"])
        self.utterance.completed = []
        self.utterance.task_data = [self.before]
        self.controller._execute(self.utterance, "append_note", {
            "note_path": str(self.path), "text": self.added, "expected_revision": self.before["revision"]}, "append")
        self.assertEqual(self.utterance.task_data[-1], self.after)
        self.assertEqual(self.path.read_bytes(), (self.before["content"] + self.added + "\n").encode())

    def test_append_without_explicit_read_captures_the_unchanged_source(self):
        self.path.write_text(self.before["content"])
        self.utterance.task_data = []
        self.controller._execute(self.utterance, "append_note", {
            "note_path": str(self.path), "text": self.added, "expected_revision": self.before["revision"]}, "append")
        self.assertEqual(self.utterance.task_data, [self.before, self.after])

    def test_cancel_during_preappend_read_cannot_write(self):
        self.path.write_text(self.before["content"])
        self.controller._current.side_effect = [True, True, False]
        self.controller._execute(self.utterance, "append_note", {
            "note_path": str(self.path), "text": self.added, "expected_revision": self.before["revision"]}, "append")
        self.assertEqual(self.path.read_text(), self.before["content"])
        self.controller._record_action.assert_not_called()

    def test_native_refresh_dispatch_uses_current_task_lease(self):
        dispatch = Mock(return_value=True)
        self.controller.backend.run_guarded = dispatch
        self.open()
        method, args, kwargs, guard = dispatch.call_args.args
        self.assertEqual(method, "refresh_note")
        self.assertEqual(args, (str(self.path), self.before, self.after, str(self.root)))
        self.assertEqual(kwargs, {})
        self.assertTrue(guard())
        self.utterance.version += 1
        self.assertFalse(guard())


if __name__ == "__main__":
    unittest.main()
