"""Regression coverage for field dictation after an independent app command."""

import copy
from dataclasses import replace
import threading
import time
import unittest

from jev_linux.config import DEFAULTS
from jev_linux.controller import Controller
from jev_linux.planning import PlanStep, TaskPlan
from jev_linux.spans import text_candidates


class SearchFieldDictationTests(unittest.TestCase):
    def test_speech_without_quote_punctuation_excludes_search_bar_destination(self):
        # Local speech recognition typically does not add spoken quote marks.
        # A field destination belongs to the action, not to the inserted text.
        for instruction in (
            "Type Hello in the search bar",
            "Enter Hello into the search bar",
            "Type Hello in the search bar.",
            "Type Hello in the YouTube search bar",
            "Type Hello in YouTube's search bar",
            "Type Hello into Google Chrome's search bar",
            "Type Hello into Acme Mail’s search field",
        ):
            with self.subTest(instruction=instruction):
                self.assertEqual(list(text_candidates(instruction).values()), ["Hello"])

    def test_quoted_content_is_preserved_even_when_it_mentions_a_search_bar(self):
        self.assertEqual(
            list(text_candidates('Type "Hello in the search bar" in the search bar').values()),
            ["Hello in the search bar"],
        )

    def test_an_ordinary_payload_prepositional_phrase_is_preserved(self):
        self.assertEqual(list(text_candidates("Type Hello in the morning").values()),
                         ["Hello in the morning"])
        self.assertEqual(
            list(text_candidates("Type Hello in the morning into the search bar").values()),
            ["Hello in the morning"],
        )


class _BrowserDesktop:
    def __init__(self):
        self.calls = []
        self.generation = 0
        self.value = ""

    def apps(self):
        return [{"id": "chrome.desktop", "name": "Google Chrome", "aliases": ["Chrome"], "browser": True}]

    def snapshot(self):
        self.generation += 1
        return {"focused_app": "Google Chrome", "windows": [
            {"app": "Google Chrome", "title": "Video site", "active": True}], "elements": [
            {"id": f"e{self.generation}_1", "app": "Google Chrome", "name": "Search",
             "role": "entry", "value": self.value, "editable": True, "focused": True,
             "active_window": True}]}

    def open_url(self, url, browser=None):
        self.calls.append(("open_url", url, browser))
        return "Opened website in Google Chrome"

    def type_text(self, text, target):
        if target != f"e{self.generation}_1":
            raise AssertionError("A previous accessibility control ID was reused")
        self.value += text
        self.calls.append(("type_text", text, target))
        return "Inserted text into Search"

    def close(self):
        pass


class _FollowupPlanner:
    def __init__(self):
        self.calls = []

    def plan(self, command, context=None):
        self.calls.append({"command": command, "context": copy.deepcopy(context)})
        action = "open_url" if command.startswith("Open ") else "type_text"
        return TaskPlan(command, (PlanStep("s1", command, "The requested action succeeded",
                                         allowed_actions=(action,)),), command=command)

    def close(self):
        pass


class _FollowupJev:
    def __init__(self):
        self.calls = []
        self.block_typing = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def decide(self, state, questions):
        self.calls.append(copy.deepcopy(state))
        performed = bool(state["completed_steps"])
        action = "open_url" if state["user_command"].startswith("Open ") else "type_text"
        if self.block_typing and action == "type_text" and not performed:
            self.entered.set()
            self.release.wait(3)
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
        self.release.set()


