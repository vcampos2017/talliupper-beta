import pytest

from receipt_reader.local_ocr import OcrLine
from receipt_reader.parser import (
    amount_at_end,
    date_from_text,
    parse_receipt,
    printed_item_count,
)


SAMPLE_TEXT = [
    "FRESH MART",
    "123 MAIN ST",
    "SAN ANTONIO TX",
    "09/03/2026 18:42",
    "WHOLE MILK 3.49",
    "BREAD 2 @ 2.00 4.00",
    "COUPON -0.50",
    "BANANAS 1.25 @ 0.59 0.74",
    "SUBTOTAL 7.73",
    "TAX 0.62",
    "TOTAL 8.35",
    "VISA 8.35",
]


def sample_lines() -> list[OcrLine]:
    return [
        OcrLine(text, 0.95, (20, index * 30, 700, index * 30 + 24))
        for index, text in enumerate(SAMPLE_TEXT)
    ]


def test_parser_extracts_receipt_fields_and_items() -> None:
    result = parse_receipt(sample_lines())
    receipt = result.receipt

    assert receipt.store_name == "FRESH MART"
    assert receipt.transaction_date == "2026-09-03"
    assert receipt.subtotal == 7.73
    assert receipt.tax == 0.62
    assert receipt.total == 8.35
    assert [item.name for item in receipt.items] == [
        "WHOLE MILK",
        "BREAD",
        "BANANAS",
    ]
    assert receipt.items[1].quantity == 2
    assert receipt.items[1].unit_price == 2
    assert receipt.items[1].discount == 0.5
    assert receipt.items[1].line_total == 3.5
    assert receipt.items[2].quantity == 1.25
    assert receipt.items[2].unit_price == 0.59
    assert receipt.confidence >= 0.9

    roles = {decision.text: decision.role for decision in result.decisions}
    assert roles["FRESH MART"] == "store"
    assert roles["COUPON -0.50"] == "discount"
    assert roles["TOTAL 8.35"] == "total"
    assert roles["VISA 8.35"] == "ignored"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("2026-09-03 18:42", "2026-09-03"),
        ("09/03/26", "2026-09-03"),
        ("09-24-2026", "2026-09-24"),
        ("1/4-20 x 2? CONNECT BLT 8PK", None),
        ("LOW PROF INS 1/4-20 8/PK", None),
        ("SEP 3, 2026", "2026-09-03"),
        ("99/99/2026", None),
    ],
)
def test_date_normalization(source: str, expected: str | None) -> None:
    assert date_from_text(source) == expected


def test_receipt_date_ignores_expiration_and_prefers_timed_transaction() -> None:
    result = parse_receipt([OcrLine(text, 0.95) for text in [
        "H-E-B",
        "ITEM F 3.96",
        "TOTAL SALE 3.96",
        "RECEIPT EXPIRES ON 12-15-26",
        "592938 09-16-26 9:46A 689/21/00195",
    ]])

    assert result.receipt.transaction_date == "2026-09-16"
    expiry = next(
        decision for decision in result.decisions if "EXPIRES" in decision.text
    )
    transaction = next(
        decision for decision in result.decisions if "09-16-26" in decision.text
    )
    assert expiry.role == "ignored"
    assert transaction.role == "date"


def test_receipt_date_does_not_use_expiration_as_fallback() -> None:
    receipt = parse_receipt([OcrLine(text, 0.95) for text in [
        "LOCAL MARKET",
        "ITEM 3.96",
        "TOTAL 3.96",
        "RECEIPT EXPIRES ON 12-15-26",
    ]]).receipt

    assert receipt.transaction_date is None


def test_money_parser_supports_negative_parentheses() -> None:
    parsed = amount_at_end("DISCOUNT ($1.25)")
    assert parsed is not None
    assert parsed[0] == -1.25


def test_parser_infers_missing_summary_values_and_warns() -> None:
    lines = [
        OcrLine("CORNER SHOP", 0.9),
        OcrLine("APPLE 1.00", 0.9),
        OcrLine("TAX 0.08", 0.9),
    ]

    receipt = parse_receipt(lines).receipt

    assert receipt.subtotal == 1.0
    assert receipt.total == 1.08
    assert any("Subtotal was inferred" in note for note in receipt.notes)
    assert any("Total was inferred" in note for note in receipt.notes)


def test_parser_recovers_fragmented_grocery_summary_conservatively() -> None:
    lines = [
        OcrLine("H-E-B SAMPLE EGGS F 2.98", 0.95),
        OcrLine("SAMPLE FRUIT FW 2.68", 0.95),
        OcrLine("SWEET PRODUCE", 0.95),
        OcrLine("0.20 Lbs @ 1.24 FW 0.25", 0.95),
        OcrLine("SOFT DRINK T 5.97", 0.95),
        OcrLine("Sal btal*** 1188", 0.82),
        OcrLine("Sales Tax 6440", 0.70),
        OcrLine("12.37", 0.98),
        OcrLine("MASTERCARD", 0.95),
        OcrLine("ITEMS PURCHASED: 4", 0.95),
        OcrLine("12.37", 0.98),
    ]

    result = parse_receipt(lines)
    receipt = result.receipt

    assert receipt.store_name == "H-E-B"
    assert [item.name for item in receipt.items] == [
        "H-E-B SAMPLE EGGS",
        "SAMPLE FRUIT",
        "SWEET PRODUCE",
        "SOFT DRINK",
    ]
    assert [item.tax_marker for item in receipt.items] == ["F", "FW", "FW", "T"]
    assert receipt.items[2].quantity == pytest.approx(0.20)
    assert receipt.items[2].unit_price == pytest.approx(1.24)
    assert receipt.subtotal == pytest.approx(11.88)
    assert receipt.tax == pytest.approx(0.49)
    assert receipt.total == pytest.approx(12.37)
    assert any("missing decimal" in decision.reason for decision in result.decisions)
    assert any("tax was inferred" in note.lower() for note in receipt.notes)


def test_parser_can_use_card_tender_as_missing_final_total() -> None:
    lines = [
        OcrLine("H-E-B SAMPLE ITEM F 11.88", 0.95),
        OcrLine("Sal btal*** 1188", 0.82),
        OcrLine("Sales Tax 6440", 0.70),
        OcrLine("*** MASTRCRD 12.37", 0.98),
        OcrLine("EFS", 0.98),
    ]

    result = parse_receipt(lines)

    assert result.receipt.subtotal == pytest.approx(11.88)
    assert result.receipt.tax == pytest.approx(0.49)
    assert result.receipt.total == pytest.approx(12.37)
    tender_decision = next(
        decision for decision in result.decisions if "MASTRCRD" in decision.text
    )
    assert tender_decision.role == "total"
    assert "Tender amount" in tender_decision.reason
    assert any("payment tender" in note for note in result.receipt.notes)


