import copy
import time
import unittest

from jev_linux.config import DEFAULTS
from jev_linux.controller import Controller, build_request, requested_browser
from jev_linux.planning import PlanStep, TaskPlan


APPS = [
    {"id": "chrome.desktop", "name": "Google Chrome", "aliases": ["chrome"], "browser": True},
    {"id": "firefox.desktop", "name": "Firefox", "aliases": ["firefox", "www", "web"], "browser": True},
]


class Desktop:
    def __init__(self):
        self.calls = []

    def apps(self):
        return APPS

    def snapshot(self):
        return {"elements": [], "windows": [], "focused_app": ""}

    def open_url(self, url, browser=None):
        self.calls.append((url, browser))
        return "Opened website in requested browser"

    def close(self):
        pass


class BusyDesktop(Desktop):
    def apps(self):
        return [*APPS, {"id": "editor.desktop", "name": "Text Editor", "browser": False}]

    def snapshot(self):
        return {"focused_app": "Text Editor", "windows": [
            {"app": "Google Chrome", "title": "YouTube", "active": False},
            {"app": "Text Editor", "title": "Document", "active": True}], "elements": [
            {"id": "editor_entry", "app": "Text Editor", "name": "Document",
             "role": "text", "editable": True, "focused": True},
            {"id": "youtube_search", "app": "Google Chrome", "name": "Search",
             "role": "entry", "editable": True, "document_id": "youtube-document"},
        ]}


class Planner:
    def plan(self, command, context=None):
        return TaskPlan(command, (PlanStep("s1", command, "The requested website opens", allowed_actions=("open_url",)),))

    def close(self):
        pass


class Jev:
    def __init__(self, action=None):
        self.action = action
        self.states = []
        self.questions = []

    def decide(self, state, questions):
        self.states.append(copy.deepcopy(state))
        self.questions.append(copy.deepcopy(questions))
        performed = bool(state["completed_steps"])
        answers = {}
        for name, question in questions.items():
            if question["type"] == "noul":
                answers[name] = {"noul": 0 if name == "sensitive" or (name == "step_complete" and not performed) else 1}
            else:
                selected = {"action": "done" if performed else self.action or "open_url",
                            "url": "u0", "browser": "default"}.get(name, "none")
                answers[name] = {"choice": selected, "confidence": .01 if name == "browser" else 1}
        return {"answers": answers}

    def close(self):
        pass


class BrowserControllerTests(unittest.TestCase):
    def run_goal(self, goal, backend=None, client=None, status="completed"):
        backend, client = backend or Desktop(), client or Jev()
        events = []
        controller = Controller(backend, {**DEFAULTS, "memory_enabled": False}, client=client, planner=Planner())
        controller.set_event_handler(events.append)
        try:
            controller.submit(goal)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if controller._active and controller._active.status == status:
                    break
                time.sleep(.01)
            self.assertEqual(controller._active.status, status, events)
            return backend.calls, client.questions
        finally:
            controller.close()
            controller._worker.join(timeout=2)

    def test_named_chrome_is_preserved_despite_uncertain_browser_choice(self):
        calls, questions = self.run_goal("Open YouTube in Google Chrome")
        self.assertEqual(calls, [("https://youtube.com", "chrome.desktop")])
        self.assertEqual(set(questions[0]["action"]["criteria"]), {"open_url", "unsupported"})
        self.assertEqual(set(questions[1]["action"]["criteria"]), {"open_url", "done", "unsupported"})

    def test_direct_open_ignores_visible_page_controls_and_unrelated_apps(self):
        backend, client = BusyDesktop(), Jev()
        calls, questions = self.run_goal("Open YouTube in Google Chrome", backend, client)
        self.assertEqual(calls, [("https://youtube.com", "chrome.desktop")])
        self.assertEqual(len(client.states), 2)
        self.assertEqual(client.states[0]["completed_steps"], [])
        self.assertEqual([effect["action"] for effect in client.states[1]["completed_steps"]], ["open_url"])
        self.assertNotIn("done", questions[0]["action"]["criteria"])
        self.assertIn("done", questions[1]["action"]["criteria"])
        for state, question_set in zip(client.states, questions):
            self.assertEqual(set(question_set),
                             {"action", "browser", "url", "is_command", "ready", "sensitive", "step_complete"})
            self.assertNotIn("wait", question_set["action"]["criteria"])
            self.assertEqual(state["desktop"]["elements"], [])
            self.assertEqual(state["desktop"]["windows"], [])
            self.assertEqual(set(state["installed_apps"]), {"chrome.desktop", "firefox.desktop"})
            self.assertEqual(state["requested_browser"], "chrome.desktop")

    def test_unsupported_direct_open_does_not_launch_any_browser(self):
        backend, client = BusyDesktop(), Jev(action="unsupported")
        calls, questions = self.run_goal("Open YouTube in Google Chrome", backend, client, status="blocked")
        self.assertEqual(calls, [])
        self.assertEqual(len(questions), 1)
        self.assertEqual(set(questions[0]["action"]["criteria"]), {"open_url", "unsupported"})

    def test_missing_or_ambiguous_browser_stays_blocked_without_default_launch(self):
        for command, requested in (("Open YouTube in Brave", "missing"),
                                   ("Open YouTube in Chrome and in Firefox", "ambiguous")):
            with self.subTest(command=command):
                backend, client = BusyDesktop(), Jev(action="unsupported")
                calls, _ = self.run_goal(command, backend, client, status="blocked")
                self.assertEqual(calls, [])
                self.assertEqual(client.states[0]["requested_browser"], requested)

    def test_unnamed_browser_keeps_default_even_with_www_alias(self):
        calls, _ = self.run_goal("Open https://www.example.com/")
        self.assertEqual(calls, [("https://www.example.com/", None)])

    def test_browser_names_inside_urls_and_topics_do_not_select_browser(self):
        browsers = {app["id"]: app for app in APPS}
        for command in ("Open https://firefox.com/", "Open firefox.com", "Search the web for Firefox tutorials"):
            self.assertIsNone(requested_browser(command, browsers))
        self.assertEqual(requested_browser("Open https://firefox.com/ in Chrome", browsers), "chrome.desktop")
        self.assertEqual(requested_browser("Open YouTube in Chrome.", browsers), "chrome.desktop")

    def test_missing_or_ambiguous_browser_never_becomes_default(self):
        browsers = {app["id"]: app for app in APPS}
        self.assertEqual(requested_browser("Open YouTube in Brave", browsers), "missing")
        self.assertEqual(requested_browser("Open YouTube in Chrome and in Firefox", browsers), "ambiguous")
        backend, client = Desktop(), Jev()
        controller = Controller(backend, {**DEFAULTS, "memory_enabled": False}, client=client)
        try:
            for command in ("Open YouTube in Brave", "Open YouTube in Chrome and in Firefox"):
                state, _, candidates = build_request(command, True, backend.snapshot(), APPS, [])
                # Even an open_url answer cannot replace an unavailable named browser with the default.
                self.assertIsNone(controller._arguments("open_url", {"url": {"choice": "u0", "confidence": 1}}, candidates, state["desktop"]))
            self.assertEqual(backend.calls, [])
        finally:
            controller.close()
            controller._worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
