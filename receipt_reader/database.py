"""SQLite persistence and CSV export for receipt records."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import mimetypes
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import ReceiptExtraction


DEFAULT_CATEGORIES = (
    "Uncategorized",
    "Groceries",
    "Dining & Restaurants",
    "Household",
    "Housing",
    "Utilities",
    "Transportation",
    "Automotive",
    "Fuel",
    "Health & Medical",
    "Pharmacy",
    "Personal Care",
    "Clothing",
    "Electronics",
    "Pet Care",
    "Pet food",
    "Children & Family",
    "Education",
    "Entertainment",
    "Subscriptions",
    "Travel",
    "Gifts & Donations",
    "Business",
    "Taxes & Fees",
    "Other",
)

PACKAGE_UNITS = ("count", "oz", "lb", "g", "kg", "fl oz", "ml", "l")
PACKAGE_UNIT_FACTORS = {
    "count": ("count", 1.0),
    "oz": ("oz", 1.0),
    "lb": ("oz", 16.0),
    "g": ("oz", 0.03527396195),
    "kg": ("oz", 35.27396195),
    "fl oz": ("fl oz", 1.0),
    "ml": ("fl oz", 0.0338140227),
    "l": ("fl oz", 33.8140227),
}


SCHEMA = """
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    store_name TEXT NOT NULL,
    transaction_date TEXT,
    currency TEXT NOT NULL,
    subtotal_cents INTEGER,
    tax_cents INTEGER,
    total_cents INTEGER,
    confidence REAL NOT NULL,
    notes_json TEXT NOT NULL,
    source_filename TEXT,
    image_sha256 TEXT,
    extraction_method TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS receipt_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    item_order INTEGER NOT NULL,
    name TEXT NOT NULL,
    product_code TEXT,
    quantity REAL NOT NULL,
    unit_price_cents INTEGER,
    discount_cents INTEGER,
    line_total_cents INTEGER NOT NULL,
    package_quantity REAL,
    package_unit TEXT,
    tax_marker TEXT,
    category TEXT NOT NULL DEFAULT 'Uncategorized'
);

CREATE TABLE IF NOT EXISTS receipt_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    source_order INTEGER NOT NULL,
    original_filename TEXT NOT NULL,
    stored_filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(receipt_id, source_order)
);

