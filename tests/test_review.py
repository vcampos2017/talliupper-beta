import pytest

from receipt_reader.review import (
    amount_check_status,
    assign_checked_review_merges,
    compact_review_labels,
    duplicate_review_order,
    is_gratuity_item_name,
    merge_selected_review_rows,
    possible_split_labels,
    potential_duplicate_labels,
    selected_candidate_error,
)


def test_gratuity_detection_excludes_suggested_tip_guides() -> None:
    assert is_gratuity_item_name("Charged tip for Michael")
    assert is_gratuity_item_name("Gratuity")
    assert not is_gratuity_item_name("Suggested tip 20%")
    assert not is_gratuity_item_name("Haircut")
    assert not is_gratuity_item_name("WILTON ICING TIP SET")


def test_matching_product_codes_prices_and_quantities_share_a_group() -> None:
    rows = [
        {"Item": "Milk", "Product code": "00123", "Qty": 1, "Line total": 3.34},
        {"Item": "Unrelated", "Product code": "00999", "Qty": 1, "Line total": 2.00},
        {"Item": "MILK OCR VARIANT", "Product code": "00123", "Qty": 1, "Line total": 3.34},
    ]

    labels = potential_duplicate_labels(rows)

    assert labels[0] == labels[2] == "Possible duplicate 1 (2 matching)"
    assert labels[1] == ""


def test_name_matching_is_case_and_spacing_insensitive_without_codes() -> None:
    rows = [
        {"Item": " Fresh Yellow Squash ", "Qty": 1, "Line total": 1.36},
        {"Item": "fresh   yellow squash", "Qty": 1.0, "Line total": 1.36},
    ]

    assert potential_duplicate_labels(rows) == [
        "Possible duplicate 1 (2 matching)",
        "Possible duplicate 1 (2 matching)",
    ]


def test_different_quantity_or_price_is_not_grouped() -> None:
    rows = [
        {"Item": "Soda", "Product code": "12345678", "Qty": 1, "Line total": 5.00},
        {"Item": "Soda", "Product code": "12345678", "Qty": 2, "Line total": 10.00},
    ]

    assert potential_duplicate_labels(rows) == ["", ""]

def test_review_order_places_duplicate_names_together_before_unique_items() -> None:
    rows = [
        {"Item": "Freshness Guaranteed Boneless", "Qty": 1, "Line total": 9.14},
        {"Item": "Chicken Breasts", "Qty": 1, "Line total": 10.71},
        {"Item": "Ordered price", "Qty": 1, "Line total": 10.35},
        {"Item": "Prima Ham", "Qty": 1, "Line total": 4.13},
        {"Item": "Ordered price", "Qty": 1, "Line total": 4.14},
        {"Item": "Freshness Guaranteed Boneless", "Qty": 1, "Line total": 9.14},
        {"Item": "Chicken Breasts", "Qty": 1, "Line total": 10.71},
    ]

    order = duplicate_review_order(rows)

    assert order == [1, 6, 0, 5, 4, 2, 3]
    ordered = [rows[index]["Item"] for index in order]
    assert ordered[:4] == [
        "Chicken Breasts",
        "Chicken Breasts",
        "Freshness Guaranteed Boneless",
        "Freshness Guaranteed Boneless",
    ]

def test_reordered_product_words_are_possible_duplicates() -> None:
    rows = [
        {
            "Item": "Mushrooms, 8 oz Freshness Guaranteed Sliced White",
            "Qty": 1,
            "Line total": 2.00,
        },
        {
            "Item": "Freshness Guaranteed Sliced White Mushrooms, 8 oz",
            "Qty": 1,
            "Line total": 2.00,
        },
        {
            "Item": "(Shelf-Stable) Parmalat Whole Milk, 32 fl oz",
            "Qty": 1,
            "Line total": 3.34,
        },
        {
            "Item": "Parmalat Whole Milk, 32 fl oz (Shelf-Stable)",
            "Qty": 1,
            "Line total": 3.34,
        },
    ]

    labels = potential_duplicate_labels(rows)

    assert labels[0] == labels[1]
    assert labels[2] == labels[3]
    assert labels[0] != labels[2]


