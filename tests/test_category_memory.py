from receipt_reader.database import (
    apply_remembered_categories, connect, initialize_database, list_categories,
    save_receipt, update_item_categories,
)
from receipt_reader.models import blank_receipt, ReceiptItem


def receipt_with(name, category="Uncategorized"):
    receipt = blank_receipt()
    receipt.items = [ReceiptItem(name=name, category=category, quantity=1,
                                unit_price=5, line_total=5, discount=None)]
    return receipt


def test_remembers_saved_choices_and_respects_overrides(tmp_path):
    db = tmp_path / "receipts.db"
    initialize_database(db)
    assert "Pet food" in list_categories(db)
    receipt_id = save_receipt(db, receipt_with("CAT FOOD 123", "Pet food"))
    incoming = receipt_with(" cat   food 123 ")
    assert apply_remembered_categories(db, incoming).items[0].category == "Pet food"
    assert incoming.items[0].category == "Uncategorized"
    assert apply_remembered_categories(db, receipt_with("CAT FOOD 456")).items[0].category == "Pet food"
    assert apply_remembered_categories(db, receipt_with("CAT FOOD 123", "Groceries")).items[0].category == "Groceries"
    update_item_categories(db, receipt_id, [0], "Groceries")
    assert apply_remembered_categories(db, incoming).items[0].category == "Groceries"
    update_item_categories(db, receipt_id, [0], "Uncategorized")
    initialize_database(db)
    assert apply_remembered_categories(db, incoming).items[0].category == "Uncategorized"


def test_existing_saved_categories_seed_memory(tmp_path):
    db = tmp_path / "receipts.db"
    initialize_database(db)
    save_receipt(db, receipt_with("EGGS", "Groceries"))
    with connect(db) as connection:
        connection.execute("DROP TABLE item_category_preferences")
    initialize_database(db)
    assert apply_remembered_categories(db, receipt_with("eggs")).items[0].category == "Groceries"
