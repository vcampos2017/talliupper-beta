import csv
import io
from pathlib import Path

from receipt_reader.database import (
    DEFAULT_CATEGORIES,
    create_category,
    delete_receipt_item,
    export_csv,
    export_personal_finance_csv,
    personal_finance_export_warnings,
    export_receipt_summary_csv,
    expense_totals,
    get_receipt,
    initialize_database,
    item_price_history,
    list_categories,
    list_item_alias_groups,
    list_item_names,
    list_recorded_item_names,
    list_receipts,
    remove_item_alias_group,
    save_receipt,
    save_item_alias_group,
    update_receipt,
    update_item_package_size,
)
from receipt_reader.models import ReceiptExtraction, ReceiptItem


def sample_receipt() -> ReceiptExtraction:
    return ReceiptExtraction(
        store_name="HEB",
        transaction_date="2026-09-03",
        currency="USD",
        items=[
            ReceiptItem(
                name="Bread",
                tax_marker="F",
                category="Groceries",
                quantity=2,
                unit_price=2.505,
                discount=0.5,
                line_total=4.51,
            )
        ],
        subtotal=4.51,
        tax=0.37,
        total=4.88,
        confidence=0.95,
        notes=["Test receipt"],
    )


def test_delete_saved_item_compacts_rows_and_preserves_printed_amounts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "receipts.db"
    initialize_database(database)

    receipt = sample_receipt()
    receipt.items = [
        ReceiptItem(
            name="Dog food",
            quantity=1,
            unit_price=12.00,
            discount=0.00,
            line_total=12.00,
            category="Pet food",
        ),
        ReceiptItem(
            name="Rewards information",
            quantity=1,
            unit_price=0.00,
            discount=0.00,
            line_total=0.00,
            category="Uncategorized",
        ),
        ReceiptItem(
            name="Cat litter",
            quantity=1,
            unit_price=8.00,
            discount=0.00,
            line_total=8.00,
            category="Pet Care",
        ),
    ]
    receipt.subtotal = 20.00
    receipt.tax = 1.65
    receipt.total = 21.65
    receipt_id = save_receipt(database, receipt)

    deleted = delete_receipt_item(database, receipt_id, 1)

    assert deleted == {
        "receipt_id": receipt_id,
        "deleted_item_order": 1,
        "deleted_item_name": "Rewards information",
        "remaining_item_count": 2,
        "item_total": 20.0,
        "printed_subtotal": 20.0,
    }
    saved = get_receipt(database, receipt_id)
    assert saved is not None
    assert [item["name"] for item in saved["items"]] == [
        "Dog food",
        "Cat litter",
    ]
    assert saved["subtotal"] == 20.0
    assert saved["tax"] == 1.65
    assert saved["total"] == 21.65

    exported_rows = list(csv.DictReader(io.StringIO(export_csv(database))))
    assert [row["item_number"] for row in exported_rows] == ["1", "2"]
    assert [row["item_name"] for row in exported_rows] == [
        "Dog food",
        "Cat litter",
    ]


def test_delete_saved_item_rejects_missing_or_only_row(tmp_path: Path) -> None:
    import pytest

    database = tmp_path / "receipts.db"
    initialize_database(database)
    receipt_id = save_receipt(database, sample_receipt())

    with pytest.raises(ValueError, match="keep at least one item"):
        delete_receipt_item(database, receipt_id, 0)
    with pytest.raises(ValueError, match="no longer available"):
        delete_receipt_item(database, receipt_id, 99)
    with pytest.raises(ValueError, match="receipt no longer exists"):
        delete_receipt_item(database, receipt_id + 1, 0)