def test_split_price_columns_and_offset_rows() -> None:
    lines = [OcrLine("HEB", 0.98, (10, 0, 80, 20))]
    for y, label, price in [
        (40, "EGGS", "2.98"),
        (70, "AVOCADO", "2.68"),
        (100, "ONIONS", "0.25"),
        (130, "SOFT DRINK", "5.97"),
        (160, "SUBTOTAL", "11.88"),
        (190, "Sales Tax", "0.49"),
        (220, "TOTAL", "12.37"),
        (250, "MASTERCARD", "12.37"),
    ]:
        lines.extend(
            [
                OcrLine(label, 0.95, (10, y, 150, y + 20)),
                OcrLine(price, 0.93, (250, y - 3, 300, y + 17)),
            ]
        )
    receipt = parse_receipt(sorted(lines, key=lambda line: line.y)).receipt
    assert [item.name for item in receipt.items] == [
        "EGGS",
        "AVOCADO",
        "ONIONS",
        "SOFT DRINK",
    ]
    assert [item.line_total for item in receipt.items] == [2.98, 2.68, 0.25, 5.97]
    assert (receipt.subtotal, receipt.tax, receipt.total) == (11.88, 0.49, 12.37)


def test_unpositioned_prices_are_not_assigned_to_nearby_descriptions() -> None:
    receipt = parse_receipt(
        [OcrLine("HEB", 0.95), OcrLine("EGGS", 0.95), OcrLine("2.98", 0.95)]
    ).receipt
    assert receipt.items == []


def test_tilted_rows_with_markers_and_weighted_description() -> None:
    rows = [OcrLine("HEB", 0.99, (10, 0, 200, 20))]
    for y, label, price in [
        (60, "EGGS F", "2.98"),
        (110, "AVOCADOS FW", "2.68"),
        (210, "0.20 Lbs @ 1.24 FW", "0.25"),
        (270, "4 SOFT DRINK MINI CANS T", "5.97"),
        (340, "Sale Subtotal***", "11.88"),
        (400, "Sales Tax***", "0.49"),
        (460, "Total Sale***", "12.37"),
    ]:
        rows.extend(
            [
                OcrLine(label, 0.95, (10, y, 600, y + 30)),
                OcrLine(price, 0.95, (800, y + 20, 900, y + 50)),
            ]
        )
    rows.append(OcrLine("SWEET ONION", 0.95, (10, 170, 500, 195)))
    receipt = parse_receipt(sorted(rows, key=lambda line: line.y)).receipt
    assert len(receipt.items) == 4
    assert [item.tax_marker for item in receipt.items] == ["F", "FW", "FW", "T"]
    assert receipt.items[2].name == "SWEET ONION"
    assert receipt.items[2].quantity == 0.2
    assert receipt.items[2].unit_price == 1.24
    assert receipt.items[3].line_total == 5.97
    assert receipt.items[3].quantity == 1
    assert (receipt.subtotal, receipt.tax, receipt.total) == (11.88, 0.49, 12.37)


def test_ambiguous_price_row_remains_unassigned() -> None:
    from receipt_reader.parser import join_price_rows

    rows = [
        OcrLine("UNCERTAIN", 0.9, (0, 20, 100, 40)),
        OcrLine("1.00", 0.9, (200, 0, 250, 20)),
        OcrLine("2.00", 0.9, (200, 40, 250, 60)),
    ]
    assert len(join_price_rows(rows)) == 3


def test_second_pass_suggests_unpriced_product_after_last_item() -> None:
    lines = [
        OcrLine("H-E-B", 0.98),
        OcrLine("SAMPLE EGGS F 2.98", 0.95),
        OcrLine("SAMPLE FRUIT FW 2.68", 0.95),
        OcrLine("SWEET PRODUCE", 0.95),
        OcrLine("0.20 Lbs @ 1.24 FW 0.25", 0.95),
        OcrLine("SPARKLING BEVERAGE MINI CANS", 0.93),
        OcrLine("SUBTOTAL", 0.90),
        OcrLine("ITEMS PURCHASED: 4", 0.98),
    ]

    result = parse_receipt(lines)

    assert len(result.receipt.items) == 3
    assert [candidate.name for candidate in result.missing_item_candidates] == [
        "SPARKLING BEVERAGE MINI CANS"
    ]
    assert result.missing_item_candidates[0].source_line == 6
    decision = next(
        decision
        for decision in result.decisions
        if decision.text == "SPARKLING BEVERAGE MINI CANS"
    )
    assert decision.role == "missing_item_candidate"


def test_second_pass_does_not_treat_dr_product_name_as_an_address() -> None:
    lines = [
        OcrLine("SAMPLE MARKET", 0.98),
        OcrLine("FIRST ITEM 2.98", 0.95),
        OcrLine("SECOND ITEM 2.68", 0.95),
        OcrLine("THIRD ITEM 0.25", 0.95),
        OcrLine("DR PEPPER MINI CANS", 0.93),
        OcrLine("SUBTOTAL 11.88", 0.95),
        OcrLine("ITEMS PURCHASED: 4", 0.98),
    ]

    result = parse_receipt(lines)

    assert [candidate.name for candidate in result.missing_item_candidates] == [
        "DR PEPPER MINI CANS"
    ]


def test_run4_candidates_link_weight_details_and_remove_ocr_debris() -> None:
    lines = [
        OcrLine("1053 1053 0902 2616 4200 195", 1.0),
        OcrLine("HEB AA CFW LG EGGS 18CT C F WHL 2.98", 0.89),
        OcrLine("JUMBO HASS AVOCADOS 1 FW 2.68", 0.83),
        OcrLine("SWEET ONION", 1.0),
        OcrLine("0.20 Lbs ü 1 1.24 FW 0.25 2", 0.77),
        OcrLine("DR PEPPER MINI CANS 1 o _", 0.43),
        OcrLine("WE", 0.49),
        OcrLine("林***** Sales Tax Sal btal** 6440 1188", 0.75),
        OcrLine("*** MASTRCRD EFS 12.37", 0.99),
        OcrLine("ITEMS PURCHASED: 4 DNE 12.37", 0.72),
    ]

    result = parse_receipt(lines)
    onion, beverage = result.missing_item_candidates

    assert len(result.receipt.items) == 2
    assert onion.name == "SWEET ONION"
    assert onion.quantity == pytest.approx(0.2)
    assert onion.unit_price == pytest.approx(1.24)
    assert onion.line_total == pytest.approx(0.25)
    assert onion.tax_marker == "FW"
    assert onion.supporting_text == "0.20 Lbs ü 1 1.24 FW 0.25 2"
    assert beverage.name == "DR PEPPER MINI CANS"
    assert beverage.line_total is None
    support_decision = next(
        decision for decision in result.decisions if decision.index == 5
    )
    assert support_decision.role == "missing_item_support"


