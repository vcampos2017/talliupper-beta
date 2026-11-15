from __future__ import annotations

import pytest

from receipt_reader.local_ocr import OcrLine
from receipt_reader.parser import parse_receipt


WALMART_OCR_LINES = [
    "Walmart",
    "SAN ANTONIO, TX",
    "PED TGD VP 002310013592 37.84",
    "SK STVP 40C 019434629387 22.00",
    "GV HH 32OZ 06053818716 F 2.48",
    "GV BTTR QTR 007874237419 F 3.06",
    "PMLT WH MIL 085706500701 F 3.34",
    "PMLT WH MIL 085706500701 F 3.34",
    "PB PURE SA 068113134368 1.66",
    "PB PURE SA 068113134368 1.66",
    "MASHEDPOTAT 019434600204 F 3.24",
    "MAC CHEESE 019434600205 F 3.24",
    "PPPRN PZA 007218073366 F 3.87",
    "MS18L CFBRN 068113112279 F 3.93",
    "SAPPRO 5PK 007618600511 F 4.72",
    "SAPPRO 5PK 007618600051 F 4.72",
    "DELI CHKN 007874237411 4.97",
    "BG PEP BACO 019434651066 F 6.92",
    "BG MARG 019434613661 F 6.92",
    "BG PEPP 019434640752 F 6.92",
    "FRENCH BREA 020098900000 F 1.47",
    "FRENCH BREA 020098900000 F 1.47",
    "SUBTOTAL 127.77",
    "TAX 1 8.25 % 5.62",
    "TAX 12 0 % 0.00",
    "TOTAL 133.39",
    "WMP MASTERCARD CREDIT TEND 133.39",
    "09/05/26 18:22:08",
    "# ITEMS SOLD 20",
]


def test_walmart_receipt_preserves_product_codes_and_all_items() -> None:
    result = parse_receipt([OcrLine(text, 0.99) for text in WALMART_OCR_LINES])
    receipt = result.receipt

    assert receipt.store_name == "Walmart"
    assert receipt.transaction_date == "2026-09-05"
    assert len(receipt.items) == 20
    assert not result.missing_item_candidates
    assert [item.line_total for item in receipt.items] == pytest.approx(
        [
            37.84, 22.00, 2.48, 3.06, 3.34, 3.34, 1.66, 1.66, 3.24, 3.24,
            3.87, 3.93, 4.72, 4.72, 4.97, 6.92, 6.92, 6.92, 1.47, 1.47,
        ]
    )
    assert sum(item.line_total for item in receipt.items) == pytest.approx(127.77)
    assert receipt.items[0].name == "PED TGD VP"
    assert receipt.items[0].product_code == "002310013592"
    assert receipt.items[2].name == "GV HH 32OZ"
    assert receipt.items[2].product_code == "06053818716"
    assert receipt.items[2].tax_marker == "F"
    assert receipt.items[-1].product_code == "020098900000"
    assert (receipt.subtotal, receipt.tax, receipt.total) == (127.77, 5.62, 133.39)


