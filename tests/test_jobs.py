from receipt_reader.jobs import (
    _remember_diagnostic,
    _set_job_stage,
    create_local_receipt_job,
    finish_receipt_job,
    get_receipt_job,
    get_receipt_job_diagnostic,
    get_receipt_job_sources,
    initialize_receipt_jobs,
)
from receipt_reader.local_ocr import OcrLine, OcrResult
from receipt_reader.ocr_diagnostic import OcrDiagnosticTrace


def test_receipt_job_survives_new_database_connections(tmp_path) -> None:
    database = tmp_path / "receipts.db"
    initialize_receipt_jobs(database)

    job_id, created = create_local_receipt_job(
        database,
        image_hash="abc123",
        prepared_pages=[b"prepared-image"],
        language="en",
        source_files=[
            {
                "filename": "receipt.png",
                "media_type": "image/png",
                "data": b"original-image",
            }
        ],
    )

    assert created is True
    job = get_receipt_job(database, job_id)
    assert job is not None
    assert job["status"] == "queued"
    assert job["page_count"] == 1
    assert get_receipt_job_sources(database, job_id) == [
        {
            "filename": "receipt.png",
            "media_type": "image/png",
            "data": b"original-image",
        }
    ]


def test_active_receipt_image_reconnects_instead_of_resubmitting(tmp_path) -> None:
    database = tmp_path / "receipts.db"
    first_id, first_created = create_local_receipt_job(
        database,
        image_hash="same-image",
        prepared_pages=[b"first"],
        language="en",
        source_files=[],
    )
    second_id, second_created = create_local_receipt_job(
        database,
        image_hash="same-image",
        prepared_pages=[b"second"],
        language="en",
        source_files=[],
    )

    assert first_created is True
    assert second_created is False
    assert second_id == first_id


def test_finished_job_records_saved_receipt_and_releases_page_bytes(tmp_path) -> None:
    database = tmp_path / "receipts.db"
    job_id, _ = create_local_receipt_job(
        database,
        image_hash="finish-me",
        prepared_pages=[b"prepared"],
        language="en",
        source_files=[],
    )

    finish_receipt_job(database, job_id, 42)

    job = get_receipt_job(database, job_id)
    assert job is not None
    assert job["status"] == "finished"
    assert job["saved_receipt_id"] == 42


def test_diagnostic_is_available_only_after_local_ocr_and_removed_when_saved(
    tmp_path,
) -> None:
    database = tmp_path / "receipts.db"
    job_id, _ = create_local_receipt_job(
        database,
        image_hash="diagnostic",
        prepared_pages=[b"receipt-image-bytes"],
        language="en",
        source_files=[],
    )
    trace = OcrDiagnosticTrace(
        standard=OcrResult(
            (
                OcrLine(
                    "Call 555-222-1234 or email person@example.com card 4111 1111 1111 1111",
                    0.9,
                    (1, 2, 3, 4),
                ),
            ),
            "test",
        ),
        stage="complete",
    )
    _remember_diagnostic(job_id, trace)

    # Queued work cannot expose diagnostics, even if a trace was retained.
    assert get_receipt_job_diagnostic(database, job_id) is None

    _set_job_stage(
        database,
        job_id,
        status="review",
        stage="Waiting for review",
        message="OCR finished.",
    )
    diagnostic = get_receipt_job_diagnostic(database, job_id)
    assert diagnostic is not None
    exported = str(diagnostic)
    persisted_job = str(get_receipt_job(database, job_id))
    assert "receipt-image-bytes" not in exported
    assert "555-222-1234" not in persisted_job
    assert "555-222-1234" not in exported
    assert "person@example.com" not in exported
    assert "4111 1111 1111 1111" not in exported

    finish_receipt_job(database, job_id, 42)
    assert get_receipt_job_diagnostic(database, job_id) is None


def test_failed_local_ocr_job_can_offer_its_in_memory_diagnostic(tmp_path) -> None:
    database = tmp_path / "receipts.db"
    job_id, _ = create_local_receipt_job(
        database,
        image_hash="failed-diagnostic",
        prepared_pages=[b"prepared"],
        language="en",
        source_files=[],
    )
    _remember_diagnostic(job_id, OcrDiagnosticTrace(stage="failed:standard_ocr"))
    _set_job_stage(
        database,
        job_id,
        status="failed",
        stage="Could not read receipt",
        message="OCR stopped.",
        error="LocalOcrError: test failure",
    )

    diagnostic = get_receipt_job_diagnostic(database, job_id)
    assert diagnostic is not None
    assert diagnostic["metadata"]["processing_stage"] == "failed:standard_ocr"
