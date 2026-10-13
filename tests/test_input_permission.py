"""A task pauses for real desktop consent without retaining an input decision."""

import copy
import time
import unittest

from jev_linux.config import DEFAULTS
from jev_linux.controller import Controller
from jev_linux.planning import PlanStep, TaskPlan


class _PermissionDesktop:
    def __init__(self):
        self.calls = []
        self.permission_checks = []
        self.generation = 0
        self.value = ""
        self.keyboard = False
        self.native_editable = False

    def apps(self):
        return [{"id": "chrome.desktop", "name": "Google Chrome", "browser": True}]

    def snapshot(self):
        self.generation += 1
        return {"focused_app": "Google Chrome", "windows": [
            {"app": "Google Chrome", "title": "Video site", "active": True}], "elements": [
            {"id": f"e{self.generation}_1", "app": "Google Chrome", "name": "Search",
             "role": "entry", "value": self.value, "editable": True, "focused": True,
             "active_window": True}]}

    def needs_desktop_permission(self, operation, arguments):
        self.permission_checks.append((operation, copy.deepcopy(arguments)))
        if operation in {"type_text", "write_text"} and not self.native_editable and not self.keyboard:
            return "keyboard"
        return None

    def status(self):
        return {"portal": {"granted": self.keyboard, "keyboard": self.keyboard, "pointer": False}}

    def type_text(self, text, target):
        if not self.keyboard and not self.native_editable:
            raise AssertionError("Input was attempted before keyboard permission")
        if target != f"e{self.generation}_1":
            raise AssertionError("The decision made before the permission pause was reused")
        self.value += text
        self.calls.append(("type_text", text, target))
        return "Inserted text into Search"

    def open_url(self, url, browser=None):
        self.calls.append(("open_url", url, browser))
        return "Opened website in Google Chrome"

    def close(self):
        pass


class _PermissionPlanner:
    def plan(self, command, context=None):
        if " and type " in command.casefold():
            opening, typing = command.split(" and ", 1)
            return TaskPlan(command, (
                PlanStep("s1", opening, "The requested website is open", allowed_actions=("open_url",)),
                PlanStep("s2", typing, "The exact dictated text is in Search", allowed_actions=("type_text",)),
            ), command=command)
        action = "open_url" if command.startswith("Open ") else "type_text"
        return TaskPlan(command, (PlanStep("s1", command, "The requested action succeeded",
                                         allowed_actions=(action,)),), command=command)

    def close(self):
        pass


class _PermissionJev:
    def __init__(self):
        self.states = []

    def decide(self, state, questions):
        self.states.append(copy.deepcopy(state))
        performed = bool(state["completed_steps"])
        action = "open_url" if state["user_command"].startswith("Open ") else "type_text"
        picks = {"action": "done" if performed else action, "app": "none", "browser": "default",
                 "target": next(iter(questions.get("target", {}).get("criteria", {})), "none"),
                 "text": next(iter(state["text_candidates"]), "none"), "text_mode": "dictation",
                 "key": "none", "direction": "none", "url": next(iter(state["url_candidates"]), "none")}
        answers = {}
        for name, question in questions.items():
            if question["type"] == "noul":
                answers[name] = {"noul": 0 if name == "sensitive" or (name == "step_complete" and not performed) else 1}
            else:
                answers[name] = {"choice": picks.get(name, "none"), "confidence": 1}
        return {"answers": answers}

    def close(self):
        pass


class InputPermissionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.backend, self.client = _PermissionDesktop(), _PermissionJev()
        self.controller = Controller(self.backend, {**DEFAULTS, "memory_enabled": False},
                                     client=self.client, planner=_PermissionPlanner(), debounce=0)
        self.events = []
        self.controller.set_event_handler(self.events.append)

    def tearDown(self):
        self.controller.close()
        self.controller._worker.join(timeout=3)
        self.assertFalse(self.controller._worker.is_alive())

    def wait_for(self, predicate, message):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail(message)

    def submit_and_wait(self, command, status):
        self.controller.submit(command)
        self.wait_for(lambda: self.controller._active and self.controller._active.text == command
                      and self.controller._active.status == status,
                      f"Task did not reach {status}")
        return self.controller._active

    def wait_for_permission_event(self):
        self.wait_for(lambda: any(event["type"] == "desktop_permission" for event in self.events),
                      "No desktop permission request was emitted")
        return next(event for event in self.events if event["type"] == "desktop_permission")

    def test_keyboard_pause_retains_no_effect_and_redecides_after_actual_grant(self):
        task = self.submit_and_wait('Type "Hello" in the search bar', "awaiting_permission")
        event = self.wait_for_permission_event()
        paused_target = self.backend.permission_checks[-1][1]["target"]
        paused_generation = self.backend.generation
        self.assertEqual(event["device"], "keyboard")
        self.assertEqual(task.permission_device, "keyboard")
        self.assertEqual(task.action_count, 0)
        self.assertEqual(task.completed, [])
        self.assertEqual(task.fingerprints, set())
        self.assertEqual(self.backend.calls, [])
        self.assertFalse(self.controller.resume_task())
        self.assertFalse(self.controller.resume_after_permission())
        self.assertEqual(task.status, "awaiting_permission")

        self.backend.keyboard = True
        self.assertTrue(self.controller.resume_after_permission())
        self.wait_for(lambda: task.status == "completed", "The permitted input did not complete")
        self.assertEqual(self.backend.value, "Hello")
        self.assertEqual(len(self.backend.calls), 1)
        self.assertNotEqual(self.backend.calls[0][2], paused_target)
        self.assertGreater(self.backend.generation, paused_generation)
        self.assertEqual(task.action_count, 1)
        self.assertEqual(len(task.completed), 1)
        self.assertEqual(len(task.fingerprints), 1)
        self.assertFalse(self.controller.resume_after_permission())

    def test_cancelled_permission_task_never_resumes_when_permission_arrives(self):
        task = self.submit_and_wait('Type "Hello" in the search bar', "awaiting_permission")
        self.wait_for_permission_event()
        self.controller.cancel()
        self.backend.keyboard = True
        self.assertFalse(self.controller.resume_after_permission())
        self.assertIsNone(self.controller._active)
        self.assertEqual(task.status, "cancelled")
        self.assertEqual(task.completed, [])
        self.assertEqual(self.backend.value, "")
        self.assertEqual(self.backend.calls, [])

    def test_replacing_permission_task_does_not_resume_its_old_input(self):
        old = self.submit_and_wait('Type "Hello" in the search bar', "awaiting_permission")
        self.wait_for_permission_event()
        replacement = self.submit_and_wait("Open YouTube in Google Chrome", "completed")
        self.backend.keyboard = True
        self.assertFalse(self.controller.resume_after_permission())
        self.assertEqual(old.status, "interrupted")
        self.assertEqual(old.completed, [])
        self.assertEqual(replacement.status, "completed")
        self.assertEqual(self.backend.value, "")
        self.assertEqual([call[0] for call in self.backend.calls], ["open_url"])

    def test_old_permission_callback_cannot_resume_a_replacement_waiting_task(self):
        old = self.submit_and_wait('Type "Hello" in the search bar', "awaiting_permission")
        event = self.wait_for_permission_event()
        self.assertEqual(event["task_id"], old.id)
        replacement = self.submit_and_wait('Type "Goodbye" in the search bar', "awaiting_permission")
        self.wait_for(lambda: any(item["type"] == "desktop_permission" and item.get("task_id") == replacement.id
                                 for item in self.events), "No permission event for the replacement task")
        self.backend.keyboard = True
        self.assertFalse(self.controller.resume_after_permission(expected_task_id=old.id))
        self.assertEqual(replacement.status, "awaiting_permission")
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(old.status, "interrupted")
        self.assertTrue(self.controller.resume_after_permission(expected_task_id=replacement.id))
        self.wait_for(lambda: replacement.status == "completed", "The replacement input did not complete")
        self.assertEqual(self.backend.value, "Goodbye")
        self.assertEqual(len(self.backend.calls), 1)

    def test_native_editable_field_runs_without_a_portal_grant(self):
        self.backend.native_editable = True
        task = self.submit_and_wait('Type "Hello" in the search bar', "completed")
        self.assertFalse(self.backend.keyboard)
        self.assertEqual(self.backend.value, "Hello")
        self.assertEqual(task.action_count, 1)
        self.assertFalse(any(event["type"] == "desktop_permission" for event in self.events))

    def test_browser_open_is_preserved_when_later_typing_waits_for_permission(self):
        command = 'Open YouTube in Google Chrome and Type "Hello" in the search bar'
        task = self.submit_and_wait(command, "awaiting_permission")
        self.wait_for_permission_event()
        self.assertEqual(task.step_index, 1)
        self.assertEqual(task.action_count, 1)
        self.assertEqual(len(task.completed), 1)
        self.assertEqual([call[0] for call in self.backend.calls], ["open_url"])
        self.backend.keyboard = True
        self.assertTrue(self.controller.resume_after_permission(expected_task_id=task.id))
        self.wait_for(lambda: task.status == "completed", "The remaining search-field input did not complete")
        self.assertEqual(task.action_count, 2)
        self.assertEqual(task.step_index, 2)
        self.assertEqual([call[0] for call in self.backend.calls], ["open_url", "type_text"])
        self.assertEqual(self.backend.value, "Hello")


if __name__ == "__main__":
    unittest.main()