def test_item_price_history_matches_only_case_and_whitespace_variants(
    tmp_path: Path,
) -> None:
    database = tmp_path / "receipts.db"
    initialize_database(database)

    first = sample_receipt()
    first.items[0].name = "Bread"
    first.items[0].quantity = 2
    first.items[0].unit_price = 2.50
    first.items[0].line_total = 4.51
    first_id = save_receipt(database, first)

    second = sample_receipt().model_copy(deep=True)
    second.transaction_date = "2026-09-10"
    second.store_name = "Walmart"
    second.items[0].name = "  bread   "
    second.items[0].quantity = 2
    second.items[0].unit_price = None
    second.items[0].line_total = 6.00
    second.subtotal, second.tax, second.total = 6.00, 0.00, 6.00
    second_id = save_receipt(database, second)

    different = sample_receipt().model_copy(deep=True)
    different.items[0].name = "Bread rolls"
    different.transaction_date = "2026-09-20"
    save_receipt(database, different)

    assert list_item_names(database, "  BRE ") == ["Bread", "Bread rolls"]
    history = item_price_history(database, " BREAD ")

    assert [row["Reference"] for row in history] == [
        f"RL-{first_id:06d}",
        f"RL-{second_id:06d}",
    ]
    assert [row["Comparable price"] for row in history] == [2.50, 3.00]
    assert [row["Price basis"] for row in history] == [
        "Saved unit price",
        "Line price / quantity",
    ]
    assert all(row["Recorded item"].strip().casefold() == "bread" for row in history)
    assert item_price_history(database, "Bread rolls")[0]["Recorded item"] == (
        "Bread rolls"
    )
    assert item_price_history(database, "   ") == []


def test_user_approved_item_alias_group_is_searchable_and_reversible(
    tmp_path: Path,
) -> None:
    database = tmp_path / "receipts.db"
    initialize_database(database)

    first = sample_receipt()
    first.items[0].name = "HEB AA CFW LG EGGS 18CT C"
    first.items[0].line_total = 2.98
    first.items[0].unit_price = None
    first.items[0].quantity = 1
    first.subtotal = first.total = 2.98
    save_receipt(database, first)

    second = sample_receipt()
    second.transaction_date = "2026-09-10"
    second.store_name = "H-E-B"
    second.items[0].name = "HEB AA CFW LG EGGS 18CT C F WHL"
    second.items[0].line_total = 3.48
    second.items[0].unit_price = None
    second.items[0].quantity = 1
    second.subtotal = second.total = 3.48
    save_receipt(database, second)

    aliases = list_recorded_item_names(database, "egg")
    canonical = save_item_alias_group(
        database, "Large eggs, 18 count", aliases
    )

    assert canonical == "Large eggs, 18 count"
    assert list_item_names(database, "large eggs") == [canonical]
    assert list_item_names(database, "f whl") == [canonical]
    assert list_item_alias_groups(database) == [
        {
            "Canonical name": canonical,
            "Recorded aliases": aliases,
        }
    ]
    grouped_history = item_price_history(database, canonical)
    assert [row["Recorded item"] for row in grouped_history] == aliases
    assert [row["Comparable price"] for row in grouped_history] == [2.98, 3.48]

    assert remove_item_alias_group(database, canonical) == 2
    assert list_item_alias_groups(database) == []
    assert list_item_names(database, "egg") == aliases
    assert len(item_price_history(database, aliases[0])) == 1


def test_item_package_size_normalizes_price_and_can_be_cleared(
    tmp_path: Path,
) -> None:
    import pytest

    database = tmp_path / "receipts.db"
    initialize_database(database)
    receipt = sample_receipt()
    receipt.items[0].name = "Large eggs"
    receipt.items[0].quantity = 2
    receipt.items[0].unit_price = None
    receipt.items[0].line_total = 5.96
    receipt.subtotal = receipt.total = 5.96
    receipt_id = save_receipt(database, receipt)

    update_item_package_size(database, receipt_id, 0, 18, "count")
    history = item_price_history(database, "Large eggs")

    assert history[0]["Package amount"] == 18
    assert history[0]["Package unit"] == "count"
    assert history[0]["Normalized price"] == 0.1656
    assert history[0]["Normalized unit"] == "count"
    assert history[0]["Normalization basis"] == (
        "Line price / (quantity × 18 count)"
    )
    assert history[0]["Line price"] == 5.96

    update_item_package_size(database, receipt_id, 0, None, None)
    cleared = item_price_history(database, "Large eggs")[0]
    assert cleared["Package amount"] is None
    assert cleared["Normalized price"] is None
    assert cleared["Line price"] == 5.96

    with pytest.raises(ValueError, match="greater than zero"):
        update_item_package_size(database, receipt_id, 0, 0, "count")
    with pytest.raises(ValueError, match="supported package unit"):
        update_item_package_size(database, receipt_id, 0, 18, "carton")
    with pytest.raises(ValueError, match="no longer available"):
        update_item_package_size(database, receipt_id, 99, 18, "count")


