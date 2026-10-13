"""Provider contract tests: malformed API output must never reach execution."""

import copy
import json
import unittest
from unittest.mock import patch

import httpx

from jev_linux.provider import JevClient, ProviderError, Writer, validate_answers


QUESTIONS = {
    "intent": {"type": "choice", "instructions": "Pick an action.",
               "criteria": {"open_app": "Launch an app.", "none": "Wait."}},
    "ready": {"type": "noul", "instructions": "Can this be executed?"},
    "amount": {"type": "score", "instructions": "How far?", "criteria": ["A little", "A lot"]},
}
ANSWERS = {
    "intent": {"type": "choice", "choice": "open_app", "confidence": 0.9,
               "probabilities": {"open_app": 0.95, "none": 0.05}},
    "ready": {"type": "noul", "noul": 0.99},
    "amount": {"type": "score", "score": 0.5, "confidence": 0.2,
               "probabilities": {"0": 0.5, "1": 0.5}, "legend": {"0": "A little", "1": "A lot"}},
}


class ProviderTests(unittest.TestCase):
    def test_openrouter_request_and_validated_result(self):
        requests = []

        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={
                "answers": ANSWERS,
                "usage": {"input_tokens": 321, "cost": 0.0001, "vendor_debug": "private"},
            })

        http = httpx.Client(transport=httpx.MockTransport(handle))
        client = JevClient({"api_key": "test-key"}, client=http)
        try:
            result = client.decide({"transcript": "open notes"}, QUESTIONS)
            client.decide("open notes", QUESTIONS)
            self.assertEqual(len(requests), 2)
            self.assertEqual(str(requests[0].url), "https://openrouter.ai/api/alpha/decisions")
            self.assertEqual(requests[0].headers["Authorization"], "Bearer test-key")
            self.assertEqual(json.loads(requests[0].content)["model"], "typesafe/jev-1.13")
            self.assertEqual(result["answers"], ANSWERS)
            self.assertGreaterEqual(result["latency_ms"], 0)
            self.assertEqual(result["usage"], {"input_tokens": 321, "cost": 0.0001})
        finally:
            http.close()

    def test_typesafe_uses_its_own_endpoint_and_key(self):
        captured = []

        def handle(request):
            captured.append(request)
            return httpx.Response(200, json={"answers": ANSWERS})

        with httpx.Client(transport=httpx.MockTransport(handle)) as http:
            client = JevClient({"provider": "typesafe", "api_key": "typesafe-test"}, client=http)
            client.decide("open notes", QUESTIONS)
        self.assertEqual(str(captured[0].url), "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(json.loads(captured[0].content)["model"], "jev-1.13.0")

    def test_missing_key_is_helpful(self):
        with patch.dict("os.environ", {}, clear=True):
            with JevClient({}) as client:
                with self.assertRaisesRegex(ProviderError, "OPENROUTER_API_KEY"):
                    client.decide("open notes", QUESTIONS)

    def test_api_errors_do_not_echo_sensitive_response(self):
        for status in (401, 402, 429, 502):
            with self.subTest(status=status):
                transport = httpx.MockTransport(lambda request: httpx.Response(status, json={
                    "error": {"message": "private-key private-transcript"},
                }))
                with httpx.Client(transport=transport) as http:
                    client = JevClient({"api_key": "private-key"}, client=http)
                    with self.assertRaises(ProviderError) as error:
                        client.decide("private-transcript", QUESTIONS)
                    self.assertEqual(error.exception.status_code, status)
                    self.assertNotIn("private-key", str(error.exception))
                    self.assertNotIn("private-transcript", str(error.exception))

    def test_malformed_response_is_not_returned(self):
        for response in ([], {}, {"answers": "open_app"}):
            with self.subTest(response=response):
                with httpx.Client(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json=response)
                )) as http:
                    client = JevClient({"api_key": "test-key"}, client=http)
                    with self.assertRaises(ProviderError):
                        client.decide("open notes", QUESTIONS)

    def test_missing_unknown_or_non_finite_answer_rejected(self):
        mutations = [
            lambda a: a.pop("ready"),
            lambda a: a.update({"extra": {"type": "noul", "noul": 1}}),
            lambda a: a["intent"].update(choice="run_arbitrary_command"),
            lambda a: a["intent"].update(confidence=float("nan")),
            lambda a: a["intent"].update(confidence=True),
            lambda a: a["ready"].update(noul=float("inf")),
            lambda a: a["ready"].update(noul=10 ** 1000),
            lambda a: a["ready"].update(noul=-0.1),
            lambda a: a["amount"].update(score=2),
            lambda a: a["amount"].update(legend={"0": "A little"}),
            lambda a: a["intent"].update(probabilities={"open_app": 0.5}),
            lambda a: a["intent"].update(probabilities={"open_app": 0.1, "none": 0.9}),
            lambda a: a["intent"].update(probabilities={"open_app": 0.5, "none": 0.1}),
            lambda a: a["intent"]["probabilities"].update(none=float("nan")),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                answers = copy.deepcopy(ANSWERS)
                mutate(answers)
                with self.assertRaises(ProviderError):
                    validate_answers(answers, QUESTIONS)

    def test_writer_is_plain_text_without_tools(self):
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [
                {"message": {"content": "Shopping list\nMilk\nBread"}, "finish_reason": "stop"}
            ]})

        with httpx.Client(transport=httpx.MockTransport(handle)) as http:
            writer = Writer({"api_key": "test-key"}, client=http)
            text = writer.generate({"style": "short"}, "Create a shopping list.")
        self.assertEqual(text, "Shopping list\nMilk\nBread")
        self.assertNotIn("tools", requests[0])
        self.assertLessEqual(requests[0]["max_tokens"], 4096)

    def test_writer_never_uses_a_typesafe_key(self):
        with patch.dict("os.environ", {}, clear=True):
            writer = Writer({"provider": "typesafe", "api_key": "typesafe-test"})
            try:
                with self.assertRaisesRegex(ProviderError, "OPENROUTER_API_KEY"):
                    writer.generate_text("Write a short note.")
            finally:
                writer.close()

    def test_writer_rejects_tool_calls_and_truncated_content(self):
        bad_choices = [
            {"message": {"content": "", "tool_calls": [{"name": "shell"}]}},
            {"message": {"content": "partial text"}, "finish_reason": "length"},
        ]
        for choice in bad_choices:
            with self.subTest(choice=choice):
                with httpx.Client(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json={"choices": [choice]})
                )) as http:
                    writer = Writer({"api_key": "test-key"}, client=http)
                    with self.assertRaises(ProviderError):
                        writer.generate_text("Write a short note.")


if __name__ == "__main__":
    unittest.main()