class IndependentSearchFollowupTests(unittest.TestCase):
    def setUp(self):
        self.backend, self.planner, self.client = _BrowserDesktop(), _FollowupPlanner(), _FollowupJev()
        self.controller = Controller(self.backend, {**DEFAULTS, "memory_enabled": False},
                                     client=self.client, planner=self.planner, debounce=0)

    def tearDown(self):
        self.client.release.set()
        self.controller.close()
        self.controller._worker.join(timeout=3)
        self.assertFalse(self.controller._worker.is_alive())

    def complete(self, command):
        self.controller.submit(command)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            active = self.controller._active
            if active and active.text == command and active.status == "completed":
                return
            time.sleep(.01)
        self.fail("The follow-up task did not complete")

    def test_completed_browser_command_keeps_session_target_with_memory_off(self):
        self.complete("Open YouTube in Google Chrome")
        self.complete('Type "Hello" in the search bar')
        context = self.planner.calls[1]["context"]["interaction_context"]
        self.assertEqual(context["browser"], "chrome.desktop")
        self.assertEqual(context["url"], "https://youtube.com")
        self.assertNotIn("target", context)
        self.assertNotIn("control", context)
        self.assertEqual(self.client.calls[-1]["interaction_context"]["browser"], "chrome.desktop")
        self.assertEqual(self.backend.value, "Hello")
        self.assertEqual(self.backend.calls[0], ("open_url", "https://youtube.com", "chrome.desktop"))
        self.assertIsNone(self.controller.memory)

    def test_cancelled_followup_never_types_or_reuses_its_control(self):
        self.complete("Open YouTube in Google Chrome")
        self.client.block_typing = True
        self.controller.submit('Type "Hello" in the search bar')
        self.assertTrue(self.client.entered.wait(3))
        self.controller.cancel()
        self.client.release.set()
        self.client.block_typing = False
        self.complete('Type "Goodbye" in the search bar')
        self.assertEqual(self.backend.value, "Goodbye")
        self.assertEqual(len([call for call in self.backend.calls if call[0] == "type_text"]), 1)
        context = self.client.calls[-1].get("interaction_context", {})
        self.assertNotIn("target", context)
        self.assertNotIn("control", context)


class _ScopedDesktop(_BrowserDesktop):
    def __init__(self):
        super().__init__()
        self.foreground = "Jev Linux"
        self.self_focused = True
        self.contexts = []
        self.values = {}
        self.selected_app = ""

    def apps(self):
        return [
            {"id": "chrome.desktop", "name": "Google Chrome", "aliases": ["Chrome"], "browser": True},
            {"id": "firefox.desktop", "name": "Firefox", "browser": True},
            {"id": "editor.desktop", "name": "Text Editor", "aliases": ["Notes"]},
            {"id": "code.desktop", "name": "Code"},
        ]

    def _snapshot(self, app):
        self.generation += 1
        self.selected_app = app
        return {"focused_app": self.foreground, "self_focused": self.self_focused,
                "windows": [{"app": app, "title": app, "active": app == self.foreground}],
                "elements": [{"id": f"e{self.generation}_1", "app": app, "name": "Search",
                              "role": "entry", "value": self.values.get(app, ""),
                              "editable": True, "focused": app == self.foreground,
                              "active_window": app == self.foreground}]}

    def snapshot(self):
        return self._snapshot(self.foreground)

    def snapshot_for_context(self, context):
        self.contexts.append(copy.deepcopy(context))
        installed = next((app for app in self.apps() if app["id"] == context.get("app_id")), {})
        return self._snapshot(installed.get("name") or context.get("app") or self.foreground)

    def type_text(self, text, target):
        if target != f"e{self.generation}_1":
            raise AssertionError("A previous accessibility control ID was reused")
        self.values[self.selected_app] = self.values.get(self.selected_app, "") + text
        self.calls.append(("type_text", text, target, self.selected_app))
        return f"Inserted text into {self.selected_app} Search"


class _ScopedPlanner(_FollowupPlanner):
    def plan(self, command, context=None):
        if "\n" in command:
            self.calls.append({"command": command, "context": copy.deepcopy(context)})
            opening, typing = command.split("\n", 1)
            return TaskPlan(command, (
                PlanStep("s1", opening, "The requested website is open", allowed_actions=("open_url",)),
                PlanStep("s2", typing, "The exact dictated text is in Search", allowed_actions=("type_text",)),
            ), command=command)
        return super().plan(command, context=context)


class _ScopedWorkerTests(unittest.TestCase):
    def setUp(self):
        self.backend, self.planner, self.client = _ScopedDesktop(), _ScopedPlanner(), _FollowupJev()
        self.controller = Controller(self.backend, {**DEFAULTS, "memory_enabled": False},
                                     client=self.client, planner=self.planner, debounce=0)

    def tearDown(self):
        self.controller.close()
        self.controller._worker.join(timeout=3)
        self.assertFalse(self.controller._worker.is_alive())

    def complete(self, command):
        self.controller.submit(command)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            task = self.controller._active
            if task and task.text == command and task.status == "completed":
                return
            time.sleep(.01)
        self.fail("The scoped task did not complete")


