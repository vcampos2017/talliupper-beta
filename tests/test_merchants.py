from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from receipt_reader.database import (
    delete_receipt,
    get_receipt,
    initialize_database,
    list_merchants,
    list_receipts,
    save_receipt,
    update_receipt_details,
    update_receipt_merchant,
)
from receipt_reader.models import ReceiptItem, blank_receipt


def test_saved_merchant_edit(tmp_path, monkeypatch):
    db = tmp_path / "receipts.db"
    initialize_database(db)
    receipt = blank_receipt()
    receipt.store_name = "Rockler"
    receipt.total = 5.40
    receipt_id = save_receipt(db, receipt)
    other_id = save_receipt(db, receipt)
    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=30).run()
    next(s for s in app.selectbox if s.label == "Open saved receipt").set_value(receipt_id).run()
    next(t for t in app.text_input if t.label == "Merchant name / location / store number").set_value("Rockler — San Antonio — Store #50")
    next(t for t in app.text_input if t.label == "Transaction date (YYYY-MM-DD)").set_value("2026-09-12")
    next(b for b in app.button if b.label == "Save receipt details").click().run()
    assert not app.exception
    assert get_receipt(db, receipt_id)["store_name"] == "Rockler — San Antonio — Store #50"
    assert get_receipt(db, receipt_id)["transaction_date"] == "2026-09-12"
    assert get_receipt(db, receipt_id)["total"] == 5.40
    assert get_receipt(db, other_id)["store_name"] == "Rockler"
    assert len(list_merchants(db)) == 2
    next(b for b in app.button if b.label == "Enter manually").click().run()
    next(s for s in app.selectbox if s.label == "Use a saved merchant").set_value("Rockler — San Antonio — Store #50").run()
    assert not app.exception
    assert next(t for t in app.text_input if t.label == "Store name").value == "Rockler — San Antonio — Store #50"
    with pytest.raises(ValueError):
        update_receipt_merchant(db, receipt_id, "   ")
    with pytest.raises(ValueError):
        update_receipt_merchant(db, 999, "Missing")


def test_saved_receipt_details_validation_and_guarded_delete(tmp_path, monkeypatch):
    db = tmp_path / "receipts.db"
    initialize_database(db)
    receipt = blank_receipt()
    receipt.store_name = "Receipt OCR noise"
    receipt.transaction_date = None
    receipt.total = 5.40
    receipt.items = [
        ReceiptItem(
            name="Test item",
            quantity=1,
            unit_price=5.40,
            line_total=5.40,
            discount=None,
        )
    ]
    receipt_id = save_receipt(db, receipt)
    other_id = save_receipt(db, receipt)

    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        update_receipt_details(
            db,
            receipt_id,
            merchant="Rockler",
            transaction_date="09/12/2026",
        )
    with pytest.raises(ValueError, match="valid transaction date"):
        update_receipt_details(
            db,
            receipt_id,
            merchant="Rockler",
            transaction_date="2026-02-30",
        )

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    selector = next(
        selectbox for selectbox in app.selectbox
        if selectbox.label == "Open saved receipt"
    )
    assert any(f"RL-{receipt_id:06d}" in option for option in selector.options)
    selector.set_value(receipt_id).run()

    delete_button = next(
        button for button in app.button if button.label == "Delete saved receipt"
    )
    assert delete_button.disabled
    next(
        checkbox for checkbox in app.checkbox
        if f"RL-{receipt_id:06d}" in checkbox.label
    ).check().run()
    next(
        button for button in app.button if button.label == "Delete saved receipt"
    ).click().run()

    assert not app.exception
    assert get_receipt(db, receipt_id) is None
    assert get_receipt(db, other_id) is not None
    assert len(get_receipt(db, other_id)["items"]) == 1
    assert len(list_receipts(db)) == 1

    deleted = delete_receipt(db, other_id)
    assert deleted["id"] == other_id
    assert list_receipts(db) == []
    with pytest.raises(ValueError, match="no longer exists"):
        delete_receipt(db, other_id)


def test_merchant_names_normalized(tmp_path):
    db = tmp_path / "receipts.db"
    initialize_database(db)
    for name in ["Rockler", " ROCKLER  ", "Rockler Store #2"]:
        receipt = blank_receipt()
        receipt.store_name = name
        save_receipt(db, receipt)
    assert list_merchants(db) == ["ROCKLER", "Rockler Store #2"]
