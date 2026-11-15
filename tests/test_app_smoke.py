from datetime import date
from pathlib import Path
import pytest

from streamlit.testing.v1 import AppTest


def test_database_viewer_loads_saved_tables(tmp_path, monkeypatch):
    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=30).run()
    for table in ["receipts", "receipt_items", "receipt_sources", "expense_categories"]:
        next(s for s in app.selectbox if s.label == "Database table").set_value(table).run()
        next(b for b in app.button if b.label == "Load database table").click().run()
        assert not app.exception
        assert any("records (up to 500)" in c.value for c in app.caption)


@pytest.mark.parametrize(
    "asset_name",
    [
        "talliupper_mascot.webp",
        "talli_reading_receipt.webp",
        "talli_reading_pages.webp",
        "talli_comparing_receipts.webp",
        "talli_receipt_ready.webp",
    ],
)
def test_talli_assets_are_packaged(asset_name: str) -> None:
    asset = Path(__file__).parents[1] / "assets" / asset_name

    assert asset.is_file()
    assert asset.read_bytes().startswith(b"RIFF")


def test_app_starts_in_local_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path / "data"))

    app_path = Path(__file__).parents[1] / "app.py"
    app = AppTest.from_file(app_path, default_timeout=20).run()

    assert not app.exception
    assert app.title[0].value == "🧾 TalliUpper"
    assert any(
        radio.value == "Local OCR (recommended)"
        for radio in [*app.radio, *app.sidebar.radio]
    )
    assert (tmp_path / "data" / "receipts.db").exists()
    assert any(
        field.label == "Create a category" for field in app.sidebar.text_input
    )
    expense_dates = next(
        radio for radio in app.radio if radio.label == "Expense dates"
    )
    assert expense_dates.options == [
        "All dates",
        "This year",
        "Choose a range",
    ]
    rendered_styles = "\n".join(block.value for block in app.markdown)
    assert "**Our mission:**" in rendered_styles
    assert "approachable, lively, and trustworthy" in rendered_styles
    assert '[data-testid="stTextInput"] [data-baseweb="input"]' in rendered_styles
    assert '[data-testid="stDateInput"] [data-baseweb="input"]' in rendered_styles
    assert '[data-testid="stCheckbox"] [data-baseweb="checkbox"]' in rendered_styles
    assert ":focus-within" in rendered_styles
    assert any(button.label == "Refresh receipts" for button in app.button)
    source = app_path.read_text(encoding="utf-8")
    assert '"Refresh receipts",\n            type="primary",' in source
    assert "TalliUpper keeps receipt processing on" in rendered_styles
    assert "receipt_reader/local_ocr.py" not in rendered_styles
    assert "receipt_reader/parser.py" not in rendered_styles
    assert ".receipt-comparison-table" in rendered_styles
    assert 'st-key-comparison_' not in source


def test_user_can_create_a_reusable_expense_category(
    tmp_path: Path, monkeypatch
) -> None:
    from receipt_reader.database import list_categories

    data_dir = tmp_path / "data"
    monkeypatch.setenv("RECEIPT_DATA_DIR", str(data_dir))
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=20
    ).run()

    next(
        field
        for field in app.sidebar.text_input
        if field.label == "Create a category"
    ).set_value("Workshop supplies")
    next(
        button
        for button in app.sidebar.button
        if button.label == "Add category"
    ).click().run()

    assert not app.exception
    assert "Workshop supplies" in list_categories(data_dir / "receipts.db")


def test_user_can_create_category_from_receipt_review(
    tmp_path: Path, monkeypatch
) -> None:
    from receipt_reader.database import list_categories
    from receipt_reader.models import ReceiptItem, blank_receipt

    data_dir = tmp_path / "data"
    monkeypatch.setenv("RECEIPT_DATA_DIR", str(data_dir))
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    receipt = blank_receipt()
    receipt.items = [
        ReceiptItem(
            name="Pet medication refill",
            quantity=1,
            unit_price=43.47,
            line_total=43.47,
            discount=None,
        )
    ]
    receipt.subtotal, receipt.tax, receipt.total = 43.47, 3.59, 47.06
    app.session_state["active_receipt"] = receipt.model_dump()
    app.session_state["receipt_version"] = 1
    app.session_state["active_image_hash"] = None
    app.session_state["active_method"] = "local-paddleocr"
    app.run()

    next(
        field
        for field in app.text_input
        if field.label == "New category or subcategory"
    ).set_value("Pet Care / Medication")
    next(
        button
        for button in app.button
        if button.label == "Add category to this receipt"
    ).click().run()

    assert not app.exception
    assert "Pet Care / Medication" in list_categories(
        data_dir / "receipts.db"
    )
    assert any(
        "Added category: Pet Care / Medication" in message.value
        for message in app.success
    )


def test_verify_and_save_is_one_step_and_selects_new_receipt(
    tmp_path, monkeypatch
):
    from receipt_reader.database import list_receipts
    from receipt_reader.models import ReceiptExtraction, ReceiptItem

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    receipt = ReceiptExtraction(
        store_name="",
        transaction_date=None,
        currency="USD",
        items=[
            ReceiptItem(
                name="Eggs",
                quantity=1,
                unit_price=2.98,
                discount=None,
                line_total=2.98,
            )
        ],
        subtotal=2.98,
        tax=0.25,
        total=3.23,
        confidence=0.7,
        notes=[],
    )
    app.session_state["active_receipt"] = receipt.model_dump()
    app.session_state["receipt_version"] = 1
    app.session_state["active_image_hash"] = None
    app.session_state["active_method"] = "manual"
    app.run()

    def save_button():
        return next(
            button
            for button in app.button
            if button.label == "Verify and save receipt"
        )

    assert not app.exception
    assert not save_button().disabled
    assert any("Not saved yet" in message.value for message in app.info)
    assert any(
        "single action confirms your review and saves" in message.value
        for message in app.info
    )
    assert any(
        button.label == "Refresh receipts" for button in app.button
    )
    assert len(list_receipts(tmp_path / "receipts.db")) == 0

    next(
        field for field in app.text_input if field.label == "Printed total"
    ).set_value("4.00").run()
    assert save_button().disabled
    assert any(
        "remaining-differences checkbox" in message.value
        for message in app.info
    )
    next(
        checkbox
        for checkbox in app.checkbox
        if checkbox.label.startswith("I reviewed the remaining differences")
    ).check().run()
    assert not save_button().disabled
    save_button().click().run()

    assert not app.exception
    assert any(
        "Saved successfully as RL-000001" in message.value
        for message in app.success
    )
    assert app.session_state["saved_receipt_selector"] == 1
    receipts = list_receipts(tmp_path / "receipts.db")
    assert len(receipts) == 1
    assert receipts[0]["total"] == 4.0


