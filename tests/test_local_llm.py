"""Source integrity and failure behavior; these do not benchmark model accuracy."""

from decimal import Decimal
from http.client import HTTPException
import json
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from receipt_reader import local_llm
from receipt_reader.local_llm import (
    LocalLlmError, SuggestedItems, suggest_items_locally, validate_suggestions,
)


def proposal(name="QUARTZ PEBBLE", name_lines=None, amount="2.35", line=2, occurrence=1):
    return {
        "name_quote": name, "name_lines": name_lines or [1],
        "amount_text": amount, "amount_line": line, "amount_occurrence": occurrence,
    }


def validate(rows, lines):
    return validate_suggestions(SuggestedItems(items=rows), lines)


def mock_ollama(monkeypatch, items, **reply_overrides):
    requests = []

    def request(path, payload, *, timeout, **kwargs):
        requests.append((path, payload, timeout, kwargs))
        if path == "/api/tags":
            return {"models": [{"name": "qwen3.5:4b", "digest": "test-digest"}]}
        return {
            "done": True, "done_reason": "stop",
            "message": {"content": json.dumps({"items": items})},
            **reply_overrides,
        }

    monkeypatch.setattr(local_llm, "_request_json", request)
    return requests


def test_split_rows_unfamiliar_names_and_exact_decimal_sum():
    rows, rejected = validate(
        [proposal(), proposal("ZINC TOKEN", [3], "0.10", 4)],
        ["QUARTZ PEBBLE", "$2.35 N", "ZINC TOKEN", "0.10"],
    )
    assert not rejected
    assert [item.name for item in rows] == ["QUARTZ PEBBLE", "ZINC TOKEN"]
    assert sum((item.line_total for item in rows), Decimal(0)) == Decimal("2.45")
    assert rows[0].source_text == "1: QUARTZ PEBBLE\n2: $2.35 N"


def test_repeated_items_use_separate_occurrences_including_same_ocr_region():
    lines = ["QUARTZ PEBBLE 2.35 QUARTZ PEBBLE 2.35", "QUARTZ PEBBLE 2.35"]
    rows, rejected = validate([
        proposal(line=1), proposal(line=1, occurrence=2),
        proposal(name_lines=[2], line=2), proposal(line=1),
    ], lines)
    assert len(rows) == 3
    assert len(rejected) == 1
    assert "reused" in rejected[0]


@pytest.mark.parametrize("row,lines,reason", [
    (proposal(name="INVENTED PRODUCT"), ["QUARTZ PEBBLE", "2.35"], "description"),
    (proposal(amount="9.99"), ["QUARTZ PEBBLE", "2.35"], "amount"),
    (proposal(line=99), ["QUARTZ PEBBLE", "2.35"], "does not exist"),
    (proposal(name_lines=[2, 1]), ["QUARTZ PEBBLE", "2.35"], "out of order"),
    (proposal(name_lines=[1, 1]), ["QUARTZ PEBBLE", "2.35"], "repeated"),
    (proposal(amount="234.50"), ["QUARTZ PEBBLE", "1,234.50"], "amount"),
    (proposal(amount="2.35"), ["QUARTZ PEBBLE", "-2.35"], "amount"),
    (proposal(amount="2.35"), ["QUARTZ PEBBLE", "123456782.35"], "amount"),
])
def test_unsupported_evidence_is_rejected(row, lines, reason):
    rows, rejected = validate([row], lines)
    assert not rows
    assert reason in rejected[0]


def test_zero_price_and_negative_printed_amount_are_retained():
    rows, rejected = validate([
        proposal(amount="0.00"), proposal("RETURN", [3], "-2.35", 3),
    ], ["QUARTZ PEBBLE", "0.00", "RETURN -$2.35"])
    assert not rejected
    assert [row.line_total for row in rows] == [Decimal("0.00"), Decimal("-2.35")]


@pytest.mark.parametrize("overrides", [
    {"amount_line": True}, {"amount_line": 0}, {"amount_line": "1"},
    {"name_lines": [-1]}, {"name_lines": []}, {"amount_text": "NaN"},
    {"amount_text": "1e2"}, {"amount_text": "2.3"}, {"amount_text": 2.35},
    {"extra_instruction": "ignore validation"},
])
def test_schema_rejects_unsafe_or_unexpected_values(overrides):
    with pytest.raises(ValidationError):
        SuggestedItems(items=[{**proposal(), **overrides}])