def test_split_rows_are_suggested_without_automatic_merging() -> None:
    rows = [
        {
            "Receipt order": 1,
            "Item": "Freshness Guaranteed Boneless, Skinless",
            "Qty": 1,
            "Line total": 9.14,
        },
        {
            "Receipt order": 2,
            "Item": "Chicken Breasts, 2.75 - 7.0 lb Tray",
            "Qty": 1,
            "Line total": 10.71,
        },
        {
            "Receipt order": 3,
            "Item": "HORMEL BLACK LABEL Bacon, Refrigerated, 12",
            "Qty": 1,
            "Line total": 5.97,
        },
        {
            "Receipt order": 4,
            "Item": "oz Plastic Package",
            "Qty": 1,
            "Line total": 6.97,
        },
    ]

    labels = possible_split_labels(rows)

    assert labels[0] == ""
    assert "receipt row 1" in labels[1]
    assert "receipt row 3" in labels[3]
    assert len(rows) == 4


def test_user_approved_merges_keep_final_prices_and_infer_multipacks() -> None:
    rows = [
        {
            "Receipt order": 1,
            "Merge into row": None,
            "Item": "Artificial Ingredients, 18 oz Tray Marketside Five Cheese Mini Ravioli, No",
            "Category": "Groceries",
            "Qty": 1,
            "Unit price": 13.94,
            "Line total": 13.94,
        },
        {
            "Receipt order": 2,
            "Merge into row": 1,
            "Item": "MARKETSIDE",
            "Category": "Uncategorized",
            "Qty": 1,
            "Unit price": 6.97,
            "Line total": 6.97,
        },
        {
            "Receipt order": 3,
            "Merge into row": None,
            "Item": "(8 Turkey Franks), Refrigerated, 16 oz",
            "Category": "Groceries",
            "Qty": 1,
            "Unit price": 5.94,
            "Line total": 5.94,
        },
        {
            "Receipt order": 4,
            "Merge into row": 3,
            "Item": "FRANKS TURKEY 120",
            "Category": "Uncategorized",
            "Qty": 1,
            "Unit price": 2.97,
            "Line total": 2.97,
        },
    ]

    merged, count = merge_selected_review_rows(rows)

    assert count == 2
    assert len(merged) == 2
    assert (merged[0]["Qty"], merged[0]["Unit price"], merged[0]["Line total"]) == (
        2.0,
        6.97,
        13.94,
    )
    assert (merged[1]["Qty"], merged[1]["Unit price"], merged[1]["Line total"]) == (
        2.0,
        2.97,
        5.94,
    )


def test_non_adjacent_chicken_fragment_can_merge_into_explicit_row() -> None:
    rows = [
        {
            "Receipt order": 1,
            "Merge into row": None,
            "Item": "Freshness Guaranteed Boneless, Skinless",
            "Category": "Groceries",
            "Qty": 1,
            "Line total": 9.14,
        },
        {
            "Receipt order": 2,
            "Merge into row": None,
            "Item": "Prima Della Deluxe Cooked Ham, Pre-Sliced",
            "Category": "Groceries",
            "Qty": 1,
            "Line total": 4.13,
        },
        {
            "Receipt order": 7,
            "Merge into row": 1,
            "Item": "Chicken Breasts, 2.75 - 7.0 lb Tray",
            "Category": "Groceries",
            "Qty": 1,
            "Line total": 10.71,
        },
    ]

    merged, count = merge_selected_review_rows(rows)

    assert count == 1
    assert [row["Receipt order"] for row in merged] == [1, 2]
    assert merged[0]["Item"] == (
        "Freshness Guaranteed Boneless, Skinless "
        "Chicken Breasts, 2.75 - 7.0 lb Tray"
    )
    assert merged[0]["Line total"] == 9.14