def test_restored_receipt_without_uploader_value_can_still_be_saved(
    tmp_path, monkeypatch
) -> None:
    from receipt_reader.models import ReceiptExtraction, ReceiptItem

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    receipt = ReceiptExtraction(
        store_name="Walmart",
        transaction_date="2026-09-26",
        currency="USD",
        items=[
            ReceiptItem(
                name="FRENCH BREAD",
                product_code="200989000000",
                category="Groceries",
                quantity=1,
                unit_price=1.47,
                discount=None,
                line_total=1.47,
                tax_marker="F",
            ),
            ReceiptItem(
                name="FRENCH BREAD",
                product_code="200989000000",
                category="Groceries",
                quantity=1,
                unit_price=1.47,
                discount=None,
                line_total=1.47,
                tax_marker="F",
            ),
        ],
        subtotal=2.94,
        tax=0.0,
        total=2.94,
        confidence=0.99,
        notes=[],
    )
    app.session_state["active_receipt"] = receipt.model_dump()
    app.session_state["receipt_version"] = "walmart-job_1"
    app.session_state["active_image_hash"] = "durable-walmart-image"
    app.session_state["active_method"] = "local-paddleocr"
    app.session_state["ocr_text"] = "# ITEMS SOLD 2"
    app.run()

    save_button = next(
        button for button in app.button
        if button.label == "Verify and save receipt"
    )
    assert not app.exception
    assert not save_button.disabled
    assert not any("The image changed" in warning.value for warning in app.warning)


def test_start_another_receipt_clears_in_progress_review(
    tmp_path, monkeypatch
) -> None:
    from receipt_reader.models import ReceiptItem, blank_receipt

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    receipt = blank_receipt()
    receipt.items = [
        ReceiptItem(
            name="Haircut",
            quantity=1,
            unit_price=20.0,
            discount=None,
            line_total=20.0,
        )
    ]
    receipt.subtotal, receipt.tax, receipt.total = 20.0, 0.0, 20.0
    app.session_state["active_receipt"] = receipt.model_dump()
    app.session_state["receipt_version"] = 1
    app.session_state["active_image_hash"] = "great-clips-image"
    app.session_state["active_method"] = "local-paddleocr"
    app.run()

    next(
        button for button in app.button
        if button.label == "Start another receipt"
    ).click().run()

    assert not app.exception
    assert "active_receipt" not in app.session_state
    assert "active_image_hash" not in app.session_state
    assert app.session_state["receipt_uploader_revision"] == 1
    assert not any(
        metric.label == "Calculated purchase/service subtotal"
        for metric in app.metric
    )


def test_exact_duplicate_can_update_saved_receipt_corrections(
    tmp_path, monkeypatch
) -> None:
    from receipt_reader.database import get_receipt, initialize_database, save_receipt
    from receipt_reader.models import ReceiptExtraction, ReceiptItem

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    database = tmp_path / "receipts.db"
    initialize_database(database)
    original = ReceiptExtraction(
        store_name="Great Clips — Northtowne SC",
        transaction_date="2026-09-14",
        currency="USD",
        items=[
            ReceiptItem(
                name="Haircut",
                category="Uncategorized",
                quantity=1,
                unit_price=20.0,
                discount=None,
                line_total=20.0,
            ),
            ReceiptItem(
                name="Charged tip for Michael",
                category="Uncategorized",
                quantity=1,
                unit_price=6.0,
                discount=None,
                line_total=6.0,
            ),
        ],
        subtotal=20.0,
        tax=0.0,
        total=26.0,
        confidence=0.99,
        notes=[],
    )
    receipt_id = save_receipt(database, original, image_sha256="great-clips-image")
    corrected = original.model_copy(deep=True)
    for item in corrected.items:
        item.category = "Personal Care"

    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    app.session_state["active_receipt"] = corrected.model_dump()
    app.session_state["receipt_version"] = 1
    app.session_state["active_image_hash"] = "great-clips-image"
    app.session_state["active_method"] = "local-paddleocr"
    app.run()

    update_button = next(
        button for button in app.button
        if button.label == "Update saved receipt"
    )
    assert not update_button.disabled
    update_button.click().run()

    assert not app.exception
    saved = get_receipt(database, receipt_id)
    assert saved is not None
    assert {item["category"] for item in saved["items"]} == {"Personal Care"}
    assert any(
        f"Updated RL-{receipt_id:06d}" in message.value
        for message in app.success
    )


def test_deleting_selected_receipt_refreshes_selector(tmp_path, monkeypatch):
    from receipt_reader.database import (
        initialize_database,
        list_receipts,
        save_receipt,
    )
    from receipt_reader.models import ReceiptExtraction, ReceiptItem

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    database_path = tmp_path / "receipts.db"
    initialize_database(database_path)

    def receipt(store_name: str, day: str, amount: float) -> ReceiptExtraction:
        return ReceiptExtraction(
            store_name=store_name,
            transaction_date=day,
            currency="USD",
            items=[
                ReceiptItem(
                    name=f"{store_name} item",
                    quantity=1,
                    unit_price=amount,
                    discount=None,
                    line_total=amount,
                )
            ],
            subtotal=amount,
            tax=0.0,
            total=amount,
            confidence=1.0,
            notes=[],
        )

    first_id = save_receipt(
        database_path,
        receipt("First Merchant", "2026-09-17", 10.00),
    )
    deleted_id = save_receipt(
        database_path,
        receipt("The Home Depot", "2026-09-18", 21.56),
    )

    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    selector = next(
        widget
        for widget in app.selectbox
        if widget.label == "Open saved receipt"
    )
    selector.set_value(deleted_id).run()

    next(
        checkbox
        for checkbox in app.checkbox
        if checkbox.label
        == f"I understand and want to delete RL-{deleted_id:06d}"
    ).check().run()
    next(
        button
        for button in app.button
        if button.label == "Delete saved receipt"
    ).click().run()

    assert not app.exception
    refreshed_selector = next(
        widget
        for widget in app.selectbox
        if widget.label == "Open saved receipt"
    )
    assert refreshed_selector.options == [
        "RL-000001 — First Merchant — 2026-09-17 — USD 10.00"
    ]
    assert refreshed_selector.value == first_id
    assert app.session_state["saved_receipt_selector"] == first_id
    assert [row["id"] for row in list_receipts(database_path)] == [first_id]
    assert any(
        f"Deleted RL-{deleted_id:06d}" in message.value
        for message in app.success
    )


