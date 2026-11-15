from receipt_reader.models import ReceiptExtraction, ReceiptItem, blank_receipt


def test_receipt_models_clean_text() -> None:
    receipt = ReceiptExtraction(
        store_name="  Corner   Market ",
        transaction_date="2026-09-03",
        currency="usd",
        items=[
            ReceiptItem(
                name="  Whole   Milk ",
                quantity=1,
                unit_price=3.49,
                discount=None,
                line_total=3.49,
            )
        ],
        subtotal=3.49,
        tax=0.0,
        total=3.49,
        confidence=0.98,
        notes=[],
    )

    assert receipt.store_name == "Corner Market"
    assert receipt.currency == "USD"
    assert receipt.items[0].name == "Whole Milk"
    assert receipt.items[0].category == "Uncategorized"


def test_receipt_item_cleans_custom_category() -> None:
    item = ReceiptItem(
        name="Sandpaper",
        category="  Workshop   supplies ",
        quantity=1,
        unit_price=4.99,
        discount=None,
        line_total=4.99,
    )

    assert item.category == "Workshop supplies"


def test_receipt_item_separates_reconciled_ocr_product_columns() -> None:
    item = ReceiptItem(
        name="7/16 BRAD POINT DRILL-CD 57839 $10.99 1 $",
        quantity=1,
        unit_price=10.99,
        discount=None,
        line_total=10.99,
    )

    assert item.name == "7/16 BRAD POINT DRILL-CD"
    assert item.product_code == "57839"


def test_receipt_item_separates_code_first_recovery_line() -> None:
    item = ReceiptItem(
        name="0074410650940 TENNENKOUB 2.25 N 2",
        tax_marker="F",
        quantity=1,
        unit_price=None,
        discount=None,
        line_total=2.25,
    )

    assert item.name == "TENNENKOUB"
    assert item.product_code == "0074410650940"
    assert item.unit_price == 2.25
    assert item.tax_marker == "F"


def test_receipt_item_recovers_supported_marker_from_code_first_line() -> None:
    item = ReceiptItem(
        name="0193937000974 A-Sha He11 3.50 T 1",
        quantity=1,
        unit_price=None,
        discount=None,
        line_total=3.50,
    )

    assert item.name == "A-Sha He11"
    assert item.product_code == "0193937000974"
    assert item.unit_price == 3.50
    assert item.tax_marker == "T"


def test_receipt_item_preserves_code_first_line_when_total_does_not_match() -> None:
    item = ReceiptItem(
        name="0074410650940 TENNENKOUB 3.25 N 2",
        quantity=1,
        unit_price=None,
        discount=None,
        line_total=2.25,
    )

    assert item.name == "0074410650940 TENNENKOUB 3.25 N 2"
    assert item.product_code is None
    assert item.unit_price is None


def test_receipt_item_preserves_numeric_name_suffix_when_values_do_not_match() -> None:
    item = ReceiptItem(
        name="FILTER MODEL 57839 $10.99 2 $",
        quantity=1,
        unit_price=12.99,
        discount=None,
        line_total=12.99,
    )

    assert item.name == "FILTER MODEL 57839 $10.99 2 $"
    assert item.product_code is None


def test_blank_receipt_is_valid() -> None:
    receipt = blank_receipt()
    assert receipt.currency == "USD"
    assert receipt.items == []
    assert receipt.total is None
