import os
import tempfile
from pathlib import Path
from streamlit.testing.v1 import AppTest
from receipt_reader.models import blank_receipt, ReceiptItem

def test_missing_item_flow():
    os.environ["RECEIPT_DATA_DIR"] = tempfile.mkdtemp(prefix="missing-item-test-")
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py", default_timeout=30).run()
    receipt = blank_receipt()
    receipt.items = [ReceiptItem(name=n, quantity=1, line_total=p, unit_price=None, discount=None)
        for n,p in [("Eggs",2.98),("Avocados",2.68),("Onion",.25)]]
    app.session_state["active_receipt"] = receipt.model_dump()
    app.session_state["active_image_hash"] = None
    app.session_state["receipt_version"] = 1
    app.session_state["ocr_text"] = "ITEMS PURCHASED: 4"
    app.run()
    def add_button():
        return next(b for b in app.button if b.label == "Add item manually")
    add_button().click().run()
    assert any("name" in e.value for e in app.error)
    for label,value in [("Printed subtotal","11.88"),("Printed sales tax","0.49"),("Printed total","12.37")]:
        next(t for t in app.text_input if t.label==label).set_value(value).run()
    next(t for t in app.text_input if t.label=="Manual item name").set_value("Sparkling beverage mini cans")
    next(t for t in app.text_input if t.label=="Manual item line price").set_value("5.97")
    add_button().click().run()
    assert not app.exception, app.exception
    review_table = app.dataframe[0].value
    assert len(review_table)==4
    assert round(review_table["Line total"].sum(),2)==11.88
    assert next(t for t in app.text_input if t.label=="Printed sales tax").value=="0.49"
    assert any(b.label=="Add item manually" for b in app.button)
    assert not next(
        b for b in app.button if b.label == "Verify and save receipt"
    ).disabled

if __name__ == "__main__":
    test_missing_item_flow()
    print("PASS: missing-item validation, insertion, count, totals preservation, and verification gate.")



def test_manual_item_entry_is_available_without_count_or_candidates():
    os.environ["RECEIPT_DATA_DIR"] = tempfile.mkdtemp(
        prefix="manual-item-no-count-test-"
    )
    app = AppTest.from_file(
        Path(__file__).resolve().parents[1] / "app.py",
        default_timeout=30,
    ).run()
    receipt = blank_receipt()
    receipt.store_name = "McDonalds #2881"
    receipt.transaction_date = "2026-08-31"
    receipt.subtotal, receipt.tax, receipt.total = 1.19, 0.10, 1.29
    app.session_state["active_receipt"] = receipt.model_dump()
    app.session_state["active_image_hash"] = None
    app.session_state["active_method"] = "local-paddleocr"
    app.session_state["receipt_version"] = 1
    app.session_state["ocr_text"] = "SUBTOTAL 1.19 TAX 0.10 TOTAL 1.29"
    app.session_state["missing_item_candidates"] = []
    app.run()

    def add_button():
        return next(b for b in app.button if b.label == "Add item manually")

    assert not app.exception
    assert add_button()
    next(
        field for field in app.text_input
        if field.label == "Manual item name"
    ).set_value("Hash Brown reward")
    next(
        field for field in app.text_input
        if field.label == "Manual item line price"
    ).set_value("0.00")
    next(
        field for field in app.number_input
        if field.label == "Manual item quantity"
    ).set_value(2.0)
    next(
        field for field in app.selectbox
        if field.label == "Manual item tax marker (optional)"
    ).set_value("T")
    next(
        field for field in app.selectbox
        if field.label == "Manual item expense category"
    ).set_value("Dining & Restaurants")
    add_button().click().run()

    assert not app.exception, app.exception
    review_table = app.dataframe[0].value
    assert len(review_table) == 1
    assert review_table.loc[0, "Item"] == "Hash Brown reward"
    assert review_table.loc[0, "Line total"] == 0.0
    assert any(metric.value == "1" for metric in app.metric)
    assert any(b.label == "Add item manually" for b in app.button)
    assert any(
        "Added Hash Brown reward — quantity 2 — USD 0.00" in message.value
        for message in app.success
    )
    assert next(
        field for field in app.text_input
        if field.label == "Manual item name"
    ).value == ""
    assert next(
        field for field in app.text_input
        if field.label == "Manual item line price"
    ).value == ""
    assert next(
        field for field in app.number_input
        if field.label == "Manual item quantity"
    ).value == 1.0
    assert next(
        field for field in app.selectbox
        if field.label == "Manual item tax marker (optional)"
    ).value == "Unknown"
    assert next(
        field for field in app.selectbox
        if field.label == "Manual item expense category"
    ).value == "Uncategorized"

    next(
        field for field in app.text_input
        if field.label == "Manual item name"
    ).set_value("Small Coke")
    next(
        field for field in app.text_input
        if field.label == "Manual item line price"
    ).set_value("1.19")
    add_button().click().run()

    assert not app.exception, app.exception
    review_table = app.dataframe[0].value
    assert list(review_table["Item"]) == ["Hash Brown reward", "Small Coke"]
    assert list(review_table["Line total"]) == [0.0, 1.19]
    assert any(
        "Added Small Coke — quantity 1 — USD 1.19" in message.value
        for message in app.success
    )
    assert next(
        field for field in app.text_input
        if field.label == "Manual item name"
    ).value == ""