def test_deleting_just_saved_receipt_clears_saved_message_on_scan_tab(
    tmp_path, monkeypatch
):
    """RL-44: after deleting a receipt, nothing on screen may still refer to it."""

    from receipt_reader.database import (
        initialize_database,
        list_receipts,
        save_receipt,
    )
    from receipt_reader.models import ReceiptExtraction, ReceiptItem

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    database_path = tmp_path / "receipts.db"
    initialize_database(database_path)
    older_id = save_receipt(
        database_path,
        ReceiptExtraction(
            store_name="Older Store",
            transaction_date="2026-09-01",
            currency="USD",
            items=[
                ReceiptItem(
                    name="Old item",
                    quantity=1,
                    unit_price=1.0,
                    discount=None,
                    line_total=1.0,
                )
            ],
            subtotal=1.0,
            tax=0.0,
            total=1.0,
            confidence=1.0,
            notes=[],
        ),
    )

    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    review = ReceiptExtraction(
        store_name="Fresh Market",
        transaction_date="2026-09-05",
        currency="USD",
        items=[
            ReceiptItem(
                name="Eggs", quantity=1, unit_price=2.98, discount=None,
                line_total=2.98,
            ),
            ReceiptItem(
                name="Milk", quantity=1, unit_price=3.02, discount=None,
                line_total=3.02,
            ),
        ],
        subtotal=6.0,
        tax=0.0,
        total=6.0,
        confidence=0.9,
        notes=[],
    )
    app.session_state["active_receipt"] = review.model_dump()
    app.session_state["receipt_version"] = 1
    app.session_state["active_image_hash"] = None
    app.session_state["active_method"] = "manual"
    app.run()
    next(
        button for button in app.button
        if button.label == "Verify and save receipt"
    ).click().run()
    saved_id = app.session_state["saved_receipt_selector"]
    assert saved_id != older_id
    assert any(
        f"Saved successfully as RL-{saved_id:06d}" in message.value
        for message in app.success
    )

    next(
        checkbox for checkbox in app.checkbox
        if checkbox.label == f"I understand and want to delete RL-{saved_id:06d}"
    ).check().run()
    next(
        button for button in app.button
        if button.label == "Delete saved receipt"
    ).click().run()

    assert not app.exception
    assert [row["id"] for row in list_receipts(database_path)] == [older_id]
    assert any(
        f"Deleted RL-{saved_id:06d}" in message.value for message in app.success
    )
    # The Scan tab must no longer claim the deleted receipt is saved, and the
    # review can be saved again.
    assert not any(
        f"Saved successfully as RL-{saved_id:06d}" in message.value
        for message in app.success
    )
    assert any(
        button.label == "Verify and save receipt" for button in app.button
    )
    assert "last_saved_receipt_id" not in app.session_state
    assert app.session_state["saved_receipt_selector"] == older_id
    assert f"confirm_delete_{saved_id}" not in app.session_state
    # Every saved-receipt section now shows only the remaining receipt.
    assert any("**Older Store**" in block.value for block in app.markdown)
    assert not any("Fresh Market" in block.value for block in app.markdown)
    merchant_field = next(
        field for field in app.text_input
        if field.label == "Merchant name / location / store number"
    )
    assert merchant_field.value == "Older Store"
    assert not any(
        checkbox.label == f"I understand and want to delete RL-{saved_id:06d}"
        for checkbox in app.checkbox
    )


def test_known_tax_markers_do_not_show_incomplete_warning(tmp_path, monkeypatch):
    from receipt_reader.models import blank_receipt, ReceiptItem

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=30).run()
    receipt = blank_receipt()
    receipt.items = [ReceiptItem(name="Eggs", quantity=1, unit_price=2.98,
                                line_total=2.98, discount=None, tax_marker="F")]
    receipt.subtotal, receipt.tax, receipt.total = 2.98, 0.0, 2.98
    app.session_state["active_receipt"] = receipt.model_dump()
    app.session_state["receipt_version"] = 1
    app.session_state["active_image_hash"] = None
    app.run()
    assert not app.exception
    assert not any("Tax markers need review" in warning.value for warning in app.warning)


def test_service_receipt_tip_matches_total_without_inflating_subtotal(
    tmp_path, monkeypatch
) -> None:
    from receipt_reader.models import ReceiptExtraction, ReceiptItem

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    receipt = ReceiptExtraction(
        store_name="Great Clips — Northtowne SC",
        transaction_date="2026-09-14",
        currency="USD",
        items=[
            ReceiptItem(
                name="Haircut",
                quantity=1,
                unit_price=20.0,
                discount=None,
                line_total=20.0,
            ),
            ReceiptItem(
                name="Charged tip for Michael",
                quantity=1,
                unit_price=6.0,
                discount=None,
                line_total=6.0,
            ),
        ],
        subtotal=20.0,
        tax=0.0,
        total=26.0,
        confidence=0.99,
        notes=[],
    )
    app.session_state["active_receipt"] = receipt.model_dump()
    app.session_state["receipt_version"] = 1
    app.session_state["active_image_hash"] = None
    app.session_state["active_method"] = "local-paddleocr"
    app.run()

    assert not app.exception
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics["Calculated purchase/service subtotal"] == "20.00"
    assert metrics["Detected tip / gratuity"] == "6.00"
    assert any("Amounts match" in message.value for message in app.success)
    assert not any("Amounts differ" in message.value for message in app.warning)
    tax_warning = next(
        message.value
        for message in app.warning
        if "Tax markers need review" in message.value
    )
    assert "Haircut" in tax_warning
    assert "Charged tip" not in tax_warning
    printable_html = "\n".join(block.value for block in app.markdown)
    assert "receipt-print-table" in printable_html
    assert "Charged tip for Michael" in printable_html