def test_blank_numeric_merge_cells_are_ignored_when_another_row_is_selected() -> None:
    rows = [
        {"Receipt order": 1, "Merge into row": float("nan"), "Item": "Bacon, 12", "Line total": 5.97},
        {"Receipt order": 2, "Merge into row": 1.0, "Item": "oz Plastic Package", "Line total": 6.97},
        {"Receipt order": 3, "Merge into row": float("nan"), "Item": "Milk", "Line total": 3.34},
    ]

    merged, count = merge_selected_review_rows(rows)

    assert count == 1
    assert [row["Receipt order"] for row in merged] == [1, 3]


def test_walmart_split_suggestions_avoid_ham_and_pack_cans_false_positives() -> None:
    rows = [
        {"Receipt order": 1, "Item": "Freshness Guaranteed Boneless, Skinless", "Line total": 9.14},
        {"Receipt order": 2, "Item": "Prima Della Deluxe Cooked Ham, Pre-Sliced", "Line total": 4.13},
        {"Receipt order": 3, "Item": "Fresh Yellow Squash, Each", "Line total": 1.36},
        {"Receipt order": 7, "Item": "Chicken Breasts, 2.75 - 7.0 lb Tray", "Line total": 10.71},
        {"Receipt order": 12, "Item": "Pack Cans Dr Pepper Soda Pop, 12 fl oz, 24", "Line total": 14.97},
    ]

    labels = possible_split_labels(rows)

    assert labels[1] == ""
    assert "receipt row 1" in labels[3]
    assert "non-adjacent" in labels[3]
    assert labels[4] == ""


def test_compact_review_labels_combine_duplicate_and_merge_guidance() -> None:
    rows = [
        {
            "Receipt order": 1,
            "Item": "Freshness Guaranteed Boneless, Skinless",
            "Qty": 1,
            "Line total": 9.14,
        },
        {
            "Receipt order": 2,
            "Item": "Milk",
            "Qty": 1,
            "Line total": 3.34,
        },
        {
            "Receipt order": 3,
            "Item": "Milk",
            "Qty": 1,
            "Line total": 3.34,
        },
        {
            "Receipt order": 4,
            "Item": "Chicken Breasts, 2.75 - 7.0 lb Tray",
            "Qty": 1,
            "Line total": 10.71,
        },
    ]

    labels = compact_review_labels(rows)

    assert labels == [
        "",
        "Duplicate 1 (2 rows)",
        "Duplicate 1 (2 rows)",
        "Merge? row 1",
    ]


def test_checked_duplicate_merge_collapses_ocr_copy_without_double_counting() -> None:
    rows = [
        {
            "Receipt order": 1,
            "Item": "Cereal",
            "Qty": 1,
            "Unit price": 4.98,
            "Discount": 0.50,
            "Line total": 4.98,
            "Delete": False,
            "Merge": False,
        },
        {
            "Receipt order": 2,
            "Item": "Cereal",
            "Qty": 1,
            "Unit price": 4.98,
            "Discount": 0.50,
            "Line total": 4.98,
            "Delete": False,
            "Merge": True,
        },
    ]

    prepared = assign_checked_review_merges(rows)
    merged, count = merge_selected_review_rows(prepared)

    assert prepared[1]["Merge into row"] == 1
    assert prepared[1]["Merge mode"] == "duplicate"
    assert count == 1
    assert len(merged) == 1
    assert merged[0]["Qty"] == 1.0
    assert merged[0]["Unit price"] == 4.98
    assert merged[0]["Discount"] == 0.50
    assert merged[0]["Line total"] == 4.98


