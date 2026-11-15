"""The optional second opinion never mutates the normal receipt workflow."""

import dataclasses
from decimal import Decimal
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from receipt_reader import local_llm
from receipt_reader.models import ReceiptExtraction, ReceiptItem


def reviewed_receipt():
    return ReceiptExtraction(
        store_name="Synthetic Market", transaction_date=None, currency="USD",
        items=[ReceiptItem(name="QUARTZ PEBBLE", quantity=1, unit_price=2.35,
                           discount=None, line_total=2.35)],
        subtotal=2.35, tax=0, total=2.35, confidence=0.8, notes=[],
    ).model_dump()


def active_app(tmp_path, monkeypatch):
    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=30).run()
    app.session_state["active_receipt"] = reviewed_receipt()
    app.session_state["receipt_version"] = 1
    app.session_state["active_method"] = "local"
    app.session_state["ocr_text"] = "QUARTZ PEBBLE\n2.35"
    return app


def test_opt_in_comparison_keeps_receipt_and_review_edits_unchanged(tmp_path, monkeypatch):
    requests = []

    def request(path, payload, *, timeout, **_):
        requests.append(path)
        if path == "/api/tags":
            return {"models": [{"name": "qwen3.5:4b", "digest": "test-digest"}]}
        return {"done": True, "message": {"content": '''{"items":[{
            "name_quote":"QUARTZ PEBBLE", "name_lines":[1],
            "amount_text":"2.35", "amount_line":2, "amount_occurrence":1
        }]}'''}}

    monkeypatch.setattr(local_llm, "_request_json", request)
    app = active_app(tmp_path, monkeypatch).run()
    assert not app.exception
    assert requests == []
    # Simulate an already edited review grid, which must survive model inference.
    app.session_state["items_1_0"] = {
        "edited_rows": {0: {"Item": "Reviewed name"}}, "added_rows": [], "deleted_rows": [],
    }
    next(button for button in app.button if button.label == "Get local AI second opinion").click().run()
    assert not app.exception
    assert requests == ["/api/tags", "/api/chat"]
    assert app.session_state["active_receipt"] == reviewed_receipt()
    assert app.session_state["items_1_0"]["edited_rows"][0]["Item"] == "Reviewed name"
    assert any("Proposed item" in frame.value.columns for frame in app.dataframe)
    assert app.session_state["local_llm_preview"][1].model_digest == "test-digest"
    app.run()
    assert requests == ["/api/tags", "/api/chat"]  # No network on rerender.
    app.session_state["ocr_text"] = "A DIFFERENT RECEIPT\n1.00"
    app.run()
    assert not any("Proposed item" in frame.value.columns for frame in app.dataframe)


def test_service_failure_keeps_normal_editor_and_save_controls(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise local_llm.LocalLlmError("Local AI unavailable. Your receipt has not changed.")

    monkeypatch.setattr(local_llm, "_request_json", unavailable)
    app = active_app(tmp_path, monkeypatch).run()
    next(button for button in app.button if button.label == "Get local AI second opinion").click().run()
    assert not app.exception
    assert app.session_state["active_receipt"] == reviewed_receipt()
    assert any("Local AI unavailable" in warning.value for warning in app.warning)
    assert any("Item" in frame.value.columns for frame in app.dataframe)
    assert any("Save" in button.label or "save" in button.label for button in app.button)


def test_no_ai_control_for_manual_receipt_without_ocr(tmp_path, monkeypatch):
    app = active_app(tmp_path, monkeypatch)
    app.session_state["ocr_text"] = ""
    app.run()
    assert not app.exception
    assert not any(button.label == "Get local AI second opinion" for button in app.button)


# RL-65: the user can add a source-backed suggestion to the review table.

CAMERA_OCR = "\n".join([
    "Item Qty Price Total", "Description",
    "601-10171 1 59.90 59.90", "SmallRig Sling Handle for DJI",
    "Subtotal 59.90", "Tax 4.94", "Total 64.84",
])


def mock_model(monkeypatch, items):
    def request(path, payload, *, timeout, **_):
        if path == "/api/tags":
            return {"models": [{"name": "qwen3.5:4b", "digest": "test-digest"}]}
        return {"done": True, "message": {"content": json.dumps({"items": items})}}

    monkeypatch.setattr(local_llm, "_request_json", request)


def zero_item_app(tmp_path, monkeypatch, ocr_text=CAMERA_OCR, items=(), decisions=()):
    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=30).run()
    app.session_state["active_receipt"] = ReceiptExtraction(
        store_name="Synthetic Camera Shop", transaction_date=None, currency="USD",
        items=list(items), subtotal=59.90, tax=4.94, total=64.84,
        confidence=0.8, notes=[],
    ).model_dump()
    app.session_state["receipt_version"] = 1
    app.session_state["active_method"] = "local"
    app.session_state["ocr_text"] = ocr_text
    app.session_state["line_decisions"] = list(decisions)
    return app.run()


def click(app, label_start):
    next(b for b in app.button if b.label.startswith(label_start)).click().run()
    assert not app.exception


def add_buttons(app):
    return [b.label for b in app.button if b.label.startswith("Add to review table")]


