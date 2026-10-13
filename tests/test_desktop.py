import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jev_linux.desktop import DesktopBackend, DesktopError, _Element
from jev_linux.portal import parse_key
from gi.repository import Atspi


class FakeStates:
    def __init__(self, *states):
        self.states = set(states)

    def contains(self, state):
        return state in self.states


class FakeApp:
    def get_name(self):
        return "Notes"

    def get_process_id(self):
        return 999999


class FakeText:
    def __init__(self, text="old document", editable=True):
        self.value = text
        self.position = 4
        self.name = "Note body"
        self.editable = editable
        self.insertions = []
        self.selection_removed = False

    def clear_cache(self):
        pass

    def get_state_set(self):
        return FakeStates(Atspi.StateType.SHOWING, Atspi.StateType.SENSITIVE,
                          Atspi.StateType.EDITABLE, Atspi.StateType.FOCUSED)

    def get_role_name(self):
        return "text"

    def get_role(self):
        return Atspi.Role.TEXT

    def get_name(self):
        return self.name

    def get_attributes(self):
        return {}

    def get_application(self):
        return FakeApp()

    def get_interfaces(self):
        return ["Text"] + (["EditableText"] if self.editable else [])

    def get_text_iface(self):
        return self

    def get_editable_text_iface(self):
        return self if self.editable else None

    def get_caret_offset(self):
        return self.position

    def set_caret_offset(self, position):
        self.position = position
        return True

    def get_n_selections(self):
        return 1

    def remove_selection(self, index):
        self.selection_removed = True
        return True

    def insert_text(self, position, text, length):
        self.insertions.append((position, text, length))
        self.value = self.value[:position] + text + self.value[position:]
        return True


class FakePortal:
    def __init__(self):
        self.typed = []
        self.keys = []

    def type_text(self, text):
        self.typed.append(text)

    def press_key(self, key):
        self.keys.append(key)


def backend_with_control(control):
    backend = DesktopBackend.__new__(DesktopBackend)
    backend._lock = threading.RLock()
    backend._own_pid = -100
    backend._portal = FakePortal()
    backend._refs = {"e1": _Element(control, control.get_name(), "text", "Notes", None,
                                   tuple(control.get_interfaces()))}
    return backend


def install_focus_snapshot(backend, *, generation=1, app_name="Notes", pid=999999,
                           bus=":1.42", window="/accessible/window1", control="/accessible/control1",
                           flags=None):
    node = FakeText("Private document content")
    node.app = SimpleNamespace(bus_name=bus)
    node.path = control
    app = FakeApp()
    app.get_name = lambda: app_name
    app.get_process_id = lambda: pid
    node.get_application = lambda: app
    target = f"e{generation}_1"
    backend._refs = {target: _Element(node, node.get_name(), "text", app_name,
                                     SimpleNamespace(path=window), tuple(node.get_interfaces()))}
    return {"focused_element_id": target, "focused_app": app_name, **(flags or {})}