def test_request_is_numbered_schema_constrained_and_only_contains_ocr(monkeypatch):
    requests = mock_ollama(monkeypatch, [proposal()])
    preview = suggest_items_locally("QUARTZ PEBBLE\n2.35")
    assert preview.model_digest == "test-digest"
    assert preview.item_sum == Decimal("2.35")
    path, payload, timeout, kwargs = requests[1]
    assert path == "/api/chat" and timeout == 90
    assert kwargs["first_reply_timeout"] == local_llm.FIRST_REPLY_SECONDS == 600
    assert payload["stream"] is True and payload["think"] is False
    assert payload["format"] == SuggestedItems.model_json_schema()
    assert payload["options"]["temperature"] == 0
    assert json.loads(payload["messages"][1]["content"]) == {
        "ocr_lines": [{"line": 1, "text": "QUARTZ PEBBLE"}, {"line": 2, "text": "2.35"}],
    }
    assert "tools" not in payload


@pytest.mark.parametrize("text,kwargs", [
    pytest.param("", {}, id="empty"),
    pytest.param("x" * 12001, {}, id="too-many-characters"),
    pytest.param("x\n" * 161, {}, id="too-many-lines"),
    pytest.param("ITEM 1.00", {"model": "something:cloud"}, id="cloud-model"),
    pytest.param("ITEM 1.00", {"timeout": 0}, id="timeout-zero"),
    pytest.param("ITEM 1.00", {"timeout": 91}, id="timeout-too-long"),
])
def test_invalid_input_never_contacts_ollama(monkeypatch, text, kwargs):
    request = MagicMock(side_effect=AssertionError("Network must not be used"))
    monkeypatch.setattr(local_llm, "_request_json", request)
    with pytest.raises(LocalLlmError):
        suggest_items_locally(text, **kwargs)
    request.assert_not_called()


@pytest.mark.parametrize("models", [[], [{"name": "qwen3.5:4b", "digest": "x", "remote_host": "cloud"}]])
def test_missing_or_remote_model_never_receives_receipt(monkeypatch, models):
    request = MagicMock(return_value={"models": models})
    monkeypatch.setattr(local_llm, "_request_json", request)
    with pytest.raises(LocalLlmError):
        suggest_items_locally("QUARTZ PEBBLE\n2.35")
    assert request.call_count == 1
    assert request.call_args.args == ("/api/tags", None)


@pytest.mark.parametrize("overrides", [
    {"done": False}, {"done_reason": "length"}, {"message": {"content": "bad json"}},
    {"message": {}}, {"message": None},
])
def test_incomplete_or_malformed_reply_is_recoverable(monkeypatch, overrides):
    mock_ollama(monkeypatch, [], **overrides)
    with pytest.raises(LocalLlmError):
        suggest_items_locally("QUARTZ PEBBLE\n2.35")


@pytest.mark.parametrize("status,body", [
    # Explicit ids keep test names short. Without them, pytest 8 puts the whole
    # 1 MB body into the test name, and Windows then cannot store that name in
    # the PYTEST_CURRENT_TEST environment variable (RL-75).
    pytest.param(302, b"", id="redirect"),
    pytest.param(500, b"PRIVATE RECEIPT TEXT", id="server-error"),
    pytest.param(200, b"invalid json", id="invalid-json"),
    pytest.param(200, b"[]", id="not-an-object"),
    pytest.param(200, b"x" * (local_llm.MAX_RESPONSE_BYTES + 1), id="too-large"),
])
def test_transport_rejects_redirects_errors_and_unbounded_or_invalid_output(monkeypatch, status, body):
    connection = MagicMock()
    connection.getresponse.return_value.status = status
    connection.getresponse.return_value.read.return_value = body
    factory = MagicMock(return_value=connection)
    monkeypatch.setattr(local_llm, "HTTPConnection", factory)
    with pytest.raises(LocalLlmError) as error:
        local_llm._request_json("/api/tags", None, timeout=3)
    factory.assert_called_once_with("127.0.0.1", 11434, timeout=3)
    connection.close.assert_called_once()
    assert "PRIVATE RECEIPT TEXT" not in str(error.value)