def test_text_walmart_invoice_groups_wrapped_items_across_four_pages() -> None:
    from io import BytesIO
    from reportlab.pdfgen import canvas

    from receipt_reader.walmart_invoice import parse_walmart_invoice_pdf

    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=(612, 792))
    pages = [
        [
            "Invoice", "Sep 28, 2026 order", "Buyer", "Sample Buyer",
            "Example address", "Great Value Macaroni & Cheese,",
            "7.25 oz 59 shopped Qty 2 $1.28",
            "Equate Lens Cleaning Wipes, 100 Count",
            "Unavailable Qty 1 $3.98",
        ],
        ["Large Brown Eggs", "18 Count 59 shopped Qty 1 $4.37"],
        ["Fresh Bananas, Each", "7 weight adjusted Qty 2 $0.11"],
        [
            "Dog Food, Beef and Chicken Recipe",
            "12.5 oz Can", "Qty 1 $23.28",
            "Subtotal $32.50", "Savings -$1.00", "$31.50",
            "Taxes $2.00", "Total $33.50",
        ],
    ]
    for page_number, lines in enumerate(pages, start=1):
        pdf.drawString(40, 760, "Order details - Walmart.com")
        pdf.drawString(40, 740, f"Page {page_number} of 4")
        for offset, line in enumerate(lines):
            pdf.drawString(60, 700 - offset * 20, line)
        pdf.showPage()
    pdf.save()

    result = parse_walmart_invoice_pdf(output.getvalue())
    assert result is not None
    receipt = result.parsed.receipt
    assert receipt.transaction_date == "2026-09-28"
    assert len(receipt.items) == 5
    assert receipt.items[0].name == "Great Value Macaroni & Cheese, 7.25 oz"
    assert receipt.items[0].quantity == 2
    assert receipt.items[0].line_total == 1.28
    assert receipt.items[-1].name == "Dog Food, Beef and Chicken Recipe 12.5 oz Can"
    assert (receipt.subtotal, receipt.tax, receipt.total) == (32.50, 2.00, 33.50)
    assert any("Unavailable" in note for note in receipt.notes)
    assert any("Listed item prices sum" in note for note in receipt.notes)


def test_printed_savings_note_is_read_only_when_explicit() -> None:
    from receipt_reader.walmart_invoice import printed_order_savings

    assert printed_order_savings([
        "Printed order savings: $13.36; compare reviewed line prices "
        "with the after-savings amount."
    ]) == 13.36
    assert printed_order_savings(["Savings may apply."]) is None


# RL-63: Walmart 10/26/2026 receipt (RL-000082) prints "TAX1 8.2500 % 1.60".
WALMART_RL63_HEAD = [
    "Walmart",
    "# ITEMS SOLD 3",
    "DR PEPPER 7 078000026650 5.67 T",
    "GV AA 36PK 194346622080 13.74 X",
    "15 PK 700 ML 078742367400 F 7.82 N",
    "SUBTOTAL 27.23",
]
WALMART_RL63_TAIL = ["TOTAL 28.83", "WALMART CASH REDEEMED 6.50", "WMP MCARD TEND 22.33"]


@pytest.mark.parametrize(
    "tax_rows",
    [
        ["TAX1 8.2500 % 1.60"],
        ["TAX1 8.2500 %", "1.60"],
        ["1.60", "TAX1 8.2500 %"],
        ["TAXI 8.2500 % 1.60"],
        ["TAXl 8.2500 %", "1.60"],
    ],
    ids=["one-row", "amount-below", "amount-above", "label-misread", "misread-and-split"],
)
def test_walmart_tax1_rate_line_reads_printed_tax(tax_rows: list[str]) -> None:
    lines = WALMART_RL63_HEAD + tax_rows + WALMART_RL63_TAIL
    receipt = parse_receipt([OcrLine(text, 0.99) for text in lines]).receipt

    assert (receipt.subtotal, receipt.tax, receipt.total) == (27.23, 1.60, 28.83)
    assert [item.line_total for item in receipt.items] == [5.67, 13.74, 7.82]


def test_split_tax_row_never_takes_the_subtotal_amount() -> None:
    # No TOTAL row, so tax cannot be inferred as total minus subtotal.
    lines = WALMART_RL63_HEAD[:-1] + ["SUBTOTAL", "27.23", "TAX1 8.2500 %"]
    receipt = parse_receipt([OcrLine(text, 0.99) for text in lines]).receipt

    assert receipt.tax is None


def test_taxi_item_is_not_read_as_tax() -> None:
    lines = ["Walmart", "TAXI FARE 25.00", "SUBTOTAL 25.00", "TOTAL 25.00"]
    receipt = parse_receipt([OcrLine(text, 0.99) for text in lines]).receipt

    assert [item.name for item in receipt.items] == ["TAXI FARE"]
    assert receipt.tax == 0.0