def test_run5_heb_names_and_tax_fields():
    result = parse_receipt([OcrLine(t, .9) for t in [
        "HEB AA CFW LG EGGS 18CT C F WHL 2.98",
        "JUMBO HASS AVOCADOS 1 FW 2.68",
        "SWEET ONION", "0.20 Lbs @ 1.24 FW 0.25",
        "DR PEPPER MINI CANS T 5.97", "SUBTOTAL 11.88",
        "SALES TAX 0.49", "TOTAL 12.37", "ITEMS PURCHASED: 4",
    ]])
    assert [i.tax_marker for i in result.receipt.items] == ["F", "FW", "FW", "T"]
    assert result.receipt.items[0].name == "HEB AA CFW LG EGGS 18CT C"
    assert result.receipt.items[1].name == "JUMBO HASS AVOCADOS"
    assert sum(i.line_total for i in result.receipt.items) == pytest.approx(11.88)


def test_home_depot_multiline_quantity_row():
    result = parse_receipt([OcrLine(t, .99) for t in [
        "HOME DEPOT", "11/13/2019 10:36 AM",
        "123456789012SHELF BRKT<A> 3.98",
        '12" WHITE MAGNUM SHELF BRACKET',
        "123456789013FELT PADS<A> 5.98", 'FELT PAD 1" BEIGE 48PK',
        "123456789014 DRAWER SLIDE<A>", 'SLIDE-20" BALLBRNG SOFT CLOSE SET',
        "2@18.48 36.96", "SUBTOTAL 46.92", "SALES TAX 3.87", "TOTAL $50.79",
        "VISA CHARGE 50.79",
    ]])
    assert [i.name for i in result.receipt.items] == ["SHELF BRKT", "FELT PADS", "DRAWER SLIDE"]
    item = result.receipt.items[-1]
    assert (item.quantity, item.unit_price, item.line_total) == (2, 18.48, 36.96)
    assert sum(i.line_total for i in result.receipt.items) == pytest.approx(46.92)
    assert (result.receipt.subtotal, result.receipt.tax, result.receipt.total) == (46.92, 3.87, 50.79)
    assert not result.missing_item_candidates


@pytest.mark.parametrize(
    "description_rows",
    [
        ["GARRY OAK ROUNDS", "54786 $16.99 1.19 $20.22"],
        ["GARRY OAK ROUNDS 16.99", "54786 $16.99 1.19 $20.22"],
    ],
)
def test_unit_price_quantity_columns_use_charged_total(description_rows):
    result = parse_receipt([OcrLine(text, 0.99) for text in [
        "ROCKLER",
        *description_rows,
        "SUBTOTAL $20.22",
        "TAX $1.67",
        "TOTAL $21.89",
    ]])

    item, = result.receipt.items
    assert item.name == "GARRY OAK ROUNDS"
    assert item.product_code == "54786"
    assert item.quantity == pytest.approx(1.19)
    assert item.unit_price == pytest.approx(16.99)
    assert item.line_total == pytest.approx(20.22)
    assert result.receipt.subtotal == pytest.approx(20.22)
    assert not result.missing_item_candidates
    item_decision = next(
        decision for decision in result.decisions if decision.role == "item"
    )
    assert "multiplied by quantity" in item_decision.reason


def test_rockler_two_line_items_join_description_to_sku_price_row():
    result = parse_receipt([OcrLine(text, 0.99) for text in [
        "ROCKLER",
        "Date. 9/24/2026 Time. 12:42",
        "Item Name Unit Price QTY Total",
        "1/4-20 x 2? CONNECT BLT 8PK",
        "46707 $5.99 1 $5.99",
        "LOW PROF INS 1/4-20 8/PK",
        "39631 $4.99 1 $4.99",
        "Subtotal $10.98",
        "Tax1.125 $0.13",
        "Tax 0.875 $0.09",
        "Tax 6.25 $0.68",
        "Total $11.88",
    ]])

    receipt = result.receipt
    assert receipt.store_name == "Rockler"
    assert receipt.transaction_date == "2026-09-24"
    assert [item.name for item in receipt.items] == [
        "1/4-20 x 2? CONNECT BLT 8PK",
        "LOW PROF INS 1/4-20 8/PK",
    ]
    assert [item.product_code for item in receipt.items] == ["46707", "39631"]
    assert [item.quantity for item in receipt.items] == [1, 1]
    assert [item.unit_price for item in receipt.items] == [5.99, 4.99]
    assert [item.line_total for item in receipt.items] == [5.99, 4.99]
    assert (receipt.subtotal, receipt.tax, receipt.total) == (10.98, 0.90, 11.88)
    assert result.missing_item_candidates == ()


def test_rockler_quantity_column_tolerates_ocr_letter_i_for_one():
    result = parse_receipt([OcrLine(text, 0.99) for text in [
        "ROCKLER",
        "1/4-20 x 2? CONNECT BLT 8PK",
        "46707 $5.99 I $5.99",
        "Subtotal $5.99",
        "Tax $0.49",
        "Total $6.48",
    ]])

    item, = result.receipt.items
    assert item.name == "1/4-20 x 2? CONNECT BLT 8PK"
    assert item.product_code == "46707"
    assert item.quantity == 1
    assert item.unit_price == 5.99
    assert item.line_total == 5.99


def test_rockler_logo_noise_and_orphan_currency_symbol_are_cleaned():
    result = parse_receipt([OcrLine(text, 0.99) for text in [
        "ROCKLER PRDWORN A RAR",
        "7/16 BRAD POINT DRILL-CD 57839 $10.99 1 $ $10.99",
        "SUBTOTAL $10.99",
        "TAX 1.125 $0.12",
        "TAX 0.875 $0.09",
        "TAX 6.25 $0.69",
        "TOTAL $11.89",
    ]])

    item, = result.receipt.items
    assert result.receipt.store_name == "Rockler"
    assert item.name == "7/16 BRAD POINT DRILL-CD"
    assert item.product_code == "57839"
    assert item.quantity == pytest.approx(1)
    assert item.unit_price == pytest.approx(10.99)
    assert item.line_total == pytest.approx(10.99)
    assert result.receipt.tax == pytest.approx(0.90)
    assert not result.missing_item_candidates


