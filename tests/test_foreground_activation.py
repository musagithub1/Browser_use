"""Portal typing must verify foreground ownership after native activation."""

import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from jev_linux.desktop import DesktopBackend, DesktopError, _Element
from jev_linux.main_thread_desktop import MainThreadDesktopBackend
from gi.repository import Atspi

from tests.test_desktop import FakeStates, FakeText


class FocusText(FakeText):
    def __init__(self, *, app_name="Google Chrome", pid=300, bus=":1.65",
                 path="/accessible/search", focused=True, focus_delay=0):
        super().__init__(editable=False)
        self.app = SimpleNamespace(bus_name=bus)
        self.path = path
        self.owner = SimpleNamespace(get_name=lambda: app_name,
                                     get_process_id=lambda: pid)
        self.focused = focused
        self.focus_delay = focus_delay
        self.focus_pending = False
        self.grab_calls = 0
        self.role = "text"

    def get_application(self):
        return self.owner

    def get_interfaces(self):
        return ["Text", "Component"]

    def get_role_name(self):
        return self.role

    def get_state_set(self):
        states = [Atspi.StateType.SHOWING, Atspi.StateType.SENSITIVE,
                  Atspi.StateType.EDITABLE]
        if self.focused:
            states.append(Atspi.StateType.FOCUSED)
        return FakeStates(*states)

    def get_component_iface(self):
        return self

    def grab_focus(self):
        self.grab_calls += 1
        self.focus_pending = True
        return True

    def clear_cache(self):
        if self.focus_pending:
            if self.focus_delay > 0:
                self.focus_delay -= 1
            else:
                self.focused = True
                self.focus_pending = False


class Portal:
    def __init__(self, *, keyboard=True):
        self.keyboard = keyboard
        self.keys = []
        self.typed = []
        self.before_type = lambda: None

    def status(self):
        return {"keyboard": self.keyboard, "granted": self.keyboard}

    def press_key(self, key):
        self.keys.append(key)

    def type_text(self, text):
        self.before_type()
        self.typed.append(text)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay


class FocusWindow:
    def __init__(self, bus, path):
        self.app = SimpleNamespace(bus_name=bus)
        self.path = path
        self.active = True
        self.pending_active = None

    def clear_cache(self):
        if self.pending_active is not None:
            self.active = self.pending_active
            self.pending_active = None

    def get_state_set(self):
        states = [Atspi.StateType.SHOWING, Atspi.StateType.SENSITIVE]
        if self.active:
            states.append(Atspi.StateType.ACTIVE)
        return FakeStates(*states)


def window(bus=":1.65", path="/accessible/window"):
    return FocusWindow(bus, path)


def element(control, owner_window):
    app = control.get_application()
    return _Element(control, control.get_name(), control.get_role_name(),
                    app.get_name(), owner_window, tuple(control.get_interfaces()),
                    app.get_process_id(), DesktopBackend._window_identity(owner_window))


