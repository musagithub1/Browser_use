"""Exercise the real planned-task worker with local, deterministic adapters."""

import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from jev_linux.config import DEFAULTS
from jev_linux.controller import Controller
from jev_linux.planning import PlanStep, TaskPlan


def eventually(predicate, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("The planned-task worker did not reach the expected state")


def launch_step(identifier="s1"):
    return PlanStep(identifier, "Open the notes app", "The notes app was opened")


class Desktop:
    def __init__(self):
        self.calls = []

    def apps(self):
        return [{"id": "notes.desktop", "name": "Text Editor", "aliases": ["notes"]}]

    def snapshot(self):
        return {"focused_app": "Text Editor", "windows": [], "elements": [
            {"id": "e1", "app": "Text Editor", "name": "Document", "role": "text",
             "editable": True, "focused": True},
        ]}

    def launch_app(self, app):
        self.calls.append(("launch", app))
        return "Opened Text Editor"

    def type_text(self, text, target):
        self.calls.append(("type", text, target))
        return "Inserted text"

    def press_key(self, key, expected_focus=None):
        self.calls.append(("key", key, expected_focus))
        return "Pressed " + key

    def focus_token(self, snapshot):
        return {"app": snapshot["focused_app"], "control": "/document/1"}

    def close(self):
        pass


class FakePlanner:
    def __init__(self, steps=(), unsupported=(), callback=None, replan_callback=None):
        self.steps = tuple(steps)
        self.unsupported = tuple(unsupported)
        self.callback = callback
        self.replan_callback = replan_callback
        self.calls = []
        self.replans = []

    def plan(self, command, context=None, feedback=None):
        self.calls.append({"command": command, "context": copy.deepcopy(context)})
        if self.callback:
            return self.callback(command, context)
        return TaskPlan(command, self.steps, self.unsupported, command=command)

    def replan(self, command, context=None, feedback=None):
        self.replans.append({"command": command, "context": copy.deepcopy(context),
                             "feedback": copy.deepcopy(feedback)})
        if self.replan_callback:
            return self.replan_callback(command, context, feedback)
        return TaskPlan(command, self.steps, self.unsupported, command=command)

    def close(self):
        pass


class FakeJev:
    """Select only the choices exposed by the real controller request."""

    def __init__(self, callback=None, generated="A concise summary of the note."):
        self.callback = callback
        self.generated = generated
        self.calls = []
        self.writer_calls = []

    def decide(self, state, questions):
        self.calls.append(copy.deepcopy(state))
        selected = self.callback(state) if self.callback else {
            "action": "done" if state["completed_steps"] else "launch_app",
        }
        if isinstance(selected, str):
            selected = {"action": selected}
        picks = {
            "action": "done", "app": "notes.desktop", "target": "e1",
            "text": next(iter(state["text_candidates"]), "none"),
            "text_mode": "dictation", "key": "Return",
            "url": next(iter(state["url_candidates"]), "none"), "direction": "none",
            "note": next(iter(state["remembered_notes"]), "none"),
        }
        picks.update(selected)
        answers = {}
        for name, question in questions.items():
            if question["type"] == "noul":
                default = 0 if name == "sensitive" else 1
                if name == "step_complete":
                    default = 1 if picks["action"] == "done" else 0
                answers[name] = {"noul": selected.get(name, default)}
            else:
                value = picks[name]
                if value not in question["criteria"]:
                    raise AssertionError(f"Fake selected unavailable {name}: {value}")
                answers[name] = {"choice": value, "confidence": selected.get(name + "_confidence", 1)}
        return {"answers": answers, "latency_ms": 1}

    def generate_text(self, instruction, state=None):
        self.writer_calls.append({"instruction": instruction, "state": copy.deepcopy(state)})
        return self.generated

    def close(self):
        pass


class PlannedTaskTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.backend = Desktop()
        self.events = []
        self.controller = None
        self.releases = []

    def tearDown(self):
        for release in self.releases:
            release.set()
        if self.controller:
            self.controller.close()
            self.controller._worker.join(timeout=3)
            self.assertFalse(self.controller._worker.is_alive(), "A task worker leaked from the test")
        self.directory.cleanup()

    def start(self, planner, client=None, **settings):
        config = {**DEFAULTS, "memory_enabled": False, "planning_enabled": True,
                  "notes_directory": self.directory.name, **settings}
        self.client = client or FakeJev()
        self.planner = planner
        self.controller = Controller(self.backend, config, client=self.client,
                                     planner=planner, debounce=0)
        self.controller.set_event_handler(self.events.append)
        return self.controller

    def completed(self, timeout=6):
        eventually(lambda: any(event["message"] == "Done." for event in self.events), timeout)
        self.assertEqual(self.controller._active.status, "completed")
        self.assertFalse([event for event in self.events if event["type"] == "error"])

    def blocked(self, status="blocked", timeout=6):
        eventually(lambda: any(event["type"] == "task_progress" and event.get("status") == status
                               for event in self.events), timeout)
        self.assertEqual(self.controller._active.status, status)
        self.assertFalse(any(event["message"] == "Done." for event in self.events))

    def gate(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        return entered, release

    def test_more_than_twelve_actions_and_identical_actions_in_distinct_steps(self):
        steps = [launch_step(f"s{i}") for i in range(13)]
        command = "; then ".join("open the notes app" for _ in steps)
        controller = self.start(FakePlanner(steps))
        controller.submit(command)
        self.completed(timeout=8)
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")] * 13)
        self.assertEqual(controller._active.step_index, 13)
        self.assertEqual(controller._active.action_count, 13)
        self.assertEqual(len(controller._active.fingerprints), 13)
        self.assertTrue(self.client.calls)
        self.assertTrue(all(state["task_goal"] == command for state in self.client.calls))
        self.assertTrue(all(state["user_command"] == "Open the notes app" for state in self.client.calls))
        self.assertEqual(self.planner.calls[0]["command"], command)
        self.assertTrue(all(event.get("goal") == command for event in self.events
                            if event["type"] == "task_progress"))

    def test_partial_launch_is_evidence_for_the_first_planned_step(self):
        payload = "hello world"
        planner = FakePlanner([launch_step(), PlanStep("s2", f'Type "{payload}"', "Text was inserted")])

        def choose(state):
            if not state["speech_final"]:
                return "launch_app"
            if state["completed_steps"]:
                return "done"
            return {"action": "type_text", "text": next(
                key for key, value in state["text_candidates"].items() if value == payload)}

        controller = self.start(planner, FakeJev(choose))
        controller.submit("open notes and type", final=False)
        eventually(lambda: len(self.backend.calls) == 1)
        controller.submit('open notes and type "hello world"', final=True)
        self.completed()
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop"), ("type", payload, "e1")])
        self.assertEqual(len(planner.calls), 1)

    def test_done_without_verified_outcome_waits_and_keeps_unfinished_step(self):
        client = FakeJev(lambda state: {"action": "done", "step_complete": 0}
                         if state["completed_steps"] else {"action": "launch_app"})
        controller = self.start(FakePlanner([launch_step()]), client)
        controller.submit("open notes")
        self.blocked()
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")])
        self.assertEqual(controller._active.step_index, 0)
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(self.planner.replans, [], "A delayed side effect must not be replanned")

    def test_verified_effect_completes_even_when_next_action_choice_is_uncertain(self):
        client = FakeJev(lambda state: {"action": "launch_app", "action_confidence": 0.3,
                                       "step_complete": 0.94}
                         if state["completed_steps"] else {"action": "launch_app"})
        controller = self.start(FakePlanner([launch_step()]), client)
        controller.submit("open notes")
        self.completed()
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")])
        self.assertEqual(controller._active.step_index, 1)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(self.planner.replans, [])

    def test_done_cannot_skip_an_action_step_with_no_performed_effect(self):
        controller = self.start(FakePlanner([launch_step()]), FakeJev(lambda state: "done"))
        controller.submit("open notes")
        self.blocked()
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(controller._active.step_index, 0)
        self.assertEqual(len(self.planner.replans), 1)

    def test_explicit_launch_does_not_offer_completion_from_an_existing_window(self):
        step = PlanStep("open", "Open Text Editor", "Text Editor was opened",
                        allowed_actions=("launch_app",))
        client = FakeJev()
        offered = []
        original_decide = client.decide

        def decide(state, questions):
            offered.append(copy.deepcopy(questions))
            return original_decide(state, questions)

        client.decide = decide
        controller = self.start(FakePlanner([step]), client)
        controller.submit("open Text Editor")
        self.completed()

        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")])
        self.assertNotIn("done", offered[0]["action"]["criteria"])
        self.assertNotIn("wait", offered[0]["action"]["criteria"])
        self.assertIn("done", offered[1]["action"]["criteria"])
        self.assertFalse(set(offered[0]) & {"browser", "target", "text", "text_mode", "url", "key", "direction", "note"})
        self.assertEqual(client.calls[0]["desktop"]["elements"], [])
        self.assertEqual(self.planner.replans, [])

    def test_explicit_later_launch_requires_its_own_current_step_effect(self):
        steps = [PlanStep(identifier, "Open Text Editor", "Text Editor was opened",
                          allowed_actions=("launch_app",)) for identifier in ("first", "second")]
        client = FakeJev()
        original_decide = client.decide

        def decide(state, questions):
            if state["current_step"]["id"] == "second" and not state["completed_steps"]:
                self.assertEqual(len(state["prior_task_results"]), 1)
                self.assertNotIn("done", questions["action"]["criteria"])
            return original_decide(state, questions)

        client.decide = decide
        controller = self.start(FakePlanner(steps), client)
        controller.submit("open Text Editor, then open Text Editor again")
        self.completed()

        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")] * 2)
        self.assertEqual(controller._active.step_index, 2)
        self.assertEqual(self.planner.replans, [])

    def test_direct_note_reads_offer_only_source_and_require_each_read_effect(self):
        payload = "Three exercises for tomorrow."
        steps = [PlanStep("save", f'Create a note containing "{payload}"', "The note was saved",
                          allowed_actions=("create_note",))]
        steps.extend(PlanStep(identifier, "Read that saved note", "The note content was read",
                              allowed_actions=("read_note",)) for identifier in ("read1", "read2"))
        client = FakeJev(lambda state: "done" if state["completed_steps"] else
                         "create_note" if state["current_step"]["id"] == "save" else "read_note")
        original_decide = client.decide
        offered = []

        def decide(state, questions):
            if state["current_step"]["id"].startswith("read"):
                offered.append(copy.deepcopy(questions))
                self.assertIn("note", questions)
                self.assertFalse(set(questions) & {"app", "browser", "target", "text", "text_mode", "url", "key", "direction"})
                self.assertEqual(state["desktop"]["elements"], [])
                self.assertEqual(state["installed_apps"], {})
                self.assertNotIn("wait", questions["action"]["criteria"])
                self.assertEqual("done" in questions["action"]["criteria"], bool(state["completed_steps"]))
            return original_decide(state, questions)

        client.decide = decide
        controller = self.start(FakePlanner(steps), client)
        with patch.object(controller, "_open_note"):
            controller.submit(f'Create a note containing "{payload}", read it, then read it again.')
            self.completed()
        self.assertEqual(len(offered), 4)
        self.assertEqual([item["action"] for item in controller._active.completed],
                         ["create_note", "read_note", "read_note"])
        self.assertEqual(self.planner.replans, [])

    def test_create_then_explicit_launch_and_reopen_finish_without_replanning(self):
        payload = "A useful note for tomorrow"
        steps = [
            PlanStep("save", f'Create a note containing "{payload}"', "The note was saved",
                     allowed_actions=("create_note",)),
            PlanStep("editor", "Open Text Editor", "Text Editor was opened",
                     allowed_actions=("launch_app",)),
            PlanStep("reopen", "Open that saved note", "The saved note was opened",
                     allowed_actions=("open_note",)),
        ]
        client = FakeJev()
        original_decide = client.decide

        def decide(state, questions):
            if not state["completed_steps"]:
                identifier = state["current_step"]["id"]
                if identifier == "save":
                    client.callback = lambda current: {"action": "create_note", "text": next(
                        key for key, value in current["text_candidates"].items() if value == payload)}
                elif identifier == "reopen":
                    self.assertNotIn("done", questions["action"]["criteria"])
                    self.assertNotIn("wait", questions["action"]["criteria"])
                    self.assertTrue(state["remembered_notes"])
                    self.assertFalse(set(questions) & {"app", "browser", "target", "text", "text_mode", "url", "key", "direction"})
                    client.callback = lambda current: "open_note"
                else:
                    self.assertNotIn("done", questions["action"]["criteria"])
                    client.callback = lambda current: "launch_app"
            else:
                client.callback = lambda current: "done"
            return original_decide(state, questions)

        client.decide = decide
        controller = self.start(FakePlanner(steps), client)
        with patch.object(controller, "_open_note") as opened:
            controller.submit(f'Create a note containing "{payload}", then open Text Editor and open that saved note.')
            self.completed()

        notes = list(Path(self.directory.name).glob("*.md"))
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].read_text(), payload + "\n")
        self.assertEqual(opened.call_count, 2)
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")])
        self.assertEqual([effect["action"] for effect in controller._active.completed],
                         ["create_note", "launch_app", "open_note"])
        self.assertEqual(self.planner.replans, [])

    def test_repeated_action_in_same_step_is_not_executed_twice(self):
        controller = self.start(FakePlanner([launch_step()]), FakeJev(lambda state: "launch_app"))
        controller.submit("open notes")
        self.blocked()
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")])
        self.assertEqual(controller._active.action_count, 1)
        self.assertEqual(self.planner.replans, [])

    def test_spoken_continue_after_action_limit_does_not_replay_completed_steps(self):
        controller = self.start(FakePlanner([launch_step(f"s{i}") for i in range(3)]),
                                task_max_actions=2)
        controller.submit("open notes three times")
        self.blocked("paused")
        utterance = controller._active
        self.assertEqual(utterance.step_index, 2)
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")] * 2)
        controller.submit("Continue task.")
        self.completed()
        self.assertIs(controller._active, utterance)
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")] * 3)
        self.assertEqual(len(self.planner.calls), 1)

    def test_last_action_at_the_limit_can_be_verified_and_finish(self):
        controller = self.start(FakePlanner([launch_step("s1"), launch_step("s2")]),
                                task_max_actions=2)
        controller.submit("open notes twice")
        self.completed()
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")] * 2)
        self.assertFalse(any(event.get("status") == "paused" for event in self.events))

    def test_time_limit_preserves_work_and_resume_renews_deadline(self):
        controller = self.start(FakePlanner([launch_step("s1"), launch_step("s2")]))
        original_launch = self.backend.launch_app

        def expire_after_first(app):
            message = original_launch(app)
            if len(self.backend.calls) == 1:
                controller._active.deadline = time.monotonic() - 1
            return message

        self.backend.launch_app = expire_after_first
        controller.submit("open notes twice")
        self.blocked("paused")
        self.assertEqual(controller._active.action_count, 1)
        self.assertTrue(controller.resume_task())
        self.completed()
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")] * 2)

    def test_deadline_expiring_during_decision_pauses_before_new_effect(self):
        def choose(state):
            self.controller._active.deadline = time.monotonic() - 1
            return "launch_app"

        controller = self.start(FakePlanner([launch_step()]), FakeJev(choose))
        controller.submit("open notes")
        self.blocked("paused")
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(controller._active.action_count, 0)

    def test_deadline_expiring_during_generation_does_not_save_late_text(self):
        step = PlanStep("draft", "Create a note about tomorrow's meeting", "A note was saved")
        client = FakeJev(lambda state: {"action": "create_note", "text_mode": "generate"})

        def late_writer(instruction, state=None):
            self.controller._active.deadline = time.monotonic() - 1
            return "Late generated prose"

        client.generate_text = late_writer
        controller = self.start(FakePlanner([step]), client)
        with patch.object(controller, "_open_note") as opened:
            controller.submit("create a note about tomorrow's meeting")
            self.blocked("paused")
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])
        opened.assert_not_called()
        self.assertEqual(controller._active.completed, [])

    def test_continue_does_not_approve_pending_action_but_spoken_confirm_does(self):
        step = PlanStep("s1", "Press Delete", "Delete was pressed")
        client = FakeJev(lambda state: "done" if state["completed_steps"] else
                         {"action": "press_key", "key": "Delete"})
        controller = self.start(FakePlanner([step]), client)
        controller.submit("press Delete")
        eventually(lambda: any(event["type"] == "confirmation" for event in self.events))
        self.assertEqual(controller._active.status, "awaiting_confirmation")
        self.assertFalse(controller.resume_task())
        controller.submit("Continue task.")
        self.assertEqual(self.backend.calls, [])
        self.assertFalse(controller._pending.get("approved", False))
        controller.submit("Confirm.")
        self.completed()
        self.assertEqual(self.backend.calls,
                         [("key", "Delete", {"app": "Text Editor", "control": "/document/1"})])
        controller.confirm()
        self.assertEqual(len(self.backend.calls), 1)

    def test_cancel_discards_pending_approval_and_later_confirm(self):
        step = PlanStep("s1", "Press Delete", "Delete was pressed")
        controller = self.start(FakePlanner([step]), FakeJev(lambda state: {
            "action": "press_key", "key": "Delete"}))
        controller.submit("press Delete")
        eventually(lambda: controller._pending is not None)
        utterance = controller._active
        controller.cancel()
        controller.submit("Confirm.")
        self.assertEqual(utterance.status, "cancelled")
        self.assertIsNone(controller._pending)
        self.assertIsNone(controller._active)
        self.assertEqual(self.backend.calls, [])

    def test_spoken_continuation_retains_original_goal_and_completed_effects(self):
        original = "open notes and wait for the note contents"
        payload = "Keep all the earlier work."
        continuation = f'and then create a note containing "{payload}"'

        def plan(command, context):
            steps = (launch_step(), PlanStep("details", "What should the note contain?",
                                             "Contents were supplied"))
            if payload in command:
                steps = (PlanStep("new_note", f'Create a note containing "{payload}"', "A note was saved"),)
            return TaskPlan(command, steps, command=command)

        def choose(state):
            if not state["speech_final"]:
                return "wait"
            if state["completed_steps"]:
                return "done"
            if state["current_step"]["id"] == "new_note":
                return {"action": "create_note", "text": next(
                    key for key, value in state["text_candidates"].items() if value == payload)}
            if state["current_step"]["id"] == "details":
                return {"action": "create_note", "action_confidence": 0.1}
            return "launch_app"

        planner = FakePlanner(callback=plan)
        controller = self.start(planner, FakeJev(choose))
        with patch.object(controller, "_open_note"):
            controller.submit(original)
            self.blocked("needs_details")
            utterance = controller._active
            controller.submit("and then create a note containing", final=False)
            controller.submit(continuation, final=True)
            self.completed()
        combined = original + " " + continuation
        self.assertIs(controller._active, utterance)
        self.assertEqual(utterance.text, combined)
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")])
        self.assertEqual([effect["action"] for effect in utterance.completed], ["launch_app", "create_note"])
        self.assertEqual([call["command"] for call in planner.calls], [original, combined])
        self.assertEqual(planner.calls[1]["context"]["completed_actions"][0]["action"], "launch_app")
        note_states = [state for state in self.client.calls
                       if state.get("current_step", {}).get("id") == "new_note"]
        self.assertTrue(note_states)
        self.assertTrue(all(state["task_goal"] == combined for state in note_states))
        notes = list(Path(self.directory.name).glob("*.md"))
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].read_text(), payload + "\n")

    def test_partial_spoken_continuation_clears_old_approval_before_new_plan(self):
        original = "press Delete"
        continuation = "and then open notes instead of deleting"

        def plan(command, context):
            step = launch_step("replacement") if "instead" in command else PlanStep(
                "delete", "Press Delete", "Delete was pressed")
            return TaskPlan(command, (step,), command=command)

        def choose(state):
            if not state["speech_final"]:
                return "wait"
            if state["completed_steps"]:
                return "done"
            if state["current_step"]["id"] == "delete":
                return {"action": "press_key", "key": "Delete"}
            return "launch_app"

        controller = self.start(FakePlanner(callback=plan), FakeJev(choose))
        controller.submit(original)
        eventually(lambda: controller._pending is not None)
        old_pending = controller._pending
        utterance = controller._active
        controller.submit("and then open notes instead", final=False)
        self.assertIsNone(controller._pending)
        controller.submit("Confirm.")
        self.assertFalse(old_pending.get("approved", False))
        self.assertEqual(self.backend.calls, [])
        controller.submit(continuation, final=True)
        self.completed()
        self.assertIs(controller._active, utterance)
        self.assertEqual(utterance.text, original + " " + continuation)
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")])
        self.assertTrue(any(event["type"] == "confirmation_cancelled" for event in self.events))
        self.assertEqual(len(self.planner.calls), 2)

    def test_continuation_during_generation_discards_stale_text_and_keeps_earlier_effect(self):
        entered, release = self.gate()
        original = "open notes and create a note about tomorrow's meeting"
        continuation = "and then cancel the draft and open notes again"

        def plan(command, context):
            steps = (launch_step("opened"), PlanStep("draft", "Create a note about tomorrow's meeting",
                                                    "A note was saved"))
            if "cancel the draft" in command:
                steps = (launch_step("replacement"),)
            return TaskPlan(command, steps, command=command)

        def choose(state):
            if state["completed_steps"]:
                return "done"
            if state["current_step"]["id"] == "draft":
                return {"action": "create_note", "text_mode": "generate"}
            return "launch_app"

        client = FakeJev(choose)

        def slow_writer(instruction, state=None):
            client.writer_calls.append({"instruction": instruction, "state": copy.deepcopy(state)})
            entered.set()
            if not release.wait(3):
                raise AssertionError("Writer gate was not released")
            return "Stale draft that must never be saved"

        client.generate_text = slow_writer
        controller = self.start(FakePlanner(callback=plan), client)
        with patch.object(controller, "_open_note") as opened:
            controller.submit(original)
            self.assertTrue(entered.wait(2))
            utterance = controller._active
            self.assertEqual(self.backend.calls, [("launch", "notes.desktop")])
            controller.submit(continuation)
            release.set()
            self.completed()
        self.assertIs(controller._active, utterance)
        self.assertEqual(utterance.text, original + " " + continuation)
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")] * 2)
        self.assertEqual([effect["action"] for effect in utterance.completed], ["launch_app", "launch_app"])
        self.assertEqual(len(client.writer_calls), 1)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])
        opened.assert_not_called()
        self.assertEqual(len(self.planner.calls), 2)

    def continuation_at_event(self, event_type, message):
        original = "press Return"
        continuation = "and then open notes instead of pressing Return"

        def plan(command, context):
            step = launch_step("replacement") if "instead" in command else PlanStep(
                "old_key", "Press Return", "Return was pressed")
            return TaskPlan(command, (step,), command=command)

        def choose(state):
            if state["completed_steps"]:
                return "done"
            return "press_key" if state["current_step"]["id"] == "old_key" else "launch_app"

        controller = self.start(FakePlanner(callback=plan), FakeJev(choose))
        changed = False

        def handler(event):
            nonlocal changed
            self.events.append(event)
            if not changed and event["type"] == event_type and event["message"] == message:
                changed = True
                controller.submit(continuation)

        controller.set_event_handler(handler)
        controller.submit(original)
        self.completed()
        self.assertTrue(changed)
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")])
        self.assertEqual(controller._active.text, original + " " + continuation)

    def test_continuation_from_decision_event_discards_old_action(self):
        self.continuation_at_event("decision", "press key")

    def test_continuation_from_running_event_discards_old_action(self):
        self.continuation_at_event("task_progress", "Running step…")

    def test_cancel_during_planning_discards_old_plan_before_new_task(self):
        entered, release = self.gate()

        def plan(command, context):
            if command == "old task":
                entered.set()
                if not release.wait(3):
                    raise AssertionError("Planner gate was not released")
            return TaskPlan(command, (launch_step(),), command=command)

        controller = self.start(FakePlanner(callback=plan))
        controller.submit("old task")
        self.assertTrue(entered.wait(1))
        old = controller._active
        controller.cancel()
        controller.submit("new task")
        release.set()
        self.completed()
        self.assertEqual(old.status, "cancelled")
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")])
        self.assertTrue(all(state["task_goal"] == "new task" for state in self.client.calls))
        self.assertFalse(any(event["type"] == "task_plan" and event["plan"]["goal"] == "old task"
                             for event in self.events))

    def test_cancel_during_decision_discards_old_action_before_new_task(self):
        entered, release = self.gate()

        def choose(state):
            if state["task_goal"] == "old task":
                entered.set()
                if not release.wait(3):
                    raise AssertionError("Decision gate was not released")
                return {"action": "press_key", "key": "Return"}
            return "done" if state["completed_steps"] else "launch_app"

        controller = self.start(FakePlanner([launch_step()]), FakeJev(choose))
        controller.submit("old task")
        self.assertTrue(entered.wait(1))
        old = controller._active
        controller.cancel()
        controller.submit("new task")
        release.set()
        self.completed()
        self.assertEqual(old.status, "cancelled")
        self.assertEqual(old.completed, [])
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")])

    def test_cancel_during_replan_discards_revised_plan(self):
        entered, release = self.gate()

        def replan(command, context, feedback):
            entered.set()
            if not release.wait(3):
                raise AssertionError("Replan gate was not released")
            return TaskPlan(command, (launch_step("alternate"),), command=command)

        client = FakeJev(lambda state: "wait" if state["task_goal"] == "old task" else
                         "done" if state["completed_steps"] else "launch_app")
        planner = FakePlanner([launch_step()], replan_callback=replan)
        controller = self.start(planner, client)
        controller.submit("old task")
        self.assertTrue(entered.wait(3))
        old = controller._active
        controller.cancel()
        controller.submit("new task")
        release.set()
        self.completed()
        self.assertEqual(old.status, "cancelled")
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")])
        self.assertFalse(any(event["type"] == "task_plan" and event["message"] == "Remaining task replanned."
                             for event in self.events))

    def test_created_note_is_read_by_later_step_and_content_reaches_summary_writer(self):
        payload = "Meeting agenda: budget, hiring, and the release date."
        steps = [PlanStep("save", f'Create a note containing "{payload}"', "A note was saved"),
                 PlanStep("read", "Read that saved note", "The saved note content was read"),
                 PlanStep("summary", "Create a note about a concise summary of that saved note",
                          "A separate summary note was saved")]

        def choose(state):
            if state["completed_steps"]:
                return "done"
            identifier = state["current_step"]["id"]
            if identifier == "save":
                return {"action": "create_note", "text": next(
                    key for key, value in state["text_candidates"].items() if value == payload)}
            if identifier == "read":
                return "read_note"
            return {"action": "create_note", "text_mode": "generate"}

        client = FakeJev(choose, generated="Budget, hiring, and release date are on the agenda.")
        command = f'Create a note containing "{payload}", read that saved note, and create a summary note.'
        controller = self.start(FakePlanner(steps), client)
        with patch.object(controller, "_open_note") as opened:
            controller.submit(command)
            self.completed()
        notes = sorted(Path(self.directory.name).glob("*.md"))
        self.assertEqual(len(notes), 2)
        self.assertEqual(opened.call_count, 2)
        self.assertEqual({note.read_text() for note in notes}, {payload + "\n", client.generated + "\n"})
        read_states = [state for state in client.calls if state["current_step"]["id"] == "read"]
        self.assertTrue(read_states[0]["remembered_notes"], "A same-task note must be selectable without persistent memory")
        self.assertEqual(len(client.writer_calls), 1)
        writer_state = client.writer_calls[0]["state"]
        self.assertEqual(writer_state["task_goal"], command)
        self.assertEqual(writer_state["task_data"][0]["content"], payload + "\n")
        self.assertEqual(writer_state["task_data"][0]["source"], "note")
        self.assertEqual(writer_state["previous_note"]["content"], payload + "\n")
        self.assertIsNone(controller.memory)

    def test_append_preserves_created_note_and_readback_reaches_later_summary(self):
        original = "First agenda item: budget."
        addition = "Second agenda item: hiring."
        steps = [PlanStep("save", f'Create a note containing "{original}"', "The agenda note was saved"),
                 PlanStep("append", f'Append "{addition}" to that saved note', "The second item was appended"),
                 PlanStep("summary", "Create a note about a summary of that updated agenda",
                          "A separate agenda summary was saved")]

        def choose(state):
            if state["completed_steps"]:
                return "done"
            identifier = state["current_step"]["id"]
            if identifier in {"save", "append"}:
                payload = original if identifier == "save" else addition
                return {"action": "create_note" if identifier == "save" else "append_note",
                        "text": next(key for key, value in state["text_candidates"].items() if value == payload)}
            return {"action": "create_note", "text_mode": "generate"}

        client = FakeJev(choose, generated="The agenda covers budget and hiring.")
        controller = self.start(FakePlanner(steps), client)
        command = f'Create a note containing "{original}", append "{addition}" to it, and summarize that updated note.'
        with patch.object(controller, "_open_note"):
            controller.submit(command)
            self.completed()
        combined = original + "\n" + addition + "\n"
        agenda_path = Path(controller._active.completed[0]["arguments"]["note_path"])
        self.assertEqual(agenda_path.read_text(), combined)
        self.assertEqual(controller._active.completed[1]["arguments"]["note_path"], str(agenda_path))
        self.assertEqual(len(list(Path(self.directory.name).glob("*.md"))), 2)
        verified_append = next(state for state in client.calls
                               if state["current_step"]["id"] == "append" and state["completed_steps"])
        self.assertEqual(verified_append["task_data"][-1]["content"], combined)
        self.assertEqual(verified_append["task_data"][-1]["appended_text"], addition)
        self.assertEqual(client.writer_calls[0]["state"]["task_data"][-1]["content"], combined)
        self.assertEqual(client.writer_calls[0]["state"]["previous_note"]["content"], combined)

    def test_unsupported_requirements_never_report_the_full_task_done(self):
        reason = "Sending the email needs explicit approval and an available email app."
        controller = self.start(FakePlanner([launch_step()], unsupported=[reason]))
        controller.submit("open notes and send an email")
        self.blocked()
        self.assertEqual(controller._active.step_index, 1)
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")])
        self.assertTrue(any(reason in event["message"] for event in self.events))
        self.assertFalse(any(event.get("status") == "completed" for event in self.events))

    def test_clarification_step_pauses_before_requesting_or_executing_an_action(self):
        step = PlanStep("s1", "Which saved note should be summarized?", "A note was identified", "clarify")
        controller = self.start(FakePlanner([step]))
        controller.submit("summarize a note")
        self.blocked("needs_details")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.backend.calls, [])

    def test_clarification_answer_retains_original_goal_and_does_not_replay_earlier_effect(self):
        command = "open notes and create a note"
        question = "What exact words should the note contain?"
        answer = "Budget £7.50; keep the punctuation."

        def plan(original, context):
            steps = (launch_step("opened"), PlanStep("ask", question, "Note contents were supplied", "clarify"))
            if context.get("clarification_answers"):
                text = context["clarification_answers"][-1]["answer"]
                steps = (PlanStep("write", f'Create a note containing "{text}"', "A note was saved"),)
            return TaskPlan(original, steps, command=original)

        def choose(state):
            if state["completed_steps"]:
                return "done"
            if state["current_step"]["id"] == "write":
                return {"action": "create_note", "text": next(
                    key for key, value in state["text_candidates"].items() if value == answer)}
            return "launch_app"

        planner = FakePlanner(callback=plan)
        controller = self.start(planner, FakeJev(choose))
        with patch.object(controller, "_open_note"):
            controller.submit(command)
            self.blocked("needs_details")
            utterance = controller._active
            controller.submit(answer)
            self.completed()
        self.assertIs(controller._active, utterance)
        self.assertEqual(utterance.text, command)
        self.assertEqual(utterance.clarifications, [{"question": question, "answer": answer}])
        self.assertEqual([call["command"] for call in planner.calls], [command, command])
        self.assertEqual(planner.calls[1]["context"]["clarification_answers"], utterance.clarifications)
        self.assertEqual(planner.calls[1]["context"]["completed_actions"][0]["action"], "launch_app")
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")])
        self.assertEqual([effect["action"] for effect in utterance.completed], ["launch_app", "create_note"])
        self.assertTrue(all(state["task_goal"] == command for state in self.client.calls))
        notes = list(Path(self.directory.name).glob("*.md"))
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].read_text(), answer + "\n")

    def test_partial_clarification_speech_is_not_used_before_final_answer(self):
        command = "create a note"
        question = "What should the note contain?"
        answer = "Keep the final transcript exactly."

        def plan(original, context):
            step = PlanStep("ask", question, "Contents were supplied", "clarify")
            if context.get("clarification_answers"):
                step = PlanStep("write", f'Create a note containing "{answer}"', "A note was saved")
            return TaskPlan(original, (step,), command=original)

        client = FakeJev(lambda state: "done" if state["completed_steps"] else {
            "action": "create_note", "text": next(
                key for key, value in state["text_candidates"].items() if value == answer)})
        controller = self.start(FakePlanner(callback=plan), client)
        with patch.object(controller, "_open_note"):
            controller.submit(command)
            self.blocked("needs_details")
            utterance = controller._active
            version = utterance.version
            original_plan = utterance.plan
            controller.submit("Keep the final", final=False)
            self.assertEqual(utterance.clarifications, [])
            self.assertEqual(utterance.version, version)
            self.assertIs(utterance.plan, original_plan)
            self.assertEqual(utterance.status, "needs_details")
            self.assertEqual(client.calls, [])
            self.assertEqual(len(self.planner.calls), 1)
            self.assertEqual(list(Path(self.directory.name).iterdir()), [])
            controller.submit(answer, final=True)
            self.completed()
        self.assertEqual(utterance.text, command)
        self.assertEqual(utterance.clarifications, [{"question": question, "answer": answer}])
        self.assertEqual(len(self.planner.calls), 2)
        self.assertEqual(self.planner.calls[1]["context"]["clarification_answers"], utterance.clarifications)

    def test_one_replan_keeps_completed_prefix_and_uses_original_goal(self):
        steps = [launch_step("first"), PlanStep("missing", "Use the unavailable control", "Control was activated")]

        def choose(state):
            if state["current_step"]["id"] == "missing":
                return "wait"
            return "done" if state["completed_steps"] else "launch_app"

        def replan(command, context, feedback):
            return TaskPlan(command, (launch_step("alternative"),), command=command)

        planner = FakePlanner(steps, replan_callback=replan)
        controller = self.start(planner, FakeJev(choose))
        command = "open notes and use the unavailable control by another available approach"
        controller.submit(command)
        self.completed()
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")] * 2)
        self.assertEqual(len(planner.replans), 1)
        replan_call = planner.replans[0]
        self.assertEqual(replan_call["command"], command)
        self.assertEqual(replan_call["feedback"]["failed_step"]["id"], "missing")
        self.assertEqual(len(replan_call["feedback"]["completed_actions"]), 1)
        self.assertEqual(replan_call["feedback"]["completed_actions"][0]["step_id"], "first")
        self.assertEqual([step.id for step in controller._active.plan.steps], ["first", "r1_alternative"])
        self.assertTrue(all(state["task_goal"] == command for state in self.client.calls))

    def test_second_failed_strategy_is_blocked_without_another_automatic_replan(self):
        controller = self.start(FakePlanner([launch_step()]), FakeJev(lambda state: "wait"))
        controller.submit("open notes")
        self.blocked()
        self.assertEqual(len(self.planner.replans), 1)
        self.assertEqual(controller._active.replan_count, 1)
        self.assertEqual(self.backend.calls, [])

    def test_changed_dictation_in_plan_is_blocked_before_writing(self):
        step = PlanStep("s1", 'Create a note containing "rewritten payload"', "A note was saved")
        client = FakeJev(lambda state: {"action": "create_note", "text": next(
            key for key, value in state["text_candidates"].items() if value == "rewritten payload")})
        controller = self.start(FakePlanner([step]), client)
        controller.submit('create a note containing "original payload"')
        self.blocked("needs_details")
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])
        self.assertEqual(client.writer_calls, [])
        self.assertEqual(self.backend.calls, [])

    def test_payload_from_later_step_cannot_be_selected_for_earlier_note(self):
        first = "First note: blue sky."
        second = "Second note: green grass."
        steps = [PlanStep("first", f'Create a note containing "{first}"', "The first note was saved"),
                 PlanStep("second", f'Create a note containing "{second}"', "The second note was saved")]

        def choose(state):
            if state["completed_steps"]:
                return "done"
            is_first = state["current_step"]["id"] == "first"
            candidates = state["text_candidates"]
            # Deliberately prefer the wrong later payload if the controller
            # exposes it as a selectable candidate in the first step.
            payload = second if not is_first or second in candidates.values() else first
            return {"action": "create_note", "text": next(
                key for key, value in candidates.items() if value == payload)}

        controller = self.start(FakePlanner(steps), FakeJev(choose))
        command = f'Create a note containing "{first}" and then create a note containing "{second}"'
        with patch.object(controller, "_open_note"):
            controller.submit(command)
            self.completed()
        first_state = next(state for state in self.client.calls if state["current_step"]["id"] == "first")
        self.assertNotIn(second, first_state["text_candidates"].values())
        self.assertEqual([effect["arguments"]["text"] for effect in controller._active.completed],
                         [first, second])
        self.assertEqual({note.read_text() for note in Path(self.directory.name).glob("*.md")},
                         {first + "\n", second + "\n"})

    def test_transient_missing_state_recovers_without_replanning(self):
        count = 0

        def choose(state):
            nonlocal count
            count += 1
            if count <= 2:
                return {"action": "done", "step_complete": 0}
            return "done" if state["completed_steps"] else "launch_app"

        controller = self.start(FakePlanner([launch_step()]), FakeJev(choose))
        controller.submit("open notes")
        self.completed()
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")])
        self.assertEqual(self.planner.replans, [])

    def test_verify_step_can_complete_from_observed_state_without_an_action(self):
        step = PlanStep("s1", "Verify that the notes app is focused", "Text Editor is focused", "verify")
        controller = self.start(FakePlanner([step]), FakeJev(lambda state: "done"))
        controller.submit("verify that notes is focused")
        self.completed()
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(controller._active.step_index, 1)

    def test_uncertain_action_is_replanned_before_any_effect(self):
        initial = launch_step()
        revised = PlanStep("resolved", "Open Text Editor", "Text Editor is open", allowed_actions=("launch_app",))
        planner = FakePlanner([initial], replan_callback=lambda command, context, feedback: TaskPlan(command, (revised,)))

        def choose(state):
            if state["completed_steps"]:
                return "done"
            return {"action": "launch_app", "action_confidence": .6 if state["current_step"]["id"] == "s1" else 1}

        controller = self.start(planner, FakeJev(choose))
        controller.submit("open notes")
        self.completed()
        self.assertEqual(len(planner.replans), 1)
        self.assertEqual(self.backend.calls, [("launch", "notes.desktop")])
        self.assertEqual(planner.replans[0]["context"]["completed_actions"], [])

    def test_composition_uses_full_step_even_without_literal_text_selection(self):
        instruction = "Create a note summarizing the page in two bullet points"
        step = PlanStep("summary", instruction, "A summary note is saved", allowed_actions=("create_note",))
        client = FakeJev(lambda state: "done" if state["completed_steps"] else {
            "action": "create_note", "text": "none", "text_confidence": .1, "text_mode": "generate"})
        controller = self.start(FakePlanner([step]), client)
        with patch.object(controller, "_open_note"):
            controller.submit(instruction)
            self.completed()
        self.assertEqual(client.writer_calls[0]["instruction"], instruction)
        self.assertEqual(len(list(Path(self.directory.name).glob("*.md"))), 1)

    def test_public_page_brief_reads_then_composes_one_saved_note_without_browser_launch(self):
        url = "https://example.org/beginner-guide"
        content = "Variables hold values. Strings contain text. Lists hold ordered items."
        instruction = ('Create a Markdown study note titled "Beginner guide" from the read page, '
                       'with five bullet points, three exercises, its source URL, and fewer than 200 words.')
        steps = [PlanStep("read", f"Read {url}", "The source text is available", allowed_actions=("read_url",)),
                 PlanStep("brief", instruction, "The requested brief is saved and opened", allowed_actions=("create_note",))]
        generated = '# Beginner guide\n\n- Variables hold values.\n- Strings contain text.\n- Lists hold ordered items.\n- Values can be reused.\n- Items can be indexed.\n\n1. Assign a value.\n2. Write a string.\n3. Index a list.\n\nSource: ' + url

        def choose(state):
            if state["completed_steps"]:
                return "done"
            if state["current_step"]["id"] == "read":
                return "read_url"
            return {"action": "create_note", "text": "none", "text_mode": "generate"}

        client = FakeJev(choose, generated=generated)
        controller = self.start(FakePlanner(steps), client)
        with patch.object(controller.web_reader, "read", return_value={"url": url, "content": content}) as reader, \
                patch.object(controller, "_open_note") as opener:
            controller.submit(f"Read {url} and {instruction} Save and open it in Text Editor.")
            self.completed()

        reader.assert_called_once_with(url)
        opener.assert_called_once()
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(len(client.writer_calls), 1)
        self.assertEqual(client.writer_calls[0]["instruction"], instruction)
        self.assertEqual(client.writer_calls[0]["state"]["task_data"],
                         [{"source": "web", "url": url, "content": content}])
        notes = list(Path(self.directory.name).glob("*.md"))
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].read_text(), generated + "\n")
        self.assertEqual([effect["action"] for effect in controller._active.completed],
                         ["read_url", "create_note"])


if __name__ == "__main__":
    unittest.main()