def test_matching_printed_count_softens_repeated_item_warning(
    tmp_path, monkeypatch
) -> None:
    from receipt_reader.models import ReceiptExtraction, ReceiptItem

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    receipt = ReceiptExtraction(
        store_name="Walmart",
        transaction_date="2026-09-26",
        currency="USD",
        items=[
            ReceiptItem(
                name="FRENCH BREAD",
                product_code="200989000000",
                tax_marker="F",
                category="Groceries",
                quantity=1,
                unit_price=1.47,
                discount=None,
                line_total=1.47,
            ),
            ReceiptItem(
                name="FRENCH BREAD",
                product_code="200989000000",
                tax_marker="F",
                category="Groceries",
                quantity=1,
                unit_price=1.47,
                discount=None,
                line_total=1.47,
            ),
        ],
        subtotal=2.94,
        tax=0.0,
        total=2.94,
        confidence=0.99,
        notes=[],
    )
    app.session_state["active_receipt"] = receipt.model_dump()
    app.session_state["receipt_version"] = 1
    app.session_state["active_image_hash"] = None
    app.session_state["active_method"] = "local-paddleocr"
    app.session_state["ocr_text"] = "# ITEMS SOLD 2"
    app.run()

    assert not app.exception
    assert any(
        "may be intentional repeat purchases" in message.value
        for message in app.info
    )
    assert not any(
        "potential duplicate group" in message.value
        for message in app.warning
    )


def test_review_table_prioritizes_human_review_columns(tmp_path, monkeypatch):
    from receipt_reader.models import blank_receipt, ReceiptItem

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    receipt = blank_receipt()
    receipt.items = [
        ReceiptItem(
            name="Eggs",
            quantity=1,
            unit_price=2.98,
            line_total=2.98,
            discount=None,
            category="Groceries",
        )
    ]
    receipt.subtotal, receipt.tax, receipt.total = 2.98, 0.0, 2.98
    app.session_state["active_receipt"] = receipt.model_dump()
    app.session_state["receipt_version"] = 1
    app.session_state["active_image_hash"] = None
    app.run()

    assert not app.exception
    review_table = app.dataframe[0].value
    assert list(review_table.columns) == [
        "Receipt order",
        "Item",
        "Line total",
        "Qty",
        "Delete",
        "Merge",
        "Review check",
        "Category",
        "Product code",
        "Tax marker",
        "Unit price",
        "Discount",
    ]
    assert any(button.label == "Delete selected items" for button in app.button)
    assert any(button.label == "Merge selected items" for button in app.button)


@pytest.mark.parametrize("printed_count", [True, False])
def test_missing_ocr_candidates_are_shown_as_selectable_rows(
    tmp_path, monkeypatch, printed_count
) -> None:
    from receipt_reader.models import ReceiptExtraction, ReceiptItem

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    receipt = ReceiptExtraction(
        store_name="Sample Market",
        transaction_date=None,
        currency="USD",
        items=[
            ReceiptItem(
                name="Eggs",
                quantity=1,
                unit_price=2.98,
                discount=None,
                line_total=2.98,
            )
        ],
        subtotal=3.23,
        tax=0.0,
        total=3.23,
        confidence=0.7,
        notes=[],
    )
    app.session_state["active_receipt"] = receipt.model_dump()
    app.session_state["receipt_version"] = 1
    app.session_state["active_image_hash"] = None
    app.session_state["active_method"] = "local-paddleocr"
    app.session_state["ocr_text"] = "ITEMS PURCHASED: 2" if printed_count else "SUBTOTAL 3.23"
    app.session_state["missing_item_candidates"] = [
        {
            "name": "SWEET ONION",
            "confidence": 1.0,
            "source_line": 4,
            "supporting_text": "0.20 Lbs @ 1.24 FW 0.25",
            "quantity": 0.2,
            "unit_price": 1.24,
            "line_total": 0.25,
            "tax_marker": "FW",
        }
    ]

    app.run()

    assert not app.exception
    assert any(
        heading.value == ("Recover missing items (1 remaining)" if printed_count else "Review possible missing items")
        for heading in app.subheader
    )
    candidate_table = app.dataframe[1].value
    assert candidate_table.loc[0, "Item name"] == "SWEET ONION"
    assert candidate_table.loc[0, "Line price"] == 0.25
    assert candidate_table.loc[0, "Tax marker"] == "FW"
    assert any(
        button.label == "Add selected OCR lines as items"
        for button in app.button
    )


def test_balanced_review_suppresses_stale_missing_item_candidates(
    tmp_path: Path, monkeypatch
) -> None:
    from receipt_reader.models import ReceiptExtraction, ReceiptItem

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    receipt = ReceiptExtraction(
        store_name="Rockler",
        transaction_date="2026-09-24",
        currency="USD",
        items=[
            ReceiptItem(
                name="7/16 BRAD POINT DRILL-CD",
                product_code="57839",
                quantity=1,
                unit_price=10.99,
                discount=None,
                line_total=10.99,
            )
        ],
        subtotal=10.99,
        tax=0.90,
        total=11.89,
        confidence=0.8,
        notes=[],
    )
    app.session_state["active_receipt"] = receipt.model_dump()
    app.session_state["receipt_version"] = 1
    app.session_state["active_method"] = "local-paddleocr"
    app.session_state["ocr_text"] = "SUBTOTAL 10.99"
    app.session_state["missing_item_candidates"] = [
        {
            "name": "ROCKLER IS NOW HIRING",
            "confidence": 0.7,
            "source_line": 4,
            "supporting_text": "",
            "quantity": 1.0,
            "unit_price": None,
            "line_total": None,
            "tax_marker": None,
        }
    ]

    app.run()

    assert not app.exception
    assert not any(
        heading.value == "Review possible missing items"
        for heading in app.subheader
    )
    rendered = "\n".join(block.value for block in app.markdown)
    assert rendered.count('class="dataframe receipt-comparison-table"') == 1