def test_ross_leading_product_codes_merchant_and_count_are_preserved():
    result = parse_receipt([OcrLine(text, 0.99) for text in [
        "ROSS",
        "DRESS FOR LESS",
        "SAN ANTONIO, TX 99903",
        "400302807701N NT 8X10(5X7) WHT & $6.99R",
        "400302809156 NT 8X10 STARBOARD $4.99R",
        "400303516312 WM BB 4-6 BLACK SU $15.99B",
        "400304494824 L UR blk baggy CARO $14.99B",
        "400305371537 RS Blk Core Slim S $10.99B",
        "Subtotal $53.95",
        "Sales Tax 8.250% $4.46",
        "Total $58.41",
        "Sold: 5 Returned: 0",
        "Date: 09/28/26 5:01:31 PM",
    ]])

    assert result.receipt.store_name == "Ross Dress for Less"
    assert result.receipt.transaction_date == "2026-09-28"
    assert [item.product_code for item in result.receipt.items] == [
        "400302807701",
        "400302809156",
        "400303516312",
        "400304494824",
        "400305371537",
    ]
    assert [item.name for item in result.receipt.items] == [
        "NT 8X10(5X7) WHT &",
        "NT 8X10 STARBOARD",
        "WM BB 4-6 BLACK SU",
        "UR blk baggy CARO",
        "RS Blk Core Slim S",
    ]
    assert sum(item.line_total for item in result.receipt.items) == pytest.approx(53.95)
    assert (result.receipt.subtotal, result.receipt.tax, result.receipt.total) == (
        53.95,
        4.46,
        58.41,
    )
    assert not any("Receipt lists" in note for note in result.receipt.notes)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ITEMS PURCHASED: 4", 4),
        ("# ITEMS SOLD 2", 2),
        ("# ITEMS ?OLD 11", 11),
        ("# ITEMS 5OLD 7", 7),
        ("# ITEMSOLD 11", 11),
        ("# ITEMS OLD 11", 11),
        ("Sold: 5 Returned: 0", 5),
        ("No printed count", None),
    ],
)
def test_printed_item_count_formats(text, expected):
    assert printed_item_count(text) == expected


def test_recovery_without_printed_count():
    result = parse_receipt([OcrLine(t, .99) for t in [
        "HOME DEPOT", "SHELF BRACKET 3.98", "DRAWER SLIDE", "SUBTOTAL 40.94",
    ]])
    candidate, = result.missing_item_candidates
    assert candidate.name == "DRAWER SLIDE"
    assert candidate.line_total is None


def test_home_depot_domain_identifies_merchant_after_survey_header():
    rows = ["User ID: example", "PASSWORD: example", "Entries must be completed within 14 days",
            "of purchase. Entrants must be 18 or", "older to enter. See complete rules on",
            "website. No purchase necessary.", "More saving.", "More doing.",
            "SHELF BRKT 3.98", "SUBTOTAL 3.98"]
    rows += ["RETURN POLICY"] * 10
    rows += ["www.homedepot.com/survey"]
    receipt = parse_receipt([OcrLine(t, .99) for t in rows]).receipt
    assert receipt.store_name == "The Home Depot"
    assert [i.line_total for i in receipt.items] == [3.98]


def test_survey_text_is_not_selected_as_merchant():
    result = parse_receipt([OcrLine(t, .99) for t in [
        "Entries must be completed within 14 days", "LOCAL MARKET", "ITEM 2.00", "SUBTOTAL 2.00",
    ]])
    assert result.receipt.store_name == "LOCAL MARKET"


def test_mcdonalds_alias_overrides_survey_prompt():
    result = parse_receipt([OcrLine(t, .99) for t in [
        "how are we doing?",
        "Let us know at 1-800-382-CAFE",
        "McDonald's Restaurant #2881",
        "11700 BLANCO RD",
        "09/22/2026 09:15 AM",
        "1 S Coke 1.19",
        "Subtotal 1.19",
        "Tax 0.10",
        "Total 1.29",
    ]])

    assert result.receipt.store_name == "McDonald's"
    assert result.receipt.transaction_date == "2026-09-22"
    assert [item.name for item in result.receipt.items] == ["S Coke"]
    assert result.receipt.total == pytest.approx(1.29)


def test_petco_logo_alias_overrides_address_and_bleedthrough_noise():
    result = parse_receipt([OcrLine(t, .99) for t in [
        "petco where the pets go 0 DRIVE",
        "125 NW LOOP 410 STE 350",
        "SAN ANTONIO, TX 99901",
        "Helped by Maggie",
        "000266523 SOPH-30# REFILL FF A 43.47 T",
        "Reg Price: 3@ 14.49",
        "Taxable Total 43.47",
        "Subtotal 43.47",
        "Tax TX 8.25% 3.59",
        "TOTAL 47.06",
    ]])

    receipt = result.receipt
    assert receipt.store_name == "Petco"
    item, = receipt.items
    assert item.name == "SOPH-30# REFILL FF A"
    assert item.product_code == "000266523"
    assert (item.quantity, item.unit_price, item.line_total) == (3.0, 14.49, 43.47)
    assert item.tax_marker == "T"
    assert receipt.subtotal == pytest.approx(43.47)
    assert receipt.tax == pytest.approx(3.59)
    assert receipt.total == pytest.approx(47.06)


def test_quantity_detail_row_that_does_not_reconcile_stays_a_row():
    # RL-43 guard: only fold "N@ unit" into the item above when N x unit
    # equals that item's charge.
    result = parse_receipt([OcrLine(t, .99) for t in [
        "PET STORE", "DOG TREATS 10.00", "Reg Price: 3@ 14.49", "Subtotal 24.49",
    ]])
    assert [item.line_total for item in result.receipt.items] == [10.00, 14.49]
    assert result.receipt.items[0].quantity == 1.0


@pytest.mark.parametrize("text, expected", [
    ("3 Items", 3),
    ("TOTAL 47.06\n3 Items\nVISA 47.06", 3),
    ("1 Item", 1),
    ("2 ITEMS FREE WITH COUPON", None),
    ("BUY 2 ITEMS 5.00", None),
    # RL-43 Dell rerun: back-of-receipt print merged onto the count line.
    ("RRN: 532455\non e1 anoada 3 Items es bruter llute evieoer lliw", 3),
    ("Buy 2 Items Get 1 Free", None),
    ("2 Items for $5.00", None),
    ("Mix 3 Items 9.99", None),
    ("WAS 12.5 Items", None),
])
def test_standalone_items_count_line(text, expected):
    assert printed_item_count(text) == expected


def test_brand_mention_alone_does_not_override_merchant():
    result = parse_receipt([OcrLine(t, .99) for t in [
        "LOCAL MARKET", "HOME DEPOT GIFT CARD 20.00", "SUBTOTAL 20.00",
    ]])
    assert result.receipt.store_name == "LOCAL MARKET"


def test_detached_tax_price_is_offered_for_missing_item_review():
    result = parse_receipt([OcrLine(t, .9) for t in [
        "HEB", "EGGS F 2.98", "DR PEPPER MINI CANS", "T 5.97",
        "SUBTOTAL 8.95", "ITEMS PURCHASED: 2",
    ]])
    assert len(result.receipt.items) == 1
    candidate, = result.missing_item_candidates
    assert (candidate.name, candidate.line_total, candidate.tax_marker) == ("DR PEPPER MINI CANS", 5.97, "T")
    assert candidate.supporting_text == "T 5.97"


