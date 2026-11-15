"""Persistent, reload-safe local receipt processing jobs."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import local_pipeline
from .database import apply_remembered_categories
from .pdf_utils import pdf_page_count
from .walmart_invoice import parse_walmart_invoice_pdf
from .ocr_diagnostic import DiagnosticPipelineError, OcrDiagnosticTrace, build_ocr_diagnostic


ACTIVE_JOB_STATUSES = {"queued", "processing"}
JOB_SCHEMA = """
CREATE TABLE IF NOT EXISTS receipt_processing_jobs (
    id TEXT PRIMARY KEY,
    image_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    message TEXT NOT NULL,
    language TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    result_json TEXT,
    error TEXT,
    saved_receipt_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_receipt_processing_jobs_hash_status
ON receipt_processing_jobs(image_hash, status);

CREATE TABLE IF NOT EXISTS receipt_processing_pages (
    job_id TEXT NOT NULL REFERENCES receipt_processing_jobs(id) ON DELETE CASCADE,
    page_order INTEGER NOT NULL,
    image_bytes BLOB NOT NULL,
    PRIMARY KEY (job_id, page_order)
);

CREATE TABLE IF NOT EXISTS receipt_processing_sources (
    job_id TEXT NOT NULL REFERENCES receipt_processing_jobs(id) ON DELETE CASCADE,
    source_order INTEGER NOT NULL,
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    source_bytes BLOB NOT NULL,
    PRIMARY KEY (job_id, source_order)
);
"""

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="talliupper-ocr")
_IN_FLIGHT: set[str] = set()
_IN_FLIGHT_LOCK = threading.Lock()
_DIAGNOSTICS: OrderedDict[str, tuple[float, OcrDiagnosticTrace, Any | None, object | None]] = OrderedDict()
_DIAGNOSTIC_TTL_SECONDS = 30 * 60
_DIAGNOSTIC_MAX_ENTRIES = 16


def _remember_diagnostic(job_id: str, trace: OcrDiagnosticTrace, parsed: Any = None, error: object = None) -> None:
    """Keep diagnostics briefly in process memory; never write them to SQLite."""
    with _IN_FLIGHT_LOCK:
        now = time.monotonic()
        for key, (created, *_rest) in list(_DIAGNOSTICS.items()):
            if now - created > _DIAGNOSTIC_TTL_SECONDS:
                del _DIAGNOSTICS[key]
        _DIAGNOSTICS[job_id] = (now, trace, parsed, error)
        _DIAGNOSTICS.move_to_end(job_id)
        while len(_DIAGNOSTICS) > _DIAGNOSTIC_MAX_ENTRIES:
            _DIAGNOSTICS.popitem(last=False)


def get_receipt_job_diagnostic(
    database_path: str | Path, job_id: str
) -> dict[str, Any] | None:
    """Return an ephemeral diagnostic for a completed or failed local OCR job."""
    job = get_receipt_job(database_path, job_id)
    if job is None or str(job["status"]) not in {"review", "failed"}:
        return None
    with _IN_FLIGHT_LOCK:
        entry = _DIAGNOSTICS.get(job_id)
        if entry is None or time.monotonic() - entry[0] > _DIAGNOSTIC_TTL_SECONDS:
            _DIAGNOSTICS.pop(job_id, None)
            return None
    _created, trace, parsed, error = entry
    return build_ocr_diagnostic(trace, parsed=parsed, error=error)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _connect(database_path: str | Path) -> sqlite3.Connection:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def _terminal(job_id: str, stage: str, message: str) -> None:
    timestamp = time.strftime("%H:%M:%S")
    print(
        f"[TalliUpper {timestamp}] JOB {job_id[:8]} | {stage:<9} {message}",
        flush=True,
    )


def initialize_receipt_jobs(database_path: str | Path) -> None:
    with _connect(database_path) as connection:
        connection.executescript(JOB_SCHEMA)


def create_local_receipt_job(
    database_path: str | Path,
    *,
    image_hash: str,
    prepared_pages: list[bytes],
    language: str,
    source_files: list[dict[str, object]],
) -> tuple[str, bool]:
    """Create one durable job, or reconnect to the active job for this image."""

    if not prepared_pages:
        raise ValueError("Select at least one prepared receipt image.")
    initialize_receipt_jobs(database_path)
    with _connect(database_path) as connection:
        existing = connection.execute(
            """
            SELECT id
            FROM receipt_processing_jobs
            WHERE image_hash = ? AND status IN ('queued', 'processing')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (image_hash,),
        ).fetchone()
        if existing is not None:
            return str(existing["id"]), False

        job_id = uuid.uuid4().hex
        timestamp = _now()
        connection.execute(
            """
            INSERT INTO receipt_processing_jobs (
                id, image_hash, status, stage, message, language, page_count,
                created_at, updated_at
            ) VALUES (?, ?, 'queued', 'Queued', ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                image_hash,
                "Receipt submitted once and waiting for the local OCR worker.",
                language,
                len(prepared_pages),
                timestamp,
                timestamp,
            ),
        )
        connection.executemany(
            """
            INSERT INTO receipt_processing_pages (job_id, page_order, image_bytes)
            VALUES (?, ?, ?)
            """,
            [
                (job_id, page_order, sqlite3.Binary(page))
                for page_order, page in enumerate(prepared_pages, start=1)
            ],
        )
        connection.executemany(
            """
            INSERT INTO receipt_processing_sources (
                job_id, source_order, filename, media_type, source_bytes
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    job_id,
                    source_order,
                    str(source.get("filename") or "receipt source"),
                    str(source.get("media_type") or "application/octet-stream"),
                    sqlite3.Binary(bytes(source.get("data") or b"")),
                )
                for source_order, source in enumerate(source_files, start=1)
            ],
        )
    _terminal(job_id, "QUEUED", f"{len(prepared_pages)} image(s) stored on the server.")
    return job_id, True


