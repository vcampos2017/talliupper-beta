"""RL-64: ``code qty price total`` rows with the item name on its own line."""

from receipt_reader.local_ocr import OcrLine
from receipt_reader.parser import parse_receipt


def _boxed(texts: list[str]) -> list[OcrLine]:
    """Left-aligned rows 50 px apart, like a narrow POS printout."""

    return [
        OcrLine(text, 0.97, (340, 300 + index * 50, 1200, 300 + index * 50 + 60))
        for index, text in enumerate(texts)
    ]


def _items(result):
    return [
        (item.name, item.product_code, item.quantity, item.unit_price, item.line_total)
        for item in result.receipt.items
    ]


def test_name_below_code_row_with_two_line_header() -> None:
    result = parse_receipt(
        _boxed(
            [
                "Corner Camera Shop",
                "10/22/2026 5:15:07 PM",
                "Item Qty Price Total",
                "Description",
                "601-10171 1 59.90 59.90",
                "SmallRig Sling Handle for DJI",
                "Subtotal 59.90",
                "Tax 4.94",
                "Total 64.84",
                "Number of items purchased:1",
            ]
        )
    )

    assert _items(result) == [
        ("SmallRig Sling Handle for DJI", "601-10171", 1.0, 59.9, 59.9)
    ]
    assert result.missing_item_candidates == ()
    assert (result.receipt.subtotal, result.receipt.tax, result.receipt.total) == (
        59.9,
        4.94,
        64.84,
    )


def test_several_items_with_names_below_and_no_header() -> None:
    result = parse_receipt(
        _boxed(
            [
                "Corner Hobby Shop",
                "10/22/2026",
                "A1234 2 4.50 9.00",
                "Brass Rod 1/8 in",
                "77-201 1 12.99 12.99 T",
                "Wood Glue 8 oz",
                "Subtotal 21.99",
                "Total 21.99",
            ]
        )
    )

    assert _items(result) == [
        ("Brass Rod 1/8 in", "A1234", 2.0, 4.5, 9.0),
        ("Wood Glue 8 oz", "77-201", 1.0, 12.99, 12.99),
    ]
    assert result.receipt.items[1].tax_marker == "T"


def test_names_above_code_rows() -> None:
    result = parse_receipt(
        _boxed(
            [
                "Corner Hobby Shop",
                "10/22/2026",
                "Brass Rod 1/8 in",
                "A1234 2 4.50 9.00",
                "Wood Glue 8 oz",
                "77-201 1 12.99 12.99",
                "Subtotal 21.99",
            ]
        )
    )

    assert _items(result) == [
        ("Brass Rod 1/8 in", "A1234", 2.0, 4.5, 9.0),
        ("Wood Glue 8 oz", "77-201", 1.0, 12.99, 12.99),
    ]


def test_quantity_merged_into_price_by_ocr() -> None:
    result = parse_receipt(
        _boxed(
            [
                "Corner Camera Shop",
                "Item Qty Price Total",
                "Description",
                "601-10171 159.90 59.90",
                "SmallRig Sling Handle for DJI",
                "Subtotal 59.90",
            ]
        )
    )

    assert _items(result) == [
        ("SmallRig Sling Handle for DJI", "601-10171", 1.0, 59.9, 59.9)
    ]


def test_code_row_whose_math_does_not_reconcile_is_left_alone() -> None:
    result = parse_receipt(
        _boxed(
            [
                "Corner Camera Shop",
                "601-10171 2 59.90 59.90",
                "SmallRig Sling Handle for DJI",
                "Subtotal 59.90",
            ]
        )
    )

    assert result.receipt.items == []


def test_column_header_does_not_end_the_item_section() -> None:
    result = parse_receipt(
        _boxed(
            [
                "Corner Camera Shop",
                "Item Qty Price Total",
                "Lens Cap 52mm 6.99",
                "Subtotal 6.99",
            ]
        )
    )

    assert [item.name for item in result.receipt.items] == ["Lens Cap 52mm"]


def test_store_street_address_is_never_a_missing_item() -> None:
    result = parse_receipt(
        _boxed(
            [
                "Corner Camera Shop",
                "1o34 Main Ave.",
                "1200 N Main St Ste 4",
                "San Antonio, Tx 99901",
                "Lens Cap 52mm 6.99",
                "Lens Cleaning Kit",
                "Subtotal 18.98",
                "Number of items purchased: 2",
            ]
        )
    )

    assert [candidate.name for candidate in result.missing_item_candidates] == [
        "Lens Cleaning Kit"
    ]