def test_contactless_terminal_amount_is_not_a_purchased_item():
    result = parse_receipt([OcrLine(text, 0.95) for text in [
        "H-E-B",
        "MI TIENDA HUEVOS ROJOS LG F 4.48",
        "LACTAID 2% MILK F 7.28",
        "Contactless Chip USD 11.76",
        "SUBTOTAL 11.76",
        "SALES TAX 0.00",
        "TOTAL 11.76",
        "ITEMS PURCHASED: 2",
    ]])

    assert [item.name for item in result.receipt.items] == [
        "MI TIENDA HUEVOS ROJOS LG",
        "LACTAID 2% MILK",
    ]
    terminal_decision = next(
        decision
        for decision in result.decisions
        if decision.text == "Contactless Chip USD 11.76"
    )
    assert terminal_decision.role == "ignored"


def test_missing_price_is_not_inferred_from_subtotal():
    result = parse_receipt([OcrLine(t, .9) for t in [
        "HEB", "EGGS F 2.98", "DR PEPPER MINI CANS T",
        "SUBTOTAL 8.95", "ITEMS PURCHASED: 2",
    ]])
    candidate, = result.missing_item_candidates
    assert candidate.name == "DR PEPPER MINI CANS"
    assert candidate.tax_marker == "T"
    assert candidate.line_total is None


@pytest.mark.parametrize("name", ["FORMULA STAGE 1", "VITAMIN B12", "EGGS 18CT", "PRODUCT WHL", "7 UP 12 PACK"])
def test_name_cleanup_preserves_product_details(name):
    result = parse_receipt([OcrLine(t, .9) for t in ["HEB", f"{name} F 2.98", "SUBTOTAL 2.98"]])
    assert result.receipt.items[0].name == name

def test_ordered_price_estimates_are_metadata_not_items() -> None:
    result = parse_receipt([OcrLine(text, 0.99) for text in [
        "WALMART",
        "Freshness Guaranteed Boneless Skinless Chicken Breasts 9.14",
        "Ordered price 10.35",
        "Prima Della Deluxe Cooked Ham Pre-Sliced 4.13",
        "Ordered price 4.14",
        "Valari Brothers Hickory Smoked Pork Hocks 6.75",
        "Ordered price 6.05",
        "SUBTOTAL 20.02",
    ]])

    assert [item.name for item in result.receipt.items] == [
        "Freshness Guaranteed Boneless Skinless Chicken Breasts",
        "Prima Della Deluxe Cooked Ham Pre-Sliced",
        "Valari Brothers Hickory Smoked Pork Hocks",
    ]
    assert [item.line_total for item in result.receipt.items] == [9.14, 4.13, 6.75]
    metadata = [
        decision
        for decision in result.decisions
        if decision.role == "item_metadata"
    ]
    assert [decision.text for decision in metadata] == [
        "Ordered price 10.35",
        "Ordered price 4.14",
        "Ordered price 6.05",
    ]
    assert any(
        "Ignored 3 ordered-price estimate lines" in note
        for note in result.receipt.notes
    )

def test_phone_interface_text_is_not_selected_as_merchant() -> None:
    result = parse_receipt([OcrLine(text, 0.99) for text in [
        "Gmail 5:08 C 40",
        "FRESH MARKET",
        "WHOLE MILK 3.34",
        "SUBTOTAL 3.34",
    ]])

    assert result.receipt.store_name == "FRESH MARKET"
    gmail_decision = next(
        decision for decision in result.decisions if decision.text.startswith("Gmail")
    )
    assert gmail_decision.role == "ignored"


def test_review_and_add_interface_text_is_not_selected_as_merchant() -> None:
    result = parse_receipt([OcrLine(text, 0.99) for text in [
        "Review item +Add",
        "LOCAL MARKET",
        "WHOLE MILK 3.34",
        "SUBTOTAL 3.34",
    ]])

    assert result.receipt.store_name == "LOCAL MARKET"
    interface_decision = next(
        decision for decision in result.decisions if decision.text == "Review item +Add"
    )
    assert interface_decision.role == "ignored"


def test_great_clips_tip_is_separate_from_service_subtotal() -> None:
    result = parse_receipt([OcrLine(text, 0.99) for text in [
        "Great Northtowne SC t Clips",
        "(210) 824-7176",
        "San Antonio, TX 99901",
        "11:14am 09-14-2026 Michael",
        "Haircut 20.00",
        "SUBTOTAL 20.00",
        "INVOICE TOTAL 20.00",
        "AMT TENDERED 26.00",
        "(Michael) MasterCard 20.00",
        "Charged tip for Michael 6.00",
        "Total 26.00",
    ]])

    receipt = result.receipt
    assert receipt.store_name == "Great Clips — Northtowne SC"
    assert [item.name for item in receipt.items] == [
        "Haircut",
        "Charged tip for Michael",
    ]
    assert [item.line_total for item in receipt.items] == [20.0, 6.0]
    assert (receipt.subtotal, receipt.tax, receipt.total) == (20.0, 0.0, 26.0)
    tip_decision = next(
        decision for decision in result.decisions if "Charged tip" in decision.text
    )
    assert tip_decision.role == "gratuity"
    assert not any("does not match" in note for note in receipt.notes)
    assert any("separating the charged gratuity" in note for note in receipt.notes)


def test_suggested_tip_guide_is_not_recorded_as_a_charge() -> None:
    receipt = parse_receipt([OcrLine(text, 0.99) for text in [
        "LOCAL SALON",
        "Haircut 20.00",
        "SUBTOTAL 20.00",
        "Suggested tip 20% 4.00",
        "TOTAL 20.00",
    ]]).receipt

    assert [item.name for item in receipt.items] == ["Haircut"]
    assert receipt.total == 20.0


def test_home_depot_quantity_row_uses_extended_charge_not_board_dimensions() -> None:
    result = parse_receipt([OcrLine(text, 0.99) for text in [
        "How doers",
        "get more done",
        "100 MAIN ROAD W,SAN ANTONIO TX 99902",
        "SHAWN M. HERBER ST0RE MGR 210-824-9677",
        "6544 00054 93903 09/18/26 03:07 PM",
        "SALE SELF CHECKOUT",
        "7809543804099 1X2X8PRIME<A>",
        "1X2-8FT PRIMED FJ S4S BOARD",
        "4@4.98 19.92",
        "SUBTOTAL 19.92",
        "SALES TAX 1.64",
        "TOTAL $21.56",
        "Learn more at homedepot.com/credit",
    ]])

    receipt = result.receipt
    assert receipt.store_name == "The Home Depot"
    assert receipt.transaction_date == "2026-09-18"
    assert len(receipt.items) == 1
    assert receipt.items[0].name == "1X2-8FT PRIMED FJ S4S BOARD"
    assert receipt.items[0].quantity == 4
    assert receipt.items[0].unit_price == 4.98
    assert receipt.items[0].line_total == 19.92
    assert receipt.subtotal == 19.92
    assert receipt.tax == 1.64
    assert receipt.total == 21.56
    assert result.missing_item_candidates == ()


