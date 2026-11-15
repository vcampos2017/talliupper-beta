from pathlib import Path
import pytest
from streamlit.testing.v1 import AppTest
from receipt_reader.database import initialize_database, save_receipt, get_receipt, update_item_categories, expense_totals, export_csv
from receipt_reader.models import blank_receipt, ReceiptItem


def test_saved_category_changes_and_custom_creation(tmp_path, monkeypatch):
    db = tmp_path / "receipts.db"
    initialize_database(db)
    receipt = blank_receipt()
    receipt.subtotal, receipt.tax, receipt.total = 5, 0, 5
    receipt.items = [ReceiptItem(name="EGGS", quantity=1, unit_price=5, line_total=5, discount=None)]
    receipt_id = save_receipt(db, receipt)
    other_id = save_receipt(db, receipt)
    with pytest.raises(ValueError):
        update_item_categories(db, receipt_id, [0, 99], "Groceries")
    assert get_receipt(db, receipt_id)["items"][0]["category"] == "Uncategorized"
    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=30).run()
    next(s for s in app.selectbox if s.label == "Open saved receipt").set_value(receipt_id).run()
    next(s for s in app.selectbox if s.label == "Category for all items").set_value("Groceries")
    next(b for b in app.button if b.label == "Apply category to all items").click().run()
    assert not app.exception
    assert get_receipt(db, receipt_id)["items"][0]["category"] == "Groceries"
    assert get_receipt(db, other_id)["items"][0]["category"] == "Uncategorized"
    assert get_receipt(db, receipt_id)["total"] == 5
    assert "Groceries" in export_csv(db)
    assert {r["Expense"]: r["Amount"] for r in expense_totals(db)}["Groceries"] == 5
    next(t for t in app.text_input if t.label == "New expense category name").set_value("Breakfast")
    next(b for b in app.button if b.label == "Create category").click().run()
    assert not app.exception
    assert "Breakfast" in next(
        s for s in app.selectbox if s.label == "Category for all items"
    ).options