@pytest.mark.parametrize("error", [TimeoutError(), ConnectionRefusedError(), HTTPException()])
def test_transport_failure_is_recoverable(monkeypatch, error):
    connection = MagicMock()
    connection.request.side_effect = error
    monkeypatch.setattr(local_llm, "HTTPConnection", MagicMock(return_value=connection))
    with pytest.raises(LocalLlmError):
        local_llm._request_json("/api/chat", {}, timeout=1)
    connection.close.assert_called_once()


@pytest.mark.parametrize("name,line", [
    ("E", "EGGS LG 18CT 4.99"),          # one letter
    ("GG", "EGGS LG 18CT 4.99"),         # inside a word
    ("8C", "EGGS LG 18CT 4.99"),         # inside a word, mostly digits
])
def test_description_must_be_whole_words_with_letters(name, line):
    rows, rejected = validate([proposal(name, [1], "4.99", 1)], [line])
    assert not rows
    assert "description" in rejected[0]


def test_one_description_cannot_claim_two_prices():
    rows, rejected = validate([
        proposal("MILK", [1], "3.49", 1), proposal("MILK", [1], "0.50", 2),
    ], ["MILK GAL 3.49", "COUPON 0.50"])
    assert [row.name for row in rows] == ["MILK"]
    assert "reused a description" in rejected[0]


def test_description_spanning_lines_counts_against_the_same_source():
    rows, rejected = validate([
        proposal("BIRD SEED", [1, 2], "6.99", 3),
        proposal("SEED", [2], "1.00", 4),
    ], ["WILD BIRD", "SEED MIX", "6.99", "1.00"])
    # "SEED" on line 2 was already used by the two-line description.
    assert len(rows) == 1 and "reused a description" in rejected[0]


def test_trailing_minus_is_negative():
    lines = ["COUPON 2.00-"]
    rows, rejected = validate([proposal("COUPON", [1], "-2.00", 1)], lines)
    assert not rejected and rows[0].line_total == Decimal("-2.00")
    assert lines[0][slice(*rows[0].amount_span)] == "2.00-"
    rows, rejected = validate([proposal("COUPON", [1], "2.00", 1)], lines)
    assert not rows and "amount" in rejected[0]


def _streaming_connection(monkeypatch, chunks):
    connection = MagicMock()
    response = connection.getresponse.return_value
    response.status = 200
    response.readline.side_effect = [*chunks, b""]
    monkeypatch.setattr(local_llm, "HTTPConnection", MagicMock(return_value=connection))
    return connection


def test_streamed_chat_is_assembled(monkeypatch):
    _streaming_connection(monkeypatch, [
        b'{"message": {"content": "{\\"items\\""}, "done": false}\n',
        b"\n",
        b'{"message": {"content": ": []}"}, "done": false}\n',
        b'{"message": {"content": ""}, "done": true, "done_reason": "stop"}\n',
    ])
    reply = local_llm._request_json("/api/chat", {"stream": True}, timeout=5)
    assert reply == {"done": True, "done_reason": "stop", "message": {"content": '{"items": []}'}}


@pytest.mark.parametrize("chunks", [
    [b'{"message": {"content": "{"}, "done": false}\n'],          # ends without done
    [b'{"error": "PRIVATE RECEIPT TEXT"}\n'],
    [b"not json\n"],
    [b'{"message": {"content": "x"}, "done": false}\n', b"x" * local_llm.MAX_RESPONSE_BYTES],
])
def test_streamed_chat_failures_are_recoverable(monkeypatch, chunks):
    mock_ollama_stream = _streaming_connection(monkeypatch, chunks)
    try:
        reply = local_llm._request_json("/api/chat", {"stream": True}, timeout=5)
    except LocalLlmError as error:
        assert "PRIVATE RECEIPT TEXT" not in str(error)
    else:
        assert reply["done"] is False
    mock_ollama_stream.close.assert_called_once()


def test_streamed_chat_stops_at_total_deadline(monkeypatch):
    _streaming_connection(monkeypatch, [b'{"message": {"content": "{"}, "done": false}\n'] * 3)
    clock = iter([0, 0, local_llm.MAX_CHAT_SECONDS + 1, local_llm.MAX_CHAT_SECONDS + 2])
    monkeypatch.setattr(local_llm.time, "monotonic", lambda: next(clock))
    with pytest.raises(LocalLlmError, match="longer than"):
        local_llm._request_json("/api/chat", {"stream": True}, timeout=5)


