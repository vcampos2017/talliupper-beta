from receipt_reader.models import ReceiptExtraction, ReceiptItem
from receipt_reader.review_report import (
    build_printable_item_table_html,
    build_review_report_html,
    review_report_filename,
)


def test_printable_item_table_keeps_names_and_columns_in_html() -> None:
    report = build_printable_item_table_html(
        [
            {
                "Receipt order": 1,
                "Item": "French Bread",
                "Product code": "200989000000",
                "Category": "Groceries",
                "Qty": 1,
                "Line total": 1.47,
            }
        ]
    )

    assert 'class="receipt-print-only"' in report
    assert 'class="receipt-print-table"' in report
    assert "French Bread" in report
    assert "200989000000" in report
    assert "$1.47" in report


def test_pre_save_report_contains_items_totals_and_difference() -> None:
    receipt = ReceiptExtraction(
        store_name="Walmart <Store>",
        transaction_date="2026-09-05",
        currency="USD",
        items=[
            ReceiptItem(
                name="Groceries & household",
                product_code="00123",
                category="Groceries",
                quantity=1,
                unit_price=244.39,
                discount=None,
                line_total=244.39,
            )
        ],
        subtotal=211.43,
        tax=5.51,
        total=216.94,
        confidence=0.8,
        notes=[],
    )

    report = build_review_report_html(receipt, expected_count=36)

    assert "Not yet saved" in report
    assert "1 entered / 36 printed" in report
    assert "$244.39" in report
    assert "$211.43" in report
    assert "$32.96" in report
    assert "Groceries &amp; household" in report
    assert "Walmart &lt;Store&gt;" in report
    assert 'onclick="window.print()"' in report
    assert "@page { size: Letter portrait" in report
    assert "table-layout: fixed" in report
    assert review_report_filename(receipt) == (
        "receipt-review-2026-09-05-walmart-store.html"
    )

def test_pre_save_report_marks_possible_split_rows() -> None:
    receipt = ReceiptExtraction(
        store_name="Walmart",
        transaction_date="2026-09-05",
        currency="USD",
        items=[
            ReceiptItem(
                name="Freshness Guaranteed Boneless, Skinless",
                quantity=1,
                unit_price=9.14,
                discount=None,
                line_total=9.14,
            ),
            ReceiptItem(
                name="Chicken Breasts, 2.75 - 7.0 lb Tray",
                quantity=1,
                unit_price=10.71,
                discount=None,
                line_total=10.71,
            ),
        ],
        subtotal=9.14,
        tax=0.0,
        total=9.14,
        confidence=0.8,
        notes=[],
    )

    report = build_review_report_html(receipt)

    assert "Possible split from receipt row 1" in report


def test_pre_save_report_separates_tip_from_printed_subtotal() -> None:
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

    report = build_review_report_html(receipt)

    assert "Tip / gratuity" in report
    assert '<div class="value">$6.00</div>' in report
    assert '<div class="value">$20.00</div>' in report



def test_pre_save_report_reconciles_walmart_printed_savings() -> None:
    receipt = ReceiptExtraction(
        store_name="Walmart", transaction_date="2026-09-28", currency="USD",
        items=[ReceiptItem(
            name="Reviewed order items", quantity=1, unit_price=None,
            discount=None, line_total=313.07
        )],
        subtotal=326.43, tax=10.18, total=323.25,
        confidence=0.8,
        notes=["Printed order savings: $13.36; compare reviewed line prices "
               "with the after-savings amount."],
    )
    report = build_review_report_html(receipt)
    assert "Printed savings" in report
    assert "Expected purchases after savings" in report
    assert "$313.07" in report
    assert "$326.43" in report
    assert "$0.00" in report
    assert 'class="value match"' in report
