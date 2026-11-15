from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from receipt_reader.database import (initialize_database, save_receipt, list_receipts,
    find_duplicate_receipts, DuplicateReceiptError)
from receipt_reader.models import blank_receipt, ReceiptItem


def receipt():
    value = blank_receipt()
    value.store_name = "Test Store"
    value.transaction_date = "2026-09-06"
    value.subtotal, value.tax, value.total = 5.0, 0.0, 5.0
    value.items = [ReceiptItem(name="Item", quantity=1, line_total=5, unit_price=5, discount=None)]
    return value


def test_duplicate_source_cannot_be_overridden(tmp_path):
    db = tmp_path / "receipts.db"
    initialize_database(db)
    first = save_receipt(db, receipt(), image_sha256="same-page")
    changed = receipt()
    changed.store_name = "Corrected store"
    with pytest.raises(DuplicateReceiptError):
        save_receipt(db, changed, image_sha256="same-page", allow_possible_duplicate=True)
    assert find_duplicate_receipts(db, changed, "same-page") == [{"id": first, "exact": True}]
    assert len(list_receipts(db)) == 1


def test_possible_duplicate_requires_explicit_override(tmp_path):
    db = tmp_path / "receipts.db"
    initialize_database(db)
    save_receipt(db, receipt(), image_sha256="photo-one")
    candidate = receipt()
    candidate.store_name = "  TEST   STORE "
    with pytest.raises(DuplicateReceiptError):
        save_receipt(db, candidate, image_sha256="photo-two")
    save_receipt(db, candidate, image_sha256="photo-two", allow_possible_duplicate=True)
    assert len(list_receipts(db)) == 2
    candidate.transaction_date = "2026-09-07"
    assert not find_duplicate_receipts(db, candidate)
    candidate.transaction_date = None
    assert not find_duplicate_receipts(db, candidate)


def test_concurrent_same_source_saves_create_one_record(tmp_path):
    db = tmp_path / "receipts.db"
    initialize_database(db)
    def attempt(_):
        try:
            return save_receipt(db, receipt(), image_sha256="same")
        except DuplicateReceiptError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(r is not None for r in results) == 1
    assert len(list_receipts(db)) == 1


def test_ui_possible_duplicate_requires_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=30).run()
    save_receipt(tmp_path / "receipts.db", receipt())
    app.session_state["active_receipt"] = receipt().model_dump()
    app.session_state["receipt_version"] = 1
    app.session_state["active_image_hash"] = None
    app.run()
    assert not app.exception
    assert next(b for b in app.button if b.label == "Verify and save receipt").disabled
    next(c for c in app.checkbox if c.label.startswith("This is a separate purchase")).check().run()
    assert not next(b for b in app.button if b.label == "Verify and save receipt").disabled
    next(b for b in app.button if b.label == "Verify and save receipt").click().run()
    assert not app.exception
    assert len(list_receipts(tmp_path / "receipts.db")) == 2
    assert any(
        "Saved successfully as RL-000002" in message.value
        for message in app.success
    )
    assert not any(
        b.label == "Verify and save receipt" for b in app.button
    )
    assert not any(
        c.label.startswith("This is a separate purchase")
        for c in app.checkbox
    )