def test_unused_ocr_product_line_is_available_when_printed_count_matches(
    tmp_path: Path, monkeypatch
) -> None:
    from receipt_reader.models import ReceiptExtraction, ReceiptItem

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    receipt = ReceiptExtraction(
        store_name="H-E-B",
        transaction_date=None,
        currency="USD",
        items=[
            ReceiptItem(
                name="LACTAID 2% MILK",
                quantity=1,
                unit_price=7.28,
                discount=None,
                line_total=7.28,
            ),
            ReceiptItem(
                name="Contactless Chip USD",
                quantity=1,
                unit_price=11.76,
                discount=None,
                line_total=11.76,
            ),
        ],
        subtotal=11.76,
        tax=0.0,
        total=11.76,
        confidence=0.7,
        notes=[],
    )
    app.session_state["active_receipt"] = receipt.model_dump()
    app.session_state["receipt_version"] = 1
    app.session_state["active_image_hash"] = None
    app.session_state["active_method"] = "local-paddleocr"
    sample_receipt = Path(__file__).parents[1] / "sample_data" / "sample_receipt.png"
    app.session_state["active_source_files"] = [
        {
            "data": sample_receipt.read_bytes(),
            "filename": sample_receipt.name,
            "media_type": "image/png",
        }
    ]
    app.session_state["ocr_text"] = "ITEMS PURCHASED: 2"
    app.session_state["missing_item_candidates"] = []
    app.session_state["line_decisions"] = [
        {
            "index": 1,
            "text": "MI TIENDA HUEVOS ROJOS LG F 4.48",
            "confidence": 0.94,
            "role": "ignored",
            "reason": "No safe parser match",
        },
        {
            "index": 2,
            "text": "Contactless Chip USD 11.76",
            "confidence": 0.98,
            "role": "ignored",
            "reason": "Payment terminal text",
        },
    ]

    app.run()

    assert not app.exception
    assert any(
        heading.value == "Review unused OCR lines"
        for heading in app.subheader
    )
    recovery_table = app.dataframe[1].value
    assert list(recovery_table["Item name"]) == ["MI TIENDA HUEVOS ROJOS LG"]
    assert recovery_table.loc[0, "Line price"] == 4.48
    assert "Contactless Chip USD" not in recovery_table["Supporting OCR"].tolist()

    viewer = next(
        toggle for toggle in app.toggle if toggle.label == "View original receipt"
    )
    viewer.set_value(True).run()

    assert not app.exception
    assert any(
        "Your current corrections stay in place." in caption.value
        for caption in app.caption
    )


def test_receipt_merchant_suggestions_exclude_survey_prompt(
    tmp_path: Path, monkeypatch
) -> None:
    from receipt_reader.models import ReceiptExtraction, ReceiptItem

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    receipt = ReceiptExtraction(
        store_name="how are we doing?",
        transaction_date="2026-09-17",
        currency="USD",
        items=[
            ReceiptItem(
                name="1 S Iced Mocha",
                quantity=1,
                unit_price=2.39,
                discount=None,
                line_total=2.39,
            )
        ],
        subtotal=2.39,
        tax=0.19,
        total=2.58,
        confidence=0.8,
        notes=[],
    )
    app.session_state["active_receipt"] = receipt.model_dump()
    app.session_state["receipt_version"] = 1
    app.session_state["active_image_hash"] = None
    app.session_state["active_method"] = "local-paddleocr"
    app.session_state["line_decisions"] = [
        {"text": "how are we doing?", "role": "ignored"},
        {"text": "McDonald's Restaurant #2881", "role": "store"},
        {"text": "11700 BLANCO RD", "role": "ignored"},
    ]
    app.run()

    merchant_selector = next(
        field
        for field in app.selectbox
        if field.label == "Merchant suggestions from receipt"
    )
    assert "McDonald's Restaurant #2881" in merchant_selector.options
    assert "how are we doing?" not in merchant_selector.options


def test_saved_receipt_apply_category_to_all_is_one_action(
    tmp_path: Path, monkeypatch
) -> None:
    from receipt_reader.database import (
        get_receipt,
        initialize_database,
        save_receipt,
    )
    from receipt_reader.models import ReceiptExtraction, ReceiptItem

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    receipt = ReceiptExtraction(
        store_name="McDonald's #2881",
        transaction_date="2026-09-17",
        currency="USD",
        items=[
            ReceiptItem(
                name="Quarter Pounder",
                quantity=1,
                unit_price=0.0,
                discount=None,
                line_total=0.0,
                category="Groceries",
            ),
            ReceiptItem(
                name="Iced Mocha",
                quantity=1,
                unit_price=2.39,
                discount=None,
                line_total=2.39,
                category="Uncategorized",
            ),
        ],
        subtotal=2.39,
        tax=0.19,
        total=2.58,
        confidence=0.9,
        notes=[],
    )
    initialize_database(tmp_path / "receipts.db")
    receipt_id = save_receipt(tmp_path / "receipts.db", receipt)
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()

    category_editor = next(
        table
        for table in app.dataframe
        if list(table.value.columns)
        == ["Receipt row", "Item", "Line price", "Category"]
    )
    assert list(category_editor.value["Item"]) == [
        "Quarter Pounder",
        "Iced Mocha",
    ]
    next(
        field
        for field in app.selectbox
        if field.label == "Category for all items"
    ).set_value("Dining & Restaurants")
    next(
        button
        for button in app.button
        if button.label == "Apply category to all items"
    ).click().run()

    assert not app.exception
    saved = get_receipt(tmp_path / "receipts.db", receipt_id)
    assert [item["category"] for item in saved["items"]] == [
        "Dining & Restaurants",
        "Dining & Restaurants",
    ]


def test_saved_receipt_item_row_can_be_deleted(tmp_path: Path, monkeypatch) -> None:
    from receipt_reader.database import (
        get_receipt,
        initialize_database,
        save_receipt,
    )
    from receipt_reader.models import ReceiptExtraction, ReceiptItem

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    database = tmp_path / "receipts.db"
    initialize_database(database)
    receipt_id = save_receipt(
        database,
        ReceiptExtraction(
            store_name="PetSmart",
            transaction_date="2026-09-30",
            currency="USD",
            items=[
                ReceiptItem(
                    name="Dog food",
                    quantity=1,
                    unit_price=12.00,
                    discount=None,
                    line_total=12.00,
                    category="Pet food",
                ),
                ReceiptItem(
                    name="Rewards information",
                    quantity=1,
                    unit_price=0.00,
                    discount=None,
                    line_total=0.00,
                ),
                ReceiptItem(
                    name="Cat litter",
                    quantity=1,
                    unit_price=8.00,
                    discount=None,
                    line_total=8.00,
                    category="Pet Care",
                ),
            ],
            subtotal=20.00,
            tax=1.65,
            total=21.65,
            confidence=0.9,
            notes=[],
        ),
    )

    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    next(
        field
        for field in app.selectbox
        if field.label == "Select the row to delete"
    ).set_value(1).run()
    next(
        checkbox
        for checkbox in app.checkbox
        if checkbox.label == "Delete row 2: Rewards information"
    ).check().run()
    next(
        button
        for button in app.button
        if button.label == "Delete selected item row"
    ).click().run()

    assert not app.exception
    saved = get_receipt(database, receipt_id)
    assert saved is not None
    assert [item["name"] for item in saved["items"]] == [
        "Dog food",
        "Cat litter",
    ]
    assert saved["subtotal"] == 20.0
    assert saved["tax"] == 1.65
    assert saved["total"] == 21.65
    assert any(
        "Deleted row 2: Rewards information" in message.value
        for message in app.success
    )



