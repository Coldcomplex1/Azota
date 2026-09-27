#!/usr/bin/env python3
"""Extract visible practice questions and explicitly exposed answer metadata.

This utility follows Azota's normal browser flow, including the deliberate
proctoring confirmation, but never fills answers or submits an attempt.  It
only reports correct answers when the page/API exposes them explicitly.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


DEFAULT_URL = "https://azota.vn/vi/de-thi/lqjgtx"
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_JSON_RESPONSE_BYTES = 2_000_000
MAX_JSON_DEPTH = 12
MAX_JSON_NODES = 20_000

PROMPT_KEYS = (
    "question_text",
    "questionText",
    "question",
    "prompt",
    "content",
    "title",
    "text",
)
QUESTION_NUMBER_KEYS = (
    "question_number",
    "questionNumber",
    "question_no",
    "questionNo",
    "question_index",
    "questionIndex",
    "display_order",
    "displayOrder",
    "order",
    "number",
    "stt",
    "no",
)
QUESTION_ID_KEYS = (
    "question_id",
    "questionId",
    "questionID",
    "uuid",
    "code",
    "_id",
    "id",
)
OPTION_KEYS = (
    "options",
    "choices",
    "answer_options",
    "answerOptions",
    "listAnswers",
    "answers",
)
CORRECT_KEYS = (
    "correct_answer",
    "correctAnswer",
    "right_answer",
    "rightAnswer",
    "answer_key",
    "answerKey",
    "expected_answer",
    "expectedAnswer",
    "official_answer",
    "officialAnswer",
    "solution",
)
SUBMITTED_KEYS = (
    "submitted_answer",
    "submittedAnswer",
    "user_answer",
    "userAnswer",
    "selected_answer",
    "selectedAnswer",
)
TRUTHY_KEYS = ("is_correct", "isCorrect", "correct", "right", "isRight")
SENSITIVE_QUERY_KEYS = re.compile(
    r"(?:pass(word)?|token|auth|authorization|api[_-]?key|secret|code|otp|cookie)",
    re.IGNORECASE,
)
SUBMISSION_MARKERS = re.compile(
    r"(?:/submit(?:[-_/]|$)|submit[-_]?test|/finish(?:[-_/]|$)|complete[-_]?test|"
    r"hand[-_]?in|nop[-_]?bai|end[-_]?test)",
    re.IGNORECASE,
)


class ExtractionError(RuntimeError):
    """Raised when the authorized browser flow cannot be completed safely."""


class SubmissionDetectedError(ExtractionError):
    """Raised if a submission-like request is observed."""


@dataclass
class QuestionRecord:
    question_number: Optional[int] = None
    question_id: Optional[str] = None
    prompt: Optional[str] = None
    options: list[Any] = field(default_factory=list)
    submitted_answer: Any = None
    correct_answer: Any = None
    answer_source: Optional[str] = None
    source_url: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Config:
    url: str
    password: str
    display_name: str
    output_dir: Path
    headless: bool = False
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @property
    def timeout_ms(self) -> int:
        return max(1, int(self.timeout_seconds * 1000))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def clean_text(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return None
    if isinstance(value, bool):
        return None
    text = unescape(str(value))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def scalar_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return value


def first_value(mapping: Mapping[str, Any], aliases: Iterable[str]) -> Any:
    normalized = {normalize_key(str(key)): value for key, value in mapping.items()}
    for alias in aliases:
        candidate = normalized.get(normalize_key(alias))
        if candidate is not None:
            return candidate
    return None


def first_key(mapping: Mapping[str, Any], aliases: Iterable[str]) -> Optional[str]:
    wanted = {normalize_key(alias) for alias in aliases}
    for key in mapping:
        if normalize_key(str(key)) in wanted:
            return str(key)
    return None


def question_number(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def question_id(value: Any) -> Optional[str]:
    if value is None or isinstance(value, (dict, list, tuple, set, bool)):
        return None
    text = str(value).strip()
    return text or None


def _option_label(option: Mapping[str, Any]) -> Any:
    return first_value(option, ("value", "label", "text", "content", "title", "answer", "id"))


def normalize_options(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        normalized: list[Any] = []
        for item in value:
            if isinstance(item, dict):
                label = _option_label(item)
                option: dict[str, Any] = {"value": label if label is not None else item}
                truth = first_value(item, TRUTHY_KEYS)
                if isinstance(truth, bool):
                    option["is_correct"] = truth
                normalized.append(option)
            else:
                normalized.append(item)
        return normalized
    if isinstance(value, dict):
        return [{"value": key, "label": item} for key, item in value.items()]
    return [value]


def correct_from_options(options: Any) -> Any:
    if not isinstance(options, list):
        return None
    correct: list[Any] = []
    for option in options:
        if not isinstance(option, dict):
            continue
        truth = first_value(option, TRUTHY_KEYS)
        if truth is True:
            correct.append(_option_label(option))
    if not correct:
        return None
    return correct[0] if len(correct) == 1 else correct


def explicit_correct_value(mapping: Mapping[str, Any], options: Any) -> Any:
    key = first_key(mapping, CORRECT_KEYS)
    if key is not None:
        value = mapping[key]
        if not isinstance(value, bool):
            return scalar_value(value)
    return correct_from_options(options)


def _looks_like_question(mapping: Mapping[str, Any], prompt: Any, options: Any, correct: Any) -> bool:
    if not prompt:
        return False
    has_number = first_value(mapping, QUESTION_NUMBER_KEYS) is not None
    has_question_key = first_key(mapping, ("question", "prompt", "question_text", "questionText")) is not None
    # Option objects commonly have ``id`` + ``text`` + ``isCorrect``.  An
    # identifier alone must not cause those option objects to be emitted as
    # separate questions.
    return bool(options or correct is not None or has_number or has_question_key)


def record_from_mapping(mapping: Mapping[str, Any], source_url: str, *, allow_orphan: bool = True) -> Optional[QuestionRecord]:
    raw_prompt = first_value(mapping, PROMPT_KEYS)
    prompt = clean_text(raw_prompt)
    options_raw = first_value(mapping, OPTION_KEYS)
    options = normalize_options(options_raw)
    correct = explicit_correct_value(mapping, options_raw)
    submitted = first_value(mapping, SUBMITTED_KEYS)
    identifier = question_id(first_value(mapping, QUESTION_ID_KEYS))
    number = question_number(first_value(mapping, QUESTION_NUMBER_KEYS))

    if not _looks_like_question(mapping, prompt, options_raw, correct):
        if not allow_orphan or correct is None or (identifier is None and number is None):
            return None
        return QuestionRecord(
            question_number=number,
            question_id=identifier,
            correct_answer=correct,
            answer_source="network",
            source_url=source_url,
        )

    return QuestionRecord(
        question_number=number,
        question_id=identifier,
        prompt=prompt,
        options=options,
        submitted_answer=scalar_value(submitted),
        correct_answer=correct,
        answer_source="network" if correct is not None else None,
        source_url=source_url,
    )


def answer_map_records(mapping: Mapping[str, Any], source_url: str) -> list[QuestionRecord]:
    """Flatten explicit ``answerKey: {question_id: answer}`` payloads."""

    records: list[QuestionRecord] = []
    for alias in CORRECT_KEYS:
        key = first_key(mapping, (alias,))
        if key is None or not isinstance(mapping[key], dict):
            continue
        # A question record may itself use a dict-shaped answer.  Only flatten
        # mappings on objects that do not also describe a question.
        if first_value(mapping, PROMPT_KEYS) is not None or first_value(mapping, OPTION_KEYS) is not None:
            continue
        for identifier, answer in mapping[key].items():
            if isinstance(answer, (dict, list)) and not answer:
                continue
            records.append(
                QuestionRecord(
                    question_id=question_id(identifier),
                    correct_answer=answer,
                    answer_source="network",
                    source_url=source_url,
                )
            )
        break
    return records


def iter_mappings(value: Any, *, depth: int = 0, counter: Optional[list[int]] = None) -> Iterable[Mapping[str, Any]]:
    if counter is None:
        counter = [0]
    if depth > MAX_JSON_DEPTH or counter[0] >= MAX_JSON_NODES:
        return
    if isinstance(value, dict):
        counter[0] += 1
        yield value
        for child in value.values():
            yield from iter_mappings(child, depth=depth + 1, counter=counter)
    elif isinstance(value, list):
        for child in value:
            yield from iter_mappings(child, depth=depth + 1, counter=counter)


def extract_payload_records(payload: Any, source_url: str) -> list[QuestionRecord]:
    records: list[QuestionRecord] = []
    seen: set[tuple[Any, ...]] = set()
    for mapping in iter_mappings(payload):
        candidates = answer_map_records(mapping, source_url)
        record = record_from_mapping(mapping, source_url)
        if record is not None:
            candidates.append(record)
        for candidate in candidates:
            key = (
                candidate.question_id,
                candidate.question_number,
                re.sub(r"\s+", " ", candidate.prompt or "").strip().lower(),
                json.dumps(candidate.correct_answer, sort_keys=True, ensure_ascii=False, default=str),
            )
            if key in seen:
                continue
            seen.add(key)
            records.append(candidate)
    return records


def extract_dom_questions(page: Any) -> list[QuestionRecord]:
    """Read visible question/input pairs from the current page in one DOM pass."""

    data = page.evaluate(
        """
        () => {
          const visible = (el) => {
            const style = window.getComputedStyle(el);
            return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetParent !== null;
          };
          const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
          const toItem = (container, index) => {
            const fullText = clean(container.innerText || container.textContent || '');
            const numberMatch = fullText.match(/(?:Câu|Question)\\s*(\\d+)/i);
            const answer = container.querySelector('textarea, input, [contenteditable="true"]');
            const prompt = fullText
              .replace(/(?:Câu|Question)\\s*\\d+/i, '')
              .replace(/Nhập đáp án/gi, '')
              .replace(/Đáp án của bạn:?/gi, '')
              .replace(/Thí sinh chưa nhập thông tin/gi, '')
              .replace(/\\s+/g, ' ')
              .trim();
            return {
              question_number: numberMatch ? Number(numberMatch[1]) : index + 1,
              question_id: answer && (answer.getAttribute('question_id') || answer.getAttribute('question-id')),
              prompt: prompt || null,
              submitted_answer: answer && (answer.value || answer.textContent || null),
              options: []
            };
          };

          // Azota's current full-test view renders one custom element per
          // question. This is more reliable than depending on an input
          // placeholder, which disappears in read-only/expired views.
          let containers = Array.from(document.querySelectorAll('app-question-for-student-at-full-test')).filter(visible);
          if (!containers.length) {
            containers = Array.from(document.querySelectorAll('[class*="question-standalone-content-box"]')).filter(visible);
          }
          if (containers.length) return containers.map(toItem);

          // Fallback for deployments that do not expose the custom element.
          const inputs = Array.from(document.querySelectorAll(
            'input[placeholder="Đáp án của bạn"], textarea[placeholder="Đáp án của bạn"], textarea, input, [contenteditable="true"]'
          )).filter(visible);
          return inputs.map((input, index) => {
            let node = input;
            let container = input;
            for (let depth = 0; node && depth < 10; depth += 1, node = node.parentElement) {
              const text = clean(node.innerText || node.textContent || '');
              if (/(?:Câu|Question)\\s*\\d+/i.test(text) && text.length <= 8000) {
                container = node;
                break;
              }
            }
            return toItem(container, index);
          });
        }
        """
    )
    return [
        QuestionRecord(
            question_number=item.get("question_number"),
            question_id=question_id(item.get("question_id")),
            prompt=clean_text(item.get("prompt")),
            options=item.get("options") or [],
            submitted_answer=clean_text(item.get("submitted_answer")),
            answer_source="dom" if item.get("prompt") else None,
        )
        for item in data or []
        if isinstance(item, dict)
    ]


def record_key(record: QuestionRecord) -> Optional[tuple[str, str]]:
    if record.question_id:
        return ("id", record.question_id)
    if record.prompt:
        return ("prompt", re.sub(r"\s+", " ", record.prompt).strip().lower())
    if record.question_number is not None:
        return ("number", str(record.question_number))
    return None


def merge_records(dom_records: list[QuestionRecord], network_records: list[QuestionRecord]) -> list[QuestionRecord]:
    merged: list[QuestionRecord] = []
    indexes: dict[tuple[str, str], int] = {}

    def add(record: QuestionRecord) -> None:
        key = record_key(record)
        if key is None:
            merged.append(record)
            return
        existing_index = indexes.get(key)
        if existing_index is None:
            indexes[key] = len(merged)
            merged.append(record)
            return
        existing = merged[existing_index]
        for attr in ("question_number", "question_id", "prompt", "options", "submitted_answer", "source_url"):
            value = getattr(record, attr)
            if value not in (None, "", []):
                setattr(existing, attr, value)
        if record.correct_answer is not None:
            existing.correct_answer = record.correct_answer
            existing.answer_source = "network"
        elif existing.answer_source is None and record.answer_source:
            existing.answer_source = record.answer_source

    for record in dom_records:
        add(record)
    for record in network_records:
        add(record)

    merged.sort(key=lambda item: (item.question_number is None, item.question_number or 0, item.prompt or ""))
    return merged


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            query.append((key, "[REDACTED]" if SENSITIVE_QUERY_KEYS.search(key) else value))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    except Exception:
        return "[REDACTED_URL]"


class NetworkCapture:
    def __init__(self) -> None:
        self.payloads: list[tuple[str, Any]] = []
        self.responses: list[dict[str, Any]] = []
        self.requests: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.submission_violations: list[dict[str, Any]] = []

    def on_request(self, request: Any) -> None:
        method = str(request.method).upper()
        url = redact_url(request.url)
        summary = {"method": method, "url": url, "resource_type": request.resource_type}
        self.requests.append(summary)
        if method in {"POST", "PUT", "PATCH", "DELETE"} and SUBMISSION_MARKERS.search(request.url):
            self.submission_violations.append(summary)

    def on_response(self, response: Any) -> None:
        headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
        content_type = headers.get("content-type", "").lower()
        summary = {
            "url": redact_url(response.url),
            "status": response.status,
            "resource_type": response.request.resource_type,
            "content_type": content_type.split(";", 1)[0],
            "json_captured": False,
        }
        if "json" not in content_type and not response.url.lower().split("?", 1)[0].endswith(".json"):
            self.responses.append(summary)
            return
        try:
            body = response.body()
            if len(body) > MAX_JSON_RESPONSE_BYTES:
                summary["skipped"] = "response too large"
            else:
                payload = json.loads(body.decode("utf-8", errors="replace"))
                self.payloads.append((response.url, payload))
                summary["json_captured"] = True
        except Exception as exc:  # response bodies can disappear during navigation
            self.errors.append(f"response parse failed for {redact_url(response.url)}: {type(exc).__name__}")
        self.responses.append(summary)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "responses": self.responses,
            "errors": self.errors,
            "submission_violations": self.submission_violations,
        }


def validate_target_url(url: str) -> None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in {"http", "https"} or not (host == "azota.vn" or host.endswith(".azota.vn")):
        raise ExtractionError("--url must point to azota.vn or a subdomain of azota.vn")


def find_start_button(page: Any, timeout_ms: int) -> Any:
    """Wait for Angular data to render before inspecting the start control."""

    start_button = page.get_by_role("button", name="Bắt đầu thi")
    try:
        start_button.wait_for(state="visible", timeout=timeout_ms)
    except Exception as exc:
        raise ExtractionError(
            "Could not find the 'Bắt đầu thi' button after waiting for the exam page to load"
        ) from exc
    count = start_button.count()
    if count == 0:
        raise ExtractionError("Could not find the 'Bắt đầu thi' button")
    if count > 1:
        raise ExtractionError("Found multiple 'Bắt đầu thi' buttons")
    return start_button


def perform_browser_flow(page: Any, config: Config) -> None:
    page.goto(config.url, wait_until="domcontentloaded", timeout=config.timeout_ms)

    start_button = find_start_button(page, config.timeout_ms)
    start_button.click()

    dialog = page.get_by_role("dialog")
    dialog.wait_for(state="visible", timeout=config.timeout_ms)
    boxes = dialog.get_by_role("textbox")
    count = boxes.count()
    if count < 2:
        raise ExtractionError("The password/display-name dialog did not expose two textboxes")
    boxes.nth(0).fill(config.password)
    boxes.nth(1).fill(config.display_name)

    confirm = dialog.get_by_role("button", name="Xác nhận")
    if confirm.count() != 1:
        raise ExtractionError("Could not identify the access confirmation button")
    confirm.click()

    try:
        page.wait_for_url("**/test/take-test/**", timeout=config.timeout_ms)
    except Exception:
        # Some deployments update the Angular route without a full navigation.
        if "/test/take-test/" not in page.url:
            raise ExtractionError("The practice attempt did not open")

    proctor_text = page.get_by_text(re.compile(r"Giám\s*Sát", re.IGNORECASE))
    if proctor_text.count():
        proctor_dialog = page.get_by_role("dialog")
        if proctor_dialog.count() == 1:
            proctor_confirm = proctor_dialog.get_by_role("button", name="Xác nhận")
            if proctor_confirm.count() == 1:
                proctor_confirm.click()
            else:
                raise ExtractionError("Proctoring dialog did not expose a unique confirmation button")

    try:
        page.wait_for_selector('input[placeholder="Đáp án của bạn"], textarea[placeholder="Đáp án của bạn"]', state="visible", timeout=config.timeout_ms)
    except Exception:
        # A question-only payload may still be captured even when inputs are custom-rendered.
        pass
    try:
        # Give the normal question/config requests a chance to settle without
        # polling or touching any answer controls.
        page.wait_for_load_state("networkidle", timeout=min(config.timeout_ms, 5000))
    except Exception:
        # Persistent sockets/analytics can prevent network-idle on production.
        pass


def write_results(output_dir: Path, records: list[QuestionRecord]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    result_dicts = [record.to_dict() for record in records]
    (output_dir / "results.json").write_text(
        json.dumps(result_dicts, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    fields = [
        "question_number",
        "question_id",
        "prompt",
        "options",
        "submitted_answer",
        "correct_answer",
        "answer_source",
        "source_url",
    ]
    with (output_dir / "results.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in result_dicts:
            row = dict(record)
            for key in ("options", "submitted_answer", "correct_answer"):
                if isinstance(row[key], (dict, list)):
                    row[key] = json.dumps(row[key], ensure_ascii=False, default=str)
            writer.writerow(row)


def run(config: Config) -> int:
    validate_target_url(config.url)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    capture = NetworkCapture()
    diagnostics: dict[str, Any] = {
        "started_at": utc_now(),
        "target_url": redact_url(config.url),
        "headless": config.headless,
        "submission_detected": False,
        "errors": [],
    }

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ExtractionError("Playwright is not installed; run pip install -r requirements.txt") from exc

    records: list[QuestionRecord] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=config.headless)
            context = browser.new_context()
            page = context.new_page()
            page.on("request", capture.on_request)
            page.on("response", capture.on_response)
            try:
                perform_browser_flow(page, config)
                dom_records = extract_dom_questions(page)
                network_records: list[QuestionRecord] = []
                for source_url, payload in capture.payloads:
                    network_records.extend(extract_payload_records(payload, source_url))
                records = merge_records(dom_records, network_records)
                diagnostics["final_url"] = redact_url(page.url)
                diagnostics["dom_question_count"] = len(dom_records)
                diagnostics["network_record_count"] = len(network_records)
            finally:
                context.close()
                browser.close()
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"Browser extraction failed: {type(exc).__name__}: {exc}") from exc
    finally:
        diagnostics["finished_at"] = utc_now()
        diagnostics["record_count"] = len(records)
        diagnostics["answer_count"] = sum(record.correct_answer is not None for record in records)
        diagnostics["submission_detected"] = bool(capture.submission_violations)
        diagnostics["network"] = capture.diagnostics()
        (config.output_dir / "diagnostics.json").write_text(
            json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    if capture.submission_violations:
        raise SubmissionDetectedError("A submission-like request was observed; results were not trusted")
    write_results(config.output_dir, records)
    return 0


def parse_args(argv: Optional[list[str]] = None) -> Config:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.getenv("AZOTA_URL", DEFAULT_URL))
    parser.add_argument("--output-dir", default="./artifacts", type=Path)
    parser.add_argument("--headless", action="store_true", help="Run Chromium headlessly instead of visibly")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="Per-step timeout in seconds")
    args = parser.parse_args(argv)

    password = os.getenv("AZOTA_EXAM_PASSWORD")
    display_name = os.getenv("AZOTA_DISPLAY_NAME")
    if not password:
        parser.error("AZOTA_EXAM_PASSWORD is required")
    if not display_name:
        parser.error("AZOTA_DISPLAY_NAME is required")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    return Config(
        url=args.url,
        password=password,
        display_name=display_name,
        output_dir=args.output_dir,
        headless=args.headless,
        timeout_seconds=args.timeout,
    )


def main(argv: Optional[list[str]] = None) -> int:
    try:
        return run(parse_args(argv))
    except ExtractionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