def test_silent_ollama_reports_a_timeout(monkeypatch):
    connection = _streaming_connection(monkeypatch, [])
    connection.getresponse.return_value.readline.side_effect = TimeoutError()
    with pytest.raises(LocalLlmError, match="stopped responding for 5 seconds"):
        local_llm._request_json("/api/chat", {"stream": True}, timeout=5)


def test_slow_start_gets_its_own_long_wait_then_idle_limit_applies(monkeypatch):
    """A slow computer may take minutes before the first piece of the reply."""
    connection = _streaming_connection(monkeypatch, [
        b'{"message": {"content": "{\\"items\\": []}"}, "done": false}\n',
        b'{"message": {"content": ""}, "done": true, "done_reason": "stop"}\n',
    ])
    factory = local_llm.HTTPConnection
    progress = []
    reply = local_llm._request_json(
        "/api/chat", {"stream": True}, timeout=90, first_reply_timeout=600,
        on_progress=progress.append,
    )
    assert reply["done"] is True
    factory.assert_called_once_with("127.0.0.1", 11434, timeout=600)
    connection.sock.settimeout.assert_called_once_with(90)
    assert progress and progress[0].startswith("Model started answering")


def test_no_first_reply_reports_slow_start_not_silence(monkeypatch):
    connection = _streaming_connection(monkeypatch, [])
    connection.getresponse.side_effect = TimeoutError()
    with pytest.raises(LocalLlmError, match="had not started answering after 10 minutes"):
        local_llm._request_json(
            "/api/chat", {"stream": True}, timeout=90, first_reply_timeout=600,
        )


def test_silence_after_first_piece_reports_stall(monkeypatch):
    connection = _streaming_connection(monkeypatch, [])
    connection.getresponse.return_value.readline.side_effect = [
        b'{"message": {"content": "{"}, "done": false}\n', TimeoutError(),
    ]
    with pytest.raises(LocalLlmError, match="stopped responding for 90 seconds"):
        local_llm._request_json(
            "/api/chat", {"stream": True}, timeout=90, first_reply_timeout=600,
        )


# RL-65: suggestions the user may add to the review table, and setup messages.

CAMERA_LINES = [
    "Item Qty Price Total", "Description",
    "601-10171 1 59.90 59.90", "SmallRig Sling Handle for DJI",
    "Subtotal 59.90", "Tax 4.94", "Total 64.84",
]


def camera_suggestion():
    rows, rejected = validate(
        [proposal("SmallRig Sling Handle for DJI", [4], "59.90", 3, 2)], CAMERA_LINES,
    )
    assert not rejected
    return rows[0]


def test_source_backed_suggestion_becomes_a_qty_one_review_row():
    item = camera_suggestion()
    row = local_llm.review_row_from_suggestion(item, CAMERA_LINES)
    assert row == {
        "Item": "SmallRig Sling Handle for DJI", "Product code": None,
        "Category": "Uncategorized", "Qty": 1.0, "Line total": 59.90,
        "Tax marker": None, "Unit price": 59.90, "Discount": None,
        "source_line": 3,
    }


def test_trailing_minus_suggestion_stays_negative_when_added():
    rows, _ = validate([proposal("COUPON", [1], "-2.00", 1)], ["COUPON 2.00-"])
    assert local_llm.review_row_from_suggestion(rows[0], ["COUPON 2.00-"])["Line total"] == -2.0


@pytest.mark.parametrize("changed_lines", [
    # The OCR text changed (another receipt, or a re-read) after the suggestion.
    ["Item Qty Price Total", "Description", "601-10171 1 59.90 59.90",
     "Sling Handle", "Subtotal 59.90"],
    ["Item Qty Price Total", "Description", "601-10171 1 59.90 49.90",
     "SmallRig Sling Handle for DJI"],
    ["SmallRig Sling Handle for DJI"],
])
def test_suggestion_that_no_longer_matches_ocr_is_refused(changed_lines):
    item = camera_suggestion()
    assert not local_llm.is_still_source_backed(item, changed_lines)
    with pytest.raises(LocalLlmError, match="not added"):
        local_llm.review_row_from_suggestion(item, changed_lines)