def test_incomplete_expense_date_range_hides_all_time_totals(
    tmp_path: Path, monkeypatch
) -> None:
    from receipt_reader.database import initialize_database, save_receipt
    from receipt_reader.models import ReceiptExtraction, ReceiptItem

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    database = tmp_path / "receipts.db"
    initialize_database(database)
    save_receipt(
        database,
        ReceiptExtraction(
            store_name="McDonald's #2881",
            transaction_date="2026-09-17",
            currency="USD",
            items=[
                ReceiptItem(
                    name="Iced Mocha",
                    quantity=1,
                    unit_price=2.39,
                    discount=None,
                    line_total=2.39,
                    category="Dining & Restaurants",
                )
            ],
            subtotal=2.39,
            tax=0.19,
            total=2.58,
            confidence=0.9,
            notes=[],
        ),
    )
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()

    next(
        radio for radio in app.radio if radio.label == "Expense dates"
    ).set_value("Choose a range").run()
    next(
        field for field in app.date_input if field.label == "Expense date range"
    ).set_value((date(2026, 9, 17),)).run()

    assert not app.exception
    assert any(
        "Select an ending date" in message.value for message in app.info
    )
    assert not any(
        metric.label == "Total item expenses" for metric in app.metric
    )


def test_expense_totals_hide_zero_dollar_categories(
    tmp_path: Path, monkeypatch
) -> None:
    from receipt_reader.database import initialize_database, save_receipt
    from receipt_reader.models import ReceiptExtraction, ReceiptItem

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    database = tmp_path / "receipts.db"
    initialize_database(database)
    save_receipt(
        database,
        ReceiptExtraction(
            store_name="Sample Store",
            transaction_date="2026-09-21",
            currency="USD",
            items=[
                ReceiptItem(
                    name="Reward item",
                    quantity=1,
                    unit_price=0.0,
                    discount=None,
                    line_total=0.0,
                    category="Groceries",
                ),
                ReceiptItem(
                    name="Coffee",
                    quantity=1,
                    unit_price=2.39,
                    discount=None,
                    line_total=2.39,
                    category="Dining & Restaurants",
                ),
            ],
            subtotal=2.39,
            tax=0.19,
            total=2.58,
            confidence=0.9,
            notes=[],
        ),
    )

    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()

    assert not app.exception
    expense_table = next(
        table for table in app.dataframe
        if list(table.value.columns) == ["Expense", "Amount"]
    )
    assert "Groceries" not in set(expense_table.value["Expense"])
    assert set(expense_table.value["Expense"]) == {"Dining & Restaurants"}


def test_mobile_friendly_category_editor_updates_review_table(
    tmp_path: Path, monkeypatch
) -> None:
    from receipt_reader.models import ReceiptItem, blank_receipt

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path / "data"))
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    receipt = blank_receipt()
    receipt.items = [
        ReceiptItem(
            name="S Coke",
            quantity=1,
            unit_price=1.19,
            line_total=1.19,
            discount=None,
            tax_marker="T",
        )
    ]
    receipt.subtotal, receipt.tax, receipt.total = 1.19, 0.10, 1.29
    app.session_state["active_receipt"] = receipt.model_dump()
    app.session_state["receipt_version"] = 1
    app.session_state["active_image_hash"] = None
    app.session_state["active_method"] = "local-paddleocr"
    app.run()

    next(
        field
        for field in app.selectbox
        if field.label == "Expense category"
    ).set_value("Dining & Restaurants")
    next(
        button
        for button in app.button
        if button.label == "Apply changes to this item"
    ).click().run()

    review_table = next(
        table for table in app.dataframe if "Category" in table.value.columns
    )
    assert review_table.value.loc[0, "Category"] == "Dining & Restaurants"
    assert any(
        "Updated row 1: S Coke, $1.19" in message.value
        for message in app.success
    )


def _mobile_editor_app(tmp_path: Path, monkeypatch):
    from receipt_reader.models import ReceiptItem, blank_receipt

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path / "data"))
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    receipt = blank_receipt()
    receipt.items = [
        ReceiptItem(
            name="S Coke", quantity=1, unit_price=1.19, line_total=1.19,
            discount=None, tax_marker="T",
        ),
        ReceiptItem(
            name="Tex", quantity=1, unit_price=None, line_total=0.0,
            discount=None,
        ),
    ]
    receipt.subtotal, receipt.tax, receipt.total = 4.57, 0.10, 4.67
    app.session_state["active_receipt"] = receipt.model_dump()
    app.session_state["receipt_version"] = 1
    app.session_state["active_image_hash"] = None
    app.session_state["active_method"] = "local-paddleocr"
    return app.run()


def _text_field(app, label):
    return next(field for field in app.text_input if field.label == label)


def test_mobile_item_editor_corrects_name_quantity_price_and_tax_marker(
    tmp_path: Path, monkeypatch
) -> None:
    app = _mobile_editor_app(tmp_path, monkeypatch)
    next(
        field for field in app.selectbox if field.label == "Item to edit"
    ).set_value(1).run()

    assert _text_field(app, "Item name").value == "Tex"
    _text_field(app, "Item name").set_value("Ground Coffee")
    _text_field(app, "Line price (charged for the whole row)").set_value("$3.38")
    _text_field(app, "Quantity").set_value("2")
    _text_field(app, "Unit price (optional)").set_value("1.69")
    next(
        field for field in app.selectbox if field.label == "Tax marker"
    ).set_value("F")
    next(
        button for button in app.button
        if button.label == "Apply changes to this item"
    ).click().run()

    assert not app.exception
    review_table = next(
        table.value for table in app.dataframe
        if "Line total" in table.value.columns
    )
    edited = review_table.loc[1]
    assert edited["Item"] == "Ground Coffee"
    assert edited["Line total"] == pytest.approx(3.38)
    assert edited["Qty"] == pytest.approx(2.0)
    assert edited["Unit price"] == pytest.approx(1.69)
    assert edited["Tax marker"] == "F"
    untouched = review_table.loc[0]
    assert untouched["Item"] == "S Coke"
    assert untouched["Line total"] == pytest.approx(1.19)
    assert untouched["Tax marker"] == "T"