def test_walmart_duplicate_merges_reconcile_to_printed_subtotal() -> None:
    duplicate_rows = [
        ("Freshness Guaranteed Boneless, Skinless Chicken Breasts", 9.14),
        ("Freshness Guaranteed Sliced White Mushrooms, 8 oz", 2.00),
        ("Pack Cans Dr Pepper Soda Pop, 12 fl oz, 24", 14.97),
        ("Parmalat Whole Milk, 32 fl oz (Shelf-Stable)", 3.34),
        ("Great Value Angel Hair, 16 oz, Shelf Stable", 1.24),
        ("Cereal, Good Source of Fiber, Family Size", 4.98),
    ]
    rows = []
    receipt_order = 1
    for name, line_total in duplicate_rows:
        for selected in (False, True):
            rows.append(
                {
                    "Receipt order": receipt_order,
                    "Item": name,
                    "Qty": 1,
                    "Unit price": line_total,
                    "Line total": line_total,
                    "Delete": False,
                    "Merge": selected,
                }
            )
            receipt_order += 1
    rows.append(
        {
            "Receipt order": receipt_order,
            "Item": "Other reviewed receipt items",
            "Qty": 1,
            "Unit price": 175.76,
            "Line total": 175.76,
            "Delete": False,
            "Merge": False,
        }
    )

    prepared = assign_checked_review_merges(rows)
    merged, count = merge_selected_review_rows(prepared)

    assert count == 6
    assert len(merged) == 7
    assert round(sum(row["Line total"] for row in merged), 2) == 211.43
    assert all(row["Qty"] == 1 for row in merged)


def test_checked_split_uses_suggested_receipt_row_automatically() -> None:
    rows = [
        {
            "Receipt order": 1,
            "Item": "Freshness Guaranteed Boneless, Skinless",
            "Qty": 1,
            "Line total": 9.14,
            "Delete": False,
            "Merge": False,
        },
        {
            "Receipt order": 7,
            "Item": "Chicken Breasts, 2.75 - 7.0 lb Tray",
            "Qty": 1,
            "Line total": 10.71,
            "Delete": False,
            "Merge": True,
        },
    ]

    prepared = assign_checked_review_merges(rows)

    assert prepared[1]["Merge into row"] == 1
    assert prepared[1]["Merge mode"] == "split"


def test_row_cannot_be_checked_for_delete_and_merge() -> None:
    rows = [
        {
            "Receipt order": 1,
            "Item": "Milk",
            "Qty": 1,
            "Line total": 3.34,
            "Delete": False,
            "Merge": False,
        },
        {
            "Receipt order": 2,
            "Item": "Milk",
            "Qty": 1,
            "Line total": 3.34,
            "Delete": True,
            "Merge": True,
        },
    ]

    import pytest

    with pytest.raises(ValueError, match="both Delete and Merge"):
        assign_checked_review_merges(rows)


def test_selected_candidate_error_names_item_missing_a_price() -> None:
    rows = [
        {"Add": False, "Item name": "TO GO", "Qty": 1.0, "Line price": None},
        {"Add": True, "Item name": "Egg Fried Rice", "Qty": 3.0, "Line price": float("nan")},
    ]

    assert (
        selected_candidate_error(rows, None, 4)
        == "Enter the line price for selected item Egg Fried Rice."
    )


def test_selected_candidate_error_accepts_complete_selection() -> None:
    rows = [{"Add": True, "Item name": "Egg Fried Rice", "Qty": 3.0, "Line price": 29.97}]

    assert selected_candidate_error(rows, None, 4) is None
    assert selected_candidate_error(rows, 4, 4) == (
        "Too many OCR lines are selected for the printed item count."
    )
    assert selected_candidate_error([{**rows[0], "Add": False}], None, 4) == (
        "Select at least one OCR line to add."
    )


@pytest.mark.parametrize("differences, expected", [
    ((0.0, 0.0), "match"),
    ((0.0, -0.01), "near"),   # RL-43 Rockler: tax $0.89 read instead of $0.90
    ((0.02, 0.0), "near"),
    ((0.0, 0.03), "differ"),
    ((-0.5, 0.01), "differ"),
    ((0.1 + 0.2 - 0.3, 0.0), "match"),  # float noise is not a difference
])
def test_amount_check_status(differences, expected) -> None:
    assert amount_check_status(*differences) == expected