def test_home_depot_recovers_quantity_when_ocr_drops_at_separator() -> None:
    result = parse_receipt([OcrLine(text, 0.99) for text in [
        "How doers",
        "get more done.",
        "100 MAIN ROAD W,SAN ANTONIO TX 99902",
        "6544 00052 51319 09/28/26 04:11 PM",
        "SALE CASHIER PATRICIA",
        "719931220653 1X6 RED OAK <A>",
        "1X6-R/L OAK BOARD",
        "506.98 34.90",
        "SUBTOTAL 34.90",
        "SALES TAX 2.88",
        "TOTAL $37.78",
        "Learn more at homedepot.com/credit",
    ]])

    receipt = result.receipt
    assert receipt.store_name == "The Home Depot"
    assert receipt.transaction_date == "2026-09-28"
    assert len(receipt.items) == 1
    item = receipt.items[0]
    assert item.name == "1X6-R/L OAK BOARD"
    assert item.quantity == 5
    assert item.unit_price == 6.98
    assert item.line_total == 34.90
    assert receipt.subtotal == 34.90
    assert receipt.tax == 2.88
    assert receipt.total == 37.78
    assert result.missing_item_candidates == ()

def test_heb_birthday_receipt_uses_main_subtotal_and_keeps_icing_tip_as_item() -> None:
    result = parse_receipt([OcrLine(text, 0.99) for text in [
        "H-E-B",
        "1058 7438 0929 2611 0700 102",
        "PIZZA 3 CHEESE 12IN SS CO F 7.99",
        "WILTON GOLD WHITE MXD CAN T 1.98",
        "CANDLE GOLD NUMBER 8 T 2 2.48",
        "CANDLE GOLD NUMBER 1",
        "WILTON TUBE ICING BLACK F 2.00",
        "WILTON ICING TIP SET T 1.98",
        "QUANTUM THERAZINC ELDRBRR 2.77 Q",
        "WILTON HPY BDAY ICING DCR F 6.48",
        "HEB MS ND NASAL DECON PE 5.24 H",
        "10 CONVENTIONAL BIRTHDA T 5.99",
        "COUPON $1.00 orig 6.99 Ea.",
        "11 CONVENTIONAL BIRTHDA T 3.99",
        "COUPON $1.00 orig 4.99 Ea.",
        "12 1 8SHT TIRAMISU BAR CAKE F 26.98",
        "Subtotal 70.36",
        "Tax 1.56",
        "Total 71.92",
        "ITEMS PURCHASED: 12",
        "FSA Subtotal 5.24",
    ]])

    receipt = result.receipt
    assert receipt.store_name == "H-E-B"
    assert receipt.transaction_date == "2026-09-29"
    assert (receipt.subtotal, receipt.tax, receipt.total) == (70.36, 1.56, 71.92)
    assert len(receipt.items) == 11
    assert sum(item.line_total for item in receipt.items) == pytest.approx(67.88)
    assert "WILTON ICING TIP SET" in [item.name for item in receipt.items]
    assert "CANDLE GOLD NUMBER 8" in [item.name for item in receipt.items]
    assert "CONVENTIONAL BIRTHDA" in [item.name for item in receipt.items]
    assert any("TIRAMISU BAR CAKE" in item.name for item in receipt.items)
    assert not any(
        item.name.startswith(("10 ", "11 ", "12 ")) for item in receipt.items
    )
    assert not any(decision.role == "gratuity" for decision in result.decisions)
    assert [candidate.name for candidate in result.missing_item_candidates] == [
        "CANDLE GOLD NUMBER 1"
    ]
    assert result.missing_item_candidates[0].line_total is None
    fsa_decision = next(
        decision for decision in result.decisions if decision.text == "FSA Subtotal 5.24"
    )
    assert fsa_decision.role == "supplemental_subtotal"



def test_five_below_four_items_and_brand_from_actual_ocr():
    texts = [
        'five BELoW', '01134 PLAZA DEL NORTE', '125 NW LOOP 410',
        'SAN AVTONIO, TX 99901', '210-555-0100', '0 SALE',
        '859661006001 SKVY POP AGWHTCHDR 4 1 @ $4.00 $4.00T',
        '842100148127 SD CARD READER $5.00T', '1 @ $5.00',
        '072320700526 CHJC0R00MS BAG $5.00T', '1 @ $5.00',
        '027541000054 NIAGARA 24CT 0.5L BO $1.00T', '1 @ $1.00',
        'Subtotal $15.00', 'Sales Tax 8.25)% $1.16',
        'Sales Tax 0.000% $0.00', 'Total $16.16', 'MasterCard $16.16',
        'Card No. XXXX3178', 'Expiration Date XX/XX', 'Auth. No. 01832P',
        'ENTRY METHOD: Contactless', 'CHIP IND: CONIACTLESS',
        'Store: 01134 Reg: 11 Tran:003459',
        'Date: 10/3/2026 12:27:37 PM Assoc: 111111',
    ]
    result = parse_receipt([OcrLine(text, .95) for text in texts])
    receipt = result.receipt
    assert receipt.store_name == 'Five Below'
    assert receipt.transaction_date == '2026-10-03'
    assert [item.line_total for item in receipt.items] == [4, 5, 5, 1]
    assert [item.quantity for item in receipt.items] == [1, 1, 1, 1]
    assert receipt.items[1].name == 'SD CARD READER'
    assert receipt.items[1].product_code == '842100148127'
    assert (receipt.subtotal, receipt.tax, receipt.total) == (15, 1.16, 16.16)
    assert sum(item.line_total for item in receipt.items) == receipt.subtotal
    assert not result.missing_item_candidates


@pytest.mark.parametrize('metadata', ['CARD PAYMENT $5.00', 'CREDIT SD CARD READER $5.00', 'CARD NO 842100148127 $5.00'])
def test_card_reader_exception_does_not_accept_tender_metadata(metadata):
    result = parse_receipt([OcrLine(text, .95) for text in ['SHOP', metadata, 'Subtotal $0.00', 'Total $0.00']])
    assert result.receipt.items == []


def test_five_below_product_mention_does_not_override_merchant():
    result = parse_receipt([OcrLine(text, .95) for text in ['OTHER SHOP', 'FIVE BELOW SHIRT $5.00', 'Subtotal $5.00', 'Total $5.00']])
    assert result.receipt.store_name == 'OTHER SHOP'