def test_database_round_trip(tmp_path: Path) -> None:
    database = tmp_path / "receipts.db"
    initialize_database(database)

    receipt_id = save_receipt(
        database,
        sample_receipt(),
        source_filename="receipt.jpg",
        extraction_method="local-paddleocr",
    )

    summaries = list_receipts(database)
    saved = get_receipt(database, receipt_id)

    assert summaries[0]["store"] == "HEB"
    assert summaries[0]["items"] == 1
    assert summaries[0]["method"] == "local-paddleocr"
    assert saved is not None
    assert saved["total"] == 4.88
    assert saved["items"][0]["unit_price"] == 2.5
    assert saved["items"][0]["tax_marker"] == "F"
    assert saved["items"][0]["category"] == "Groceries"
    assert saved["source_filename"] == "receipt.jpg"
    assert saved["extraction_method"] == "local-paddleocr"


def test_update_receipt_replaces_reviewed_fields_without_changing_identity(
    tmp_path: Path,
) -> None:
    database = tmp_path / "receipts.db"
    initialize_database(database)
    original = sample_receipt()
    receipt_id = save_receipt(
        database,
        original,
        source_filename="receipt.jpg",
        image_sha256="same-image",
        extraction_method="local-paddleocr",
    )

    corrected = original.model_copy(deep=True)
    corrected.store_name = "H-E-B — Store #123"
    corrected.items[0].category = "Groceries"
    corrected.items[0].name = "Large eggs"
    corrected.notes.append("Reviewed correction.")
    update_receipt(database, receipt_id, corrected)

    saved = get_receipt(database, receipt_id)
    assert saved is not None
    assert saved["id"] == receipt_id
    assert saved["store_name"] == "H-E-B — Store #123"
    assert saved["items"][0]["name"] == "Large eggs"
    assert saved["items"][0]["category"] == "Groceries"
    assert saved["source_filename"] == "receipt.jpg"
    assert saved["extraction_method"] == "local-paddleocr"
    assert saved["created_at"]
    assert saved["updated_at"]


def test_csv_export(tmp_path: Path) -> None:
    database = tmp_path / "receipts.db"
    initialize_database(database)
    save_receipt(database, sample_receipt())

    exported = export_csv(database)

    assert "receipt_id,item_number,transaction_date,store_name" in exported
    assert "2026-09-03,HEB,manual,USD" in exported
    assert "Bread" in exported
    assert "tax_marker" in exported
    assert "category" in exported
    assert ",F,Groceries,Bread," in exported
    rows = list(csv.DictReader(io.StringIO(exported)))
    assert rows[0]["item_number"] == "1"


def test_csv_exports_can_be_filtered_and_summarized(tmp_path: Path) -> None:
    database = tmp_path / "receipts.db"
    initialize_database(database)
    save_receipt(database, sample_receipt())

    second = sample_receipt().model_copy(deep=True)
    second.store_name = "Petco #123"
    second.transaction_date = "2026-09-12"
    second.items[0].name = "Pet medication refill"
    second.items[0].category = "Pet Care"
    second.items[0].line_total = 43.47
    second.items[0].unit_price = 43.47
    second.subtotal, second.tax, second.total = 43.47, 3.59, 47.06
    save_receipt(database, second)

    petco_items = export_csv(
        database,
        start_date="2026-09-10",
        end_date="2026-09-30",
        stores=["Petco #123"],
        categories=["Pet Care"],
    )
    assert "Pet medication refill" in petco_items
    assert "Bread" not in petco_items

    no_categories = export_csv(database, categories=[])
    assert no_categories.count("\n") == 1

    summaries = export_receipt_summary_csv(database)
    assert "receipt_id,transaction_date,store_name,currency,item_count" in summaries
    assert "2026-09-03,HEB,USD,1,4.51,0.37,4.88,manual" in summaries
    assert "2026-09-12,Petco #123,USD,1,43.47,3.59,47.06,manual" in summaries

    petco_summary = export_receipt_summary_csv(
        database, stores=["Petco #123"]
    )
    assert "Petco #123" in petco_summary
    assert "HEB" not in petco_summary


