"""Planning is bounded, structured, and preserves exact user instructions."""

from dataclasses import FrozenInstanceError
import json
import unittest
from unittest.mock import patch

import httpx

from jev_linux.planning import DEFAULT_PLANNER_MODEL, PlanStep, Planner, PlanningError, SUPPORTED_ACTIONS, validate_plan


def step(identifier="s1", instruction="Open Text Editor", kind="action", allowed_actions=None):
    return {"id": identifier, "instruction": instruction,
            "expected_outcome": "Text Editor is visible", "kind": kind,
            "allowed_actions": ["launch_app"] if allowed_actions is None else allowed_actions}


def plan_data(steps=None, unsupported=None):
    return {"goal": "Open notes and create the requested note",
            "steps": [step()] if steps is None else steps,
            "unsupported": [] if unsupported is None else unsupported}


def completion(data, finish_reason="stop", **message_fields):
    return {"choices": [{"message": {"content": json.dumps(data), **message_fields},
                         "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 250, "completion_tokens": 200, "cost": 0.0005}}


class PlanningTests(unittest.TestCase):
    def test_default_model_and_strict_schema_request(self):
        captured = []
        def handler(request):
            captured.append(request)
            return httpx.Response(200, json=completion(plan_data()))
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            planner = Planner({"openrouter_api_key": "test-key"}, http)
            result = planner.plan("Open notes", {"capabilities": ["launch_app"]})
            body = json.loads(captured[0].content)
            self.assertEqual(str(captured[0].url), "https://openrouter.ai/api/v1/chat/completions")
            self.assertEqual(captured[0].headers["Authorization"], "Bearer test-key")
            self.assertEqual(body["model"], DEFAULT_PLANNER_MODEL)
            self.assertEqual(body["temperature"], 0)
            self.assertGreaterEqual(body["max_tokens"], 4096)
            self.assertEqual(body["response_format"]["type"], "json_schema")
            self.assertTrue(body["response_format"]["json_schema"]["strict"])
            step_schema = body["response_format"]["json_schema"]["schema"]["properties"]["steps"]["items"]
            self.assertIn("allowed_actions", step_schema["required"])
            action_schema = step_schema["properties"]["allowed_actions"]
            self.assertEqual(action_schema["type"], "array")
            self.assertEqual(action_schema["items"], {"type": "string"})
            self.assertTrue(body["provider"]["require_parameters"])
            self.assertNotIn("tools", body)
            self.assertEqual(result.command, "Open notes")
            self.assertGreaterEqual(result.latency_ms, 0)
            self.assertEqual(result.usage["cost"], 0.0005)
            with self.assertRaises(FrozenInstanceError):
                result.goal = "different"
            with self.assertRaises(FrozenInstanceError):
                result.steps[0].instruction = "different"
            self.assertEqual(result.as_dict(), plan_data())
            self.assertEqual(result.steps[0].allowed_actions, ("launch_app",))

    def test_allowed_actions_are_bounded_known_unique_action_names(self):
        allowed = ["create_note", "append_note"]
        result = validate_plan(plan_data([step(allowed_actions=allowed)]))
        self.assertEqual(result.steps[0].allowed_actions, tuple(allowed))
        self.assertEqual(result.as_dict()["steps"][0]["allowed_actions"], allowed)
        for malformed in (None, "create_note", {"create_note": True}, [1],
                          ["execute_shell"], ["done"], ["launch_app", "launch_app"]):
            data = plan_data()
            data["steps"][0]["allowed_actions"] = malformed
            with self.subTest(actions=malformed):
                with self.assertRaises(PlanningError):
                    validate_plan(data)

    def test_empty_allowed_actions_preserve_legacy_step_exports(self):
        legacy = PlanStep("s1", "Check the visible note", "The note is visible", "verify")
        self.assertEqual(legacy.allowed_actions, ())
        self.assertNotIn("allowed_actions", legacy.as_dict())
        parsed = validate_plan(plan_data([step(kind="verify", allowed_actions=[])]))
        self.assertEqual(parsed.steps[0].allowed_actions, ())

    def test_read_and_compose_note_keeps_all_requirements_without_browser_steps(self):
        url = "https://example.org/tutorial?topic=basics"
        constraints = ('Create a Markdown study note titled "Beginner guide" from the read page, '
                       'with five bullet points, three exercises, its source URL, and fewer than 200 words.')
        command = f'Read {url} and {constraints} Save and open it in Text Editor.'
        data = plan_data([
            step("read", f"Read {url}", allowed_actions=["read_url"]),
            step("brief", constraints, allowed_actions=["create_note"]),
        ])
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=completion(data))

        context = {"available_actions": {"read_url": "Read a public page", "create_note": "Create a note"}}
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            result = Planner({"api_key": "test-key"}, http).plan(command, context)
        self.assertEqual([item.allowed_actions for item in result.steps], [("read_url",), ("create_note",)])
        self.assertEqual(result.steps[1].instruction, constraints)
        self.assertEqual(json.loads(requests[0]["messages"][1]["content"])["original_command"], command)
        guidance = requests[0]["messages"][0]["content"]
        self.assertIn("does not need a browser", guidance)
        self.assertIn("no standalone generate/compose operation", guidance)

    def test_direct_read_removes_invented_browser_opening_without_dropping_deliverable(self):
        url = "https://example.org/guide"
        compose = step("brief", 'Create a summary note titled "Guide" with three practice tasks',
                       allowed_actions=["create_note"])
        opener = step("browser", f"Open {url} in Brave", allowed_actions=["open_url"])
        reader = step("read", f"Read {url}", allowed_actions=["read_url"])
        command = f'Read {url} and create a summary note titled "Guide" with three practice tasks, then open it in Text Editor.'
        result = validate_plan(plan_data([opener, reader, compose]), command)
        self.assertEqual([item.id for item in result.steps], ["read", "brief"])
        self.assertEqual(result.steps[1].instruction, compose["instruction"])
        self.assertFalse(any("Brave" in item.instruction for item in result.steps))

    def test_direct_read_replaces_an_opening_surrogate_with_the_requested_reader(self):
        url = "https://example.org/path?language=en#lesson"
        command = f"Please read the page at {url} and create a summary note"
        opener = step("source", f"Open {url} in Firefox", allowed_actions=["open_url"])
        compose = step("note", "Create a summary note from the source page", allowed_actions=["create_note"])
        result = validate_plan(plan_data([opener, compose]), command)
        self.assertEqual(result.steps[0].id, "source")
        self.assertEqual(result.steps[0].allowed_actions, ("read_url",))
        self.assertEqual(result.steps[0].instruction, f"Please read the page at {url}")
        self.assertEqual(result.steps[1].instruction, compose["instruction"])

    def test_direct_read_does_not_remove_requested_navigation_or_unavailable_actions(self):
        url = "https://example.org/guide"
        opener = step("browser", f"Open {url} in Chrome", allowed_actions=["open_url"])
        reader = step("read", f"Read {url}", allowed_actions=["read_url"])
        for command in (
                f"Read {url} and then open that page in Chrome",
                f"Read {url} and open it in the browser",
                f"Read {url} and open it",
                f'Create a note saying "Read {url}"',
                f"If I say ready, read {url}"):
            with self.subTest(command=command):
                result = validate_plan(plan_data([opener, reader]), command)
                self.assertEqual(result.steps[0].allowed_actions, ("open_url",))
        result = validate_plan(plan_data([opener]), f"Read {url}",
                               context={"available_actions": {"open_url": "Open a website"}})
        self.assertEqual(result.steps[0].allowed_actions, ("open_url",))

    def test_removing_invented_read_navigation_cannot_drop_a_dictated_payload(self):
        url = "https://example.org/guide"
        command = f'Read {url} and create a note titled "Exact title"'
        opener = step("browser", f'Open {url} in Brave with "Exact title"', allowed_actions=["open_url"])
        reader = step("read", f"Read {url}", allowed_actions=["read_url"])
        compose = step("note", "Create a summary note", allowed_actions=["create_note"])
        with self.assertRaisesRegex(PlanningError, "quoted content"):
            validate_plan(plan_data([opener, reader, compose]), command)

    def test_missing_direct_read_is_inserted_despite_matching_past_memory(self):
        url = "https://example.org/lessons"
        command = f'Read {url} and create a note titled "Lesson notes"'
        compose = step("reading", 'Create a note titled "Lesson notes" from the source page',
                       allowed_actions=["create_note"])
        history = {"memory": {"recent_tasks": [{"command": command, "actions": [
            {"action": "read_url", "url": url}, {"action": "create_note", "note_path": "/tmp/old-note.md"}]}]}}
        result = validate_plan(plan_data([compose]), command, context=history)
        self.assertEqual([item.allowed_actions for item in result.steps], [("read_url",), ("create_note",)])
        self.assertEqual(result.steps[0].instruction, f"Read {url}")
        self.assertNotEqual(result.steps[0].id, result.steps[1].id)
        self.assertEqual(result.steps[1].instruction, compose["instruction"])

    def test_direct_read_is_not_repeated_after_exact_current_task_effect(self):
        url = "https://example.org/lessons?lesson=2"
        command = f"Read {url} and create a summary note"
        compose = step("note", "Create a summary note from the read source", allowed_actions=["create_note"])
        for effect_url, performed in ((url, True), ("https://example.org/lessons?lesson=1", False)):
            for key in ("completed_actions", "completed_steps"):
                with self.subTest(effect_url=effect_url, key=key):
                    evidence = {key: [{"action": "read_url", "arguments": {"url": effect_url}}]}
                    result = validate_plan(plan_data([compose]), command, context=evidence)
                    self.assertEqual(len(result.steps), 1 if performed else 2)
                    result = validate_plan(plan_data([compose]), command, feedback=evidence)
                    self.assertEqual(len(result.steps), 1 if performed else 2)

    def test_fresh_read_and_create_cannot_be_replaced_by_historical_verification(self):
        url = "https://example.org/lessons"
        command = f"Read {url} and create a beginner study brief from that page"
        verify = step("old", "The requested task was already completed in a previous interaction",
                      kind="verify", allowed_actions=[])
        context = {"memory": {"recent_tasks": [{"command": command, "actions": [
            {"action": "read_url", "url": url}, {"action": "create_note", "note_path": "/tmp/old.md"}]}]},
                   "interaction_context": {"note_path": "/tmp/old.md"}}
        with self.assertRaisesRegex(PlanningError, "fresh requested action"):
            validate_plan(plan_data([verify]), command, context=context)
        # Reading in the current task still leaves its requested NEW note to do.
        context["completed_actions"] = [{"action": "read_url", "arguments": {"url": url}}]
        with self.assertRaisesRegex(PlanningError, "fresh requested action"):
            validate_plan(plan_data([verify]), command, context=context)
        context["completed_actions"].append({"action": "create_note", "arguments": {"note_path": "/tmp/current.md"}})
        result = validate_plan(plan_data([verify]), command, context=context)
        self.assertEqual(result.steps[0].kind, "verify")

    def test_history_based_verification_retry_keeps_memory_and_full_command(self):
        url = "https://example.org/lessons"
        command = f'Read {url} and create a study brief titled "Weekend study" with five bullets'
        context = {"memory": {"recent_tasks": [{"command": command, "status": "completed"}]}}
        invalid = plan_data([step("old", "The study brief already exists from a previous task",
                                  kind="verify", allowed_actions=[])])
        valid = plan_data([step("read", f"Read {url}", allowed_actions=["read_url"]),
                           step("write", 'Create a study brief titled "Weekend study" with five bullets',
                                allowed_actions=["create_note"])])
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=completion(invalid if len(requests) == 1 else valid))

        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            result = Planner({"api_key": "test-key"}, http).plan(command, context)
        self.assertEqual(len(requests), 2)
        for request in requests:
            payload = json.loads(request["messages"][1]["content"])
            self.assertEqual(payload["original_command"], command)
            self.assertEqual(payload["context"], context)
        correction = json.loads(requests[1]["messages"][1]["content"])["feedback"]
        self.assertIn("historical result", correction["planning_correction"])
        self.assertEqual([item.allowed_actions for item in result.steps], [("read_url",), ("create_note",)])
        self.assertEqual(result.usage["cost"], 0.001)

    def test_repeated_history_verification_is_rejected_after_one_correction(self):
        command = "Create a new study brief"
        invalid = plan_data([step("old", "The old brief already exists", kind="verify", allowed_actions=[])])
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=completion(invalid))

        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            with self.assertRaisesRegex(PlanningError, "fresh requested action"):
                Planner({"api_key": "test-key"}, http).plan(command, {"memory": {"recent_tasks": []}})
        self.assertEqual(len(requests), 2)

    def test_fresh_action_guard_preserves_recall_conditional_and_unsupported_plans(self):
        verify = step("check", "Verify the requested existing brief", kind="verify", allowed_actions=[])
        for command in ("Recall my previous study brief", "Reuse the previous brief",
                        "Reopen the study brief", "If I say ready, create a new study brief",
                        "Check whether the previous note exists"):
            with self.subTest(command=command):
                result = validate_plan(plan_data([verify]), command)
                self.assertEqual(result.steps[0].kind, "verify")
        result = validate_plan(plan_data([verify], ["The requested note creation is unavailable"]),
                               "Create a new study brief")
        self.assertTrue(result.unsupported)

    def test_standalone_generated_append_section_is_not_an_executable_plan(self):
        command = 'Read that note and append a "Next actions" section with a two-week plan. Preserve every existing word.'
        reader = step("read", "Read the remembered note", allowed_actions=["read_note"])
        appendix = step("append", "Append the generated section to the same note", allowed_actions=["append_note"])
        for allowed in ([], ["read_note"], ["create_note"]):
            with self.subTest(allowed=allowed):
                generation = step("generate", 'Generate a "Next actions" section with a two-week plan',
                                  allowed_actions=allowed)
                with self.assertRaisesRegex(PlanningError, "standalone generation"):
                    validate_plan(plan_data([reader, generation, appendix]), command)

    def test_executable_composition_accepts_direct_writing_and_requested_separate_artifacts(self):
        cases = [
            ("Compose a new summary note", [step("note", "Compose a new summary note", allowed_actions=["create_note"])]),
            ("Compose and append a new section", [step("append", "Compose and append a new section", allowed_actions=["append_note"])]),
            ("Write a summary into the visible editor", [step("write", "Draft a summary for the visible editor", allowed_actions=["write_text"])]),
            ("Generate a new note and append its text to the previous note", [
                step("draft", "Generate a new note", allowed_actions=["create_note"]),
                step("append", "Append the generated note to the previous note", allowed_actions=["append_note"])]),
        ]
        for command, steps in cases:
            with self.subTest(command=command):
                result = validate_plan(plan_data(steps), command)
                self.assertEqual(len(result.steps), len(steps))
        # Legacy missing-action instructions remain accepted for normal work.
        legacy = step("append", "Append the requested exact words")
        legacy.pop("allowed_actions")
        self.assertEqual(validate_plan(plan_data([legacy])).steps[0].allowed_actions, ())

    def test_generation_correction_keeps_all_append_constraints_and_current_read_evidence(self):
        command = ('Read that brief and append a "Next actions" section with a two-week practice plan. '
                   'Base it on the exercises, preserve all existing content, and reopen the same note.')
        appendix_instruction = ('Append a "Next actions" section with a two-week practice plan to the same note. '
                                'Compose it from the exercises read in the brief and preserve all existing content.')
        context = {"memory": {"recent_tasks": [{"actions": [{"action": "create_note", "note_path": "/tmp/brief.md"}]}]},
                   "completed_actions": [{"action": "read_note", "arguments": {"note_path": "/tmp/brief.md"}}]}
        invalid = plan_data([
            step("read", "Read the existing brief", allowed_actions=["read_note"]),
            step("generate", 'Generate a "Next actions" section with a two-week practice plan from its exercises',
                 allowed_actions=[]),
            step("append", "Append the generated section and preserve existing content", allowed_actions=["append_note"]),
            step("open", "Reopen the same note", allowed_actions=["open_note"]),
        ])
        valid = plan_data([step("append", appendix_instruction, allowed_actions=["append_note"]),
                           step("open", "Reopen the same note", allowed_actions=["open_note"])])
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=completion(invalid if len(requests) == 1 else valid))

        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            result = Planner({"api_key": "test-key"}, http).plan(command, context)
        self.assertEqual(len(requests), 2)
        for request in requests:
            payload = json.loads(request["messages"][1]["content"])
            self.assertEqual(payload["original_command"], command)
            self.assertEqual(payload["context"], context)
        correction = json.loads(requests[1]["messages"][1]["content"])["feedback"]["planning_correction"]
        self.assertIn("standalone generation task", correction)
        self.assertIn("ALL composition constraints", correction)
        self.assertEqual(result.steps[0].instruction, appendix_instruction)
        self.assertEqual([item.allowed_actions for item in result.steps], [("append_note",), ("open_note",)])

    def test_repeated_standalone_generation_is_rejected_after_one_correction(self):
        invalid = plan_data([step("generate", "Generate a concise summary", allowed_actions=[])])
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=completion(invalid))

        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            with self.assertRaisesRegex(PlanningError, "standalone generation"):
                Planner({"api_key": "test-key"}, http).plan("Append a concise summary to the same note")
        self.assertEqual(len(requests), 2)

    def test_quoted_clarification_payload_must_survive_the_revised_plan(self):
        payload = "teh  literal\nsecond line"
        answer = f'Make it say "{payload}" exactly.'
        context = {"clarification_answers": [{"question": "What should the note contain?", "answer": answer}]}
        captured = []
        for preserved in (True, False):
            instruction = f'Create a note containing "{payload}"' if preserved else "Create a note containing the literal"
            data = plan_data([step(instruction=instruction, allowed_actions=["create_note"])])

            def handler(request):
                captured.append(json.loads(request.content))
                return httpx.Response(200, json=completion(data))

            with self.subTest(preserved=preserved):
                with httpx.Client(transport=httpx.MockTransport(handler)) as http:
                    planner = Planner({"api_key": "test-key"}, http)
                    if preserved:
                        result = planner.plan("Create a note", context)
                        self.assertIn(payload, result.steps[0].instruction)
                        self.assertEqual(result.command, "Create a note")
                    else:
                        with self.assertRaisesRegex(PlanningError, "quoted content"):
                            planner.plan("Create a note", context)
        self.assertEqual(json.loads(captured[0]["messages"][1]["content"])["context"], context)

    def test_long_command_is_sent_in_full_without_payload_changes(self):
        payload = "Exact spelling: teh  note\n" + "long dictated content " * 700
        command = f'Open notes and create a note containing "{payload}", then open https://example.com.'
        expected = plan_data([step("s1", f'Create a note containing "{payload}"'),
                              step("s2", "Open https://example.com")])
        captured = []
        def handler(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json=completion(expected))
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            result = Planner({"api_key": "test-key"}, http).plan(command)
        sent = json.loads(captured[0]["messages"][1]["content"])
        self.assertEqual(sent["original_command"], command)
        self.assertEqual(result.command, command)
        self.assertIn(payload, result.steps[0].instruction)
        self.assertGreater(captured[0]["max_tokens"], 8192)

    def test_replan_sends_feedback_and_does_not_require_repeating_completed_literal(self):
        command = 'Create a note saying "Milk and bread", then open the browser.'
        feedback = {"completed_steps": [{"action": "create_note", "text": "Milk and bread"}],
                    "failure": "The browser did not appear"}
        captured = []
        def handler(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json=completion(plan_data([step("s2", "Open the browser")])))
        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            result = Planner({"api_key": "test-key"}, http).replan(command, {"capabilities": ["launch_app"]}, feedback)
        sent = json.loads(captured[0]["messages"][1]["content"])
        self.assertEqual(sent["feedback"], feedback)
        self.assertEqual(result.steps[0].id, "s2")
        self.assertNotIn("Create", result.steps[0].instruction)

    def test_visible_youtube_focus_does_not_replace_requested_opening(self):
        command = 'Open YouTube in Google Chrome. Then type "Hello" in the YouTube search bar.'
        focus = step("s1", "Focus the Google Chrome window that has YouTube open.",
                     allowed_actions=["click"])
        typing = step("s2", 'Type "Hello" into the YouTube search bar.', allowed_actions=["type_text"])
        context = {"desktop": {"active_window": "YouTube — Google Chrome"},
                   "memory": {"recent_tasks": [{"actions": [{"action": "open_url",
                               "url": "https://youtube.com", "browser": "google-chrome.desktop"}]}]}}
        with httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=completion(plan_data([focus, typing]))))) as http:
            result = Planner({"api_key": "test-key"}, http).plan(command, context)
        self.assertEqual(len(result.steps), 2)
        self.assertEqual(result.steps[0].instruction, "Open YouTube in Google Chrome.")
        self.assertEqual(result.steps[0].allowed_actions, ("open_url",))
        self.assertEqual(result.steps[0].id, "s1")
        self.assertEqual(result.steps[1].instruction, typing["instruction"])
        self.assertEqual(result.steps[1].allowed_actions, ("type_text",))

    def test_omitted_opening_is_inserted_without_discarding_later_work(self):
        command = 'Open https://example.com/a?x=1 in Firefox, then type "Hello" in the search bar.'
        typing = step("opening", 'Type "Hello" in the search bar', allowed_actions=["type_text"])
        result = validate_plan(plan_data([typing]), command)
        self.assertEqual(len(result.steps), 2)
        self.assertEqual(result.steps[0].allowed_actions, ("open_url",))
        self.assertIn("https://example.com/a?x=1 in Firefox", result.steps[0].instruction)
        self.assertNotEqual(result.steps[0].id, result.steps[1].id)
        self.assertEqual(result.steps[1].instruction, typing["instruction"])

    def test_pure_website_opener_preserves_requested_browser_and_order(self):
        command = 'Open YouTube in Google Chrome. Then type "Hello" in its search bar.'
        typing = step("s1", 'Type "Hello" in its search bar', allowed_actions=["type_text"])
        for instruction in ("Open YouTube", "Open https://www.youtube.com/ in Firefox"):
            with self.subTest(instruction=instruction):
                opener = step("s2", instruction, allowed_actions=["open_url"])
                result = validate_plan(plan_data([typing, opener]), command)
                self.assertEqual([item.id for item in result.steps], ["s2", "s1"])
                self.assertEqual(result.steps[0].instruction, "Open YouTube in Google Chrome.")

    def test_completed_opening_requires_current_task_exact_url_and_browser(self):
        command = 'Open YouTube in Google Chrome. Then type "Hello" in its search bar.'
        typing = step(instruction='Type "Hello" in its search bar', allowed_actions=["type_text"])
        for url, browser, completed in (
                ("https://www.youtube.com/", "google-chrome.desktop", True),
                ("https://youtube.com/watch?v=123", "google-chrome.desktop", False),
                ("https://youtube.com/?q=hello", "google-chrome.desktop", False),
                ("http://youtube.com/", "google-chrome.desktop", False),
                ("https://youtube.com", "firefox.desktop", False),
                ("https://youtube.com", None, False)):
            with self.subTest(url=url, browser=browser):
                effects = {"completed_actions": [{"action": "open_url", "arguments": {"url": url, "browser": browser}}]}
                result = validate_plan(plan_data([typing]), command, context=effects)
                self.assertEqual(len(result.steps), 1 if completed else 2)
                result = validate_plan(plan_data([typing]), command, feedback=effects)
                self.assertEqual(len(result.steps), 1 if completed else 2)
        result = validate_plan(plan_data([typing]), command,
                               context={"completed_steps": ["Open YouTube in Google Chrome"]},
                               completed_text="Open YouTube in Google Chrome")
        self.assertEqual(len(result.steps), 2)

    def test_combined_opening_cannot_silently_omit_or_switch_named_browser(self):
        command = 'Open YouTube in Google Chrome. Then type "Hello" in its search bar.'
        for instruction in ('Open YouTube and type "Hello" in its search bar',
                            'Open YouTube in Firefox and type "Hello" in its search bar'):
            combined = step(instruction=instruction, allowed_actions=["open_url", "type_text"])
            with self.subTest(instruction=instruction):
                with self.assertRaisesRegex(PlanningError, "browser"):
                    validate_plan(plan_data([combined]), command)
        good = step(instruction='Open YouTube in Chrome and type "Hello" in its search bar',
                    allowed_actions=["open_url", "type_text"])
        self.assertEqual(validate_plan(plan_data([good]), command).steps[0].instruction, good["instruction"])

    def test_conditional_and_dictated_opening_words_are_not_repaired(self):
        commands = ('If Chrome is available, open YouTube.',
                    'Open YouTube in Chrome if it is available.',
                    'Open YouTube in Chrome if I say ready.',
                    'Open YouTube in Chrome provided I say ready.',
                    'Open notes about YouTube.',
                    'Create a note saying "Open YouTube in Chrome".',
                    'Open YouTube in Chrome and watch a video.')
        for command in commands:
            with self.subTest(command=command):
                item = step(instruction=command, allowed_actions=["create_note"])
                result = validate_plan(plan_data([item]), command)
                self.assertEqual(len(result.steps), 1)
                self.assertEqual(result.steps[0].instruction, command)

    def test_cli_list_and_gui_mapping_of_installed_browsers_are_supported(self):
        browser = {"id": "google-chrome.desktop", "name": "Google Chrome", "browser": True, "aliases": ["Chrome"]}
        command = 'Open YouTube in Google Chrome. Then type "Hello".'
        data = plan_data([step(instruction='Type "Hello"', allowed_actions=["type_text"])])
        for installed in ([browser], {browser["id"]: browser}):
            with self.subTest(installed_type=type(installed)):
                result = validate_plan(data, command, context={"installed_apps": installed})
                self.assertEqual(result.steps[0].allowed_actions, ("open_url",))

    def test_replacing_focus_surrogate_cannot_discard_dictated_literal(self):
        command = 'Open YouTube in Chrome. Then type "Hello" in the search bar.'
        focus = step(instruction='Focus the Chrome window with YouTube and "Hello"', allowed_actions=["click"])
        with self.assertRaisesRegex(PlanningError, "quoted content"):
            validate_plan(plan_data([focus]), command)

    def test_synthesized_opening_outcome_stays_bounded_for_long_urls(self):
        command = 'Open https://example.com/' + 'a' * 2200 + '. Then type "Hello".'
        data = plan_data([step(instruction='Type "Hello"', allowed_actions=["type_text"])])
        result = validate_plan(data, command)
        self.assertEqual(len(result.steps), 2)
        self.assertLess(len(result.steps[0].expected_outcome), 2000)
        self.assertIn('a' * 2200, result.steps[0].instruction)

    def test_missing_opening_does_not_overflow_plan_or_invent_unavailable_capability(self):
        items = [step(str(index), 'Type "Hello"', allowed_actions=["type_text"]) for index in range(30)]
        command = 'Open YouTube. Then type "Hello".'
        with self.assertRaisesRegex(PlanningError, "step limit"):
            validate_plan(plan_data(items), command)
        result = validate_plan(plan_data(items), command, context={"available_actions": {"type_text": "Type"}})
        self.assertEqual(len(result.steps), 30)
        result = validate_plan(plan_data([], ["Opening websites is unavailable"]), "Open YouTube")
        self.assertEqual(result.steps, ())

    def test_missing_or_changed_quoted_payload_is_rejected(self):
        for command in ('Type "teh  example"', "Type ‘teh example’", "Type 'teh example'"):
            with self.subTest(command=command):
                with self.assertRaisesRegex(PlanningError, "quoted content"):
                    validate_plan(plan_data([step(instruction="Type the example")]), command)
        with self.assertRaisesRegex(PlanningError, "quoted content"):
            validate_plan(plan_data([step(instruction="Type hello")]), 'Type “hello  world”')

    def test_unknown_fields_duplicate_ids_and_oversized_plans_are_rejected(self):
        candidates = []
        unknown = plan_data(); unknown["code"] = "arbitrary provider program"; candidates.append(unknown)
        extra_step = step(); extra_step["tool"] = "shell"; candidates.append(plan_data([extra_step]))
        candidates.append(plan_data([step(), step()]))
        candidates.append(plan_data([step(str(index)) for index in range(31)]))
        candidates.append(plan_data([step(instruction="x" * 20001)]))
        candidates.append(plan_data([step(kind="execute_code")]))
        candidates.append(plan_data([], []))
        candidates.append({"goal": "Task", "steps": "run it", "unsupported": []})
        for candidate in candidates:
            with self.subTest(candidate_type=type(candidate)):
                with self.assertRaises(PlanningError):
                    validate_plan(candidate)

    def test_unsupported_requirements_are_visible_and_not_pretended_completed(self):
        data = plan_data([step("s1", "Open the email app")], ["Sending the email is not supported by available controls."])
        result = validate_plan(data, "Open email and send a message")
        self.assertEqual(len(result.steps), 1)
        self.assertIn("not supported", result.unsupported[0])
        self.assertEqual(result.as_dict()["unsupported"], data["unsupported"])
        wholly_unsupported = validate_plan(plan_data([], ["Installing applications is unavailable."]))
        self.assertEqual(wholly_unsupported.steps, ())
        named_unsupported = validate_plan(plan_data([], ["Exporting Budget2026.pdf is unavailable."]),
                                          'Export "Budget2026.pdf"')
        self.assertIn("Budget2026.pdf", named_unsupported.unsupported[0])

    def test_truncated_tool_call_and_non_json_responses_are_rejected(self):
        responses = [
            completion(plan_data(), finish_reason="length"),
            completion(plan_data(), tool_calls=[{"type": "function", "function": {"name": "shell"}}]),
            {"choices": [{"message": {"content": "```json\n{}\n```"}, "finish_reason": "stop"}]},
            {"choices": [{"message": {"content": '{"goal":"first","goal":"second","steps":[],"unsupported":[]}'}}]},
            {"choices": [{"message": {"content": '{"goal":NaN,"steps":[],"unsupported":[]}'}}]},
            {"choices": []},
        ]
        for response in responses:
            with self.subTest(response=response):
                with httpx.Client(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=response)
                )) as http:
                    with self.assertRaises(PlanningError):
                        Planner({"api_key": "test-key"}, http).plan("Open notes")

    def test_errors_do_not_echo_vendor_response_key_or_instruction(self):
        for status in (401, 402, 429, 400):
            with self.subTest(status=status):
                with httpx.Client(transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, json={"error": {"message": "private-key private-command"}})
                )) as http:
                    with self.assertRaises(PlanningError) as error:
                        Planner({"api_key": "private-key"}, http).plan("private-command")
                self.assertEqual(error.exception.status_code, status)
                self.assertNotIn("private-key", str(error.exception))
                self.assertNotIn("private-command", str(error.exception))

    def test_can_start_without_a_key_but_does_not_use_typesafe_key(self):
        with patch.dict("os.environ", {}, clear=True):
            with Planner({"provider": "typesafe", "api_key": "typesafe-secret"}) as planner:
                with self.assertRaisesRegex(PlanningError, "OPENROUTER_API_KEY"):
                    planner.plan("Open notes")

    def test_invalid_or_oversized_inputs_are_rejected_without_request(self):
        requests = []
        with httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request))) as http:
            planner = Planner({"api_key": "test-key"}, http)
            with self.assertRaisesRegex(PlanningError, "20,000"):
                planner.plan("x" * 20001)
            with self.assertRaises(PlanningError):
                planner.plan("Open notes", {"value": float("nan")})
            with self.assertRaises(PlanningError):
                planner.plan("Open notes", {"value": object()})
            with self.assertRaises(PlanningError):
                planner.plan("Open notes", {"value": "x" * 250001})
        self.assertEqual(requests, [])

    def test_large_completed_task_context_is_preserved_within_the_new_bound(self):
        context = {"completed_actions": [{"text": "exact prior content " * 8000}]}
        captured = []

        def handler(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json=completion(plan_data()))

        with httpx.Client(transport=httpx.MockTransport(handler)) as http:
            result = Planner({"api_key": "test-key"}, http).plan("Open notes", context)
        self.assertEqual(result.command, "Open notes")
        sent = json.loads(captured[0]["messages"][1]["content"])
        self.assertEqual(sent["context"], context)

    def test_borrowed_http_client_lifetime_and_closed_planner(self):
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=completion(plan_data())))) as http:
            planner = Planner({"api_key": "test-key"}, http)
            planner.close(); planner.close()
            self.assertFalse(http.is_closed)
            with self.assertRaisesRegex(PlanningError, "closed"):
                planner.plan("Open notes")


if __name__ == "__main__":
    unittest.main()