class ScopedSearchFollowupTests(_ScopedWorkerTests):
    def test_jev_command_entry_preserves_completed_browser_target(self):
        self.complete("Open YouTube in Google Chrome")
        self.complete('Type "Hello" in the search bar')
        self.assertEqual(self.backend.values, {"Google Chrome": "Hello"})
        self.assertEqual(self.client.calls[-1]["desktop"]["focused_app"], "Jev Linux")
        self.assertTrue(self.client.calls[-1]["desktop"]["self_focused"])
        self.assertEqual(self.backend.calls[-1][-1], "Google Chrome")

    def test_voice_followup_uses_the_app_the_user_brought_forward(self):
        self.complete("Open YouTube in Google Chrome")
        self.backend.foreground = "Text Editor"
        self.backend.self_focused = False
        self.complete('Type "Hello" in the search bar')
        self.assertEqual(self.backend.values, {"Text Editor": "Hello"})
        self.assertEqual(self.backend.calls[-1][-1], "Text Editor")
        self.assertEqual(self.controller.interaction_context["app"], "Text Editor")

    def test_explicit_new_app_overrides_the_preceding_browser(self):
        self.complete("Open YouTube in Google Chrome")
        self.complete('Type "Hello" in Text Editor')
        self.assertEqual(self.backend.values, {"Text Editor": "Hello"})
        self.assertEqual(self.backend.calls[-1][-1], "Text Editor")
        self.assertEqual(self.controller.interaction_context["app"], "Text Editor")
        self.complete('Type " again" in the search bar')
        self.assertEqual(self.backend.values, {"Text Editor": "Hello again"})
        self.assertEqual(self.backend.calls[-1][-1], "Text Editor")

    def test_an_application_name_inside_dictation_does_not_retarget(self):
        self.complete("Open YouTube in Google Chrome")
        self.complete('Type "open Firefox" in the search bar')
        self.assertEqual(self.backend.values, {"Google Chrome": "open Firefox"})
        self.assertEqual(self.backend.calls[-1][-1], "Google Chrome")
        self.assertFalse(any(context.get("app") == "Firefox" for context in self.backend.contexts))

    def test_later_step_stays_with_the_browser_opened_by_the_same_goal(self):
        self.backend.foreground = "Code"
        self.backend.self_focused = False
        self.complete('Open YouTube in Google Chrome\nType "Hello" in the search bar')
        self.assertEqual(self.backend.values, {"Google Chrome": "Hello"})
        self.assertEqual([call[0] for call in self.backend.calls], ["open_url", "type_text"])
        self.assertEqual(self.backend.calls[-1][-1], "Google Chrome")
        self.assertEqual(self.client.calls[-1]["desktop"]["focused_app"], "Code")


class _PageWithRestorePrompt(_ScopedDesktop):
    def __init__(self):
        super().__init__()
        self.loading_snapshots = 0
        self.not_visible_count = 0

    def _snapshot(self, app):
        snapshot = super()._snapshot(app)
        # An unrelated browser prompt can be more prominent than the requested
        # page. A field dictation step must never interpret it as a new task.
        snapshot["elements"].insert(0, {"id": f"e{self.generation}_restore", "app": app,
                                        "name": "Restore pages", "role": "push button",
                                        "editable": False, "focused": True, "active_window": True})
        snapshot["requested_url_visible"] = self.loading_snapshots == 0
        if self.loading_snapshots:
            self.loading_snapshots -= 1
            self.not_visible_count += 1
        return snapshot


class _InspectingFieldJev(_FollowupJev):
    def __init__(self):
        super().__init__()
        self.questions = []

    def decide(self, state, questions):
        self.questions.append(copy.deepcopy(questions))
        return super().decide(state, questions)


