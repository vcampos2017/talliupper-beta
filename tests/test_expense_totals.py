from pathlib import Path
from streamlit.testing.v1 import AppTest
from receipt_reader.database import initialize_database, save_receipt, expense_totals
from receipt_reader.models import blank_receipt, ReceiptItem


def test_expenses_sum_line_prices_and_separate_currencies(tmp_path, monkeypatch):
    db = tmp_path / "receipts.db"
    initialize_database(db)
    for currency, price in [("USD", 6), ("USD", 4), ("EUR", 9)]:
        receipt = blank_receipt()
        receipt.currency = currency
        receipt.subtotal, receipt.tax, receipt.total = price, 0, price
        receipt.items = [ReceiptItem(name="Cat food cans", quantity=2, unit_price=price/2,
            line_total=price, discount=None, category="Cat food")]
        save_receipt(db, receipt)
    assert expense_totals(db) == [
        {"Expense": "Cat food", "Currency": "USD", "Amount": 10},
        {"Expense": "Cat food", "Currency": "EUR", "Amount": 9}]
    assert expense_totals(db, by_item=True)[0]["Expense"] == "Cat food cans"
    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=30).run()
    assert not app.exception
    assert any(t.label == "Expense totals" for t in app.tabs)
    assert any(t.label == "Export center" for t in app.tabs)
    assert any(h.value == "Export center" for h in app.subheader)
    next(s for s in app.selectbox if s.label == "Expense currency").set_value("USD").run()
    assert not app.exception
    assert next(m for m in app.metric if m.label == "Total item expenses").value == "USD 10.00"