CREATE TABLE IF NOT EXISTS expense_categories (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    is_builtin INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 1000,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item_category_preferences (
    item_key TEXT PRIMARY KEY,
    category TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_category_preferences (
    product_code TEXT PRIMARY KEY,
    category TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS item_name_aliases (
    item_key TEXT PRIMARY KEY,
    recorded_name TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_receipts_transaction_date
ON receipts(transaction_date);

CREATE INDEX IF NOT EXISTS idx_receipts_image_sha256
ON receipts(image_sha256);

CREATE INDEX IF NOT EXISTS idx_receipt_items_receipt_id
ON receipt_items(receipt_id);

CREATE INDEX IF NOT EXISTS idx_receipt_sources_receipt_id
ON receipt_sources(receipt_id);

CREATE INDEX IF NOT EXISTS idx_item_name_aliases_canonical_key
ON item_name_aliases(canonical_key);
"""


MAX_SOURCE_FILE_BYTES = 50 * 1024 * 1024
MAX_RECEIPT_SOURCE_BYTES = 150 * 1024 * 1024
SAFE_SOURCE_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".pdf"
}


def _money_to_cents(value: float | None) -> int | None:
    if value is None:
        return None
    return round(float(value) * 100)


def _cents_to_money(value: int | None) -> float | None:
    if value is None:
        return None
    return value / 100


def connect(database_path: str | Path) -> sqlite3.Connection:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(database_path: str | Path) -> None:
    with connect(database_path) as connection:
        connection.executescript(SCHEMA)
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(receipts)")
        }
        item_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(receipt_items)")
        }
        if "product_code" not in item_columns:
            connection.execute(
                "ALTER TABLE receipt_items ADD COLUMN product_code TEXT"
            )
        if "tax_marker" not in item_columns:
            connection.execute(
                "ALTER TABLE receipt_items ADD COLUMN tax_marker TEXT"
            )
        if "category" not in item_columns:
            connection.execute(
                "ALTER TABLE receipt_items ADD COLUMN category "
                "TEXT NOT NULL DEFAULT 'Uncategorized'"
            )
        if "package_quantity" not in item_columns:
            connection.execute(
                "ALTER TABLE receipt_items ADD COLUMN package_quantity REAL"
            )
        if "package_unit" not in item_columns:
            connection.execute(
                "ALTER TABLE receipt_items ADD COLUMN package_unit TEXT"
            )
        if "extraction_method" not in columns:
            connection.execute(
                "ALTER TABLE receipts ADD COLUMN extraction_method "
                "TEXT NOT NULL DEFAULT 'unknown'"
            )
        if "updated_at" not in columns:
            connection.execute(
                "ALTER TABLE receipts ADD COLUMN updated_at TEXT"
            )
        connection.executemany(
            """
            INSERT OR IGNORE INTO expense_categories (
                name, is_builtin, sort_order, created_at
            ) VALUES (?, 1, ?, ?)
            """,
            [
                (name, order, datetime.now(UTC).isoformat(timespec="seconds"))
                for order, name in enumerate(DEFAULT_CATEGORIES)
            ],
        )
        for row in connection.execute(
            "SELECT name, product_code, category FROM receipt_items "
            "WHERE category != 'Uncategorized' ORDER BY id DESC"
        ).fetchall():
            connection.execute(
                "INSERT OR IGNORE INTO item_category_preferences VALUES (?, ?)",
                (_item_key(row["name"]), row["category"]),
            )
            if row["product_code"]:
                connection.execute(
                    "INSERT OR IGNORE INTO product_category_preferences VALUES (?, ?)",
                    (_product_key(row["product_code"]), row["category"]),
                )


def list_categories(database_path: str | Path) -> list[str]:
    """Return built-in categories first, followed by user-created categories."""

    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT name
            FROM expense_categories
            ORDER BY is_builtin DESC, sort_order, name COLLATE NOCASE
            """
        ).fetchall()
    return [str(row["name"]) for row in rows]


def create_category(database_path: str | Path, name: str) -> str:
    """Create and persist a reusable custom expense category."""

    cleaned = " ".join(name.split()).strip()
    if not cleaned:
        raise ValueError("Enter a category name.")
    if len(cleaned) > 50:
        raise ValueError("Category names must be 50 characters or fewer.")
    if any(ord(character) < 32 for character in cleaned):
        raise ValueError("Category names cannot contain control characters.")

    with connect(database_path) as connection:
        try:
            connection.execute(
                """
                INSERT INTO expense_categories (
                    name, is_builtin, sort_order, created_at
                ) VALUES (?, 0, 1000, ?)
                """,
                (cleaned, datetime.now(UTC).isoformat(timespec="seconds")),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f'The category "{cleaned}" already exists.') from exc
    return cleaned


def _source_archive_root(
    database_path: str | Path,
    source_archive_dir: str | Path | None = None,
) -> Path:
    return (
        Path(source_archive_dir)
        if source_archive_dir is not None
        else Path(database_path).parent / "receipt_sources"
    )


def _validated_source_files(
    source_files: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    total_size = 0
    for source_order, source in enumerate(source_files or [], start=1):
        raw_data = source.get("data", b"")
        if not isinstance(raw_data, (bytes, bytearray)):
            raise ValueError("A receipt source file could not be read as bytes.")
        data = bytes(raw_data)
        if not data:
            raise ValueError("An empty receipt source file cannot be archived.")
        if len(data) > MAX_SOURCE_FILE_BYTES:
            raise ValueError("Each original receipt file must be 50 MB or smaller.")
        total_size += len(data)
        if total_size > MAX_RECEIPT_SOURCE_BYTES:
            raise ValueError(
                "The original files for one receipt must total 150 MB or less."
            )

        original_filename = Path(
            str(source.get("filename") or f"receipt-source-{source_order}")
        ).name
        original_filename = "".join(
            character
            for character in original_filename
            if ord(character) >= 32 and character not in "<>:\"/\\|?*"
        ).strip()[:180]
        if not original_filename:
            original_filename = f"receipt-source-{source_order}"
        suffix = Path(original_filename).suffix.casefold()
        stored_suffix = suffix if suffix in SAFE_SOURCE_SUFFIXES else ".bin"
        media_type = str(source.get("media_type") or "").strip()
        if not media_type:
            media_type = mimetypes.guess_type(original_filename)[0] or (
                "application/octet-stream"
            )
        validated.append(
            {
                "source_order": source_order,
                "data": data,
                "original_filename": original_filename,
                "stored_filename": f"source-{source_order:03d}{stored_suffix}",
                "media_type": media_type,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return validated


def save_receipt(
    database_path: str | Path,
    receipt: ReceiptExtraction,
    *,
    source_filename: str | None = None,
    image_sha256: str | None = None,
    extraction_method: str = "manual",
    allow_possible_duplicate: bool = False,
    source_files: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    source_archive_dir: str | Path | None = None,
) -> int:
    """Save a receipt and optional original files, returning the database id."""

    validated_sources = _validated_source_files(source_files)
    archive_root = _source_archive_root(database_path, source_archive_dir)
    temporary_directory: Path | None = None
    receipt_directory: Path | None = None
    created_receipt_directory = False
    try:
        with connect(database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicates = _duplicate_matches(connection, receipt, image_sha256)
            if duplicates and (
                any(duplicate["exact"] for duplicate in duplicates)
                or not allow_possible_duplicate
            ):
                raise DuplicateReceiptError(duplicates)
            cursor = connection.execute(
            """
            INSERT INTO receipts (
                store_name, transaction_date, currency, subtotal_cents,
                tax_cents, total_cents, confidence, notes_json,
                source_filename, image_sha256, extraction_method, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt.store_name,
                receipt.transaction_date,
                receipt.currency,
                _money_to_cents(receipt.subtotal),
                _money_to_cents(receipt.tax),
                _money_to_cents(receipt.total),
                receipt.confidence,
                json.dumps(receipt.notes),
                source_filename,
                image_sha256,
                extraction_method,
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
            receipt_id = int(cursor.lastrowid)
            for item in receipt.items:
                if item.category != "Uncategorized":
                    connection.execute(
                        "INSERT OR REPLACE INTO item_category_preferences VALUES (?, ?)",
                        (_item_key(item.name), item.category),
                    )
                    if item.product_code:
                        connection.execute(
                            "INSERT OR REPLACE INTO product_category_preferences VALUES (?, ?)",
                            (_product_key(item.product_code), item.category),
                        )

            connection.executemany(
                """
                INSERT INTO receipt_items (
                    receipt_id, item_order, name, product_code, quantity,
                    unit_price_cents, discount_cents, line_total_cents,
                    tax_marker, category
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        receipt_id,
                        order,
                        item.name,
                        item.product_code,
                        item.quantity,
                        _money_to_cents(item.unit_price),
                        _money_to_cents(item.discount),
                        _money_to_cents(item.line_total),
                        item.tax_marker,
                        item.category,
                    )
                    for order, item in enumerate(receipt.items)
                ],
            )

            if validated_sources:
                archive_root.mkdir(parents=True, exist_ok=True)
                receipt_reference = f"RL-{receipt_id:06d}"
                receipt_directory = archive_root / receipt_reference
                if receipt_directory.exists():
                    raise ValueError(
                        f"The archive folder for {receipt_reference} already exists. "
                        "No receipt was saved."
                    )
                temporary_directory = archive_root / (
                    f".{receipt_reference}-{uuid.uuid4().hex}.tmp"
                )
                temporary_directory.mkdir()
                created_at = datetime.now(UTC).isoformat(timespec="seconds")
                for source in validated_sources:
                    target = temporary_directory / source["stored_filename"]
                    target.write_bytes(source["data"])
                    connection.execute(
                        """
                        INSERT INTO receipt_sources (
                            receipt_id, source_order, original_filename,
                            stored_filename, media_type, size_bytes, sha256,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            receipt_id,
                            source["source_order"],
                            source["original_filename"],
                            source["stored_filename"],
                            source["media_type"],
                            source["size_bytes"],
                            source["sha256"],
                            created_at,
                        ),
                    )
                os.replace(temporary_directory, receipt_directory)
                temporary_directory = None
                created_receipt_directory = True
            return receipt_id
    except Exception:
        if temporary_directory is not None and temporary_directory.exists():
            shutil.rmtree(temporary_directory, ignore_errors=True)
        if (
            created_receipt_directory
            and receipt_directory is not None
            and receipt_directory.exists()
        ):
            shutil.rmtree(receipt_directory, ignore_errors=True)
        raise


def update_receipt(
    database_path: str | Path,
    receipt_id: int,
    receipt: ReceiptExtraction,
) -> None:
    """Replace reviewed receipt fields and item rows without creating a copy.

    The receipt id, original source metadata/files, image hash, extraction method,
    and creation timestamp are preserved. This is the safe path for correcting an
    exact duplicate that is already stored locally.
    """

    updated_at = datetime.now(UTC).isoformat(timespec="seconds")
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT id FROM receipts WHERE id = ?", (receipt_id,)
        ).fetchone()
        if existing is None:
            raise ValueError("The selected receipt no longer exists.")

        connection.execute(
            """
            UPDATE receipts
            SET store_name = ?, transaction_date = ?, currency = ?,
                subtotal_cents = ?, tax_cents = ?, total_cents = ?,
                confidence = ?, notes_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                receipt.store_name,
                receipt.transaction_date,
                receipt.currency,
                _money_to_cents(receipt.subtotal),
                _money_to_cents(receipt.tax),
                _money_to_cents(receipt.total),
                receipt.confidence,
                json.dumps(receipt.notes),
                updated_at,
                receipt_id,
            ),
        )
        connection.execute(
            "DELETE FROM receipt_items WHERE receipt_id = ?", (receipt_id,)
        )
        connection.executemany(
            """
            INSERT INTO receipt_items (
                receipt_id, item_order, name, product_code, quantity,
                unit_price_cents, discount_cents, line_total_cents,
                tax_marker, category
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    receipt_id,
                    order,
                    item.name,
                    item.product_code,
                    item.quantity,
                    _money_to_cents(item.unit_price),
                    _money_to_cents(item.discount),
                    _money_to_cents(item.line_total),
                    item.tax_marker,
                    item.category,
                )
                for order, item in enumerate(receipt.items)
            ],
        )
        for item in receipt.items:
            if item.category == "Uncategorized":
                continue
            connection.execute(
                "INSERT OR REPLACE INTO item_category_preferences VALUES (?, ?)",
                (_item_key(item.name), item.category),
            )
            if item.product_code:
                connection.execute(
                    "INSERT OR REPLACE INTO product_category_preferences VALUES (?, ?)",
                    (_product_key(item.product_code), item.category),
                )


class DuplicateReceiptError(ValueError):
    def __init__(self, matches):
        self.matches = matches
        super().__init__("Receipt may already be saved as " + ", ".join(f"#{m['id']}" for m in matches) + ". No new entry was created.")


def _duplicate_matches(connection, receipt, image_sha256):
    matches = []
    normalized_store = " ".join(receipt.store_name.casefold().split())
    rows = connection.execute(
        "SELECT id, image_sha256, store_name, transaction_date, total_cents, currency FROM receipts "
        "WHERE (? IS NOT NULL AND image_sha256 = ?) OR "
        "(transaction_date = ? AND total_cents = ?)",
        (image_sha256 or None, image_sha256 or None, receipt.transaction_date, _money_to_cents(receipt.total)),
    ).fetchall()
    for row in rows:
        exact = bool(image_sha256 and image_sha256 == row["image_sha256"])
        possible = bool(normalized_store and receipt.transaction_date and receipt.total is not None
                        and normalized_store == " ".join(row["store_name"].casefold().split())
                        and receipt.currency.upper() == row["currency"].upper())
        if exact or possible:
            matches.append({"id": row["id"], "exact": exact})
    return matches


def find_duplicate_receipts(database_path, receipt, image_sha256=None):
    """Exact source matches, or possible same-store/date/currency/total matches."""
    with connect(database_path) as connection:
        return _duplicate_matches(connection, receipt, image_sha256)


def list_receipts(database_path: str | Path) -> list[dict[str, Any]]:
    """Return receipt summaries newest transaction first."""

    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT
                r.*,
                COUNT(DISTINCT i.id) AS item_count,
                COUNT(DISTINCT s.id) AS source_count
            FROM receipts AS r
            LEFT JOIN receipt_items AS i ON i.receipt_id = r.id
            LEFT JOIN receipt_sources AS s ON s.receipt_id = r.id
            GROUP BY r.id
            ORDER BY COALESCE(r.transaction_date, r.created_at) DESC, r.id DESC
            """
        ).fetchall()

    return [
        {
            "id": row["id"],
            "date": row["transaction_date"],
            "store": row["store_name"],
            "items": row["item_count"],
            "source_files": row["source_count"],
            "subtotal": _cents_to_money(row["subtotal_cents"]),
            "tax": _cents_to_money(row["tax_cents"]),
            "total": _cents_to_money(row["total_cents"]),
            "currency": row["currency"],
            "confidence": row["confidence"],
            "method": row["extraction_method"],
        }
        for row in rows
    ]


def get_receipt(database_path: str | Path, receipt_id: int) -> dict[str, Any] | None:
    with connect(database_path) as connection:
        receipt = connection.execute(
            "SELECT * FROM receipts WHERE id = ?", (receipt_id,)
        ).fetchone()
        if receipt is None:
            return None
        items = connection.execute(
            """
            SELECT * FROM receipt_items
            WHERE receipt_id = ?
            ORDER BY item_order, id
            """,
            (receipt_id,),
        ).fetchall()
        sources = connection.execute(
            """
            SELECT id, source_order, original_filename, stored_filename,
                   media_type, size_bytes, sha256, created_at
            FROM receipt_sources
            WHERE receipt_id = ?
            ORDER BY source_order, id
            """,
            (receipt_id,),
        ).fetchall()

    return {
        "id": receipt["id"],
        "store_name": receipt["store_name"],
        "transaction_date": receipt["transaction_date"],
        "currency": receipt["currency"],
        "subtotal": _cents_to_money(receipt["subtotal_cents"]),
        "tax": _cents_to_money(receipt["tax_cents"]),
        "total": _cents_to_money(receipt["total_cents"]),
        "confidence": receipt["confidence"],
        "notes": json.loads(receipt["notes_json"]),
        "source_filename": receipt["source_filename"],
        "extraction_method": receipt["extraction_method"],
        "created_at": receipt["created_at"],
        "updated_at": receipt["updated_at"],
        "items": [
            {
                "tax_marker": item["tax_marker"],
                "category": item["category"],
                "name": item["name"],
                "product_code": item["product_code"],
                "quantity": item["quantity"],
                "unit_price": _cents_to_money(item["unit_price_cents"]),
                "discount": _cents_to_money(item["discount_cents"]),
                "line_total": _cents_to_money(item["line_total_cents"]),
                "package_quantity": item["package_quantity"],
                "package_unit": item["package_unit"],
            }
            for item in items
        ],
        "sources": [dict(source) for source in sources],
    }


def _receipt_source_path(
    archive_root: Path,
    receipt_id: int,
    stored_filename: str,
) -> Path:
    receipt_directory = (archive_root / f"RL-{receipt_id:06d}").resolve()
    source_path = (receipt_directory / stored_filename).resolve()
    if source_path.parent != receipt_directory:
        raise ValueError("An archived receipt file has an unsafe stored path.")
    return source_path


def list_receipt_sources(
    database_path: str | Path,
    receipt_id: int,
    *,
    source_archive_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Return archived-source metadata and integrity status for one receipt."""

    archive_root = _source_archive_root(database_path, source_archive_dir)
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT id, receipt_id, source_order, original_filename,
                   stored_filename, media_type, size_bytes, sha256, created_at
            FROM receipt_sources
            WHERE receipt_id = ?
            ORDER BY source_order, id
            """,
            (receipt_id,),
        ).fetchall()

    sources: list[dict[str, Any]] = []
    for row in rows:
        source = dict(row)
        path = _receipt_source_path(
            archive_root, int(row["receipt_id"]), str(row["stored_filename"])
        )
        exists = path.is_file()
        actual_size = path.stat().st_size if exists else None
        checksum_ok = False
        if exists and actual_size == row["size_bytes"]:
            checksum_ok = hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
        source.update(
            {
                "exists": exists,
                "actual_size_bytes": actual_size,
                "checksum_ok": checksum_ok,
            }
        )
        sources.append(source)
    return sources


def read_receipt_source(
    database_path: str | Path,
    source_id: int,
    *,
    source_archive_dir: str | Path | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Read one archived original only when its size and checksum still match."""

    archive_root = _source_archive_root(database_path, source_archive_dir)
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT * FROM receipt_sources WHERE id = ?", (source_id,)
        ).fetchone()
    if row is None:
        raise ValueError("The selected original receipt file no longer exists.")
    path = _receipt_source_path(
        archive_root, int(row["receipt_id"]), str(row["stored_filename"])
    )
    if not path.is_file():
        raise ValueError("The archived original receipt file is missing.")
    data = path.read_bytes()
    if len(data) != row["size_bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise ValueError("The archived original receipt file failed its integrity check.")
    return dict(row), data


def build_backup_zip(
    database_path: str | Path,
    *,
    source_archive_dir: str | Path | None = None,
) -> bytes:
    """Build a verified ZIP containing a consistent database snapshot and originals."""

    archive_root = _source_archive_root(database_path, source_archive_dir)
    with closing(connect(database_path)) as connection:
        receipt_count = int(connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0])
        source_rows = connection.execute(
            "SELECT * FROM receipt_sources ORDER BY receipt_id, source_order, id"
        ).fetchall()

    verified_sources: list[tuple[sqlite3.Row, Path]] = []
    failures: list[str] = []
    for row in source_rows:
        path = _receipt_source_path(
            archive_root, int(row["receipt_id"]), str(row["stored_filename"])
        )
        if not path.is_file():
            failures.append(f"RL-{int(row['receipt_id']):06d}: missing {row['original_filename']}")
            continue
        data = path.read_bytes()
        if len(data) != row["size_bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
            failures.append(f"RL-{int(row['receipt_id']):06d}: changed {row['original_filename']}")
            continue
        verified_sources.append((row, path))
    if failures:
        raise ValueError(
            "Backup stopped because archived originals failed verification: "
            + "; ".join(failures)
        )

    manifest = {
        "format_version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "receipt_count": receipt_count,
        "source_count": len(verified_sources),
        "sources": [
            {
                "receipt_reference": f"RL-{int(row['receipt_id']):06d}",
                "original_filename": row["original_filename"],
                "stored_filename": row["stored_filename"],
                "media_type": row["media_type"],
                "size_bytes": row["size_bytes"],
                "sha256": row["sha256"],
            }
            for row, _ in verified_sources
        ],
    }

    with tempfile.TemporaryDirectory(prefix="receiptlens-backup-") as temp_name:
        snapshot_path = Path(temp_name) / "receipts.db"
        with closing(connect(database_path)) as source_connection:
            with closing(sqlite3.connect(snapshot_path)) as snapshot_connection:
                source_connection.backup(snapshot_connection)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot_path, "receiptlens-backup/receipts.db")
            archive.writestr(
                "receiptlens-backup/manifest.json",
                json.dumps(manifest, indent=2, sort_keys=True),
            )
            for row, path in verified_sources:
                archive.write(
                    path,
                    "receiptlens-backup/receipt_sources/"
                    f"RL-{int(row['receipt_id']):06d}/{row['stored_filename']}",
                )
        return output.getvalue()


def export_csv(
    database_path: str | Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    stores: list[str] | tuple[str, ...] | None = None,
    categories: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Create a filtered, spreadsheet-friendly CSV with one row per item."""

    output = io.StringIO(newline="")
    fieldnames = [
        "receipt_id",
        "item_number",
        "transaction_date",
        "store_name",
        "extraction_method",
        "currency",
        "receipt_subtotal",
        "receipt_tax",
        "receipt_total",
        "tax_marker",
        "category",
        "item_name",
        "product_code",
        "quantity",
        "unit_price",
        "item_discount",
        "line_total",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    conditions: list[str] = []
    parameters: list[object] = []
    if start_date:
        conditions.append("r.transaction_date >= ?")
        parameters.append(start_date)
    if end_date:
        conditions.append("r.transaction_date <= ?")
        parameters.append(end_date)
    if stores is not None:
        if not stores:
            return output.getvalue()
        conditions.append(
            f"r.store_name IN ({','.join('?' for _ in stores)})"
        )
        parameters.extend(stores)
    if categories is not None:
        if not categories:
            return output.getvalue()
        conditions.append(
            f"i.category IN ({','.join('?' for _ in categories)})"
        )
        parameters.extend(categories)
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with connect(database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT
                r.id AS receipt_id,
                r.transaction_date,
                r.store_name,
                r.extraction_method,
                r.currency,
                r.subtotal_cents,
                r.tax_cents,
                r.total_cents,
                i.item_order,
                i.tax_marker,
                i.category,
                i.name AS item_name,
                i.product_code,
                i.quantity,
                i.unit_price_cents,
                i.discount_cents,
                i.line_total_cents
            FROM receipts AS r
            LEFT JOIN receipt_items AS i ON i.receipt_id = r.id
            {where_clause}
            ORDER BY COALESCE(r.transaction_date, r.created_at), r.id, i.item_order
            """,
            parameters,
        ).fetchall()

    for row in rows:
        writer.writerow(
            {
                "receipt_id": row["receipt_id"],
                "item_number": (
                    row["item_order"] + 1
                    if row["item_order"] is not None
                    else ""
                ),
                "transaction_date": row["transaction_date"] or "",
                "store_name": row["store_name"],
                "extraction_method": row["extraction_method"],
                "currency": row["currency"],
                "receipt_subtotal": _cents_to_money(row["subtotal_cents"]),
                "receipt_tax": _cents_to_money(row["tax_cents"]),
                "receipt_total": _cents_to_money(row["total_cents"]),
                "tax_marker": row["tax_marker"] or "",
                "category": row["category"] or "Uncategorized",
                "item_name": row["item_name"] or "",
                "product_code": row["product_code"] or "",
                "quantity": row["quantity"] if row["quantity"] is not None else "",
                "unit_price": _cents_to_money(row["unit_price_cents"]),
                "item_discount": _cents_to_money(row["discount_cents"]),
                "line_total": _cents_to_money(row["line_total_cents"]),
            }
        )

    return output.getvalue()


def export_receipt_summary_csv(
    database_path: str | Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    stores: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Create a filtered CSV with one row per saved receipt."""

    output = io.StringIO(newline="")
    fieldnames = [
        "receipt_id",
        "transaction_date",
        "store_name",
        "currency",
        "item_count",
        "receipt_subtotal",
        "receipt_tax",
        "receipt_total",
        "extraction_method",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    conditions: list[str] = []
    parameters: list[object] = []
    if start_date:
        conditions.append("r.transaction_date >= ?")
        parameters.append(start_date)
    if end_date:
        conditions.append("r.transaction_date <= ?")
        parameters.append(end_date)
    if stores is not None:
        if not stores:
            return output.getvalue()
        conditions.append(
            f"r.store_name IN ({','.join('?' for _ in stores)})"
        )
        parameters.extend(stores)
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with connect(database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT
                r.id AS receipt_id,
                r.transaction_date,
                r.store_name,
                r.currency,
                COUNT(i.id) AS item_count,
                r.subtotal_cents,
                r.tax_cents,
                r.total_cents,
                r.extraction_method
            FROM receipts AS r
            LEFT JOIN receipt_items AS i ON i.receipt_id = r.id
            {where_clause}
            GROUP BY r.id
            ORDER BY COALESCE(r.transaction_date, r.created_at), r.id
            """,
            parameters,
        ).fetchall()

    for row in rows:
        writer.writerow(
            {
                "receipt_id": row["receipt_id"],
                "transaction_date": row["transaction_date"] or "",
                "store_name": row["store_name"],
                "currency": row["currency"],
                "item_count": row["item_count"],
                "receipt_subtotal": _cents_to_money(row["subtotal_cents"]),
                "receipt_tax": _cents_to_money(row["tax_cents"]),
                "receipt_total": _cents_to_money(row["total_cents"]),
                "extraction_method": row["extraction_method"],
            }
        )

    return output.getvalue()


def export_personal_finance_csv(
    database_path: str | Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    stores: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Create one expense transaction per receipt using a finance-app CSV schema.

    The column names and value conventions follow Quicken Simplifi's published
    custom CSV import format.  TalliUpper retains the authoritative item-level
    detail; this export carries a compact category breakdown in Notes.
    """

    output = io.StringIO(newline="")
    fieldnames = [
        "Date",
        "Payee",
        "Amount",
        "Category",
        "Tags",
        "Notes",
        "Check_No",
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    conditions: list[str] = []
    parameters: list[object] = []
    if start_date:
        conditions.append("r.transaction_date >= ?")
        parameters.append(start_date)
    if end_date:
        conditions.append("r.transaction_date <= ?")
        parameters.append(end_date)
    if stores is not None:
        if not stores:
            return output.getvalue()
        conditions.append(
            f"r.store_name IN ({','.join('?' for _ in stores)})"
        )
        parameters.extend(stores)
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with connect(database_path) as connection:
        receipts = connection.execute(
            f"""
            SELECT
                r.id AS receipt_id,
                r.transaction_date,
                r.store_name,
                r.currency,
                r.subtotal_cents,
                r.tax_cents,
                r.total_cents,
                COUNT(i.id) AS item_count
            FROM receipts AS r
            LEFT JOIN receipt_items AS i ON i.receipt_id = r.id
            {where_clause}
            GROUP BY r.id
            ORDER BY COALESCE(r.transaction_date, r.created_at), r.id
            """,
            parameters,
        ).fetchall()

        for receipt in receipts:
            category_rows = connection.execute(
                """
                SELECT
                    COALESCE(NULLIF(TRIM(category), ''), 'Uncategorized') AS category,
                    COALESCE(SUM(line_total_cents), 0) AS amount_cents
                FROM receipt_items
                WHERE receipt_id = ?
                GROUP BY COALESCE(NULLIF(TRIM(category), ''), 'Uncategorized')
                ORDER BY amount_cents DESC, category COLLATE NOCASE
                """,
                (receipt["receipt_id"],),
            ).fetchall()

            categories = [row["category"] for row in category_rows]
            if not categories:
                category = "Uncategorized"
            elif len(categories) == 1:
                category = categories[0]
            else:
                category = "Mixed purchase"
            currency = (receipt["currency"] or "USD").upper()
            item_subtotal_cents = sum(row["amount_cents"] for row in category_rows)
            category_summary = "; ".join(
                f'{row["category"]} {currency} {row["amount_cents"] / 100:.2f}'
                for row in category_rows
            ) or f"Uncategorized {currency} 0.00"
            receipt_reference = f'RL-{receipt["receipt_id"]:06d}'
            item_label = "item" if receipt["item_count"] == 1 else "items"
            note_parts = [
                receipt_reference,
                f'{receipt["item_count"]} {item_label}',
                category_summary,
            ]
            if receipt["tax_cents"] is not None:
                note_parts.append(
                    f'Tax {currency} {receipt["tax_cents"] / 100:.2f}'
                )
            expected_subtotal_cents = receipt["subtotal_cents"]
            if (
                expected_subtotal_cents is None
                and receipt["total_cents"] is not None
                and receipt["tax_cents"] is not None
            ):
                expected_subtotal_cents = (
                    receipt["total_cents"] - receipt["tax_cents"]
                )
            if expected_subtotal_cents is not None:
                unallocated_cents = expected_subtotal_cents - item_subtotal_cents
                if unallocated_cents:
                    note_parts.append(
                        f"Unallocated {currency} {unallocated_cents / 100:.2f}"
                    )
            notes = " | ".join(note_parts)

            transaction_date = ""
            if receipt["transaction_date"]:
                try:
                    parsed_date = datetime.strptime(
                        receipt["transaction_date"], "%Y-%m-%d"
                    )
                    transaction_date = (
                        f"{parsed_date.month}/{parsed_date.day}/{parsed_date:%y}"
                    )
                except ValueError:
                    transaction_date = ""

            total_cents = receipt["total_cents"]
            amount = "" if total_cents is None else f"{-abs(total_cents) / 100:.2f}"
            writer.writerow(
                {
                    "Date": transaction_date,
                    "Payee": receipt["store_name"],
                    "Amount": amount,
                    "Category": category,
                    "Tags": "TalliUpper",
                    "Notes": notes,
                    "Check_No": receipt_reference,
                }
            )

    return output.getvalue()


def personal_finance_export_warnings(
    database_path: str | Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    stores: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    """Return non-blocking data-quality warnings for a finance export selection."""

    if stores is not None and not stores:
        return []

    conditions: list[str] = []
    parameters: list[object] = []
    if start_date:
        conditions.append("r.transaction_date >= ?")
        parameters.append(start_date)
    if end_date:
        conditions.append("r.transaction_date <= ?")
        parameters.append(end_date)
    if stores is not None:
        conditions.append(
            f"r.store_name IN ({','.join('?' for _ in stores)})"
        )
        parameters.extend(stores)
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with connect(database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT
                r.id AS receipt_id,
                r.transaction_date,
                r.store_name,
                UPPER(COALESCE(NULLIF(TRIM(r.currency), ''), 'USD')) AS currency,
                r.total_cents,
                r.image_sha256,
                COUNT(i.id) AS item_count
            FROM receipts AS r
            LEFT JOIN receipt_items AS i ON i.receipt_id = r.id
            {where_clause}
            GROUP BY r.id
            ORDER BY r.id
            """,
            parameters,
        ).fetchall()

    def references(matching_rows: list[sqlite3.Row]) -> str:
        return ", ".join(f'RL-{row["receipt_id"]:06d}' for row in matching_rows)

    warnings: list[str] = []
    missing_dates = [row for row in rows if not row["transaction_date"]]
    if missing_dates:
        warnings.append(
            f"{len(missing_dates)} selected receipt(s) have no transaction date: "
            f"{references(missing_dates)}. Their exported Date will be blank."
        )

    zero_items = [row for row in rows if row["item_count"] == 0]
    if zero_items:
        warnings.append(
            f"{len(zero_items)} selected receipt(s) have zero item rows: "
            f"{references(zero_items)}. Review them before importing."
        )

    metadata_patterns = (
        r"\bappr(?:oval)?\s*(?:no|number|#)",
        r"\bref(?:erence)?\s*(?:no|number|#)",
        r"entries?\s+must\s+be\s+completed",
        r"\bauth(?:orization)?\s*(?:no|number|code|#)",
    )

    def suspicious_merchant(store_name: str) -> bool:
        normalized = " ".join(store_name.casefold().split())
        if any(re.search(pattern, normalized) for pattern in metadata_patterns):
            return True
        words = normalized.split()
        return (
            len(words) >= 3
            and all(len(word) == 1 and not word.isascii() for word in words[-2:])
        )

    suspicious_merchants = [
        row for row in rows if suspicious_merchant(row["store_name"])
    ]
    if suspicious_merchants:
        warnings.append(
            f"{len(suspicious_merchants)} selected receipt(s) have merchant names "
            f"that may be receipt text or OCR noise: {references(suspicious_merchants)}."
        )

    image_groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        image_hash = str(row["image_sha256"] or "").strip()
        if image_hash:
            image_groups.setdefault(image_hash, []).append(row)
    identical_images = [
        group for group in image_groups.values() if len(group) > 1
    ]
    if identical_images:
        group_labels = "; ".join(references(group) for group in identical_images)
        warnings.append(
            f"{len(identical_images)} identical source-image group(s) are selected: "
            f"{group_labels}. These records came from the same receipt image and "
            "should be compared before importing."
        )

    identical_image_pairs = {
        frozenset(row["receipt_id"] for row in group)
        for group in identical_images
    }
    duplicate_groups: dict[tuple[str, str, str, int], list[sqlite3.Row]] = {}
    for row in rows:
        if not row["transaction_date"] or row["total_cents"] is None:
            continue
        merchant_key = re.sub(r"[^a-z0-9]", "", row["store_name"].casefold())
        if not merchant_key:
            continue
        key = (
            merchant_key,
            row["transaction_date"],
            row["currency"],
            row["total_cents"],
        )
        duplicate_groups.setdefault(key, []).append(row)
    likely_duplicates = [
        group
        for group in duplicate_groups.values()
        if len(group) > 1
        and frozenset(row["receipt_id"] for row in group)
        not in identical_image_pairs
    ]
    if likely_duplicates:
        group_labels = "; ".join(references(group) for group in likely_duplicates)
        warnings.append(
            f"{len(likely_duplicates)} likely duplicate transaction group(s) are "
            f"selected: {group_labels}. Compare them before importing."
        )

    return warnings


def expense_totals(
    database_path: str | Path,
    *,
    by_item: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    """Filtered item spending, keeping currencies separate and prices in cents."""
    field = "i.name" if by_item else "i.category"
    conditions: list[str] = []
    parameters: list[object] = []
    if start_date:
        conditions.append("r.transaction_date >= ?")
        parameters.append(start_date)
    if end_date:
        conditions.append("r.transaction_date <= ?")
        parameters.append(end_date)
    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with connect(database_path) as connection:
        rows = connection.execute(
            f"""
            SELECT {field} AS label, UPPER(r.currency) AS currency,
                   SUM(i.line_total_cents) AS amount_cents
            FROM receipt_items i JOIN receipts r ON r.id = i.receipt_id
            {where_clause}
            GROUP BY {field}, UPPER(r.currency)
            ORDER BY amount_cents DESC, label
            """,
            parameters,
        ).fetchall()
    return [{"Expense": row["label"], "Currency": row["currency"],
             "Amount": row["amount_cents"] / 100} for row in rows]


def list_recorded_item_names(
    database_path: str | Path, search: str = ""
) -> list[str]:
    """Return original saved item names grouped only by case and whitespace."""

    search_key = _item_key(search)
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT name FROM receipt_items ORDER BY id"
        ).fetchall()

    names_by_key: dict[str, str] = {}
    for row in rows:
        display_name = " ".join(str(row["name"]).split())
        item_key = _item_key(display_name)
        if search_key and search_key not in item_key:
            continue
        names_by_key.setdefault(item_key, display_name)
    return sorted(names_by_key.values(), key=str.casefold)


def list_item_alias_groups(database_path: str | Path) -> list[dict[str, Any]]:
    """Return user-approved canonical product names and their recorded aliases."""

    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT canonical_name, canonical_key, recorded_name
            FROM item_name_aliases
            ORDER BY canonical_name COLLATE NOCASE, recorded_name COLLATE NOCASE
            """
        ).fetchall()

    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        group = groups.setdefault(
            row["canonical_key"],
            {"Canonical name": row["canonical_name"], "Recorded aliases": []},
        )
        group["Recorded aliases"].append(row["recorded_name"])
    return list(groups.values())


def save_item_alias_group(
    database_path: str | Path,
    canonical_name: str,
    recorded_names: list[str],
) -> str:
    """Group two or more recorded names without changing receipt item rows."""

    cleaned_canonical = " ".join(canonical_name.split()).strip()
    if not cleaned_canonical:
        raise ValueError("Enter a canonical product name.")
    canonical_key = _item_key(cleaned_canonical)
    aliases_by_key = {
        _item_key(name): " ".join(name.split()).strip()
        for name in recorded_names
        if _item_key(name)
    }
    if len(aliases_by_key) < 2:
        raise ValueError("Select at least two different recorded item names.")

    with connect(database_path) as connection:
        existing_names = {
            _item_key(row["name"])
            for row in connection.execute("SELECT name FROM receipt_items")
        }
        missing = set(aliases_by_key) - existing_names
        if missing:
            raise ValueError(
                "One or more selected item names are no longer available. Reload the page."
            )

        collision = canonical_key in existing_names and canonical_key not in aliases_by_key
        if collision:
            raise ValueError(
                "That canonical name is already a different recorded item. "
                "Select it as an alias or choose another canonical name."
            )

        placeholders = ", ".join("?" for _ in aliases_by_key)
        conflicts = connection.execute(
            f"""
            SELECT DISTINCT canonical_name
            FROM item_name_aliases
            WHERE item_key IN ({placeholders}) AND canonical_key != ?
            """,
            [*aliases_by_key, canonical_key],
        ).fetchall()
        if conflicts:
            names = ", ".join(row["canonical_name"] for row in conflicts)
            raise ValueError(
                f"A selected name already belongs to another group: {names}. "
                "Remove that group before reassigning it."
            )

        now = datetime.now(UTC).isoformat(timespec="seconds")
        connection.execute(
            """
            UPDATE item_name_aliases
            SET canonical_name = ?
            WHERE canonical_key = ?
            """,
            (cleaned_canonical, canonical_key),
        )
        connection.executemany(
            """
            INSERT OR REPLACE INTO item_name_aliases (
                item_key, recorded_name, canonical_name, canonical_key, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (item_key, display_name, cleaned_canonical, canonical_key, now)
                for item_key, display_name in aliases_by_key.items()
            ],
        )
    return cleaned_canonical


def remove_item_alias_group(
    database_path: str | Path, canonical_name: str
) -> int:
    """Remove a grouping preference while leaving every receipt item unchanged."""

    canonical_key = _item_key(canonical_name)
    with connect(database_path) as connection:
        cursor = connection.execute(
            "DELETE FROM item_name_aliases WHERE canonical_key = ?",
            (canonical_key,),
        )
    return int(cursor.rowcount)


def list_item_names(
    database_path: str | Path, search: str = ""
) -> list[str]:
    """Return searchable product names, applying user-approved alias groups."""

    search_key = _item_key(search)
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT name
            FROM receipt_items
            ORDER BY id
            """
        ).fetchall()
        aliases = {
            row["item_key"]: (row["canonical_name"], row["canonical_key"])
            for row in connection.execute(
                "SELECT item_key, canonical_name, canonical_key FROM item_name_aliases"
            )
        }

    products: dict[str, dict[str, Any]] = {}
    for row in rows:
        recorded_name = " ".join(str(row["name"]).split())
        recorded_key = _item_key(recorded_name)
        alias = aliases.get(recorded_key)
        display_name, product_key = alias or (recorded_name, recorded_key)
        product = products.setdefault(
            product_key,
            {"display": display_name, "search_keys": {product_key}},
        )
        product["search_keys"].add(recorded_key)

    return sorted(
        (
            product["display"]
            for product in products.values()
            if not search_key
            or any(search_key in key for key in product["search_keys"])
        ),
        key=str.casefold,
    )


def item_price_history(
    database_path: str | Path, item_name: str
) -> list[dict[str, Any]]:
    """Return read-only purchase history for one case/whitespace-normalized name."""

    requested_key = _item_key(item_name)
    target_key = requested_key
    if not target_key:
        return []
    with connect(database_path) as connection:
        alias_rows = connection.execute(
            "SELECT item_key, canonical_key FROM item_name_aliases"
        ).fetchall()
        aliases = {
            row["item_key"]: row["canonical_key"] for row in alias_rows
        }
        canonical_keys = set(aliases.values())
        if requested_key in aliases:
            target_key = aliases[requested_key]
        elif requested_key in canonical_keys:
            target_key = requested_key
        rows = connection.execute(
            """
            SELECT
                r.id AS receipt_id,
                r.transaction_date,
                r.store_name,
                UPPER(r.currency) AS currency,
                i.item_order,
                i.name,
                i.product_code,
                i.category,
                i.quantity,
                i.unit_price_cents,
                i.line_total_cents,
                i.package_quantity,
                i.package_unit
            FROM receipt_items AS i
            JOIN receipts AS r ON r.id = i.receipt_id
            ORDER BY
                r.transaction_date IS NULL,
                r.transaction_date,
                r.id,
                i.item_order
            """
        ).fetchall()

    history: list[dict[str, Any]] = []
    for row in rows:
        recorded_key = _item_key(str(row["name"]))
        if aliases.get(recorded_key, recorded_key) != target_key:
            continue
        quantity = float(row["quantity"])
        line_price = row["line_total_cents"] / 100
        if row["unit_price_cents"] is not None:
            comparable_price = row["unit_price_cents"] / 100
            price_basis = "Saved unit price"
        else:
            comparable_price = round(line_price / quantity, 2)
            price_basis = "Line price / quantity"
        package_quantity = row["package_quantity"]
        package_unit = row["package_unit"]
        normalized_price = None
        normalized_unit = None
        normalization_basis = None
        if package_quantity is not None and package_unit in PACKAGE_UNIT_FACTORS:
            normalized_unit, conversion_factor = PACKAGE_UNIT_FACTORS[package_unit]
            total_content = quantity * float(package_quantity) * conversion_factor
            if total_content > 0:
                normalized_price = round(line_price / total_content, 4)
                normalization_basis = (
                    "Line price / (quantity × "
                    f"{float(package_quantity):g} {package_unit})"
                )
        history.append(
            {
                "_receipt_id": int(row["receipt_id"]),
                "_item_order": int(row["item_order"]),
                "Reference": f"RL-{int(row['receipt_id']):06d}",
                "Date": row["transaction_date"],
                "Merchant": row["store_name"],
                "Recorded item": row["name"],
                "Product code": row["product_code"],
                "Category": row["category"],
                "Quantity": quantity,
                "Line price": line_price,
                "Comparable price": comparable_price,
                "Price basis": price_basis,
                "Package amount": package_quantity,
                "Package unit": package_unit,
                "Normalized price": normalized_price,
                "Normalized unit": normalized_unit,
                "Normalization basis": normalization_basis,
                "Currency": row["currency"],
            }
        )
    return history


def update_item_package_size(
    database_path: str | Path,
    receipt_id: int,
    item_order: int,
    package_quantity: float | None,
    package_unit: str | None,
) -> None:
    """Set or clear package content without changing original receipt values."""

    if package_quantity is None and package_unit is None:
        cleaned_quantity = None
        cleaned_unit = None
    else:
        if package_quantity is None or package_unit is None:
            raise ValueError("Enter both a package amount and package unit.")
        cleaned_quantity = float(package_quantity)
        cleaned_unit = " ".join(package_unit.casefold().split())
        if not math.isfinite(cleaned_quantity) or cleaned_quantity <= 0:
            raise ValueError("Package amount must be greater than zero.")
        if cleaned_unit not in PACKAGE_UNITS:
            raise ValueError("Choose a supported package unit.")

    with connect(database_path) as connection:
        item = connection.execute(
            "SELECT id FROM receipt_items WHERE receipt_id = ? AND item_order = ?",
            (receipt_id, item_order),
        ).fetchone()
        if item is None:
            raise ValueError("The selected saved item is no longer available.")
        connection.execute(
            """
            UPDATE receipt_items
            SET package_quantity = ?, package_unit = ?
            WHERE receipt_id = ? AND item_order = ?
            """,
            (cleaned_quantity, cleaned_unit, receipt_id, item_order),
        )


def update_item_categories(database_path, receipt_id: int, item_orders: list[int], category: str) -> None:
    """Change only categories on selected items of an existing receipt."""
    if not item_orders:
        raise ValueError("Select at least one item.")
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        canonical = connection.execute("SELECT name FROM expense_categories WHERE name = ? COLLATE NOCASE", (category,)).fetchone()
        if canonical is None:
            raise ValueError("Choose an existing category or create it first.")
        valid = {r[0] for r in connection.execute("SELECT item_order FROM receipt_items WHERE receipt_id = ?", (receipt_id,))}
        if not set(item_orders) <= valid:
            raise ValueError("The selected receipt items are no longer available. Reload the receipt.")
        connection.executemany("UPDATE receipt_items SET category = ? WHERE receipt_id = ? AND item_order = ?",
                               [(canonical[0], receipt_id, order) for order in set(item_orders)])
        for order in set(item_orders):
            item = connection.execute(
                "SELECT name, product_code FROM receipt_items "
                "WHERE receipt_id = ? AND item_order = ?",
                (receipt_id, order),
            ).fetchone()
            connection.execute(
                "INSERT OR REPLACE INTO item_category_preferences VALUES (?, ?)",
                (_item_key(item["name"]), canonical[0]),
            )
            if item["product_code"]:
                connection.execute(
                    "INSERT OR REPLACE INTO product_category_preferences VALUES (?, ?)",
                    (_product_key(item["product_code"]), canonical[0]),
                )


def delete_receipt_item(
    database_path: str | Path,
    receipt_id: int,
    item_order: int,
) -> dict[str, Any]:
    """Delete one saved item and compact the remaining receipt row numbers.

    Printed receipt amounts remain unchanged so the saved record continues to
    reflect the source document. The caller can compare the returned item total
    with the stored printed subtotal and show any remaining difference.
    """

    updated_at = datetime.now(UTC).isoformat(timespec="seconds")
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        receipt = connection.execute(
            "SELECT id, subtotal_cents FROM receipts WHERE id = ?",
            (receipt_id,),
        ).fetchone()
        if receipt is None:
            raise ValueError("The selected receipt no longer exists.")

        items = connection.execute(
            """
            SELECT id, item_order, name, line_total_cents
            FROM receipt_items
            WHERE receipt_id = ?
            ORDER BY item_order, id
            """,
            (receipt_id,),
        ).fetchall()
        selected = next(
            (row for row in items if int(row["item_order"]) == item_order),
            None,
        )
        if selected is None:
            raise ValueError(
                "The selected saved item is no longer available. Reload the receipt."
            )
        if len(items) == 1:
            raise ValueError(
                "A saved receipt must keep at least one item. Delete the entire "
                "receipt instead if this row is not a valid purchase."
            )

        connection.execute(
            "DELETE FROM receipt_items WHERE id = ?",
            (int(selected["id"]),),
        )
        remaining = connection.execute(
            """
            SELECT id, line_total_cents
            FROM receipt_items
            WHERE receipt_id = ?
            ORDER BY item_order, id
            """,
            (receipt_id,),
        ).fetchall()
        connection.executemany(
            "UPDATE receipt_items SET item_order = ? WHERE id = ?",
            [(order, int(row["id"])) for order, row in enumerate(remaining)],
        )
        connection.execute(
            "UPDATE receipts SET updated_at = ? WHERE id = ?",
            (updated_at, receipt_id),
        )

    item_total_cents = sum(int(row["line_total_cents"]) for row in remaining)
    return {
        "receipt_id": receipt_id,
        "deleted_item_order": item_order,
        "deleted_item_name": str(selected["name"]),
        "remaining_item_count": len(remaining),
        "item_total": _cents_to_money(item_total_cents),
        "printed_subtotal": _cents_to_money(receipt["subtotal_cents"]),
    }


def _item_key(name: str) -> str:
    return " ".join(name.casefold().split())


def _product_key(product_code: str) -> str:
    return re.sub(r"\s+", "", product_code).casefold()


CATEGORY_KEYWORDS = (
    ("Pharmacy", (
        "suppository", "suppositories", "medicine", "ibuprofen",
        "acetaminophen", "bandage",
    )),
    ("Health & Medical", ("vitamin supplement", "first aid", "thermometer")),
    ("Automotive", ("motor oil", "windshield", "wiper", "antifreeze", "car wash")),
    ("Pet food", ("cat food", "dog food", "pet food", "kibble")),
    ("Pet Care", ("cat litter", "flea", "pet shampoo")),
    ("Personal Care", (
        "conditioner", "shampoo", "deodorant", "toothpaste", "body wash",
    )),
    ("Clothing", ("shirt", "pants", "socks", "shoes", "dress")),
    ("Electronics", ("usb", "charger", "headphone", "earbud", "hdmi")),
    ("Household", (
        "detergent", "trash bag", "paper towel", "toilet paper", "cleaner",
        "napkin", "hanger",
    )),
    ("Groceries", (
        "chicken", "pork", "ham", "beef", "milk", "cheese", "cereal",
        "potato", "potatoes", "onion", "squash", "mushroom", "couscous",
        "sauce", "bread", "soda", "pepper", "avocado", "egg", "turnip",
        "mandarin", "orange", "leek", "spaghetti", "pasta", "sugar",
        "bacon", "cream", "ravioli", "frank", "turkey", "flour",
    )),
)
MERCHANT_DEFAULTS = (
    (("mcdonald", "restaurant", "cafe", "pizza"), "Dining & Restaurants"),
    (("home depot", "lowe's", "lowes", "rockler"), "Household"),
    (("walmart", "wal-mart", "h-e-b", "heb"), "Groceries"),
)


def _fallback_category(item_name: str, store_name: str) -> str:
    item_key = _item_key(item_name)
    for category, keywords in CATEGORY_KEYWORDS:
        if any(keyword in item_key for keyword in keywords):
            return category
    merchant_key = _item_key(store_name)
    for merchant_terms, category in MERCHANT_DEFAULTS:
        if any(term in merchant_key for term in merchant_terms):
            return category
    return "Uncategorized"


def suggest_category(
    database_path: str | Path,
    *,
    item_name: str,
    product_code: str | None = None,
    store_name: str = "",
) -> str:
    """Suggest from saved choices first, then local keyword and merchant rules."""
    with connect(database_path) as connection:
        if product_code:
            row = connection.execute(
                "SELECT category FROM product_category_preferences "
                "WHERE product_code = ?",
                (_product_key(product_code),),
            ).fetchone()
            if row:
                return str(row["category"])
        row = connection.execute(
            "SELECT category FROM item_category_preferences WHERE item_key = ?",
            (_item_key(item_name),),
        ).fetchone()
        if row:
            return str(row["category"])
    return _fallback_category(item_name, store_name)


def list_merchants(database_path) -> list[str]:
    """List saved merchant names, preserving distinct locations and store numbers."""
    with connect(database_path) as connection:
        rows = connection.execute("SELECT store_name FROM receipts ORDER BY id DESC").fetchall()
    names = {}
    for row in rows:
        name = " ".join(row[0].split())
        if name:
            names.setdefault(name.casefold(), name)
    return sorted(names.values(), key=str.casefold)


def update_receipt_merchant(database_path, receipt_id: int, merchant: str) -> None:
    """Backward-compatible merchant-only saved receipt correction."""

    current = get_receipt(database_path, receipt_id)
    if current is None:
        raise ValueError("The selected receipt no longer exists.")
    update_receipt_details(
        database_path,
        receipt_id,
        merchant=merchant,
        transaction_date=current["transaction_date"],
    )


def update_receipt_details(
    database_path: str | Path,
    receipt_id: int,
    *,
    merchant: str,
    transaction_date: str | None,
) -> None:
    """Correct editable saved-receipt metadata without changing amounts or items."""

    merchant = " ".join(merchant.split())
    if not merchant:
        raise ValueError("Enter a merchant name.")

    transaction_date = (transaction_date or "").strip() or None
    if transaction_date:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", transaction_date):
            raise ValueError("Enter the transaction date as YYYY-MM-DD, or leave it blank.")
        try:
            datetime.strptime(transaction_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(
                "Enter a valid transaction date as YYYY-MM-DD, or leave it blank."
            ) from exc

    with connect(database_path) as connection:
        result = connection.execute(
            "UPDATE receipts SET store_name = ?, transaction_date = ? WHERE id = ?",
            (merchant, transaction_date, receipt_id),
        )
        if result.rowcount != 1:
            raise ValueError("The selected receipt no longer exists.")


def delete_receipt(
    database_path: str | Path,
    receipt_id: int,
    *,
    source_archive_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Delete one receipt, its rows, and its archived originals as one operation."""

    archive_root = _source_archive_root(database_path, source_archive_dir)
    receipt_directory = archive_root / f"RL-{receipt_id:06d}"
    quarantine_directory: Path | None = None
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        receipt = connection.execute(
            "SELECT id, store_name, transaction_date FROM receipts WHERE id = ?",
            (receipt_id,),
        ).fetchone()
        if receipt is None:
            raise ValueError("The selected receipt no longer exists.")
        source_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM receipt_sources WHERE receipt_id = ?",
                (receipt_id,),
            ).fetchone()[0]
        )
        try:
            if receipt_directory.exists():
                quarantine_directory = archive_root / (
                    f".deleting-RL-{receipt_id:06d}-{uuid.uuid4().hex}"
                )
                os.replace(receipt_directory, quarantine_directory)
            connection.execute("DELETE FROM receipts WHERE id = ?", (receipt_id,))
            connection.commit()
        except Exception:
            connection.rollback()
            if quarantine_directory is not None and quarantine_directory.exists():
                os.replace(quarantine_directory, receipt_directory)
            raise

    cleanup_warning = None
    if quarantine_directory is not None and quarantine_directory.exists():
        try:
            shutil.rmtree(quarantine_directory)
        except OSError as exc:
            cleanup_warning = (
                "The receipt record was deleted, but its quarantined source folder "
                f"could not be removed: {exc}"
            )
    return {
        "id": receipt["id"],
        "store_name": receipt["store_name"],
        "transaction_date": receipt["transaction_date"],
        "source_count": source_count,
        "source_cleanup_warning": cleanup_warning,
    }


def remembered_categories(database_path) -> dict[str, str]:
    with connect(database_path) as connection:
        return dict(connection.execute("SELECT item_key, category FROM item_category_preferences").fetchall())


def apply_remembered_categories(database_path, receipt):
    """Fill uncategorized items from local history, keywords, then merchant defaults."""
    with connect(database_path) as connection:
        name_preferences = dict(
            connection.execute(
                "SELECT item_key, category FROM item_category_preferences"
            ).fetchall()
        )
        code_preferences = dict(
            connection.execute(
                "SELECT product_code, category FROM product_category_preferences"
            ).fetchall()
        )

    def categorized(item):
        if item.category != "Uncategorized":
            return item
        category = None
        if item.product_code:
            category = code_preferences.get(_product_key(item.product_code))
        category = category or name_preferences.get(_item_key(item.name))
        category = category or _fallback_category(item.name, receipt.store_name)
        return item.model_copy(update={"category": category})

    return receipt.model_copy(
        update={"items": [categorized(item) for item in receipt.items]}
    )