class ForegroundActivationTests(unittest.TestCase):
    def setUp(self):
        self.target = FocusText()
        self.target_window = window()
        self.other = FocusText(app_name="Text Editor", pid=100, bus=":1.25",
                              path="/accessible/editor")
        self.other_window = window(":1.25", "/accessible/editor-window")
        self.backend = DesktopBackend.__new__(DesktopBackend)
        self.backend._lock = threading.RLock()
        self.backend._own_pid = 900
        self.backend._portal = Portal()
        self.backend._refs = {"chosen": element(self.target, self.target_window)}
        self.backend._apps = {}
        self.backend._last_snapshot = {}
        self.clock = FakeClock()
        for name, replacement in (("time.monotonic", self.clock.monotonic),
                                  ("time.sleep", self.clock.sleep)):
            patcher = patch("jev_linux.desktop." + name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.generation = 0
        self.snapshots_after_key = 0
        self.current_foreground = None

    def publish(self, control, owner_window):
        # Real scans clear old element IDs. The original requested object must
        # survive that refresh and be revalidated against its original record.
        self.generation += 1
        focused_id = f"e{self.generation}_1"
        focused = element(control, owner_window)
        self.backend._refs = {focused_id: focused}
        self.current_foreground = (control, owner_window)
        snapshot = {
            "focused_app": focused.app,
            "focused_element_id": focused_id,
            "windows": [{"app": focused.app, "pid": focused.pid,
                         "window_id": focused.window_id, "active": True}],
            "focus_incomplete": False,
            "self_focused": False,
            "password_focused": False,
        }
        self.backend._last_snapshot = snapshot
        return snapshot

    def background_until_activation(self, *, focus_delay=0, destination=None,
                                    before_foreground=None):
        destination = destination or (self.target, self.target_window)

        def snapshot(**kwargs):
            if self.backend._portal.keys:
                self.snapshots_after_key += 1
                if self.snapshots_after_key > focus_delay:
                    if before_foreground:
                        before_foreground()
                    return self.publish(*destination)
            return self.publish(self.other, self.other_window)

        return snapshot

    def start_typing_with_gtk_owner_focus(self):
        # Jev's own focus-out notification cannot run until its GTK callback
        # returns. Sending an activation shortcut alone does not process it.
        self.gtk_owner_focused = True
        self.other.owner.get_name = lambda: "Jev Linux"
        self.other.owner.get_process_id = lambda: self.backend._own_pid
        self.target_window.active = False
        self.scheduled = queue.Queue()
        self.resume_delays = []

        def snapshot(**kwargs):
            if not self.gtk_owner_focused:
                return self.publish(self.target, self.target_window)
            result = self.publish(self.other, self.other_window)
            result["self_focused"] = True
            return result

        def schedule_after(delay, callback):
            self.resume_delays.append(delay)
            self.scheduled.put(callback)

        for patcher in (patch.object(self.backend, "snapshot", side_effect=snapshot),
                        patch.object(self.backend, "_activation_shortcut", return_value="Super+4"),
                        patch.object(self.backend, "close", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)
        proxy = MainThreadDesktopBackend(self.backend, schedule=self.scheduled.put,
                                         schedule_after=schedule_after)
        outcome = {}

        def type_on_worker():
            try:
                outcome["result"] = proxy.type_text("Hello", "chosen")
            except Exception as error:
                outcome["error"] = error

        worker = threading.Thread(target=type_on_worker)
        worker.start()

        def cleanup():
            proxy.close()
            worker.join(1)

        self.addCleanup(cleanup)
        return proxy, worker, outcome

    def dispatch_typing_phase(self):
        callback = self.scheduled.get(timeout=1)
        self.assertIs(callback(), False)

    def process_gtk_focus_out(self):
        self.gtk_owner_focused = False
        self.other_window.active = False
        self.target_window.active = True

    def test_gtk_owner_focus_out_runs_between_activation_and_verified_typing_callbacks(self):
        _, worker, outcome = self.start_typing_with_gtk_owner_focus()
        self.dispatch_typing_phase()

        self.assertTrue(worker.is_alive())
        self.assertTrue(self.gtk_owner_focused)
        self.assertEqual(self.backend._portal.keys, ["Super+4"])
        self.assertEqual(self.backend._portal.typed, [])
        self.assertEqual(self.resume_delays, [0.05])
        self.assertEqual(self.clock.sleeps, [])

        self.process_gtk_focus_out()
        self.dispatch_typing_phase()
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", outcome)
        self.assertIs(self.current_foreground[0], self.target)
        self.assertIs(self.current_foreground[1], self.target_window)
        self.assertEqual(self.backend._portal.keys, ["Super+4"])
        self.assertEqual(self.backend._portal.typed, ["Hello"])

    def test_resumable_typing_keeps_original_target_across_interphase_snapshot_ids(self):
        proxy, worker, outcome = self.start_typing_with_gtk_owner_focus()
        self.dispatch_typing_phase()
        first_ids = set(self.backend._refs)
        proxy.snapshot(preferred_app="Text Editor")
        self.assertTrue(first_ids.isdisjoint(self.backend._refs))
        self.assertNotIn("chosen", self.backend._refs)

        self.process_gtk_focus_out()
        self.dispatch_typing_phase()
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", outcome)
        self.assertEqual(self.backend._portal.keys, ["Super+4"])
        self.assertEqual(self.backend._portal.typed, ["Hello"])

    def test_resumable_typing_revalidates_original_metadata_after_interphase_snapshot(self):
        proxy, worker, outcome = self.start_typing_with_gtk_owner_focus()
        self.dispatch_typing_phase()
        self.process_gtk_focus_out()
        self.target.name = "Different input"
        proxy.snapshot(preferred_app="Google Chrome")
        fresh_element = next(iter(self.backend._refs.values()))
        self.assertEqual(fresh_element.name, "Different input")

        self.dispatch_typing_phase()
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertIsInstance(outcome["error"], DesktopError)
        self.assertRegex(str(outcome["error"]), "changed")
        self.assertEqual(self.backend._portal.keys, ["Super+4"])
        self.assertEqual(self.backend._portal.typed, [])

    def test_delayed_component_focus_is_polled_before_typing(self):
        self.target.focused = False
        self.target.focus_delay = 3
        sibling = FocusText(path="/accessible/toolbar")

        def snapshot(**kwargs):
            control = self.target if self.target.focused else sibling
            return self.publish(control, self.target_window)

        with patch.object(self.backend, "snapshot", side_effect=snapshot), \
             patch.object(self.backend, "_activation_shortcut", return_value="Super+4") as shortcut:
            self.backend.type_text("hello", "chosen")

        self.assertEqual(self.target.grab_calls, 1)
        self.assertTrue(self.clock.sleeps)
        self.assertLessEqual(sum(self.clock.sleeps), 0.5)
        self.assertEqual(self.backend._portal.typed, ["hello"])
        self.assertEqual(self.backend._portal.keys, [])
        shortcut.assert_not_called()

    def test_unacknowledged_component_focus_has_bounded_deadline_and_never_types(self):
        self.target.focused = False
        self.target.focus_delay = 1000
        sibling = FocusText(path="/accessible/toolbar")
        with patch.object(self.backend, "snapshot", side_effect=lambda **kwargs: self.publish(sibling, self.target_window)), \
             patch.object(self.backend, "_activation_shortcut", return_value="Super+4") as shortcut:
            with self.assertRaises(DesktopError):
                self.backend.type_text("hello", "chosen")

        self.assertEqual(self.target.grab_calls, 1)
        self.assertAlmostEqual(sum(self.clock.sleeps), 0.5)
        self.assertEqual(self.backend._portal.typed, [])
        self.assertEqual(self.backend._portal.keys, [])
        shortcut.assert_not_called()

    def test_background_remembered_focus_activates_once_and_waits_for_foreground(self):
        # Chromium can retain FOCUSED while another application owns the desktop.
        self.assertTrue(self.target.focused)
        snapshot = self.background_until_activation(focus_delay=2)

        def verify_before_type():
            self.assertGreater(self.snapshots_after_key, 2)
            self.assertIs(self.current_foreground[0], self.target)
            self.assertIs(self.current_foreground[1], self.target_window)

        self.backend._portal.before_type = verify_before_type
        with patch.object(self.backend, "snapshot", side_effect=snapshot), \
             patch.object(self.backend, "_activation_shortcut", return_value="Super+4") as shortcut:
            self.backend.type_text("hello", "chosen")

        shortcut.assert_called_once()
        self.assertEqual(self.backend._portal.keys, ["Super+4"])
        self.assertEqual(self.backend._portal.typed, ["hello"])

    def test_app_activation_that_never_reaches_foreground_is_bounded_and_not_repeated(self):
        snapshot = self.background_until_activation(focus_delay=1000)
        with patch.object(self.backend, "snapshot", side_effect=snapshot), \
             patch.object(self.backend, "_activation_shortcut", return_value="Super+4") as shortcut:
            with self.assertRaises(DesktopError):
                self.backend.type_text("hello", "chosen")

        shortcut.assert_called_once()
        self.assertGreaterEqual(sum(self.clock.sleeps), 3.0)
        self.assertLessEqual(sum(self.clock.sleeps), 3.05 + 1e-9)
        self.assertEqual(self.backend._portal.keys, ["Super+4"])
        self.assertEqual(self.backend._portal.typed, [])

    def test_missing_favorite_mapping_sends_no_keys_or_text(self):
        with patch.object(self.backend, "snapshot", side_effect=self.background_until_activation()), \
             patch.object(self.backend, "_activation_shortcut", return_value=None):
            with self.assertRaises(DesktopError):
                self.backend.type_text("hello", "chosen")

        self.assertEqual(self.backend._portal.keys, [])
        self.assertEqual(self.backend._portal.typed, [])

    def test_keyboard_consent_is_required_before_app_activation(self):
        self.backend._portal.keyboard = False
        with patch.object(self.backend, "snapshot", side_effect=self.background_until_activation()), \
             patch.object(self.backend, "_activation_shortcut", return_value="Super+4"):
            with self.assertRaises(DesktopError):
                self.backend.type_text("hello", "chosen")

        self.assertEqual(self.backend._portal.keys, [])
        self.assertEqual(self.backend._portal.typed, [])

    def test_activation_landing_in_another_window_never_types(self):
        wrong = FocusText(path="/accessible/popup-field")
        wrong_window = window(path="/accessible/popup-window")
        snapshot = self.background_until_activation(destination=(wrong, wrong_window))
        with patch.object(self.backend, "snapshot", side_effect=snapshot), \
             patch.object(self.backend, "_activation_shortcut", return_value="Super+4"):
            with self.assertRaises(DesktopError):
                self.backend.type_text("hello", "chosen")

        self.assertEqual(self.backend._portal.keys, ["Super+4"])
        self.assertEqual(self.backend._portal.typed, [])

    def assert_original_control_change_is_rejected(self, attribute, replacement):
        snapshot = self.background_until_activation(
            before_foreground=lambda: setattr(self.target, attribute, replacement))
        with patch.object(self.backend, "snapshot", side_effect=snapshot), \
             patch.object(self.backend, "_activation_shortcut", return_value="Super+4"):
            with self.assertRaises(DesktopError):
                self.backend.type_text("hello", "chosen")

        self.assertEqual(self.backend._portal.typed, [])

    def test_original_control_name_is_revalidated_after_snapshot_refresh(self):
        self.assert_original_control_change_is_rejected("name", "Changed field")

    def test_original_control_role_is_revalidated_after_snapshot_refresh(self):
        self.assert_original_control_change_is_rejected("role", "entry")

    def test_original_process_restart_after_activation_never_types(self):
        snapshot = self.background_until_activation(
            before_foreground=lambda: setattr(self.target.owner, "get_process_id", lambda: 301))
        with patch.object(self.backend, "snapshot", side_effect=snapshot), \
             patch.object(self.backend, "_activation_shortcut", return_value="Super+4"):
            with self.assertRaises(DesktopError):
                self.backend.type_text("hello", "chosen")

        self.assertEqual(self.backend._portal.keys, ["Super+4"])
        self.assertEqual(self.backend._portal.typed, [])

    def test_already_intended_foreground_does_not_use_app_shortcut(self):
        with patch.object(self.backend, "snapshot", side_effect=lambda **kwargs: self.publish(self.target, self.target_window)), \
             patch.object(self.backend, "_activation_shortcut", return_value="Super+4") as shortcut:
            self.backend.type_text("hello", "chosen")

        shortcut.assert_not_called()
        self.assertEqual(self.backend._portal.keys, [])
        self.assertEqual(self.backend._portal.typed, ["hello"])

    def test_changed_global_control_after_focus_handoff_never_types(self):
        replacement = FocusText(path="/accessible/other-field")

        def snapshot(**kwargs):
            control = self.target if self.generation == 0 else replacement
            return self.publish(control, self.target_window)

        with patch.object(self.backend, "snapshot", side_effect=snapshot), \
             patch.object(self.backend, "_activation_shortcut", return_value="Super+4"):
            with self.assertRaises(DesktopError):
                self.backend.type_text("hello", "chosen")

        self.assertEqual(self.backend._portal.typed, [])

    def test_changed_global_window_of_same_accessible_object_never_types(self):
        moved_window = window(path="/accessible/replacement-window")

        def snapshot(**kwargs):
            owner_window = self.target_window if self.generation == 0 else moved_window
            return self.publish(self.target, owner_window)

        with patch.object(self.backend, "snapshot", side_effect=snapshot), \
             patch.object(self.backend, "_activation_shortcut", return_value="Super+4"):
            with self.assertRaises(DesktopError):
                self.backend.type_text("hello", "chosen")

        self.assertEqual(self.backend._portal.typed, [])

    def test_focus_lost_while_revalidating_final_global_target_never_types(self):
        clear_cache = self.target.clear_cache

        def lose_focus_after_final_snapshot():
            clear_cache()
            if self.generation > 1:
                self.target.focused = False

        with patch.object(self.target, "clear_cache", side_effect=lose_focus_after_final_snapshot), \
             patch.object(self.backend, "snapshot", side_effect=lambda **kwargs: self.publish(self.target, self.target_window)), \
             patch.object(self.backend, "_activation_shortcut", return_value="Super+4"):
            with self.assertRaises(DesktopError):
                self.backend.type_text("hello", "chosen")

        self.assertEqual(self.backend._portal.typed, [])

    def test_window_activation_lost_during_final_field_revalidation_never_types(self):
        clear_cache = self.target.clear_cache

        def deactivate_window_after_final_snapshot():
            clear_cache()
            if self.generation > 1:
                self.target_window.pending_active = False

        with patch.object(self.target, "clear_cache", side_effect=deactivate_window_after_final_snapshot), \
             patch.object(self.backend, "snapshot", side_effect=lambda **kwargs: self.publish(self.target, self.target_window)), \
             patch.object(self.backend, "_activation_shortcut", return_value="Super+4"):
            with self.assertRaises(DesktopError):
                self.backend.type_text("hello", "chosen")

        # Chromium's renderer can preserve field FOCUSED after its native
        # window yields desktop activation to another application.
        self.assertTrue(self.target.focused)
        self.assertEqual(self.backend._portal.typed, [])

    def test_final_global_focus_flags_block_typing_after_handoff(self):
        for flag in ("self_focused", "password_focused", "focus_incomplete", "focus_ambiguous"):
            with self.subTest(flag=flag):
                self.generation = 0
                self.backend._refs = {"chosen": element(self.target, self.target_window)}
                self.backend._portal = Portal()

                def snapshot(**kwargs):
                    result = self.publish(self.target, self.target_window)
                    if self.generation > 1:
                        result[flag] = True
                    return result

                with patch.object(self.backend, "snapshot", side_effect=snapshot), \
                     patch.object(self.backend, "_activation_shortcut", return_value="Super+4"):
                    with self.assertRaises(DesktopError):
                        self.backend.type_text("hello", "chosen")

                self.assertEqual(self.backend._portal.typed, [])

    def test_same_app_wrong_foreground_window_never_invokes_dock_shortcut(self):
        wrong = FocusText(path="/accessible/popup-field")
        wrong_window = window(path="/accessible/popup-window")
        with patch.object(self.backend, "snapshot", side_effect=lambda **kwargs: self.publish(wrong, wrong_window)), \
             patch.object(self.backend, "_activation_shortcut", return_value="Super+4") as shortcut:
            with self.assertRaises(DesktopError):
                self.backend.type_text("hello", "chosen")

        shortcut.assert_not_called()
        self.assertEqual(self.backend._portal.keys, [])
        self.assertEqual(self.backend._portal.typed, [])


if __name__ == "__main__":
    unittest.main()