class PageFieldReadinessTests(_ScopedWorkerTests):
    def setUp(self):
        self.backend, self.planner, self.client = _PageWithRestorePrompt(), _ScopedPlanner(), _InspectingFieldJev()
        self.controller = Controller(self.backend, {**DEFAULTS, "memory_enabled": False},
                                     client=self.client, planner=self.planner, debounce=0)

    def test_field_dictation_excludes_restore_controls_and_unrelated_action_choices(self):
        self.complete("Open YouTube in Google Chrome")
        self.client.calls.clear()
        self.client.questions.clear()
        self.complete('Type "Hello" in the search bar')
        self.assertEqual(self.backend.values, {"Google Chrome": "Hello"})
        for state, questions in zip(self.client.calls, self.client.questions):
            self.assertTrue(state["desktop"]["elements"])
            self.assertTrue(all(element.get("editable") for element in state["desktop"]["elements"]))
            self.assertFalse(any("Restore" in value for value in questions["target"]["criteria"].values()))
            self.assertNotIn("click", questions["action"]["criteria"])
            for unrelated in ("app", "browser", "url", "direction", "key", "note"):
                self.assertNotIn(unrelated, questions)
        self.assertEqual([call[0] for call in self.backend.calls], ["open_url", "type_text"])

    def test_delayed_requested_page_waits_without_asking_to_restore_or_replan(self):
        self.complete("Open YouTube in Google Chrome")
        self.client.calls.clear()
        self.client.questions.clear()
        self.backend.loading_snapshots = 4
        self.complete('Type "Hello" in the search bar')
        self.assertEqual(self.backend.not_visible_count, 4)
        self.assertTrue(self.client.calls)
        self.assertTrue(all(state["desktop"]["requested_url_visible"] for state in self.client.calls))
        self.assertEqual(self.controller._active.replan_count, 0)
        self.assertEqual(len(self.planner.calls), 2)
        self.assertEqual(self.backend.values, {"Google Chrome": "Hello"})
        self.assertEqual([call[0] for call in self.backend.calls], ["open_url", "type_text"])


class _CollapsedSearchDesktop(_ScopedDesktop):
    def __init__(self):
        super().__init__()
        self.expanded = False
        self.keyboard = True

    def _snapshot(self, app):
        snapshot = super()._snapshot(app)
        common = {"app": app, "active_window": True, "pid": 42, "window_id": "chrome-window"}
        search = {**common, "id": f"e{self.generation}_search", "document_id": "youtube-document",
                  "name": "Search", "role": "entry" if self.expanded else "push button",
                  "editable": self.expanded, "focused": False, "value": self.values.get(app, ""),
                  "actions": [] if self.expanded else ["press"]}
        snapshot["elements"] = [
            {**common, "id": f"e{self.generation}_restore", "name": "Restore pages",
             "role": "push button", "editable": False, "focused": True, "actions": ["press"]},
            {**common, "id": f"e{self.generation}_omnibox", "name": "Address and search bar",
             "role": "entry", "editable": True, "focused": False, "value": "https://youtube.com"},
            search,
        ]
        if self.expanded:
            snapshot["elements"].append(
                {**common, "id": f"e{self.generation}_submit", "document_id": "youtube-document",
                 "name": "Search", "role": "push button", "editable": False,
                 "focused": False, "actions": ["press"]})
        snapshot["requested_url_visible"] = True
        return snapshot

    def needs_desktop_permission(self, operation, arguments):
        return "keyboard" if operation == "type_text" and not self.keyboard else None

    def status(self):
        return {"portal": {"keyboard": self.keyboard, "pointer": True, "granted": self.keyboard}}

    def click(self, target):
        if self.expanded or target != f"e{self.generation}_search":
            raise AssertionError("Only the fresh collapsed page Search button may be clicked")
        self.expanded = True
        self.calls.append(("click", target, "Search"))
        return "Revealed the page Search field"

    def type_text(self, text, target):
        if not self.keyboard or not self.expanded or target != f"e{self.generation}_search":
            raise AssertionError("Typing requires permission and the fresh expanded page field")
        self.values[self.selected_app] = self.values.get(self.selected_app, "") + text
        self.calls.append(("type_text", text, target, self.selected_app))
        return "Inserted text into the page Search field"