def get_receipt_job(
    database_path: str | Path, job_id: str
) -> dict[str, Any] | None:
    if not job_id:
        return None
    initialize_receipt_jobs(database_path)
    with _connect(database_path) as connection:
        row = connection.execute(
            "SELECT * FROM receipt_processing_jobs WHERE id = ?", (job_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def get_receipt_job_result(
    database_path: str | Path, job_id: str
) -> dict[str, Any] | None:
    job = get_receipt_job(database_path, job_id)
    if not job or not job.get("result_json"):
        return None
    return json.loads(str(job["result_json"]))


def get_receipt_job_sources(
    database_path: str | Path, job_id: str
) -> list[dict[str, object]]:
    with _connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT filename, media_type, source_bytes
            FROM receipt_processing_sources
            WHERE job_id = ?
            ORDER BY source_order
            """,
            (job_id,),
        ).fetchall()
    return [
        {
            "filename": str(row["filename"]),
            "media_type": str(row["media_type"]),
            "data": bytes(row["source_bytes"]),
        }
        for row in rows
    ]


def _load_pages(database_path: str | Path, job_id: str) -> list[bytes]:
    with _connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT image_bytes
            FROM receipt_processing_pages
            WHERE job_id = ?
            ORDER BY page_order
            """,
            (job_id,),
        ).fetchall()
    return [bytes(row["image_bytes"]) for row in rows]


def _set_job_stage(
    database_path: str | Path,
    job_id: str,
    *,
    status: str,
    stage: str,
    message: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    with _connect(database_path) as connection:
        connection.execute(
            """
            UPDATE receipt_processing_jobs
            SET status = ?, stage = ?, message = ?, result_json = COALESCE(?, result_json),
                error = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                stage,
                message,
                json.dumps(result) if result is not None else None,
                error,
                _now(),
                job_id,
            ),
        )


def finish_receipt_job(
    database_path: str | Path, job_id: str, saved_receipt_id: int
) -> None:
    with _connect(database_path) as connection:
        connection.execute(
            """
            UPDATE receipt_processing_jobs
            SET status = 'finished', stage = 'Finished',
                message = 'Receipt verified and saved.',
                saved_receipt_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (saved_receipt_id, _now(), job_id),
        )
        connection.execute(
            "DELETE FROM receipt_processing_pages WHERE job_id = ?", (job_id,)
        )
    with _IN_FLIGHT_LOCK:
        _DIAGNOSTICS.pop(job_id, None)
    _terminal(job_id, "FINISHED", f"Saved as receipt {saved_receipt_id}.")


def _run_local_receipt_job(database_path: str | Path, job_id: str) -> None:
    started = time.perf_counter()
    try:
        job = get_receipt_job(database_path, job_id)
        if job is None or str(job["status"]) not in ACTIVE_JOB_STATUSES:
            return
        pages = _load_pages(database_path, job_id)
        if not pages:
            raise RuntimeError("The receipt job has no prepared image pages.")
        _set_job_stage(
            database_path,
            job_id,
            status="processing",
            stage="Reading receipt",
            message=(
                "Local OCR is loading models and reading receipt lines. "
                "It is safe to refresh Safari."
            ),
        )
        _terminal(job_id, "START", f"Reading {len(pages)} stored image(s).")
        sources = get_receipt_job_sources(database_path, job_id)
        result = None
        if len(sources) == 1 and str(sources[0]["filename"]).lower().endswith(".pdf"):
            pdf_bytes = bytes(sources[0]["data"])
            if pdf_page_count(pdf_bytes) == len(pages):
                result = parse_walmart_invoice_pdf(pdf_bytes)
        if result is None:
            result = (
                local_pipeline.process_receipt_locally(
                    pages[0], language=str(job["language"])
                )
                if len(pages) == 1
                else local_pipeline.process_receipt_pages_locally(
                    pages, language=str(job["language"])
                )
            )
        receipt = apply_remembered_categories(
            database_path, result.parsed.receipt
        )
        diagnostic_trace = getattr(result, "diagnostic", None)
        if diagnostic_trace is not None:
            _remember_diagnostic(job_id, diagnostic_trace, result.parsed)
        payload = {
            "receipt": receipt.model_dump(mode="json"),
            "image_hash": str(job["image_hash"]),
            "extraction_method": result.ocr.engine,
            "ocr_text": result.ocr.text,
            "line_decisions": [
                asdict(decision) for decision in result.parsed.decisions
            ],
            "missing_item_candidates": [
                asdict(candidate)
                for candidate in result.parsed.missing_item_candidates
            ],
        }
        _set_job_stage(
            database_path,
            job_id,
            status="review",
            stage="Waiting for review",
            message="OCR finished. Reconnecting Safari to the review screen.",
            result=payload,
        )
        _terminal(
            job_id,
            "REVIEW",
            f"{len(result.ocr.lines)} OCR lines and "
            f"{len(receipt.items)} item rows ready in "
            f"{time.perf_counter() - started:.1f}s.",
        )
    except Exception as exc:
        trace = exc.trace if isinstance(exc, DiagnosticPipelineError) else OcrDiagnosticTrace(stage="failed_before_ocr")
        original_error = exc.original_error if isinstance(exc, DiagnosticPipelineError) else exc
        _remember_diagnostic(job_id, trace, error=original_error)
        _set_job_stage(
            database_path,
            job_id,
            status="failed",
            stage="Could not read receipt",
            message="The receipt job stopped. Review the terminal error and try again.",
            error=f"{type(original_error).__name__}: {original_error}",
        )
        _terminal(job_id, "ERROR", f"{type(exc).__name__}: {exc}")
    finally:
        with _IN_FLIGHT_LOCK:
            _IN_FLIGHT.discard(job_id)


def submit_local_receipt_job(database_path: str | Path, job_id: str) -> bool:
    """Submit at most one worker future for a durable receipt job."""

    job = get_receipt_job(database_path, job_id)
    if job is None or str(job["status"]) not in ACTIVE_JOB_STATUSES:
        return False
    with _IN_FLIGHT_LOCK:
        if job_id in _IN_FLIGHT:
            return False
        _IN_FLIGHT.add(job_id)
    _EXECUTOR.submit(_run_local_receipt_job, database_path, job_id)
    return True
