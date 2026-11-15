"""H-E-B vendor profile: two-line quantity rows, coupon notes, unit counts.

Fixture lines follow a real H-E-B receipt (2026-10-25) as PaddleOCR read it:
multi-unit purchases print the numbered name on one row and
``3 Ea. @ 1/ 2.77  8.31`` on the next.
"""

from receipt_reader.local_ocr import OcrLine
from receipt_reader.parser import (
    parse_receipt,
    printed_count_matches,
    purchased_unit_count,
)
from receipt_reader.vendors import vendor_profile_for
from receipt_reader.vendors import heb


def _lines(*texts: str) -> list[OcrLine]:
    return [OcrLine(text, 0.95) for text in texts]


HEB_RECEIPT = (
    "H-E-B",
    "1077 4384 1025 2616 5900 195",
    "1 QUANTUM THERAZINC ELDRBRR",
    "3 Ea.@ 1/ 2.77 8.31 Q",
    "2 LINDT EXCELLENCE SEA SALT",
    "2 Ea. @ 1/ 3.97 T 7.94",
    "COUPON $1.00 orig 4.97 Ea.",
    "3 KEWPIE MAYONNAISE F 5.37",
    "4 GRNS PPKT PB CAP SZ 15.8 T 19.98",
    "Subtotal 41.60",
    "Tax 2.30",
    "Total 43.90",
    "MASTRCRD EPS 43.90",
    "ITEMS PURCHASED: 7",
    "SALE & PRICE CUT SAVINGS : $1.62",
    "FREE/COUPON SAVINGS : $2.00",
)


def test_heb_profile_detected_only_for_heb():
    assert vendor_profile_for(_lines(*HEB_RECEIPT)) is heb.PROFILE
    assert vendor_profile_for(_lines("WALMART", "MILK 3.48 N")) is None


def test_two_line_quantity_rows_keep_name_quantity_and_price():
    result = parse_receipt(_lines(*HEB_RECEIPT))
    items = result.receipt.items

    assert [item.name for item in items] == [
        "QUANTUM THERAZINC ELDRBRR",
        "LINDT EXCELLENCE SEA SALT",
        "KEWPIE MAYONNAISE",
        "GRNS PPKT PB CAP SZ 15.8",
    ]
    assert [item.quantity for item in items] == [3, 2, 1, 1]
    assert [item.unit_price for item in items][:2] == [2.77, 3.97]
    assert [item.line_total for item in items] == [8.31, 7.94, 5.37, 19.98]
    # Q is not a supported marker, so it stays unknown instead of guessed.
    assert [item.tax_marker for item in items] == [None, "T", "F", "T"]


def test_coupon_note_is_metadata_not_a_purchase_or_discount():
    result = parse_receipt(_lines(*HEB_RECEIPT))

    coupon = next(d for d in result.decisions if d.text.startswith("COUPON"))
    assert coupon.role == "item_metadata"
    # The Lindt line total is already net of the coupon; it must not drop again.
    assert result.receipt.items[1].line_total == 7.94
    assert result.receipt.items[1].discount is None
    assert result.missing_item_candidates == ()


def test_printed_unit_count_matches_without_false_missing_items():
    result = parse_receipt(_lines(*HEB_RECEIPT))

    assert result.receipt.subtotal == 41.60
    assert result.receipt.total == 43.90
    assert result.missing_item_candidates == ()
    assert not any("Receipt lists 7 items" in note for note in result.receipt.notes)


def test_dropped_quantity_digit_is_recovered_from_the_arithmetic():
    lines = list(HEB_RECEIPT)
    lines[5] = "Ea. @ 1/ 3.97 T 7.94"
    items = parse_receipt(_lines(*lines)).receipt.items

    assert items[1].name == "LINDT EXCELLENCE SEA SALT"
    assert items[1].quantity == 2


def test_duplicate_ocr_name_region_is_collapsed():
    lines = list(HEB_RECEIPT)
    lines.insert(5, "2 LINDT EXCELLENCE SEA SALT")
    items = parse_receipt(_lines(*lines)).receipt.items

    assert [item.name for item in items].count("LINDT EXCELLENCE SEA SALT") == 1
    assert items[1].quantity == 2


