import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

import receipt_reader.database as database_module
from receipt_reader.database import (
    build_backup_zip,
    delete_receipt,
    get_receipt,
    initialize_database,
    list_receipt_sources,
    list_receipts,
    read_receipt_source,
    save_receipt,
)
from receipt_reader.models import ReceiptExtraction, ReceiptItem


def receipt(store: str = "Archive Test") -> ReceiptExtraction:
    return ReceiptExtraction(
        store_name=store,
        transaction_date="2026-09-13",
        currency="USD",
        items=[
            ReceiptItem(
                name="Dog food",
                category="Pet Care",
                quantity=1,
                unit_price=12.34,
                discount=None,
                line_total=12.34,
            )
        ],
        subtotal=12.34,
        tax=1.02,
        total=13.36,
        confidence=0.95,
        notes=[],
    )


def test_original_source_round_trip_and_integrity(tmp_path: Path) -> None:
    database = tmp_path / "receipts.db"
    archive_root = tmp_path / "originals"
    initialize_database(database)

    source_data = b"test-image-bytes"
    receipt_id = save_receipt(
        database,
        receipt(),
        source_files=[
            {
                "filename": "../IMG 0001.PNG",
                "media_type": "image/png",
                "data": source_data,
            }
        ],
        source_archive_dir=archive_root,
    )

    assert list_receipts(database)[0]["source_files"] == 1
    assert get_receipt(database, receipt_id)["sources"][0]["original_filename"] == "IMG 0001.PNG"
    sources = list_receipt_sources(
        database, receipt_id, source_archive_dir=archive_root
    )
    assert sources[0]["exists"] is True
    assert sources[0]["checksum_ok"] is True
    metadata, restored = read_receipt_source(
        database, sources[0]["id"], source_archive_dir=archive_root
    )
    assert metadata["media_type"] == "image/png"
    assert restored == source_data


def test_backup_contains_database_manifest_and_verified_sources(tmp_path: Path) -> None:
    database = tmp_path / "receipts.db"
    archive_root = tmp_path / "originals"
    initialize_database(database)
    receipt_id = save_receipt(
        database,
        receipt(),
        source_files=[
            {
                "filename": "receipt.pdf",
                "media_type": "application/pdf",
                "data": b"%PDF-test",
            }
        ],
        source_archive_dir=archive_root,
    )

    backup = build_backup_zip(database, source_archive_dir=archive_root)
    with zipfile.ZipFile(io.BytesIO(backup)) as archive:
        names = set(archive.namelist())
        assert "receiptlens-backup/receipts.db" in names
        assert "receiptlens-backup/manifest.json" in names
        assert f"receiptlens-backup/receipt_sources/RL-{receipt_id:06d}/source-001.pdf" in names
        manifest = json.loads(
            archive.read("receiptlens-backup/manifest.json").decode("utf-8")
        )
        assert manifest["format_version"] == 1
        assert manifest["receipt_count"] == 1
        assert manifest["source_count"] == 1
        snapshot_path = tmp_path / "snapshot.db"
        snapshot_path.write_bytes(archive.read("receiptlens-backup/receipts.db"))
    with sqlite3.connect(snapshot_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM receipt_sources").fetchone()[0] == 1


def test_backup_closes_sqlite_files_before_the_temporary_directory_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "receipts.db"
    initialize_database(database)
    save_receipt(database, receipt())

    real_connect = sqlite3.connect
    opened_connections: list[sqlite3.Connection] = []

    def tracked_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        opened_connections.append(connection)
        return connection

    monkeypatch.setattr(database_module.sqlite3, "connect", tracked_connect)

    build_backup_zip(database)

    assert opened_connections
    for connection in opened_connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_changed_source_blocks_download_and_backup(tmp_path: Path) -> None:
    database = tmp_path / "receipts.db"
    archive_root = tmp_path / "originals"
    initialize_database(database)
    receipt_id = save_receipt(
        database,
        receipt(),
        source_files=[{"filename": "receipt.jpg", "data": b"original"}],
        source_archive_dir=archive_root,
    )
    source = list_receipt_sources(
        database, receipt_id, source_archive_dir=archive_root
    )[0]
    (archive_root / f"RL-{receipt_id:06d}" / source["stored_filename"]).write_bytes(
        b"changed"
    )

    checked = list_receipt_sources(
        database, receipt_id, source_archive_dir=archive_root
    )[0]
    assert checked["checksum_ok"] is False
    with pytest.raises(ValueError, match="integrity check"):
        read_receipt_source(
            database, source["id"], source_archive_dir=archive_root
        )
    with pytest.raises(ValueError, match="failed verification"):
        build_backup_zip(database, source_archive_dir=archive_root)


def test_delete_removes_database_rows_and_archived_sources(tmp_path: Path) -> None:
    database = tmp_path / "receipts.db"
    archive_root = tmp_path / "originals"
    initialize_database(database)
    receipt_id = save_receipt(
        database,
        receipt(),
        source_files=[{"filename": "receipt.jpg", "data": b"original"}],
        source_archive_dir=archive_root,
    )
    other_id = save_receipt(database, receipt("Other Store"))

    deleted = delete_receipt(
        database, receipt_id, source_archive_dir=archive_root
    )

    assert deleted["source_count"] == 1
    assert deleted["source_cleanup_warning"] is None
    assert not (archive_root / f"RL-{receipt_id:06d}").exists()
    assert get_receipt(database, receipt_id) is None
    assert get_receipt(database, other_id) is not None
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM receipt_items WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM receipt_sources WHERE receipt_id = ?", (receipt_id,)
        ).fetchone()[0] == 0


def test_empty_original_is_rejected_without_saving_receipt(tmp_path: Path) -> None:
    database = tmp_path / "receipts.db"
    initialize_database(database)

    with pytest.raises(ValueError, match="empty"):
        save_receipt(
            database,
            receipt(),
            source_files=[{"filename": "receipt.jpg", "data": b""}],
        )
    assert list_receipts(database) == []