class _CollapsedSearchJev(_InspectingFieldJev):
    def decide(self, state, questions):
        self.calls.append(copy.deepcopy(state))
        self.questions.append(copy.deepcopy(questions))
        opening = state["user_command"].startswith("Open ")
        performed = any(action["action"] == ("open_url" if opening else "type_text")
                        for action in state["completed_steps"])
        preparation = state.get("field_preparation", {})
        if performed:
            action, target = "done", "none"
        elif opening:
            action, target = "open_url", "none"
        elif preparation:
            action, target = "click", preparation["reveal_controls"][0]
        else:
            action = "type_text"
            target = next(element["id"] for element in state["desktop"]["elements"]
                          if element.get("editable") and element.get("document_id")
                          and element.get("name") == "Search")
        picks = {"action": action, "target": target, "text": next(iter(state["text_candidates"]), "none"),
                 "text_mode": "dictation", "app": "none", "browser": "default", "key": "none",
                 "direction": "none", "url": next(iter(state["url_candidates"]), "none")}
        answers = {}
        for name, question in questions.items():
            if question["type"] == "noul":
                answers[name] = {"noul": 0 if name == "sensitive" or (name == "step_complete" and not performed) else 1}
            else:
                answers[name] = {"choice": picks.get(name, "none"), "confidence": 1}
        return {"answers": answers}


class _AlreadyOpenBrowserPlanner(_ScopedPlanner):
    def plan(self, command, context=None):
        self.calls.append({"command": command, "context": copy.deepcopy(context)})
        return TaskPlan(command, (PlanStep("s1", 'Type "Hello" in the search bar',
                                         "The exact dictated text is in Search", allowed_actions=("type_text",)),),
                        command=command)


class _MixedSearchPlanner(_ScopedPlanner):
    def plan(self, command, context=None):
        plan = super().plan(command, context=context)
        return replace(plan, steps=tuple(
            replace(step, allowed_actions=("click", "type_text"))
            if step.allowed_actions == ("type_text",) else step for step in plan.steps))


class _WrongSearchControlJev(_CollapsedSearchJev):
    """Offer one invalid live control to exercise controller target checks."""

    def __init__(self, control, phase):
        super().__init__()
        self.control = control
        self.phase = phase
        self.attempted = False

    def decide(self, state, questions):
        result = super().decide(state, questions)
        preparing = bool(state.get("field_preparation"))
        typed = any(action["action"] == "type_text" for action in state["completed_steps"])
        phase_matches = preparing if self.phase == "collapsed" else not preparing
        if not self.attempted and state["user_command"].startswith("Type ") and not typed and phase_matches:
            search = next(element for element in state["desktop"]["elements"]
                          if element.get("document_id") and element.get("name") == "Search")
            self.attempted = True
            result["answers"]["action"] = {
                "choice": "click" if self.control == "submit" else "type_text", "confidence": 1}
            result["answers"]["target"] = {
                "choice": search["id"].rsplit("_", 1)[0] + "_" + self.control, "confidence": 1}
        return result


class _PrematureSearchCompletionJev(_CollapsedSearchJev):
    def __init__(self):
        super().__init__()
        self.attempted = False

    def decide(self, state, questions):
        result = super().decide(state, questions)
        revealed = any(action["action"] == "click" for action in state["completed_steps"])
        typed = any(action["action"] == "type_text" for action in state["completed_steps"])
        if not self.attempted and state["user_command"].startswith("Type ") and revealed and not typed:
            self.attempted = True
            result["answers"]["action"] = {"choice": "done", "confidence": 1}
            result["answers"]["target"] = {"choice": "none", "confidence": 1}
            result["answers"]["step_complete"] = {"noul": 1}
        return result