def test_walmart_detached_prices_are_promoted_without_manual_reentry() -> None:
    lines = [
        "Walmart",
        "# ITEMS SOLD 11",
        "LDSSGLZHONBN 024300865400 F",
        "2.00 N",
        "CHEESECAKE 194346717820 F",
        "6.72 N",
        "GLASS WIPES 070612175010",
        "3.97 X",
        "FRENCH BREAD 200989000000 F",
        "1.47 N",
        "FRENCH BREAD 200989000000 F",
        "1.47 N",
        "CHEESECAKE 194346717840 F",
        "7.98 N",
        "DRY SAUSAGE 038616900010 F",
        "8.47 N",
        "SITE MERCH 889698929570",
        "14.88 X",
        "SITE MERCH 889698929560",
        "14.88 X",
        "AVOCADO 750032608690 F",
        "3 AT 1 FOR 0.56 1.68 N",
        "LATCH BOXES 073149097330",
        "14.98 X",
        "SUBTOTAL 78.50",
        "TAX1 8.2500 % 4.02",
        "TOTAL 82.52",
        "10/11/26 13:37:41",
    ]

    result = parse_receipt([OcrLine(text, 0.99) for text in lines])
    receipt = result.receipt

    assert receipt.store_name == "Walmart"
    assert receipt.transaction_date == "2026-10-11"
    assert len(receipt.items) == 11
    assert sum(item.line_total for item in receipt.items) == pytest.approx(78.50)
    assert (receipt.subtotal, receipt.tax, receipt.total) == (78.50, 4.02, 82.52)
    assert result.missing_item_candidates == ()

    avocado = next(item for item in receipt.items if item.name == "AVOCADO")
    assert avocado.product_code == "750032608690"
    assert avocado.quantity == 3
    assert avocado.unit_price == 0.56
    assert avocado.line_total == 1.68
    # Walmart's trailing N/X is the tax status; the F before it is food
    # eligibility, so the avocado (printed "1.68 N") is not taxed.
    assert avocado.tax_marker == "N"
    taxed = [item for item in receipt.items if item.tax_marker == "X"]
    assert [item.name for item in taxed] == [
        "GLASS WIPES", "SITE MERCH", "SITE MERCH", "LATCH BOXES"
    ]
    assert sum(item.line_total for item in taxed) == pytest.approx(48.71)
    assert all(item.tax_marker == "N" for item in receipt.items if item not in taxed)

    french_bread = [item for item in receipt.items if item.name == "FRENCH BREAD"]
    assert len(french_bread) == 2
    assert [item.line_total for item in french_bread] == [1.47, 1.47]
    site_merch = [item for item in receipt.items if item.name == "SITE MERCH"]
    assert len(site_merch) == 2
    assert {item.product_code for item in site_merch} == {
        "889698929570",
        "889698929560",
    }


def test_walmart_multiline_ocr_block_is_split_before_price_linking() -> None:
    result = parse_receipt(
        [
            OcrLine("Walmart", 0.99),
            OcrLine("# ITEMS SOLD 2", 0.99),
            OcrLine(
                "FRENCH BREAD 200989000000 F\n1.47 N\n"
                "LATCH BOXES 073149097330\n14.98 X",
                0.95,
            ),
            OcrLine("SUBTOTAL 16.45", 0.99),
            OcrLine("TAX 1.24", 0.99),
            OcrLine("TOTAL 17.69", 0.99),
        ]
    )

    assert [item.name for item in result.receipt.items] == [
        "FRENCH BREAD",
        "LATCH BOXES",
    ]
    assert [item.line_total for item in result.receipt.items] == [1.47, 14.98]
    assert result.missing_item_candidates == ()


def _lines(texts: list[str]) -> list[OcrLine]:
    return [OcrLine(text, 0.98) for text in texts]


def test_restaurant_ticket_restores_missing_decimal_and_leading_quantity() -> None:
    result = parse_receipt(
        _lines(
            [
                "Lucky Noodle House",
                "71 TO GO",
                "3 Egg Fried Rice $2997",
                "1 Pork Egq Roll(3) $5.99",
                "Stuffed Carb $6.99",
                "Subtotal $42.95",
                "Tax $3.54",
                "Total: $46.49",
                "20%:$1384",
            ]
        )
    )

    items = [
        (item.name, item.quantity, item.unit_price, item.line_total)
        for item in result.receipt.items
    ]
    assert items == [
        ("Egg Fried Rice", 3.0, 9.99, 29.97),
        ("Pork Egq Roll(3)", 1.0, 5.99, 5.99),
        ("Stuffed Carb", 1.0, 6.99, 6.99),
    ]
    assert any(
        "restored a missing decimal" in decision.reason
        for decision in result.decisions
    )


def test_dollar_price_with_space_for_decimal_point() -> None:
    result = parse_receipt(
        _lines(["Lucky Noodle House", "3 Egg Fried Rice $29 97", "Subtotal $29.97"])
    )

    assert [(item.quantity, item.line_total) for item in result.receipt.items] == [
        (3.0, 29.97)
    ]


@pytest.mark.parametrize(
    ("text", "name", "quantity"),
    [
        ("12 OZ COFFEE $4.99", "12 OZ COFFEE", 1.0),
        ("5 BANANAS 1.00", "5 BANANAS", 1.0),
    ],
)
def test_leading_number_is_not_a_quantity_for_sizes_or_plain_prices(
    text: str, name: str, quantity: float
) -> None:
    result = parse_receipt(_lines(["FRESH MART", "HOT DOG $3.00", text, "SUBTOTAL 9.99"]))

    item = result.receipt.items[-1]
    assert (item.name, item.quantity) == (name, quantity)


def test_restaurant_item_names_continue_on_following_lines() -> None:
    lines = [
        OcrLine("Lucky Noodle House", 0.99, (427, 341, 1246, 439)),
        OcrLine("Stuffed Carb $6.99", 0.99, (523, 995, 1237, 1056)),
        OcrLine("Rangoon(5)", 0.99, (523, 1057, 810, 1114)),
        OcrLine("Sauteed $9.99", 0.99, (520, 1132, 1238, 1193)),
        OcrLine("Shredded", 0.99, (522, 1191, 761, 1245)),
        OcrLine("Cabbage", 0.99, (518, 1245, 743, 1311)),
        OcrLine("Subtotal $16.98", 0.99, (427, 1443, 1235, 1500)),
    ]

    result = parse_receipt(lines)

    assert [item.name for item in result.receipt.items] == [
        "Stuffed Carb Rangoon(5)",
        "Sauteed Shredded Cabbage",
    ]
    assert not result.missing_item_candidates


def test_unpriced_line_is_not_joined_without_boxes_or_when_it_starts_the_next_item() -> None:
    no_boxes = parse_receipt(
        _lines(["FRESH MART", "MILK 3.49", "BANANAS", "SUBTOTAL 3.49"])
    )
    assert [item.name for item in no_boxes.receipt.items] == ["MILK"]

    next_product = parse_receipt(
        [
            OcrLine("FRESH MART", 0.99, (100, 0, 600, 40)),
            OcrLine("MILK 3.49", 0.99, (100, 100, 900, 140)),
            OcrLine("BANANAS", 0.99, (100, 145, 300, 185)),
            OcrLine("2 @ 0.50 1.00", 0.99, (150, 190, 900, 230)),
            OcrLine("SUBTOTAL 4.49", 0.99, (100, 240, 900, 280)),
        ]
    )
    assert next_product.receipt.items[0].name == "MILK"


