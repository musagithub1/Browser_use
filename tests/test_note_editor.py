"""Safety checks for reloading a note without discarding user edits."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gi
gi.require_version("Atspi", "2.0")
from gi.repository import Atspi

from jev_linux.desktop import DesktopError
from jev_linux.note_editor import refresh_note_steps
from jev_linux.notes import read_note


class States:
    def __init__(self, *states):
        self.states = set(states)

    def contains(self, state):
        return state in self.states


class Node:
    def __init__(self, name="", role="label", children=(), *, text=None, action=None,
                 attrs=None, showing=True):
        self.name, self.role, self.children = name, role, list(children)
        self.text, self.action, self.attrs = text, action, attrs or {}
        self.showing = showing
        self.actions = 0

    def clear_cache(self):
        pass

    def get_name(self):
        return self.name

    def get_attributes(self):
        return self.attrs

    def get_state_set(self):
        states = [Atspi.StateType.SENSITIVE, Atspi.StateType.ENABLED]
        if self.showing:
            states.append(Atspi.StateType.SHOWING)
        if self.text is not None:
            states.append(Atspi.StateType.EDITABLE)
        return States(*states)

    def get_interfaces(self):
        return ["Text", "EditableText"] if self.text is not None else []

    def get_role_name(self):
        return self.role

    def get_child_count(self):
        return len(self.children)

    def get_child_at_index(self, index):
        return self.children[index]

    def get_text_iface(self):
        return self

    def get_character_count(self):
        return len(self.text)

    def get_text(self, start, end):
        return self.text[start:end]

    def get_action_iface(self):
        return self if self.action is not None else None

    def get_n_actions(self):
        return 1

    def do_action(self, index):
        self.actions += 1
        self.action()
        return True


class NoteEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "Brief.md"
        self.path.write_bytes(b"# Brief\nOriginal analysis.\n")
        self.before = read_note(self.path, self.root)
        self.added = "## Action plan\nDay 1: verify the source."
        self.path.write_bytes(self.path.read_bytes() + (self.added + "\n").encode())
        self.after = {**read_note(self.path, self.root), "appended_text": self.added,
                      "previous_revision": self.before["revision"]}
        self.body = Node(role="text", text=self.before["content"])
        self.button = Node("Discard Changes and Reload", "push button", action=self.reload)
        self.banner = Node("File Has Changed on Disk")
        self.filename = Node(self.path.name, attrs={"file-uri": self.path.as_uri()})
        self.window = Node("Text Editor", "frame", [self.filename, self.body, self.banner, self.button])
        self.app = Node("gnome-text-editor", "application", [self.window],
                        attrs={"application-id": "org.gnome.TextEditor"})
        self.desktop = Node("Desktop", "desktop frame", [self.app])

    def reload(self):
        self.body.text = self.after["content"]
        self.button.showing = False
        self.banner.showing = False

    def show_confirmation(self):
        def confirm():
            self.reload()
            self.alert.showing = False
        self.discard = Node("Discard", "push button", action=confirm)
        cancel = Node("Cancel", "push button", action=lambda: None)
        self.alert = Node(f"Discard Changes to “{self.path.name}”?", "alert", [cancel, self.discard])
        self.window.children.append(self.alert)

    def steps(self):
        return refresh_note_steps(str(self.path), self.before, self.after, self.root,
                                  desktop=self.desktop)

    def complete(self, steps=None):
        steps = steps or self.steps()
        for _ in range(100):
            try:
                next(steps)
            except StopIteration as result:
                return result.value
        self.fail("Refresh did not settle")

    def test_known_unchanged_buffer_reloads_once_and_verifies_full_updated_body(self):
        self.assertIs(self.complete(), True)
        self.assertEqual(self.button.actions, 1)
        self.assertEqual(self.body.text, self.path.read_text())

    def test_initial_read_only_phase_yields_before_reload_and_can_be_cancelled(self):
        steps = self.steps()
        self.assertEqual(next(steps), 0)
        self.assertEqual(self.button.actions, 0)
        steps.close()
        self.assertEqual(self.button.actions, 0)

    def test_native_gtk4_sensitive_button_works_without_enabled_state(self):
        self.button.get_state_set = lambda: States(Atspi.StateType.SHOWING, Atspi.StateType.SENSITIVE)
        self.assertIs(self.complete(), True)
        self.assertEqual(self.button.actions, 1)

    def test_insensitive_reload_button_is_never_activated(self):
        self.button.get_state_set = lambda: States(Atspi.StateType.SHOWING)
        with self.assertRaisesRegex(DesktopError, "not available"):
            self.complete()
        self.assertEqual(self.button.actions, 0)

    def test_already_updated_editor_returns_verified_without_reload(self):
        self.body.text = self.after["content"]
        self.assertIs(self.complete(), True)
        self.assertEqual(self.button.actions, 0)

    def test_one_gtk_final_newline_is_allowed_without_dropping_other_whitespace(self):
        self.body.text += "\n"
        self.assertIs(self.complete(), True)
        self.body.text = self.before["content"] + "\n\n"
        with self.assertRaisesRegex(DesktopError, "unsaved"):
            self.complete()
        self.assertEqual(self.button.actions, 1)

    def test_one_gtk_omitted_final_newline_is_allowed(self):
        self.body.text = self.before["content"][:-1]
        self.assertIs(self.complete(), True)
        self.assertEqual(self.button.actions, 1)

    def test_restored_background_tabs_do_not_count_as_visible_document_bodies(self):
        restored = Node("Tabs", "page tab list", [
            Node(f"Background {index}", "page tab", [Node(role="text", text="private")])
            for index in range(190)])
        self.window.children.insert(0, restored)
        self.assertIs(self.complete(), True)
        self.assertEqual(self.button.actions, 1)

    def test_native_text_interface_static_calls_avoid_accessible_method_collision(self):
        from jev_linux.note_editor import _body_text, _Document
        # Real Accessible.get_text is a get_text_iface alias. Model that
        # collision and require the correct Text interface static binding.
        with patch.object(Atspi, "Accessible", Node), patch.object(
                Atspi.Text, "get_character_count", return_value=len(self.body.text)) as count, patch.object(
                Atspi.Text, "get_text", return_value=self.body.text) as text:
            self.assertEqual(_body_text(_Document(self.window, self.body, None, False)), self.body.text)
        count.assert_called_once_with(self.body)
        text.assert_called_once_with(self.body, 0, len(self.body.text))

    def test_user_edits_and_dirty_marker_are_never_discarded(self):
        for dirty in ("Edited by the user.", self.before["content"] + " "):
            self.body.text = dirty
            with self.assertRaisesRegex(DesktopError, "unsaved"):
                self.complete()
        self.body.text = self.before["content"]
        self.filename.attrs["modified"] = "true"
        with self.assertRaisesRegex(DesktopError, "unsaved"):
            self.complete()
        self.assertEqual(self.button.actions, 0)

    def test_wrong_file_association_duplicate_window_or_body_never_reloads(self):
        self.filename.attrs["file-uri"] = (self.root / "Other.md").as_uri()
        with self.assertRaisesRegex(DesktopError, "association"):
            self.complete()
        self.filename.attrs["file-uri"] = self.path.as_uri()
        other = Node("Text Editor", "frame", [Node(self.path.name), Node(role="text", text="other")])
        self.app.children.append(other)
        with self.assertRaisesRegex(DesktopError, "More than one"):
            self.complete()
        self.app.children.pop()
        self.window.children.append(Node(role="text", text=self.before["content"]))
        with self.assertRaisesRegex(DesktopError, "ambiguous"):
            self.complete()
        self.assertEqual(self.button.actions, 0)

    def test_exposed_folder_and_dirty_window_title_are_checked(self):
        folder = Node(str(self.root))
        self.window.children.append(folder)
        folder.name = str(self.root / "other-folder")
        with self.assertRaisesRegex(DesktopError, "folder association"):
            self.complete()
        folder.name = str(self.root)
        self.window.name = "*" + self.path.name + " - Text Editor"
        with self.assertRaisesRegex(DesktopError, "unsaved"):
            self.complete()
        self.assertEqual(self.button.actions, 0)

    def test_disk_changed_since_append_is_not_reloaded(self):
        self.path.write_text("New external edit")
        with self.assertRaisesRegex(DesktopError, "changed again"):
            self.complete()
        self.assertEqual(self.button.actions, 0)

    def test_symlink_replacement_is_not_reloaded(self):
        other = self.root / "Other.md"
        self.path.rename(other)
        self.path.symlink_to(other)
        with self.assertRaisesRegex(DesktopError, "path changed"):
            self.complete()
        self.assertEqual(self.button.actions, 0)

    def test_truncated_or_unlinked_evidence_is_rejected(self):
        self.before["truncated"] = True
        with self.assertRaisesRegex(DesktopError, "Full before"):
            self.complete()
        self.before["truncated"] = False
        self.after["previous_revision"] = "a" * 64
        with self.assertRaisesRegex(DesktopError, "linked"):
            self.complete()
        self.assertEqual(self.button.actions, 0)

    def test_buffer_change_during_immediate_revalidation_prevents_reload(self):
        from jev_linux import note_editor
        original = note_editor._disk_matches
        def external_edit(*args):
            original(*args)
            self.body.text = "Unsaved input after initial disk validation"
        with patch.object(note_editor, "_disk_matches", side_effect=external_edit):
            with self.assertRaisesRegex(DesktopError, "unsaved"):
                self.complete()
        self.assertEqual(self.button.actions, 0)

    def test_reload_acceptance_without_updated_body_is_not_completion(self):
        self.button.action = lambda: None
        now = iter(range(10))
        steps = refresh_note_steps(str(self.path), self.before, self.after, self.root,
                                  desktop=self.desktop, clock=lambda: next(now))
        with self.assertRaisesRegex(DesktopError, "could not be verified"):
            self.complete(steps)
        self.assertEqual(self.button.actions, 1)

    def test_transient_incomplete_tree_after_reload_retries_reads_without_reloading_again(self):
        def reload_then_replace_tree():
            self.reload()
            original = self.body.get_child_count
            pending = {"once": True}
            def count():
                if pending["once"]:
                    pending["once"] = False
                    return -1
                return original()
            self.body.get_child_count = count
        self.button.action = reload_then_replace_tree
        self.assertIs(self.complete(), True)
        self.assertEqual(self.button.actions, 1)
        self.assertEqual(self.body.text, self.after["content"])

    def test_persistent_incomplete_tree_after_reload_stops_without_second_action(self):
        def reload_then_incomplete_tree():
            self.reload()
            self.body.get_child_count = lambda: -1
        self.button.action = reload_then_incomplete_tree
        now = iter(range(10))
        steps = refresh_note_steps(str(self.path), self.before, self.after, self.root,
                                  desktop=self.desktop, clock=lambda: next(now))
        with self.assertRaisesRegex(DesktopError, "could not be verified"):
            self.complete(steps)
        self.assertEqual(self.button.actions, 1)

    def test_post_reload_ambiguity_does_not_enter_transient_retry(self):
        def reload_then_ambiguous_tree():
            self.reload()
            self.window.children.append(Node(role="text", text=self.after["content"]))
        self.button.action = reload_then_ambiguous_tree
        with self.assertRaisesRegex(DesktopError, "ambiguous"):
            self.complete()
        self.assertEqual(self.button.actions, 1)

    def test_exact_native_reload_confirmation_is_guarded_and_completed_once(self):
        self.button.action = self.show_confirmation
        self.assertIs(self.complete(), True)
        self.assertEqual(self.button.actions, 1)
        self.assertEqual(self.discard.actions, 1)
        self.assertEqual(self.body.text, self.after["content"])

    def test_pending_exact_confirmation_does_not_repeat_initial_reload_request(self):
        self.show_confirmation()
        self.assertIs(self.complete(), True)
        self.assertEqual(self.button.actions, 0)
        self.assertEqual(self.discard.actions, 1)

    def test_wrong_filename_confirmation_is_never_approved(self):
        self.button.action = self.show_confirmation
        steps = self.steps()
        next(steps)
        next(steps)
        self.alert.name = "Discard Changes to “Other.md”?"
        with self.assertRaisesRegex(DesktopError, "unfamiliar"):
            self.complete(steps)
        self.assertEqual(self.discard.actions, 0)

    def test_changed_buffer_or_disk_while_confirmation_waits_is_not_discarded(self):
        for mutation in (lambda: setattr(self.body, "text", "New unsaved input"),
                         lambda: self.path.write_text("New external content")):
            with self.subTest(mutation=mutation):
                self.setUp()
                self.button.action = self.show_confirmation
                steps = self.steps()
                next(steps)  # Read-only initial phase.
                next(steps)  # Initial reload request opens the native alert.
                self.assertEqual(next(steps), 0)  # Lease boundary before confirmation.
                mutation()
                with self.assertRaises(DesktopError):
                    self.complete(steps)
                self.assertEqual(self.discard.actions, 0)

    def test_confirmation_acceptance_without_updated_text_is_not_completion(self):
        self.show_confirmation()
        self.discard.action = lambda: setattr(self.alert, "showing", False)
        now = iter(range(10))
        steps = refresh_note_steps(str(self.path), self.before, self.after, self.root,
                                  desktop=self.desktop, clock=lambda: next(now))
        with self.assertRaisesRegex(DesktopError, "could not be verified"):
            self.complete(steps)
        self.assertEqual(self.button.actions, 0)
        self.assertEqual(self.discard.actions, 1)


if __name__ == "__main__":
    unittest.main()
