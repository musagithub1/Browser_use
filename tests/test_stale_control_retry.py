"""Retry only read-only target validation; never replay a started input effect."""

import copy
import time
import unittest

from jev_linux.config import DEFAULTS
from jev_linux.controller import Controller
from jev_linux.desktop import DesktopError, StaleControlError
from tests.test_input_permission import _PermissionDesktop, _PermissionJev, _PermissionPlanner


class _ChangingDesktop(_PermissionDesktop):
    def __init__(self, stale_checks=0, preflight_error=None, input_error=None):
        super().__init__()
        self.keyboard = True
        self.stale_checks = stale_checks
        self.preflight_error = preflight_error
        self.input_error = input_error

    def needs_desktop_permission(self, operation, arguments):
        if self.stale_checks or self.preflight_error:
            self.permission_checks.append((operation, copy.deepcopy(arguments)))
            if self.stale_checks:
                self.stale_checks -= 1
                raise StaleControlError("This control changed after the snapshot.")
            raise self.preflight_error
        return super().needs_desktop_permission(operation, arguments)

    def type_text(self, text, target):
        if self.input_error:
            # A portal operation can fail after a partial effect. Its error
            # must never be treated like a harmless preflight rejection.
            self.calls.append(("type_text", text, target))
            self.value += text[:1]
            raise self.input_error
        return super().type_text(text, target)


class StaleControlRetryTests(unittest.TestCase):
    def setUp(self):
        self.controller = None
        self.events = []

    def tearDown(self):
        if self.controller:
            self.controller.close()
            self.controller._worker.join(timeout=3)
            self.assertFalse(self.controller._worker.is_alive())

    def start(self, backend):
        self.backend = backend
        self.client = _PermissionJev()
        self.controller = Controller(backend, {**DEFAULTS, "memory_enabled": False},
                                     client=self.client, planner=_PermissionPlanner(), debounce=0)
        self.controller.set_event_handler(self.events.append)
        return self.controller

    def wait_for(self, predicate):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail("The task worker did not reach the expected state")

    def submit(self, command='Type "Hello" in the search bar'):
        self.controller.submit(command)
        self.wait_for(lambda: self.controller._active is not None)
        return self.controller._active

    def test_two_preflight_rejections_redecide_fresh_ids_and_type_exactly_once(self):
        self.start(_ChangingDesktop(stale_checks=2))
        task = self.submit()
        self.wait_for(lambda: task.status == "completed")
        self.assertEqual(self.backend.value, "Hello")
        self.assertEqual(len(self.backend.calls), 1)
        targets = [arguments["target"] for _, arguments in self.backend.permission_checks]
        self.assertEqual(len(targets), 3)
        self.assertEqual(len(set(targets)), 3)
        self.assertEqual(task.action_count, 1)
        self.assertEqual(len(task.completed), 1)
        self.assertEqual(len(task.fingerprints), 1)
        self.assertEqual(task.replan_count, 0)
        self.assertFalse(any(event["type"] == "error" for event in self.events))
        self.assertTrue(all(not state["completed_steps"] for state in self.client.states[:3]))

    def test_third_preflight_rejection_blocks_with_no_effect_or_replan(self):
        self.start(_ChangingDesktop(stale_checks=99))
        task = self.submit()
        self.wait_for(lambda: task.status == "blocked")
        self.assertEqual(len(self.backend.permission_checks), 3)
        self.assertEqual(len(self.client.states), 3)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(task.completed, [])
        self.assertEqual(task.fingerprints, set())
        self.assertEqual(task.action_count, 0)
        self.assertEqual(task.replan_count, 0)
        self.assertFalse(any(event["type"] == "desktop_permission" for event in self.events))

    def test_retry_reaches_permission_pause_without_reusing_its_target_after_grant(self):
        desktop = _ChangingDesktop(stale_checks=1)
        desktop.keyboard = False
        self.start(desktop)
        task = self.submit()
        self.wait_for(lambda: task.status == "awaiting_permission")
        before_grant = [arguments["target"] for _, arguments in desktop.permission_checks]
        self.assertEqual(len(set(before_grant)), 2)
        self.assertEqual(task.action_count, 0)
        self.assertEqual(task.fingerprints, set())
        self.assertEqual(desktop.calls, [])
        desktop.keyboard = True
        self.assertTrue(self.controller.resume_after_permission(expected_task_id=task.id))
        self.wait_for(lambda: task.status == "completed")
        self.assertNotIn(desktop.calls[0][2], before_grant)
        self.assertEqual(desktop.value, "Hello")
        self.assertEqual(len(desktop.calls), 1)

    def test_cancellation_during_retry_never_takes_another_decision_or_types(self):
        self.start(_ChangingDesktop(stale_checks=1))
        cancelled = []

        def cancel_after_rejection(event):
            self.events.append(event)
            if "Refreshing its controls before input" in event["message"]:
                cancelled.append(self.controller._active)
                self.controller.cancel()

        self.controller.set_event_handler(cancel_after_rejection)
        self.controller.submit('Type "Hello" in the search bar')
        self.wait_for(lambda: bool(cancelled))
        time.sleep(.4)
        self.assertEqual(cancelled[0].status, "cancelled")
        self.assertEqual(len(self.backend.permission_checks), 1)
        self.assertEqual(len(self.client.states), 1)
        self.assertEqual(self.backend.calls, [])

    def test_real_input_stale_error_after_partial_effect_is_never_retried(self):
        self.start(_ChangingDesktop(input_error=StaleControlError("Input target changed after focus")))
        task = self.submit()
        self.wait_for(lambda: task.status == "failed")
        self.assertEqual(self.backend.value, "H")
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(len(self.backend.permission_checks), 1)
        self.assertEqual(len(self.client.states), 1)
        self.assertEqual(task.replan_count, 0)
        self.assertFalse(any("Refreshing its controls" in event["message"] for event in self.events))

    def test_other_preflight_safety_rejection_is_never_retried(self):
        self.start(_ChangingDesktop(preflight_error=DesktopError("Password fields cannot be controlled.")))
        task = self.submit()
        self.wait_for(lambda: task.status == "failed")
        self.assertEqual(len(self.backend.permission_checks), 1)
        self.assertEqual(len(self.client.states), 1)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(task.action_count, 0)
        self.assertEqual(task.replan_count, 0)


if __name__ == "__main__":
    unittest.main()
