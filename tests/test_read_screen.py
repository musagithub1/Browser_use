import copy
from itertools import count
import time
import unittest

from jev_linux.config import DEFAULTS
from jev_linux.controller import Controller


class ScreenBackend:
    def __init__(self, snapshot):
        self.state = snapshot

    def apps(self):
        return []

    def snapshot(self):
        return copy.deepcopy(self.state)

    def close(self):
        pass


class ReadScreenClient:
    def decide(self, state, questions):
        operation = "done" if state["completed_steps"] else "read_screen"
        answers = {}
        for name, question in questions.items():
            if question["type"] == "noul":
                answers[name] = {"noul": 0 if name == "sensitive" else 1}
            else:
                value = operation if name == "action" else "none"
                answers[name] = {"choice": value, "confidence": 1}
        return {"answers": answers, "latency_ms": 1}

    def close(self):
        pass


class NoNetworkReader:
    def read(self, *args, **kwargs):
        raise AssertionError("Reading the screen must not make a web request.")

    def close(self):
        pass


_element_ids = count(1)


def active_element(value, *, app="Browser", name="Page text", active_window=True):
    return {"id": f"e{next(_element_ids)}", "app": app, "role": "text", "name": name, "value": value,
            "active_window": active_window, "focused": True}


class ReadScreenTests(unittest.TestCase):
    def setUp(self):
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:
            controller.close()
            controller._worker.join(timeout=2)
            self.assertFalse(controller._worker.is_alive(), "Screen-read worker did not stop.")

    def run_read(self, snapshot):
        events = []
        controller = Controller(ScreenBackend(snapshot),
                                {**DEFAULTS, "memory_enabled": False, "planning_enabled": False},
                                client=ReadScreenClient(), web_reader=NoNetworkReader(), debounce=0)
        self.controllers.append(controller)
        controller.set_event_handler(events.append)
        controller.submit("Read the text in the active page")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if any(event["type"] == "error" or event["message"] == "Done." for event in events):
                return controller, events
            time.sleep(0.01)
        self.fail("Screen-read worker did not report a result.")

    def test_reads_active_values_and_names_without_background_window_leakage(self):
        snapshot = {"focused_app": "Browser", "elements": [
            active_element("The foreground article."),
            active_element("", name="Article heading"),
            active_element("Private mailbox in another Browser window", active_window=False),
            active_element("Private text in another app", app="Notes"),
        ]}
        controller, events = self.run_read(snapshot)
        self.assertFalse([event for event in events if event["type"] == "error"])
        self.assertEqual(controller._active.task_data,
                         [{"source": "screen", "app": "Browser",
                           "content": "The foreground article.\nArticle heading"}])
        self.assertEqual(controller._active.completed[0]["action"], "read_screen")

    def test_rejects_incomplete_ambiguous_password_and_jev_focus(self):
        for flag in ("focus_incomplete", "focus_ambiguous", "password_focused", "self_focused"):
            with self.subTest(flag=flag):
                snapshot = {"focused_app": "Browser", "elements": [active_element("Should not be read")], flag: True}
                _, events = self.run_read(snapshot)
                self.assertTrue(any(event["type"] == "error" for event in events))
                self.assertFalse(any(event["type"] == "action" for event in events))

    def test_background_only_snapshot_does_not_count_as_readable_screen_content(self):
        snapshot = {"focused_app": "Browser", "elements": [
            active_element("A background page", active_window=False),
            active_element("Different app", app="Notes"),
        ]}
        _, events = self.run_read(snapshot)
        self.assertTrue(any(event["type"] == "error" for event in events))
        self.assertFalse(any(event["type"] == "action" for event in events))

    def test_element_without_active_window_evidence_is_not_assumed_foreground(self):
        element = active_element("Unverified window text")
        element.pop("active_window")
        _, events = self.run_read({"focused_app": "Browser", "elements": [element]})
        self.assertTrue(any(event["type"] == "error" for event in events))
        self.assertFalse(any(event["type"] == "action" for event in events))

    def test_active_screen_text_is_deduplicated_and_bounded(self):
        snapshot = {"focused_app": "Browser", "elements": [
            active_element("Same visible paragraph"), active_element("Same visible paragraph"),
            active_element("x" * 20000),
        ]}
        controller, events = self.run_read(snapshot)
        self.assertFalse([event for event in events if event["type"] == "error"])
        content = controller._active.task_data[0]["content"]
        self.assertEqual(content.count("Same visible paragraph"), 1)
        self.assertEqual(len(content), 16000)


if __name__ == "__main__":
    unittest.main()