@pytest.mark.parametrize("item", [
    # Rows that never came from validated OCR evidence.
    local_llm.EvidenceItem("Invented Lens", Decimal("59.90"), (4,), 3, (18, 23), ""),
    local_llm.EvidenceItem("SmallRig Sling Handle for DJI", Decimal("99.99"), (4,), 3, (18, 23), ""),
    local_llm.EvidenceItem("SmallRig Sling Handle for DJI", Decimal("59.90"), (4,), 3, (0, 9), ""),
    local_llm.EvidenceItem("SmallRig Sling Handle for DJI", Decimal("59.90"), (5,), 3, (18, 23), ""),
    local_llm.EvidenceItem("SmallRig Sling Handle for DJI", Decimal("59.90"), (40,), 3, (18, 23), ""),
    local_llm.EvidenceItem("4", Decimal("59.90"), (4,), 3, (18, 23), ""),
])
def test_only_source_backed_rows_can_be_added(item):
    assert camera_suggestion().amount_span == (18, 23)
    assert not local_llm.is_still_source_backed(item, CAMERA_LINES)
    with pytest.raises(LocalLlmError):
        local_llm.review_row_from_suggestion(item, CAMERA_LINES)


def test_ollama_not_running_says_how_to_install_and_start_it(monkeypatch):
    connection = MagicMock()
    connection.request.side_effect = ConnectionRefusedError()
    monkeypatch.setattr(local_llm, "HTTPConnection", MagicMock(return_value=connection))
    with pytest.raises(LocalLlmError) as error:
        suggest_items_locally("QUARTZ PEBBLE\n2.35")
    message = str(error.value)
    assert "not running" in message
    assert "https://ollama.com/download" in message
    assert "ollama pull qwen3.5:4b" in message


def test_missing_model_names_the_pull_command_and_an_installed_alternative(monkeypatch):
    request = MagicMock(return_value={"models": [{"name": "qwen3.5:2b", "digest": "d"}]})
    monkeypatch.setattr(local_llm, "_request_json", request)
    with pytest.raises(LocalLlmError) as error:
        suggest_items_locally("QUARTZ PEBBLE\n2.35", model="qwen3.5:4b")
    message = str(error.value)
    assert "ollama pull qwen3.5:4b" in message
    assert "qwen3.5:2b is installed" in message
    request = MagicMock(return_value={"models": []})
    monkeypatch.setattr(local_llm, "_request_json", request)
    with pytest.raises(LocalLlmError) as error:
        suggest_items_locally("QUARTZ PEBBLE\n2.35", model="qwen3.5:2b")
    assert "ollama pull qwen3.5:2b" in str(error.value)
    assert "is installed" not in str(error.value)


# RL-66: a printed UPC/SKU goes in the Product code column, not the name.

@pytest.mark.parametrize(("lines", "quote", "amount_line", "name", "code"), [
    # Home Depot prints the UPC first.
    (["095624930055 BASE MDF <A> 18.48"], "095624930055 BASE MDF <A>", 1,
     "BASE MDF <A>", "095624930055"),
    # Walmart prints it after the name, sometimes with its F glued on.
    (["GV MILK 007874235187F 3.12"], "GV MILK 007874235187F", 1,
     "GV MILK", "007874235187"),
    # No code: the name is kept exactly.
    (["SmallRig Sling Handle for DJI 59.90"], "SmallRig Sling Handle for DJI", 1,
     "SmallRig Sling Handle for DJI", None),
    # Short numbers and dashed part numbers are part of the name.
    (["601-10171 SLING HANDLE 59.90"], "601-10171 SLING HANDLE", 1,
     "601-10171 SLING HANDLE", None),
    (["2X4 STUD 96 IN 4.25"], "2X4 STUD 96 IN", 1, "2X4 STUD 96 IN", None),
])
def test_added_row_splits_printed_product_code(lines, quote, amount_line, name, code):
    amount = lines[amount_line - 1].rsplit(" ", 1)[1]
    rows, rejected = validate([proposal(quote, [1], amount, amount_line)], lines)
    assert not rejected
    row = local_llm.review_row_from_suggestion(rows[0], lines)
    assert (row["Item"], row["Product code"]) == (name, code)
    assert row["Line total"] == float(amount)