def _sherwin_williams_lines(price_row: str) -> list[OcrLine]:
    # RL-42: OCR lines from the 2026-10-21 Sherwin-Williams receipt (RL-000075).
    return [
        OcrLine(text, 0.95, (20, index * 30, 700, index * 30 + 24))
        for index, text in enumerate([
            "Sherwin-Williams",
            "10/21/2026",
            "6513-00972 D19W00151 GALLON D19W151",
            "CASHMERE ES EXTRA",
            price_row,
            "Discount (% 40.00) -34.60",
            "Subtotal 51.89",
            "Tax 4.28",
            "Total 56.17",
        ])
    ]


@pytest.mark.parametrize("price_row", ["1.00 86.49 86.49", "1.00 @ 86.49 86.49"])
def test_sherwin_williams_quantity_kept_when_ocr_drops_at_sign(price_row):
    receipt = parse_receipt(_sherwin_williams_lines(price_row)).receipt

    assert len(receipt.items) == 1
    item = receipt.items[0]
    assert item.name == "CASHMERE ES EXTRA"
    assert item.quantity == 1
    assert item.unit_price == 86.49
    assert item.discount == 34.60
    assert item.line_total == 51.89
    assert (receipt.subtotal, receipt.tax, receipt.total) == (51.89, 4.28, 56.17)



def _lines(texts: list[str]) -> list[OcrLine]:
    return [
        OcrLine(text, 0.95, (20, index * 30, 700, index * 30 + 24))
        for index, text in enumerate(texts)
    ]


def test_rl41_brand_header_with_store_number_beats_city_state_zip() -> None:
    """RL-41: The UPS Store receipt saved the city/state/ZIP line as merchant."""

    receipt = parse_receipt(
        _lines(
            [
                "The UPS Store #0123",
                "8521 Blanco Rd STE 2",
                "SAN ANTONIO. TX 99901-0000",
                "(210) 349-4955",
                "10/08/2026 2:14 PM",
                "Ground 1 @ 16.97 16.97",
                "SUBTOTAL 16.97",
                "TAX 0.00",
                "TOTAL 16.97",
                "VISA 16.97",
            ]
        )
    ).receipt

    assert receipt.store_name == "The UPS Store #0123"
    assert receipt.transaction_date == "2026-10-08"
    assert receipt.total == 16.97


@pytest.mark.parametrize(
    "header",
    ["Corner Hardware Store #12", "Lone Star Pet Store No. 7", "The Book Store"],
)
def test_rl41_any_brand_before_store_number_is_kept(header: str) -> None:
    receipt = parse_receipt(
        _lines([header, "AUSTIN, TX 78701", "09/03/2026", "WIDGET 4.00", "TOTAL 4.00"])
    ).receipt

    assert receipt.store_name == header


def test_rl41_bare_store_number_and_city_line_are_not_merchants() -> None:
    receipt = parse_receipt(
        _lines(["Store #0123", "SAN ANTONIO, TX 99901", "09/03/2026", "WIDGET 4.00", "TOTAL 4.00"])
    ).receipt

    assert receipt.store_name == ""


def test_rl41_item_name_is_not_merchant_when_logo_is_missed() -> None:
    """RL-41 (H-E-B case): with no merchant text, leave the store blank.

    The logo was not read, so the first line with letters was an item name
    priced by the row below it. Guessing it put a product into Saved
    merchants; a blank store asks the user instead.
    """

    receipt = parse_receipt(
        _lines(
            [
                "8371 0262 1025 2616 4410 113",
                "1 QUANTUM THERAZINC ELDRBRR",
                "3 Ea.@ 1/ 2.77 8.31 Q",
                "2 LINDT EXCELLENCE SEA SALT",
                "2 Ea. @ 1/ 3.97 T 7.94",
                "Subtotal 16.25",
                "Tax 0.00",
                "Total 16.25",
            ]
        )
    ).receipt

    assert receipt.store_name == ""


def test_rl41_merchant_search_stops_at_first_priced_row() -> None:
    receipt = parse_receipt(
        _lines(["WIDGET 4.00", "GADGET DELUXE 6.00", "TOTAL 10.00"])
    ).receipt

    assert receipt.store_name == ""


def _ups_lines(header: str, terminal: str) -> list[OcrLine]:
    return _lines(
        [
            header,
            "8521 Blanco Rd STE 2",
            "SAN ANTONIO. TX 99901-0000",
            f"Terminal....: {terminal} Date.: 10/8/2026",
            "Ground $16.97",
            "Subtotal $16.97",
            "Total $16.97",
        ]
    )


def test_rl41_store_number_hash_read_as_one_is_restored_when_confirmed() -> None:
    """Dell rerun 2026-11-01: OCR read ``#0123`` as ``10123``; the terminal ID
    ``P0S0123A`` on the same receipt confirms the store number."""

    receipt = parse_receipt(_ups_lines("The UPS Store 10123", "P0S0123A")).receipt

    assert receipt.store_name == "The UPS Store #0123"


@pytest.mark.parametrize(
    "terminal",
    ["REG 7", "P0S10123A"],  # no confirmation / the full number is printed elsewhere
)
def test_rl41_store_number_left_as_read_without_confirmation(terminal: str) -> None:
    receipt = parse_receipt(_ups_lines("The UPS Store 10123", terminal)).receipt

    assert receipt.store_name == "The UPS Store 10123"


def test_rl41_suite_and_redacted_lines_are_not_merchants() -> None:
    receipt = parse_receipt(
        _lines(["The UPS Store #0123", "[REDACTED ADDRESS] STE 2", "Ground $16.97", "Total $16.97"])
    ).receipt

    assert receipt.store_name == "The UPS Store #0123"


@pytest.mark.parametrize("label", ["Tex", "T4X", "Tak", "TAX"])
def test_rl41_misread_tax_label_is_not_an_item(label: str) -> None:
    """UPS Store Dell rerun: ``Tax $0.00`` was read as ``Tex $0.00`` and became
    a $0.00 item."""

    receipt = parse_receipt(
        _lines(
            [
                "The UPS Store #0123",
                "Ground $16.97",
                f"{label} $0.00",
                "Subtotal $16.97",
                "Total tax $0.00",
                "Total $16.97",
            ]
        )
    ).receipt

    assert [item.name for item in receipt.items] == ["Ground"]
    assert receipt.tax == 0.0


def test_rl41_item_names_starting_like_tax_stay_items() -> None:
    receipt = parse_receipt(
        _lines(
            ["LOCAL MARKET", "TEXAS TOAST 2.99", "TEX MEX BOWL 8.99", "SUBTOTAL 11.98", "TAX 0.99", "TOTAL 12.97"]
        )
    ).receipt

    assert [item.name for item in receipt.items] == ["TEXAS TOAST", "TEX MEX BOWL"]
    assert receipt.tax == 0.99
