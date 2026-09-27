import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from azota_extract import (
    NetworkCapture,
    QuestionRecord,
    SUBMISSION_MARKERS,
    extract_dom_questions,
    extract_payload_records,
    find_start_button,
    merge_records,
    redact_url,
    write_results,
)


class FakePage:
    def __init__(self, value):
        self.value = value

    def evaluate(self, _script):
        return self.value


class ExtractionTests(unittest.TestCase):
    def test_dom_questions_extract_prompt_and_empty_answer(self):
        page = FakePage(
            [
                {
                    "question_number": 1,
                    "question_id": "q1",
                    "prompt": "A question from the practice page",
                    "submitted_answer": None,
                    "options": [],
                },
                {
                    "question_number": 2,
                    "prompt": "Second question",
                    "submitted_answer": "typed value",
                    "options": [],
                },
            ]
        )
        records = extract_dom_questions(page)
        self.assertEqual([record.question_number for record in records], [1, 2])
        self.assertEqual(records[0].question_id, "q1")
        self.assertEqual(records[0].correct_answer, None)
        self.assertEqual(records[0].answer_source, "dom")
        self.assertEqual(records[1].submitted_answer, "typed value")

    def test_network_explicit_answer_key(self):
        payload = {
            "questions": [
                {
                    "id": "q1",
                    "number": 1,
                    "questionText": "Choose the answer",
                    "options": ["A", "B"],
                    "correctAnswer": "B",
                }
            ]
        }
        records = extract_payload_records(payload, "https://azota.vn/api/test")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].question_id, "q1")
        self.assertEqual(records[0].correct_answer, "B")
        self.assertEqual(records[0].answer_source, "network")

    def test_option_correct_flags_are_exported(self):
        payload = {
            "question": {
                "id": "q2",
                "content": "Pick one",
                "answers": [
                    {"id": "a", "text": "Wrong", "isCorrect": False},
                    {"id": "b", "text": "Right", "isCorrect": True},
                ],
            }
        }
        records = extract_payload_records(payload, "https://azota.vn/api/test")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].correct_answer, "Right")

    def test_question_only_payload_is_not_answer_inferred(self):
        payload = {"questions": [{"id": "q3", "number": 3, "text": "No key here"}]}
        records = extract_payload_records(payload, "https://azota.vn/api/test")
        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0].correct_answer)
        self.assertIsNone(records[0].answer_source)

    def test_answer_key_mapping_is_flattened(self):
        payload = {"answerKey": {"q1": "B", "q2": "A"}}
        records = extract_payload_records(payload, "https://azota.vn/api/test")
        self.assertEqual({record.question_id for record in records}, {"q1", "q2"})
        self.assertEqual({record.correct_answer for record in records}, {"A", "B"})

    def test_unrelated_json_is_ignored(self):
        records = extract_payload_records({"ok": True, "message": "fine"}, "https://azota.vn/api/config")
        self.assertEqual(records, [])

    def test_network_answer_merges_into_dom_question(self):
        dom = [QuestionRecord(question_number=1, prompt="Same question", answer_source="dom")]
        network = [
            QuestionRecord(
                question_number=1,
                prompt="Same question",
                correct_answer="B",
                answer_source="network",
                source_url="https://azota.vn/api/test",
            )
        ]
        merged = merge_records(dom, network)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].correct_answer, "B")
        self.assertEqual(merged[0].answer_source, "network")

    def test_secret_query_parameters_are_redacted(self):
        url = "https://azota.vn/api/test?token=abc123&foo=bar&password=secret"
        redacted = redact_url(url)
        self.assertNotIn("abc123", redacted)
        self.assertNotIn("secret", redacted)
        self.assertIn("foo=bar", redacted)

    def test_json_and_csv_outputs_match(self):
        records = [
            QuestionRecord(
                question_number=1,
                prompt="Prompt",
                options=["A", "B"],
                correct_answer="B",
                answer_source="network",
            )
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            write_results(output, records)
            data = json.loads((output / "results.json").read_text(encoding="utf-8"))
            with (output / "results.csv").open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(data), len(rows))
            self.assertEqual(rows[0]["correct_answer"], "B")

    def test_dom_extractor_uses_one_page_evaluation(self):
        page = Mock()
        page.evaluate.return_value = []
        extract_dom_questions(page)
        page.evaluate.assert_called_once()

    def test_start_button_waits_for_async_page_render(self):
        page = Mock()
        button = Mock()
        button.count.return_value = 1
        page.get_by_role.return_value = button
        found = find_start_button(page, 3000)
        self.assertIs(found, button)
        button.wait_for.assert_called_once_with(state="visible", timeout=3000)

    def test_submission_guard_detects_submit_like_mutations(self):
        capture = NetworkCapture()
        request = Mock(method="POST", url="https://azota.vn/api/submit-test", resource_type="xhr")
        capture.on_request(request)
        self.assertTrue(SUBMISSION_MARKERS.search(request.url))
        self.assertEqual(len(capture.submission_violations), 1)

    def test_submission_guard_ignores_normal_question_loads(self):
        capture = NetworkCapture()
        request = Mock(method="GET", url="https://azota.vn/api/questions", resource_type="xhr")
        capture.on_request(request)
        self.assertEqual(capture.submission_violations, [])


if __name__ == "__main__":
    unittest.main()