class CollapsedSearchFieldTests(_ScopedWorkerTests):
    def setUp(self):
        self.backend, self.planner, self.client = _CollapsedSearchDesktop(), _ScopedPlanner(), _CollapsedSearchJev()
        self.controller = Controller(self.backend, {**DEFAULTS, "memory_enabled": False},
                                     client=self.client, planner=self.planner, debounce=0)

    def test_collapsed_page_search_is_revealed_then_typed_using_a_fresh_control(self):
        self.complete('Open YouTube in Google Chrome\nType "Hello" in the search bar')
        self.assertEqual([call[0] for call in self.backend.calls], ["open_url", "click", "type_text"])
        self.assertEqual(self.backend.values, {"Google Chrome": "Hello"})
        self.assertNotEqual(self.backend.calls[1][1], self.backend.calls[2][2])
        prepared = [(state, questions) for state, questions in zip(self.client.calls, self.client.questions)
                    if state.get("field_preparation")]
        self.assertEqual(len(prepared), 1)
        state, questions = prepared[0]
        self.assertEqual(len(state["field_preparation"]["reveal_controls"]), 1)
        self.assertEqual(set(questions["target"]["criteria"]),
                         {*state["field_preparation"]["reveal_controls"], "none"})
        self.assertTrue(all(not element.get("editable") for element in state["desktop"]["elements"]))
        self.assertNotIn("type_text", questions["action"]["criteria"])
        self.assertNotIn("press_key", questions["action"]["criteria"])
        self.assertFalse(any("Restore" in value for value in questions["target"]["criteria"].values()))
        expanded = [(state, questions) for state, questions in zip(self.client.calls, self.client.questions)
                    if state["user_command"].startswith("Type ") and not state.get("field_preparation")]
        self.assertTrue(expanded)
        for state, questions in expanded:
            self.assertNotIn("click", questions["action"]["criteria"])
            self.assertTrue(all(element.get("editable") and element.get("document_id")
                                for element in state["desktop"]["elements"]))
            self.assertFalse(any(target.endswith(("_omnibox", "_submit"))
                                 for target in questions["target"]["criteria"]))

    def test_omnibox_choice_is_rejected_before_revealing_the_page_field(self):
        self.client = self.controller.client = _WrongSearchControlJev("omnibox", "collapsed")
        self.complete('Open YouTube in Google Chrome\nType "Hello" in the search bar')
        self.assertTrue(self.client.attempted)
        self.assertEqual([call[0] for call in self.backend.calls], ["open_url", "click", "type_text"])
        self.assertEqual(self.backend.values, {"Google Chrome": "Hello"})

    def test_omnibox_choice_is_rejected_after_revealing_the_page_field(self):
        self.client = self.controller.client = _WrongSearchControlJev("omnibox", "expanded")
        self.complete('Open YouTube in Google Chrome\nType "Hello" in the search bar')
        self.assertTrue(self.client.attempted)
        self.assertEqual([call[0] for call in self.backend.calls], ["open_url", "click", "type_text"])
        self.assertEqual(self.backend.values, {"Google Chrome": "Hello"})

    def test_high_completion_score_after_reveal_alone_still_requires_typing(self):
        self.client = self.controller.client = _PrematureSearchCompletionJev()
        self.complete('Open YouTube in Google Chrome\nType "Hello" in the search bar')
        self.assertTrue(self.client.attempted)
        self.assertEqual([call[0] for call in self.backend.calls], ["open_url", "click", "type_text"])
        self.assertEqual(self.backend.values, {"Google Chrome": "Hello"})

    def test_visible_editable_page_field_does_not_click_search_submit(self):
        self.backend.expanded = True
        self.client = self.controller.client = _WrongSearchControlJev("submit", "expanded")
        self.complete('Open YouTube in Google Chrome\nType "Hello" in the search bar')
        self.assertTrue(self.client.attempted)
        self.assertEqual([call[0] for call in self.backend.calls], ["open_url", "type_text"])
        self.assertEqual(self.backend.values, {"Google Chrome": "Hello"})
        typing = [(state, questions) for state, questions in zip(self.client.calls, self.client.questions)
                  if state["user_command"].startswith("Type ")]
        self.assertTrue(typing)
        for state, questions in typing:
            self.assertNotIn("field_preparation", state)
            self.assertNotIn("click", questions["action"]["criteria"])
            self.assertFalse(any(target.endswith("_submit") for target in questions["target"]["criteria"]))

    def test_permission_pause_after_reveal_preserves_click_and_resumes_only_typing(self):
        self.backend.keyboard = False
        self.controller.submit('Open YouTube in Google Chrome\nType "Hello" in the search bar')
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            task = self.controller._active
            if task and task.status == "awaiting_permission":
                break
            time.sleep(.01)
        else:
            self.fail("Revealed field did not pause for keyboard permission")
        self.assertEqual([call[0] for call in self.backend.calls], ["open_url", "click"])
        self.assertEqual(task.action_count, 2)
        self.assertTrue(self.backend.expanded)
        paused_generation = self.backend.generation
        self.backend.keyboard = True
        self.assertTrue(self.controller.resume_after_permission(expected_task_id=task.id))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and task.status != "completed":
            time.sleep(.01)
        self.assertEqual(task.status, "completed")
        self.assertGreater(self.backend.generation, paused_generation)
        self.assertEqual([call[0] for call in self.backend.calls], ["open_url", "click", "type_text"])
        self.assertEqual(self.backend.values, {"Google Chrome": "Hello"})

    def test_full_goal_browser_name_scopes_an_abridged_plan_for_an_already_open_page(self):
        self.backend.expanded = True
        self.backend.foreground = "Code"
        self.backend.self_focused = False
        self.controller.planner = _AlreadyOpenBrowserPlanner()
        self.assertEqual(self.controller.interaction_context, {})
        self.complete('Open YouTube in Google Chrome\nType "Hello" in the search bar')
        self.assertEqual(self.backend.values, {"Google Chrome": "Hello"})
        self.assertEqual([call[0] for call in self.backend.calls], ["type_text"])
        self.assertEqual(self.client.calls[0]["desktop"]["focused_app"], "Code")
        self.assertTrue(any(context.get("app") == "Google Chrome"
                            and context.get("url") == "https://youtube.com" for context in self.backend.contexts))