@pytest.mark.parametrize(
    ("label", "value", "message"),
    [
        ("Item name", "  ", "Enter an item name."),
        ("Line price (charged for the whole row)", "abc", "Line price must be a number"),
        ("Quantity", "0", "Quantity must be a positive number"),
    ],
)
def test_mobile_item_editor_rejects_invalid_values(
    tmp_path: Path, monkeypatch, label, value, message
) -> None:
    app = _mobile_editor_app(tmp_path, monkeypatch)
    _text_field(app, label).set_value(value)
    next(
        button for button in app.button
        if button.label == "Apply changes to this item"
    ).click().run()

    assert not app.exception
    assert any(message in error.value for error in app.error)
    review_table = next(
        table.value for table in app.dataframe
        if "Line total" in table.value.columns
    )
    assert review_table.loc[0, "Item"] == "S Coke"
    assert review_table.loc[0, "Line total"] == pytest.approx(1.19)




def test_user_can_split_combined_ocr_item_without_guessing_prices(
    tmp_path: Path, monkeypatch
) -> None:
    from receipt_reader.models import ReceiptItem, blank_receipt

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path / "data"))
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    receipt = blank_receipt()
    receipt.items = [
        ReceiptItem(
            name="MI TIENDA HUEVOS ROJOS LG F LACTAID 2% MILK F 4.48",
            quantity=1,
            unit_price=7.28,
            line_total=7.28,
            discount=None,
        )
    ]
    app.session_state["active_receipt"] = receipt.model_dump()
    app.session_state["receipt_version"] = 1
    app.session_state["active_method"] = "local-paddleocr"
    app.session_state["ocr_text"] = "ITEMS PURCHASED:2"
    app.run()

    assert not app.exception
    assert any(
        exp.label == "Split a combined OCR item" for exp in app.expander
    )
    for label, value in [
        ("First purchased item name", "MI TIENDA HUEVOS ROJOS LG"),
        ("First charged line price", "7.28"),
        ("Second purchased item name", "LACTAID 2% MILK"),
        ("Second charged line price", "4.48"),
    ]:
        next(field for field in app.text_input if field.label == label).set_value(value)
    next(
        button for button in app.button
        if button.label == "Replace combined row with two items"
    ).click().run()

    assert not app.exception
    items = app.session_state["active_receipt"]["items"]
    assert [item["name"] for item in items] == [
        "MI TIENDA HUEVOS ROJOS LG", "LACTAID 2% MILK"
    ]
    assert [item["line_total"] for item in items] == [7.28, 4.48]
    assert all(item["tax_marker"] is None for item in items)
    assert any(metric.label == "Item rows entered" and metric.value == "2" for metric in app.metric)



def test_split_review_tolerates_incomplete_ocr_display_values(
    tmp_path: Path, monkeypatch
) -> None:
    from receipt_reader.models import ReceiptItem, blank_receipt

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path / "data"))
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    receipt = blank_receipt()
    receipt.items = [
        ReceiptItem(
            name="COMBINED OCR ROW",
            quantity=1,
            unit_price=0,
            line_total=0,
            discount=None,
        )
    ]
    payload = receipt.model_dump()
    payload["items"][0]["line_total"] = None
    app.session_state["active_receipt"] = payload
    app.session_state["receipt_version"] = 1
    app.session_state["active_method"] = "local-paddleocr"
    app.session_state["ocr_text"] = "ITEMS PURCHASED:2"
    app.run()

    assert not app.exception
    assert any(
        exp.label == "Split a combined OCR item" for exp in app.expander
    )
    assert any("price not identified" in caption.value for caption in app.caption)


def test_review_grid_keeps_printed_row_order_with_duplicate_suggestions(
    tmp_path, monkeypatch
) -> None:
    from receipt_reader.models import ReceiptItem, blank_receipt

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    receipt = blank_receipt()
    receipt.items = [
        ReceiptItem(name="First product", quantity=1, unit_price=1.00,
                    discount=None, line_total=1.00),
        ReceiptItem(name="Repeated product", quantity=1, unit_price=2.00,
                    discount=None, line_total=2.00),
        ReceiptItem(name="Third product", quantity=1, unit_price=3.00,
                    discount=None, line_total=3.00),
        ReceiptItem(name="Repeated product", quantity=1, unit_price=2.00,
                    discount=None, line_total=2.00),
    ]
    receipt.subtotal, receipt.tax, receipt.total = 8.00, 0.00, 8.00
    app.session_state["active_receipt"] = receipt.model_dump()
    app.session_state["receipt_version"] = 1
    app.session_state["active_image_hash"] = None
    app.run()

    assert not app.exception
    table = app.dataframe[0].value
    assert list(table["Receipt order"]) == [1, 2, 3, 4]
    assert list(table["Item"]) == [
        "First product", "Repeated product", "Third product", "Repeated product"
    ]
    assert any("printed receipt order" in c.value for c in app.caption)


def test_walmart_savings_compares_net_purchases_without_false_warning(
    tmp_path, monkeypatch
) -> None:
    from receipt_reader.models import ReceiptItem, blank_receipt

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    receipt = blank_receipt()
    receipt.store_name = "Walmart"
    receipt.transaction_date = "2026-09-28"
    receipt.items = [
        ReceiptItem(
            name="Reviewed Walmart purchases", quantity=1,
            unit_price=None, discount=None, line_total=313.07
        )
    ]
    receipt.subtotal, receipt.tax, receipt.total = 326.43, 10.18, 323.25
    receipt.notes = [
        "Printed order savings: $13.36; compare reviewed line prices "
        "with the after-savings amount."
    ]
    app.session_state["active_receipt"] = receipt.model_dump()
    app.session_state["receipt_version"] = 1
    app.session_state["active_image_hash"] = None
    app.run()

    assert not app.exception
    assert any("Amounts match" in message.value for message in app.success)
    assert not any("Amounts differ" in message.value for message in app.warning)
    assert any("expected purchases $313.07" in c.value for c in app.caption)
    assert any(
        "Purchases after printed savings" in block.value
        for block in app.markdown
    )