class DesktopTests(unittest.TestCase):
    def test_insert_preserves_existing_document_at_cursor_and_handles_unicode(self):
        control = FakeText()
        backend = backend_with_control(control)
        backend.type_text("café ", "e1")
        self.assertEqual(control.value, "old café document")
        self.assertEqual(control.insertions, [(4, "café ", 6)])
        self.assertEqual(control.position, 9)
        self.assertEqual(backend._portal.typed, [])

    def test_changed_control_name_is_rejected(self):
        control = FakeText()
        backend = backend_with_control(control)
        control.name = "Password"
        with self.assertRaisesRegex(DesktopError, "changed"):
            backend.type_text("secret", "e1")
        self.assertEqual(control.insertions, [])

    def test_password_attribute_is_rejected(self):
        control = FakeText()
        backend = backend_with_control(control)
        control.get_attributes = lambda: {"input-type": "password"}
        with self.assertRaisesRegex(DesktopError, "Password"):
            backend.type_text("secret", "e1")

    def test_portal_fallback_does_not_type_into_different_focused_control(self):
        intended = FakeText(editable=False)
        foreground = FakeText(editable=False)
        backend = backend_with_control(intended)
        backend._refs["e2"] = _Element(foreground, foreground.get_name(), "text", "Notes", None,
                                       tuple(foreground.get_interfaces()))
        with patch.object(backend, "_focused_target", return_value="e2"):
            with self.assertRaisesRegex(DesktopError, "foreground target"):
                backend.type_text("hello", "e1")
        self.assertEqual(backend._portal.typed, [])

    def test_portal_fallback_clears_selection_and_checks_exact_foreground_control(self):
        control = FakeText(editable=False)
        backend = backend_with_control(control)
        with patch.object(backend, "_focused_target", return_value="e1"):
            backend.type_text("hello", "e1")
        self.assertTrue(control.selection_removed)
        self.assertEqual(backend._portal.typed, ["hello"])

    def test_unverified_foreground_focus_refuses_global_keys(self):
        backend = backend_with_control(FakeText())
        with patch.object(backend, "snapshot", return_value={"focus_incomplete": True}):
            with self.assertRaisesRegex(DesktopError, "could not be verified"):
                backend._focused_target()

    def test_url_rejects_non_web_schemes_and_embedded_credentials(self):
        backend = backend_with_control(FakeText())
        for url in ("file:///etc/passwd", "javascript:alert(1)", "https://user:pass@example.com", "https://example.com\n"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                backend.open_url(url)

    def test_supported_key_chords_and_rejected_system_sequence(self):
        for key in ("Ctrl+n", "Shift+Tab", "Page_Up", "Ctrl+Shift+N", "F12"):
            self.assertTrue(all(parse_key(key)))
        for key in ("Ctrl+Alt+Delete", "Ctrl++", "XF86PowerOff", "Ctrl+Ctrl+N"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                parse_key(key)

    def test_bound_shortcut_survives_new_snapshot_generation_of_same_control(self):
        backend = backend_with_control(FakeText())
        original = install_focus_snapshot(backend, generation=1)
        token = backend.focus_token(original)
        self.assertNotIn("Private document content", json.dumps(token))
        self.assertNotIn("Note body", json.dumps(token))
        self.assertNotIn("e1_1", json.dumps(token))
        # Token can round-trip through JSON; a new GI proxy/snapshot eID for
        # the same application/window/control must remain the same target.
        token = json.loads(json.dumps(token))
        current = install_focus_snapshot(backend, generation=8)
        with patch.object(backend, "snapshot", return_value=current):
            backend.press_key("Delete", expected_focus=token)
        self.assertEqual(backend._portal.keys, ["Delete"])

    def test_bound_shortcut_rejects_changed_app_window_control_and_restart(self):
        changes = [{"app_name": "Browser"}, {"window": "/accessible/window2"},
                   {"control": "/accessible/control2"}, {"pid": 888888}, {"bus": ":1.99"}]
        for change in changes:
            with self.subTest(change=change):
                backend = backend_with_control(FakeText())
                token = backend.focus_token(install_focus_snapshot(backend))
                current = install_focus_snapshot(backend, generation=2, **change)
                with patch.object(backend, "snapshot", return_value=current):
                    with self.assertRaisesRegex(DesktopError, "focused target changed"):
                        backend.press_key("Delete", expected_focus=token)
                self.assertEqual(backend._portal.keys, [])

    def test_bound_shortcut_rejects_unverified_password_and_jev_focus(self):
        for flag in ("focus_incomplete", "focus_ambiguous", "password_focused", "self_focused"):
            with self.subTest(flag=flag):
                backend = backend_with_control(FakeText())
                token = backend.focus_token(install_focus_snapshot(backend))
                current = install_focus_snapshot(backend, generation=2, flags={flag: True})
                with patch.object(backend, "snapshot", return_value=current):
                    with self.assertRaisesRegex(DesktopError, "focused target changed"):
                        backend.press_key("Delete", expected_focus=token)
                self.assertEqual(backend._portal.keys, [])

    def test_focus_token_from_old_snapshot_rejects_expired_element_id(self):
        backend = backend_with_control(FakeText())
        old = install_focus_snapshot(backend, generation=1)
        install_focus_snapshot(backend, generation=2)
        with self.assertRaisesRegex(DesktopError, "expired"):
            backend.focus_token(old)

    def test_focus_token_rechecks_control_focus_after_snapshot(self):
        backend = backend_with_control(FakeText())
        current = install_focus_snapshot(backend)
        node = backend._refs[current["focused_element_id"]].accessible
        node.get_state_set = lambda: FakeStates(Atspi.StateType.SHOWING, Atspi.StateType.SENSITIVE)
        with self.assertRaisesRegex(DesktopError, "no longer has verified focus"):
            backend.focus_token(current)

    def test_focus_token_requires_stable_atspi_identity(self):
        backend = backend_with_control(FakeText())
        current = install_focus_snapshot(backend, control=None)
        with self.assertRaisesRegex(DesktopError, "no stable identity"):
            backend.focus_token(current)

    def test_unbound_shortcut_preserves_default_verified_focus_behavior(self):
        backend = backend_with_control(FakeText())
        current = install_focus_snapshot(backend)
        with patch.object(backend, "snapshot", return_value=current):
            backend.press_key("Ctrl+n")
        self.assertEqual(backend._portal.keys, ["Ctrl+n"])


if __name__ == "__main__":
    unittest.main()