def test_expense_totals_can_be_filtered_by_transaction_date(
    tmp_path: Path,
) -> None:
    database = tmp_path / "receipts.db"
    initialize_database(database)

    older = sample_receipt()
    older.transaction_date = "2025-12-31"
    older.items[0].line_total = 4.00
    older.subtotal = older.total = 4.00
    save_receipt(database, older)

    current = sample_receipt()
    current.transaction_date = "2026-09-03"
    current.items[0].line_total = 6.00
    current.subtotal = current.total = 6.00
    save_receipt(database, current)

    all_rows = expense_totals(database)
    current_rows = expense_totals(
        database,
        start_date="2026-01-01",
        end_date="2026-12-31",
    )

    assert sum(row["Amount"] for row in all_rows) == 10.00
    assert sum(row["Amount"] for row in current_rows) == 6.00


def test_personal_finance_export_uses_one_transaction_per_receipt(tmp_path: Path) -> None:
    database = tmp_path / "receipts.db"
    initialize_database(database)

    single = sample_receipt()
    single.store_name = "HEB, North"
    single_id = save_receipt(database, single)

    mixed = sample_receipt().model_copy(deep=True)
    mixed.store_name = "Walmart Blanco"
    mixed.transaction_date = "2026-09-05"
    mixed.items.append(
        ReceiptItem(
            name="Paper towels",
            category="Household",
            quantity=1,
            unit_price=3.49,
            discount=0.0,
            line_total=3.49,
        )
    )
    mixed.subtotal, mixed.tax, mixed.total = 8.77, 0.66, 9.43
    mixed_id = save_receipt(database, mixed)

    exported = export_personal_finance_csv(database)
    rows = list(csv.DictReader(io.StringIO(exported)))

    assert len(rows) == 2
    assert list(rows[0]) == [
        "Date",
        "Payee",
        "Amount",
        "Category",
        "Tags",
        "Notes",
        "Check_No",
    ]
    assert rows[0] == {
        "Date": "9/3/26",
        "Payee": "HEB, North",
        "Amount": "-4.88",
        "Category": "Groceries",
        "Tags": "TalliUpper",
        "Notes": (
            f"RL-{single_id:06d} | 1 item | Groceries USD 4.51 | Tax USD 0.37"
        ),
        "Check_No": f"RL-{single_id:06d}",
    }
    assert rows[1]["Date"] == "9/5/26"
    assert rows[1]["Payee"] == "Walmart Blanco"
    assert rows[1]["Amount"] == "-9.43"
    assert rows[1]["Category"] == "Mixed purchase"
    assert rows[1]["Tags"] == "TalliUpper"
    assert rows[1]["Check_No"] == f"RL-{mixed_id:06d}"
    assert rows[1]["Notes"] == (
        f"RL-{mixed_id:06d} | 2 items | Groceries USD 4.51; "
        "Household USD 3.49 | Tax USD 0.66 | Unallocated USD 0.77"
    )
    assert '"HEB, North"' in exported

    filtered = export_personal_finance_csv(
        database,
        start_date="2026-09-05",
        end_date="2026-09-05",
        stores=["Walmart Blanco"],
    )
    filtered_rows = list(csv.DictReader(io.StringIO(filtered)))
    assert len(filtered_rows) == 1
    assert filtered_rows[0]["Payee"] == "Walmart Blanco"

    empty = export_personal_finance_csv(database, stores=[])
    assert list(csv.DictReader(io.StringIO(empty))) == []