def test_quantity_row_that_does_not_reconcile_is_left_alone():
    assert heb.preprocess(_lines("1 WIDGET", "3 Ea. @ 1/ 2.77 9.99")) == _lines(
        "1 WIDGET", "3 Ea. @ 1/ 2.77 9.99"
    )


def test_unit_count_helpers():
    assert purchased_unit_count([3, 2, 1, 1]) == 7
    # A weighed item counts once; a blank cell counts as one row.
    assert purchased_unit_count([1.42, float("nan"), None]) == 3
    assert printed_count_matches(7, rows=4, units=7)
    assert printed_count_matches(4, rows=4, units=7)
    assert not printed_count_matches(9, rows=4, units=7)
    assert printed_count_matches(None, rows=4, units=7)


def test_untaxed_receipt_without_tip_note_does_not_mention_gratuity():
    # RL-52: H-E-B receipt (2026-10-26) with one untaxed item and no tip.
    receipt = parse_receipt(_lines(
        "H-E-B",
        "1077 6449 1026 2608 5600 195",
        "1 LIPTON TEA BAGS 100CT F 4.97",
        "Total 4.97",
        "MASTRCRD EPS 4.97",
        "ITEMS PURCHASED: 1",
    )).receipt

    assert (receipt.subtotal, receipt.tax, receipt.total) == (4.97, 0.0, 4.97)
    tax_notes = [note for note in receipt.notes if "zero tax was inferred" in note]
    assert len(tax_notes) == 1
    assert "gratuity" not in tax_notes[0]


# RL-60: real PaddleOCR often misreads the H-E-B logo and drops the printed
# row numbers on name lines (live Dell rerun 2026-11-04, job a1d4ddf7).
LOGO_MISREAD_RECEIPT = (
    "E-",
    "1077 4384 1025 2616 5900 195",
    "QUANTUM THERAZINC ELDRBRR",
    "3 Ea.@ 1/ 2.77 8.31 Q",
    "2 LINDT EXCELLENCE SEA SALT",
    "2 Ea. @ 1/ 3.97 T 7.94",
    "COUPON $1.00 orig 4.97 Ea.",
    "KEWPIE MAYONNAISE F 5.37",
    "GRNS PPKT PB CAP SZ 15.8 T 19.98",
    "Subtotal 41.60",
    "Tax 2.30",
    "Total 43.90",
    "CREDIT CARD EPS 43.90",
    "ITEMS PURCHASED: 7",
)


def test_misread_logo_is_recognized_from_the_layout():
    result = parse_receipt(_lines(*LOGO_MISREAD_RECEIPT))
    receipt = result.receipt

    assert receipt.store_name == "H-E-B"
    assert any("inferred from the receipt layout" in note for note in receipt.notes)
    # Knowing the store enables H-E-B's date encoded in the transaction number.
    assert receipt.transaction_date == "2026-10-25"
    assert [item.name for item in receipt.items] == [
        "QUANTUM THERAZINC ELDRBRR",
        "LINDT EXCELLENCE SEA SALT",
        "KEWPIE MAYONNAISE",
        "GRNS PPKT PB CAP SZ 15.8",
    ]
    assert [item.quantity for item in receipt.items] == [3, 2, 1, 1]
    assert result.missing_item_candidates == ()


def test_store_code_in_garbled_text_identifies_heb():
    # Full-length photo header from 2026-10-27 (job 9523b04b).
    lines = _lines("FOR qtuttt", "TEXAS HEB195SON", "MILK F 3.48", "Total 3.48")
    assert vendor_profile_for(lines) is heb.PROFILE
    assert parse_receipt(lines).receipt.store_name == "H-E-B"


def test_layout_evidence_needs_both_quantity_row_and_unit_count():
    quantity_row_only = _lines("CORNER MARKET", "APPLES", "3 Ea. @ 1/ 0.50 1.50")
    unit_count_only = _lines("CORNER MARKET", "APPLES 1.50", "ITEMS PURCHASED: 1")
    assert vendor_profile_for(quantity_row_only) is None
    assert vendor_profile_for(unit_count_only) is None
    assert vendor_profile_for(_lines("THEBES CAFE", "HEBREW BOOKS 9.99")) is None