def test_suggested_row_is_added_to_review_table_but_not_saved(tmp_path, monkeypatch):
    mock_model(monkeypatch, [{
        "name_quote": "SmallRig Sling Handle for DJI", "name_lines": [4],
        "amount_text": "59.90", "amount_line": 3, "amount_occurrence": 2,
    }])
    app = zero_item_app(tmp_path, monkeypatch)
    click(app, "Get local AI second opinion")
    assert add_buttons(app) == ["Add to review table: SmallRig Sling Handle for DJI — \\$59.90"]
    click(app, "Add to review table")
    rows = app.session_state["review_rows_1"]
    assert [(r["Receipt order"], r["Item"], r["Qty"], r["Line total"]) for r in rows] == [
        (1, "SmallRig Sling Handle for DJI", 1.0, 59.90),
    ]
    assert any("Added from Local AI" in s.value for s in app.success)
    # The review table shows it; nothing was saved or changed underneath.
    assert any(
        "SmallRig Sling Handle for DJI" in list(frame.value.get("Item", []))
        for frame in app.dataframe if "Item" in frame.value.columns
    )
    assert app.session_state["active_receipt"]["items"] == []
    assert "last_saved_receipt_id" not in app.session_state
    # The comparison stays open, marked as added, and cannot be added twice.
    assert add_buttons(app) == []
    assert any(
        "Added to review table" in list(frame.value["Status"])
        for frame in app.dataframe if "Status" in frame.value.columns
    )


def test_rows_go_where_they_were_printed_and_add_all(tmp_path, monkeypatch):
    ocr = "BRASS KEY 3.00\nQUARTZ PEBBLE 2.35\nZINC TOKEN 0.10"
    mock_model(monkeypatch, [
        {"name_quote": "BRASS KEY", "name_lines": [1], "amount_text": "3.00",
         "amount_line": 1, "amount_occurrence": 1},
        {"name_quote": "ZINC TOKEN", "name_lines": [3], "amount_text": "0.10",
         "amount_line": 3, "amount_occurrence": 1},
    ])
    parsed = ReceiptItem(name="QUARTZ PEBBLE", quantity=1, unit_price=2.35,
                         discount=None, line_total=2.35)
    app = zero_item_app(
        tmp_path, monkeypatch, ocr_text=ocr, items=[parsed],
        decisions=[{"index": 2, "text": "QUARTZ PEBBLE 2.35", "role": "item",
                    "confidence": 0.99, "reason": "Matched an item line"}],
    )
    click(app, "Get local AI second opinion")
    assert len(add_buttons(app)) == 2
    click(app, "Add all 2 suggested rows")
    rows = app.session_state["review_rows_1"]
    assert [(r["Receipt order"], r["Item"]) for r in rows] == [
        (1, "BRASS KEY"), (2, "QUARTZ PEBBLE"), (3, "ZINC TOKEN"),
    ]
    assert sorted(app.session_state["recovered_source_lines"]) == [1, 3]


def test_no_add_button_when_ocr_text_no_longer_backs_the_row(tmp_path, monkeypatch):
    mock_model(monkeypatch, [{
        "name_quote": "SmallRig Sling Handle for DJI", "name_lines": [4],
        "amount_text": "59.90", "amount_line": 3, "amount_occurrence": 2,
    }])
    app = zero_item_app(tmp_path, monkeypatch)
    click(app, "Get local AI second opinion")
    # Tamper with the stored suggestion: it no longer matches its OCR line.
    preview_id, preview = app.session_state["local_llm_preview"]
    forged = local_llm.EvidenceItem(
        "Invented Lens", Decimal("59.90"), (4,), 3, (18, 23), "",
    )
    app.session_state["local_llm_preview"] = (
        preview_id, dataclasses.replace(preview, items=(forged,)),
    )
    app.run()
    assert not app.exception
    assert add_buttons(app) == []


# RL-66: a UPC printed before the name goes in its own column, and $ amounts
# are shown as text instead of LaTeX math.

HOME_DEPOT_OCR = "\n".join([
    "095624930055 BASE MDF <A> 18.48",
    "SUBTOTAL 18.48", "SALES TAX 1.52", "TOTAL $20.00",
])


def test_added_row_moves_leading_upc_to_product_code(tmp_path, monkeypatch):
    mock_model(monkeypatch, [{
        "name_quote": "095624930055 BASE MDF <A>", "name_lines": [1],
        "amount_text": "18.48", "amount_line": 1, "amount_occurrence": 1,
    }])
    app = zero_item_app(tmp_path, monkeypatch, ocr_text=HOME_DEPOT_OCR)
    click(app, "Get local AI second opinion")
    preview = next(f.value for f in app.dataframe if "Proposed item" in f.value.columns)
    assert list(preview["Proposed item"]) == ["BASE MDF <A>"]
    assert list(preview["UPC / SKU"]) == ["095624930055"]
    assert add_buttons(app) == ["Add to review table: BASE MDF <A> — \\$18.48"]
    click(app, "Add to review table")
    rows = app.session_state["review_rows_1"]
    assert [(r["Item"], r["Product code"], r["Line total"]) for r in rows] == [
        ("BASE MDF <A>", "095624930055", 18.48),
    ]
    assert not any("095624930055" in s.value for s in app.success)


def test_summary_line_escapes_dollar_signs(tmp_path, monkeypatch):
    mock_model(monkeypatch, [{
        "name_quote": "SmallRig Sling Handle for DJI", "name_lines": [4],
        "amount_text": "59.90", "amount_line": 3, "amount_occurrence": 2,
    }])
    app = zero_item_app(tmp_path, monkeypatch)
    click(app, "Get local AI second opinion")
    summary = next(m.value for m in app.markdown if m.value.startswith("Original parser"))
    # Two bare $ signs would make Streamlit render the text between them as math.
    assert summary == (
        "Original parser: 0 rows, \\$0.00. "
        "Local AI: 1 source-backed rows, \\$59.90."
    )
    assert all(
        "$" not in text.replace("\\$", "")
        for text in [m.value for m in app.markdown] + [c.value for c in app.caption]
        + [w.value for w in app.warning] + add_buttons(app)
    )