class MixedSearchFieldTests(_ScopedWorkerTests):
    def setUp(self):
        self.backend, self.planner, self.client = _CollapsedSearchDesktop(), _MixedSearchPlanner(), _CollapsedSearchJev()
        self.controller = Controller(self.backend, {**DEFAULTS, "memory_enabled": False},
                                     client=self.client, planner=self.planner, debounce=0)

    def assert_field_choices(self):
        typing = [(state, questions) for state, questions in zip(self.client.calls, self.client.questions)
                  if state["user_command"].startswith("Type ")]
        self.assertTrue(typing)
        for state, questions in typing:
            self.assertFalse(any(target.endswith(("_omnibox", "_submit"))
                                 for target in questions["target"]["criteria"]))
            if state.get("field_preparation"):
                self.assertIn("click", questions["action"]["criteria"])
                self.assertNotIn("type_text", questions["action"]["criteria"])
                self.assertEqual(set(questions["target"]["criteria"]),
                                 {*state["field_preparation"]["reveal_controls"], "none"})
            else:
                self.assertNotIn("click", questions["action"]["criteria"])
                self.assertTrue(all(element.get("editable") and element.get("document_id")
                                    for element in state["desktop"]["elements"]))

    def test_mixed_click_and_type_rejects_omnibox_before_reveal(self):
        self.client = self.controller.client = _WrongSearchControlJev("omnibox", "collapsed")
        self.complete('Open YouTube in Google Chrome\nType "Hello" in the search bar')
        self.assertTrue(self.client.attempted)
        self.assertEqual([call[0] for call in self.backend.calls], ["open_url", "click", "type_text"])
        self.assertEqual(self.backend.values, {"Google Chrome": "Hello"})
        self.assert_field_choices()

    def test_mixed_click_and_type_rejects_omnibox_after_reveal(self):
        self.client = self.controller.client = _WrongSearchControlJev("omnibox", "expanded")
        self.complete('Open YouTube in Google Chrome\nType "Hello" in the search bar')
        self.assertTrue(self.client.attempted)
        self.assertEqual([call[0] for call in self.backend.calls], ["open_url", "click", "type_text"])
        self.assertEqual(self.backend.values, {"Google Chrome": "Hello"})
        self.assert_field_choices()

    def test_mixed_click_and_type_cannot_complete_after_reveal_alone(self):
        self.client = self.controller.client = _PrematureSearchCompletionJev()
        self.complete('Open YouTube in Google Chrome\nType "Hello" in the search bar')
        self.assertTrue(self.client.attempted)
        self.assertEqual([call[0] for call in self.backend.calls], ["open_url", "click", "type_text"])
        self.assertEqual(self.backend.values, {"Google Chrome": "Hello"})
        self.assert_field_choices()

    def test_mixed_click_and_type_rejects_submit_when_page_field_is_editable(self):
        self.backend.expanded = True
        self.client = self.controller.client = _WrongSearchControlJev("submit", "expanded")
        self.complete('Open YouTube in Google Chrome\nType "Hello" in the search bar')
        self.assertTrue(self.client.attempted)
        self.assertEqual([call[0] for call in self.backend.calls], ["open_url", "type_text"])
        self.assertEqual(self.backend.values, {"Google Chrome": "Hello"})
        self.assert_field_choices()


if __name__ == "__main__":
    unittest.main()
