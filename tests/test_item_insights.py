import os
from pathlib import Path

from streamlit.testing.v1 import AppTest

from receipt_reader.database import initialize_database, save_receipt
from receipt_reader.models import ReceiptExtraction, ReceiptItem


def insight_receipt(
    *, merchant: str, transaction_date: str, price: float,
    item_name: str = "Paper Towels",
) -> ReceiptExtraction:
    return ReceiptExtraction(
        store_name=merchant,
        transaction_date=transaction_date,
        currency="USD",
        items=[
            ReceiptItem(
                name=item_name,
                category="Household",
                quantity=1,
                unit_price=None,
                discount=None,
                line_total=price,
            )
        ],
        subtotal=price,
        tax=0.0,
        total=price,
        confidence=1.0,
        notes=[],
    )


def test_item_insights_search_metrics_history_and_no_results(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "receipts.db"
    initialize_database(database)
    first_id = save_receipt(
        database,
        insight_receipt(
            merchant="Walmart", transaction_date="2026-09-01", price=4.00
        ),
    )
    second_id = save_receipt(
        database,
        insight_receipt(
            merchant="H-E-B", transaction_date="2026-09-10", price=5.00
        ),
    )

    os.environ["RECEIPT_DATA_DIR"] = str(data_dir)
    app = AppTest.from_file(
        Path(__file__).resolve().parents[1] / "app.py",
        default_timeout=30,
    ).run()

    search = next(
        field
        for field in app.text_input
        if field.label == "Search saved item names"
    )
    search.set_value("paper towels").run()

    assert not app.exception, app.exception
    assert next(
        field
        for field in app.selectbox
        if field.label == "Matching saved item"
    ).value == "Paper Towels"
    metric_values = {
        metric.label: metric.value
        for metric in app.metric
        if metric.label
        in {
            "Matching purchases",
            "Recorded line spending",
            "Lowest comparable price",
            "Most recent dated price",
        }
    }
    assert metric_values == {
        "Matching purchases": "2",
        "Recorded line spending": "USD 9.00",
        "Lowest comparable price": "USD 4.00",
        "Most recent dated price": "USD 5.00",
    }
    history = next(
        table.value
        for table in app.dataframe
        if {"Reference", "Comparable price", "Price basis"}.issubset(
            table.value.columns
        )
    )
    assert list(history["Reference"]) == [
        f"RL-{first_id:06d}",
        f"RL-{second_id:06d}",
    ]
    assert list(history["Comparable price"]) == [4.0, 5.0]

    next(
        field
        for field in app.text_input
        if field.label == "Search saved item names"
    ).set_value("definitely-not-saved").run()
    assert any("No saved item name contains" in warning.value for warning in app.warning)


def test_item_insights_can_create_and_remove_an_alias_group(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "receipts.db"
    initialize_database(database)
    first_name = "HEB AA CFW LG EGGS 18CT C"
    second_name = "HEB AA CFW LG EGGS 18CT C F WHL"
    save_receipt(
        database,
        insight_receipt(
            merchant="HEB",
            transaction_date="2026-09-01",
            price=2.98,
            item_name=first_name,
        ),
    )
    save_receipt(
        database,
        insight_receipt(
            merchant="H-E-B",
            transaction_date="2026-09-10",
            price=3.48,
            item_name=second_name,
        ),
    )

    os.environ["RECEIPT_DATA_DIR"] = str(data_dir)
    app = AppTest.from_file(
        Path(__file__).resolve().parents[1] / "app.py",
        default_timeout=30,
    ).run()

    next(
        field
        for field in app.text_input
        if field.label == "Find recorded names to group"
    ).set_value("egg").run()
    next(
        field
        for field in app.multiselect
        if field.label == "Recorded item names"
    ).set_value([first_name, second_name])
    next(
        field
        for field in app.text_input
        if field.label == "Canonical product name"
    ).set_value("Large eggs, 18 count")
    next(
        button
        for button in app.button
        if button.label == "Save product-name group"
    ).click().run()

    assert not app.exception, app.exception
    assert any(
        'Saved "Large eggs, 18 count"' in message.value
        for message in app.success
    )
    next(
        field
        for field in app.text_input
        if field.label == "Search saved item names"
    ).set_value("f whl").run()
    assert next(
        field
        for field in app.selectbox
        if field.label == "Matching saved item"
    ).value == "Large eggs, 18 count"
    assert next(
        metric
        for metric in app.metric
        if metric.label == "Matching purchases"
    ).value == "2"

    next(
        button
        for button in app.button
        if button.label == "Remove selected product-name group"
    ).click().run()
    assert not app.exception, app.exception
    assert any(
        'Removed "Large eggs, 18 count"' in message.value
        for message in app.success
    )


def test_item_insights_can_save_and_clear_package_size(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "receipts.db"
    initialize_database(database)
    save_receipt(
        database,
        insight_receipt(
            merchant="H-E-B",
            transaction_date="2026-09-10",
            price=2.98,
            item_name="Large eggs",
        ),
    )

    os.environ["RECEIPT_DATA_DIR"] = str(data_dir)
    app = AppTest.from_file(
        Path(__file__).resolve().parents[1] / "app.py",
        default_timeout=30,
    ).run()
    next(
        field
        for field in app.text_input
        if field.label == "Search saved item names"
    ).set_value("large eggs").run()
    assert "2026-09-10" in next(
        field
        for field in app.selectbox
        if field.label == "Saved purchase row"
    ).value
    assert "receipt item 1" in next(
        field
        for field in app.selectbox
        if field.label == "Saved purchase row"
    ).value
    next(
        field
        for field in app.number_input
        if field.label == "Package amount per purchased unit"
    ).set_value(18)
    next(
        button for button in app.button if button.label == "Save package size"
    ).click().run()

    assert not app.exception, app.exception
    assert any("Saved 18 count" in message.value for message in app.success)
    metric_values = {metric.label: metric.value for metric in app.metric}
    assert metric_values["Normalized coverage"] == "1 of 1"
    assert metric_values["Lowest price per count"] == "USD 0.1656"

    next(
        button for button in app.button if button.label == "Clear package size"
    ).click().run()
    assert not app.exception, app.exception
    assert any("Cleared package size" in message.value for message in app.success)
    assert not any(
        metric.label == "Normalized coverage" for metric in app.metric
    )


def test_item_insights_distinguishes_identical_rows_and_explains_one_point_chart(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    database = data_dir / "receipts.db"
    initialize_database(database)
    receipt = insight_receipt(
        merchant="H-E-B",
        transaction_date="2026-09-10",
        price=2.98,
        item_name="Large eggs",
    )
    receipt.items.append(
        ReceiptItem(
            name="Large eggs",
            category="Household",
            quantity=1,
            unit_price=None,
            discount=None,
            line_total=2.98,
        )
    )
    receipt.subtotal = 5.96
    receipt.total = 5.96
    save_receipt(database, receipt)

    os.environ["RECEIPT_DATA_DIR"] = str(data_dir)
    app = AppTest.from_file(
        Path(__file__).resolve().parents[1] / "app.py",
        default_timeout=30,
    ).run()
    next(
        field
        for field in app.text_input
        if field.label == "Search saved item names"
    ).set_value("large eggs").run()

    selector = next(
        field
        for field in app.selectbox
        if field.label == "Saved purchase row"
    )
    assert len(selector.options) == 2
    assert selector.options[0] != selector.options[1]
    assert "receipt item 1" in selector.options[0]
    assert "receipt item 2" in selector.options[1]
    assert any(
        "Only one purchase date is available" in caption.value
        for caption in app.caption
    )