def test_personal_finance_export_handles_zero_items_and_reports_risks(
    tmp_path: Path,
) -> None:
    database = tmp_path / "receipts.db"
    initialize_database(database)

    zero_item_receipt = ReceiptExtraction(
        store_name="Appr No : 02554P Ref No : 836408",
        transaction_date=None,
        currency="USD",
        items=[],
        subtotal=12.37,
        tax=0.0,
        total=12.37,
        confidence=0.5,
        notes=[],
    )
    zero_id = save_receipt(database, zero_item_receipt)

    duplicate = sample_receipt()
    first_duplicate_id = save_receipt(database, duplicate)
    second_duplicate = duplicate.model_copy(deep=True)
    second_duplicate.store_name = "H-E-B"
    second_duplicate_id = save_receipt(database, second_duplicate)

    rows = list(csv.DictReader(io.StringIO(export_personal_finance_csv(database))))
    zero_row = next(row for row in rows if row["Check_No"] == f"RL-{zero_id:06d}")
    assert zero_row["Category"] == "Uncategorized"
    assert zero_row["Notes"] == (
        f"RL-{zero_id:06d} | 0 items | Uncategorized USD 0.00 | "
        "Tax USD 0.00 | Unallocated USD 12.37"
    )

    image_duplicate = sample_receipt().model_copy(deep=True)
    image_duplicate.transaction_date = "2026-09-21"
    image_duplicate.store_name = "Corrected Merchant Name"
    image_duplicate_id = save_receipt(
        database,
        image_duplicate,
        image_sha256="second-photo",
    )
    original_image_id = save_receipt(
        database,
        image_duplicate.model_copy(update={"transaction_date": "2026-09-22"}),
        image_sha256="first-photo",
    )
    import sqlite3
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE receipts SET image_sha256 = ? WHERE id IN (?, ?)",
            ("same-source-image", image_duplicate_id, original_image_id),
        )

    warnings = personal_finance_export_warnings(database)
    combined = "\n".join(warnings)
    assert f"RL-{zero_id:06d}" in combined
    assert "no transaction date" in combined
    assert "zero item rows" in combined
    assert "receipt text or OCR noise" in combined
    assert "likely duplicate transaction group" in combined
    assert f"RL-{first_duplicate_id:06d}" in combined
    assert f"RL-{second_duplicate_id:06d}" in combined
    assert "identical source-image group" in combined
    assert f"RL-{image_duplicate_id:06d}" in combined
    assert f"RL-{original_image_id:06d}" in combined

    filtered_warnings = personal_finance_export_warnings(
        database, stores=["HEB"]
    )
    assert filtered_warnings == []
    assert personal_finance_export_warnings(database, stores=[]) == []


def test_database_migrates_existing_item_table(tmp_path: Path) -> None:
    import sqlite3

    database = tmp_path / "receipts.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE receipt_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_id INTEGER NOT NULL,
                item_order INTEGER NOT NULL,
                name TEXT NOT NULL,
                quantity REAL NOT NULL,
                unit_price_cents INTEGER,
                discount_cents INTEGER,
                line_total_cents INTEGER NOT NULL
            )
            """
        )

    initialize_database(database)

    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(receipt_items)")
        }
    assert "tax_marker" in columns
    assert "category" in columns
    assert "package_quantity" in columns
    assert "package_unit" in columns


def test_default_and_custom_categories_are_persisted(tmp_path: Path) -> None:
    import pytest

    database = tmp_path / "receipts.db"
    initialize_database(database)

    assert list_categories(database)[: len(DEFAULT_CATEGORIES)] == list(
        DEFAULT_CATEGORIES
    )
    assert create_category(database, "  Workshop   supplies  ") == (
        "Workshop supplies"
    )
    assert list_categories(database)[-1] == "Workshop supplies"

    with pytest.raises(ValueError, match="already exists"):
        create_category(database, "workshop supplies")

