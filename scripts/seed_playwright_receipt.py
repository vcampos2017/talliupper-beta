"""Create deterministic, non-personal data for the Playwright browser test."""

from __future__ import annotations

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from receipt_reader.database import initialize_database, save_receipt
from receipt_reader.models import ReceiptExtraction, ReceiptItem


EXPECTED_DATA_DIRECTORY = (PROJECT_ROOT / ".playwright-data").resolve()


def main() -> None:
    data_directory = Path(
        os.environ.get("RECEIPT_DATA_DIR", EXPECTED_DATA_DIRECTORY)
    ).resolve()
    if data_directory != EXPECTED_DATA_DIRECTORY:
        raise RuntimeError(
            "Playwright seed data must use the repository's .playwright-data folder."
        )

    data_directory.mkdir(parents=True, exist_ok=True)
    database_path = data_directory / "receipts.db"
    for suffix in ("", "-shm", "-wal"):
        candidate = Path(f"{database_path}{suffix}")
        if candidate.exists():
            candidate.unlink()

    initialize_database(database_path)
    save_receipt(
        database_path,
        ReceiptExtraction(
            store_name="Playwright Demo Store",
            transaction_date="2026-10-10",
            currency="USD",
            items=[
                ReceiptItem(
                    name="Rewards information",
                    quantity=1,
                    unit_price=0.0,
                    discount=None,
                    line_total=0.0,
                    category="Uncategorized",
                ),
                ReceiptItem(
                    name="Coffee beans",
                    quantity=1,
                    unit_price=12.0,
                    discount=None,
                    line_total=12.0,
                    category="Groceries",
                ),
                ReceiptItem(
                    name="Coffee filters",
                    quantity=1,
                    unit_price=8.0,
                    discount=None,
                    line_total=8.0,
                    category="Household",
                ),
            ],
            subtotal=20.0,
            tax=1.65,
            total=21.65,
            confidence=1.0,
            notes=["Synthetic Playwright fixture; no personal receipt data."],
        ),
        extraction_method="manual",
    )


if __name__ == "__main__":
    main()