def test_ocr_diagnostic_download_is_opt_in_and_redacted(tmp_path, monkeypatch):
    """The review UI only exposes the bounded diagnostic through a download control."""
    import json
    import streamlit as st
    import receipt_reader.jobs as jobs
    from receipt_reader.local_ocr import OcrLine, OcrResult
    from receipt_reader.ocr_diagnostic import OcrDiagnosticTrace

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    database = tmp_path / "receipts.db"
    job_id, _ = jobs.create_local_receipt_job(
        database,
        image_hash="diagnostic-review",
        prepared_pages=[b"private-image-bytes"],
        language="en",
        source_files=[],
    )
    jobs._remember_diagnostic(
        job_id,
        OcrDiagnosticTrace(
            standard=OcrResult(
                (
                    OcrLine(
                        "phone 555-222-1234 email person@example.com card 4111 1111 1111 1111",
                        0.9,
                        (1, 2, 3, 4),
                    ),
                ),
                "test",
            ),
            stage="complete",
        ),
    )
    jobs._set_job_stage(
        database,
        job_id,
        status="failed",
        stage="Could not read receipt",
        message="OCR stopped.",
        error="LocalOcrError: test failure",
    )

    downloads = {}
    original_download = st.download_button

    def capture_download(label, data, *args, **kwargs):
        downloads[label] = (data, kwargs)
        return original_download(label, data, *args, **kwargs)

    monkeypatch.setattr(st, "download_button", capture_download)
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    )
    app.query_params["job"] = job_id
    app.run()

    assert not app.exception
    assert "Download OCR Diagnostic JSON" in downloads
    payload, options = downloads["Download OCR Diagnostic JSON"]
    document = json.loads(payload)
    assert options["on_click"] == "ignore"
    assert options["mime"] == "application/json"
    assert options["file_name"] == f"ocr-diagnostic-{job_id[:8]}.json"
    exported = json.dumps(document)
    assert "private-image-bytes" not in exported
    assert "555-222-1234" not in exported
    assert "person@example.com" not in exported
    assert "4111 1111 1111 1111" not in exported


def test_downloads_do_not_rebuild_backup_during_navigation(tmp_path, monkeypatch):
    """Unrequested backup work cannot delay CSVs; requested backups stay fresh."""
    import io
    import json
    import zipfile
    import streamlit as st
    import receipt_reader.database as database
    from receipt_reader.models import ReceiptExtraction, ReceiptItem

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    db = tmp_path / "receipts.db"
    database.initialize_database(db)

    def save_sample(day):
        database.save_receipt(db, ReceiptExtraction(
            store_name="Petco", transaction_date=day, currency="USD",
            items=[ReceiptItem(name="Refill", quantity=3, unit_price=14.49,
                               discount=None, line_total=43.47)],
            subtotal=43.47, tax=3.59, total=47.06, confidence=0.9, notes=[],
        ))

    save_sample("2026-09-11")
    build_calls = []
    original_build = database.build_backup_zip
    def counted_build(*args, **kwargs):
        build_calls.append(True)
        return original_build(*args, **kwargs)
    monkeypatch.setattr(database, "build_backup_zip", counted_build)

    downloads = {}
    original_download = st.download_button
    def capture_download(label, data, *args, **kwargs):
        downloads[label] = (data, kwargs.get("on_click", "rerun"))
        return original_download(label, data, *args, **kwargs)
    monkeypatch.setattr(st, "download_button", capture_download)

    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    assert not app.exception
    assert not build_calls
    next(b for b in app.button if b.label == "Refresh receipts").click().run()
    assert not app.exception
    assert not build_calls
    assert downloads["Download all data as CSV"][0].startswith(bytes([239, 187, 191]))
    assert b"43.47" in downloads["Download all data as CSV"][0]
    assert all(mode == "ignore" for _, mode in downloads.values())

    generate = downloads["Download full backup (database + original files)"][0]
    assert callable(generate)
    # A receipt saved after the page rendered must be in the requested backup.
    save_sample("2026-09-12")
    with zipfile.ZipFile(io.BytesIO(generate())) as archive:
        manifest = json.loads(archive.read("receiptlens-backup/manifest.json"))
    assert manifest["receipt_count"] == 2
    assert len(build_calls) == 1

    # A broken backup must not block ordinary page rendering or CSV creation.
    def failed_backup(*args, **kwargs):
        raise ValueError("Archived source failed verification")
    monkeypatch.setattr(database, "build_backup_zip", failed_backup)
    app.run()
    assert not app.exception
    assert b"43.47" in downloads["Download all data as CSV"][0]
    with pytest.raises(ValueError, match="Archived source failed verification"):
        downloads["Download full backup (database + original files)"][0]()


def test_restored_store_field_preserves_session_value_without_default_warning(
    tmp_path, monkeypatch, caplog
):
    from receipt_reader.models import ReceiptExtraction
    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(
        Path(__file__).parents[1] / "app.py", default_timeout=30
    ).run()
    receipt = ReceiptExtraction(
        store_name="OCR merchant", transaction_date=None, currency="USD",
        items=[], subtotal=None, tax=None, total=None, confidence=0.5, notes=[],
    )
    app.session_state["active_receipt"] = receipt.model_dump()
    app.session_state["receipt_version"] = 2
    app.session_state["active_method"] = "manual"
    app.session_state["store_2"] = "Petco"
    caplog.clear()
    app.run()
    assert not app.exception
    assert next(f for f in app.text_input if f.label == "Store name").value == "Petco"
    assert not any("created with a default value" in r.message for r in caplog.records)

def test_saved_merchant_edit_refreshes_selector_and_preserves_receipt(tmp_path, monkeypatch):
    from receipt_reader.database import initialize_database, save_receipt, get_receipt
    from receipt_reader.models import ReceiptExtraction, ReceiptItem

    monkeypatch.setenv('RECEIPT_DATA_DIR', str(tmp_path))
    db = tmp_path / 'receipts.db'
    initialize_database(db)
    receipt = ReceiptExtraction(
        store_name='01134 PLAZA DEL NORTE', transaction_date='2026-10-03',
        currency='USD', items=[ReceiptItem(name='SD CARD READER', quantity=1, unit_price=5, discount=None, line_total=5)],
        subtotal=5, tax=.41, total=5.41, confidence=.95, notes=[],
    )
    receipt_id = save_receipt(db, receipt, extraction_method='manual')
    app = AppTest.from_file(Path(__file__).parents[1] / 'app.py', default_timeout=30).run()
    assert not app.exception
    selector = next(s for s in app.selectbox if s.label == 'Open saved receipt')
    old_key = selector.key
    next(f for f in app.text_input if f.label == 'Merchant name / location / store number').set_value('Five Below')
    next(b for b in app.button if b.label == 'Save receipt details').click().run()
    assert not app.exception
    selector = next(s for s in app.selectbox if s.label == 'Open saved receipt')
    assert selector.value == receipt_id
    assert selector.key != old_key
    assert 'Five Below' in selector.options[0]
    assert 'PLAZA DEL NORTE' not in selector.options[0]
    stored = get_receipt(db, receipt_id)
    assert stored['store_name'] == 'Five Below'
    assert stored['transaction_date'] == '2026-10-03'
    assert stored['total'] == 5.41
    assert len(stored['items']) == 1
    app.run()
    assert not app.exception
    assert 'Five Below' in next(s for s in app.selectbox if s.label == 'Open saved receipt').options[0]
