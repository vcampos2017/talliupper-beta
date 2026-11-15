"""Streamlit user interface for ReceiptLens."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import re
import os
import sqlite3
import time
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from pydantic import ValidationError

from receipt_reader.database import (
    PACKAGE_UNITS,
    build_backup_zip,
    create_category,
    export_csv,
    export_personal_finance_csv,
    personal_finance_export_warnings,
    export_receipt_summary_csv,
    get_receipt,
    initialize_database,
    list_categories,
    list_receipt_sources,
    list_receipts,
    save_receipt,
    read_receipt_source,
    find_duplicate_receipts,
    DuplicateReceiptError,
    expense_totals,
    item_price_history,
    list_item_alias_groups,
    list_item_names,
    list_recorded_item_names,
    remove_item_alias_group,
    save_item_alias_group,
    update_item_categories,
    update_item_package_size,
    apply_remembered_categories,
    suggest_category,
    list_merchants,
    delete_receipt_item,
    delete_receipt,
    update_receipt,
    update_receipt_details,
)
from receipt_reader.extractor import DEFAULT_MODEL, ReceiptExtractionError, extract_receipt
from receipt_reader.image_utils import InvalidImageError, prepare_receipt_image
from receipt_reader.jobs import (
    ACTIVE_JOB_STATUSES,
    create_local_receipt_job,
    finish_receipt_job,
    get_receipt_job,
    get_receipt_job_result,
    get_receipt_job_sources,
    get_receipt_job_diagnostic,
    initialize_receipt_jobs,
    submit_local_receipt_job,
)
from receipt_reader.ocr_recovery import (
    clean_candidate,
    clean_recovered_line,
    printed_position,
    split_product_code,
)
from receipt_reader.pdf_utils import pdf_page_count, render_pdf_page
from receipt_reader.local_ocr import LocalOcrError
from receipt_reader.local_llm import (
    LOCAL_MODELS,
    EvidenceItem,
    LocalLlmError,
    is_still_source_backed,
    review_row_from_suggestion,
    suggest_items_locally,
    suggestion_key,
)
from receipt_reader.local_pipeline import (
    process_receipt_locally,
    process_receipt_pages_locally,
)
from receipt_reader.models import TAX_MARKERS, ReceiptExtraction, ReceiptItem, blank_receipt
from receipt_reader.parser import (
    printed_count_matches,
    printed_item_count,
    purchased_unit_count,
)
from receipt_reader.review import (
    assign_checked_review_merges,
    compact_review_labels,
    is_gratuity_item_name,
    amount_check_status,
    merge_selected_review_rows,
    possible_split_labels,
    potential_duplicate_labels,
    selected_candidate_error,
)
from receipt_reader.review_report import (
    build_printable_item_table_html,
    build_review_report_html,
    review_report_filename,
)
from receipt_reader.saved_pdf import build_saved_receipt_pdf, saved_receipt_pdf_filename
from receipt_reader.source_preview import receipt_source_previews
from receipt_reader.walmart_invoice import printed_order_savings


BASE_DIR = Path(__file__).resolve().parent

# Cache only bounded rendered pages, never live PDFium document handles.
cached_pdf_page_count = st.cache_data(max_entries=4, show_spinner=False)(pdf_page_count)
cached_render_pdf_page = st.cache_data(max_entries=4, show_spinner=False)(render_pdf_page)
cached_receipt_source_previews = st.cache_data(max_entries=12, show_spinner=False)(
    receipt_source_previews
)
load_dotenv(BASE_DIR / ".env")
DATA_DIR = Path(os.getenv("RECEIPT_DATA_DIR", BASE_DIR / "data"))
DATABASE_PATH = DATA_DIR / "receipts.db"
SOURCE_ARCHIVE_DIR = DATA_DIR / "receipt_sources"
SAMPLE_RECEIPT_PATH = BASE_DIR / "sample_data" / "sample_receipt.png"
TALLIUPPER_MASCOT_PATH = BASE_DIR / "assets" / "talliupper_mascot.webp"
TALLI_READING_RECEIPT_PATH = BASE_DIR / "assets" / "talli_reading_receipt.webp"
TALLI_READING_PAGES_PATH = BASE_DIR / "assets" / "talli_reading_pages.webp"
TALLI_COMPARING_RECEIPTS_PATH = BASE_DIR / "assets" / "talli_comparing_receipts.webp"
TALLI_RECEIPT_READY_PATH = BASE_DIR / "assets" / "talli_receipt_ready.webp"

PROCESSING_LOCAL = "Local OCR (recommended)"
PROCESSING_CLOUD = "OpenAI vision (optional)"
LANGUAGES = {"English": "en", "Spanish": "es", "Portuguese": "pt"}
MISSION_STATEMENT = (
    "Make understanding everyday spending approachable, lively, and trustworthy—"
    "while keeping people in control of their receipts and data."
)


def terminal_status(stage: str, message: str) -> None:
    """Print one concise, timestamped development event to PowerShell."""

    timestamp = time.strftime("%H:%M:%S")
    print(f"[TalliUpper {timestamp}] {stage:<9} {message}", flush=True)


def money_text(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


def safe_receipt_order_label(value: object, fallback_index: int) -> str:
    """Format an OCR row number without letting incomplete data crash review."""

    try:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError
        return str(int(number))
    except (TypeError, ValueError):
        return str(fallback_index + 1)


def safe_line_price_label(value: object) -> str:
    """Format an OCR price for display, or explain that it needs review."""

    try:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError
        return f"${number:,.2f}"
    except (TypeError, ValueError):
        return "price not identified"


def parse_optional_money(value: str, label: str) -> float | None:
    cleaned = value.strip().replace("$", "").replace(",", "")
    if not cleaned:
        return None
    try:
        number = float(cleaned)
        if not math.isfinite(number):
            raise ValueError("Enter a finite amount")
        return round(number, 2)
    except ValueError as exc:
        raise ValueError(f"{label} must be a number, such as 12.34.") from exc


def parse_optional_date(value: str) -> str | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        return date.fromisoformat(cleaned).isoformat()
    except ValueError as exc:
        raise ValueError(
            "Transaction date must use YYYY-MM-DD, such as 2026-09-03."
        ) from exc


def show_talli_working_overlay(image_path: Path, message: str):
    """Show a centered, blocking Talli status card while receipt processing runs."""

    image_data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    placeholder = st.empty()
    placeholder.markdown(
        f"""
        <style>
          .talli-working-overlay {{
            position: fixed;
            inset: 0;
            z-index: 999999;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 1rem;
            background: rgba(15, 23, 42, 0.28);
            -webkit-backdrop-filter: blur(2px);
            backdrop-filter: blur(2px);
            cursor: wait;
          }}
          .talli-working-card {{
            width: min(300px, 78vw);
            padding: 1rem;
            border: 1px solid rgba(59, 36, 24, 0.12);
            border-radius: 1.25rem;
            background: #fffdf7;
            box-shadow: 0 18px 50px rgba(15, 23, 42, 0.28);
            text-align: center;
            color: #3b2418;
          }}
          .talli-working-card img {{
            display: block;
            width: 100%;
            height: auto;
            border-radius: 1rem;
          }}
          .talli-working-card p {{
            margin: 0.75rem 0 0;
            font-size: 1.05rem;
            font-weight: 700;
          }}
          @media (max-width: 480px) {{
            .talli-working-card {{
              width: min(260px, 82vw);
              padding: 0.75rem;
            }}
          }}
        </style>
        <div class="talli-working-overlay">
          <div class="talli-working-card" role="status" aria-live="polite">
            <img src="data:image/webp;base64,{image_data}" alt="">
            <p>{message}</p>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    return placeholder


MERCHANT_CANDIDATE_WORDS = re.compile(
    r"\b(?:RESTAURANT|MARKET|MART|STORE|FOODS?|PHARMACY|CAFE|COFFEE|"
    r"GRILL|DINER|BAKERY|DEPOT)\b",
    re.IGNORECASE,
)
MERCHANT_CANDIDATE_NOISE = re.compile(
    r"\b(?:HOW\s+ARE\s+WE\s+DOING|LET\s+US\s+KNOW|SURVEY|"
    r"THANK\s+YOU|REWARDS?|CUSTOMER|CASHIER|SUBTOTAL|TOTAL|TAX|"
    r"PHONE|TEL|EMAIL|APPLY\s+AT)\b",
    re.IGNORECASE,
)


def merchant_line_suggestions(
    line_decisions: list[dict[str, object]],
    saved_merchants: list[str],
) -> list[str]:
    """Rank plausible merchant OCR lines while excluding prompts and totals."""

    def normalized(value: str) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))

    saved_tokens = [
        {token.rstrip("s") for token in normalized(merchant).split() if len(token) >= 4}
        for merchant in saved_merchants
    ]
    ranked: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for index, decision in enumerate(line_decisions[:25]):
        text = str(decision.get("text") or "").strip()
        key = normalized(text)
        if (
            not text
            or key in seen
            or len(text) > 80
            or MERCHANT_CANDIDATE_NOISE.search(text)
            or re.search(r"(?:https?://|www\.|@|\b\d{3}[- )]\d{3}[- ]\d{4}\b)", text, re.IGNORECASE)
            or re.search(r"\$?\d+\.\d{2}\s*$", text)
        ):
            continue
        score = 0
        if decision.get("role") == "store":
            score += 100
        if MERCHANT_CANDIDATE_WORDS.search(text):
            score += 35
        candidate_tokens = {token.rstrip("s") for token in key.split() if len(token) >= 4}
        if any(candidate_tokens & merchant_tokens for merchant_tokens in saved_tokens):
            score += 50
        if re.search(r"#\s*\d{2,6}\b|\bRESTAURANT\s+\d{2,6}\b", text, re.IGNORECASE):
            score += 15
        if score:
            seen.add(key)
            ranked.append((-score, index, text))
    return [text for _, _, text in sorted(ranked)[:5]]


OCR_RECOVERY_NOISE = re.compile(
    r"\b(?:SUB\s*-?\s*TOTAL|TOTAL|TAX|RECEIPT|TRANSACTION|CASHIER|REGISTER|"
    r"TERMINAL|AUTH|APPROVAL|CREDIT|DEBIT|VISA|MASTERCARD|MASTRCRD|"
    r"MASTERCRD|EFT|EFS|AMEX|DISCOVER|CASH|CHANGE|TENDERED|CARD|PAYMENT|"
    r"CONTACTLESS|CHIP|APPR(?:OVAL)?\s+NO|REF\s+NO|ISSUER|AID|TVR|IAD|TSI|"
    r"THANK|CUSTOMER|PHONE|ITEMS\s+(?:PURCHASED|SOLD)|SAVINGS?|SAVED|"
    r"ENTRY\s+METHOD|PURCHASE\s*-?\s*APPROVED|HELPED\s+BY|EMPLOYEE\s+NO|"
    r"CURRENT\s+BUSINESS\s+HOURS|RETURN\s+POLICY|DID\s+YOU\s+KNOW|"
    r"RESERVE\s+ITEMS|ELIGIBLE\s+FOR\s+RETURN|NOW\s+HIRING|APPLY\s+TODAY|"
    r"STREET|ROAD|AVENUE|BOULEVARD|DRIVE|HIGHWAY|LANE)\b",
    re.IGNORECASE,
)
OCR_RECOVERY_AMOUNT = re.compile(
    r"(?P<amount>\d{1,7}(?:,\d{3})*\.\d{2})\s*[A-Z]{0,2}\s*$",
    re.IGNORECASE,
)


def ocr_line_recovery_candidates(
    line_decisions: list[dict[str, object]],
    *,
    excluded_lines: set[int] | None = None,
    store_name: str | None = None,
) -> list[dict[str, object]]:
    """Offer unused product-like OCR lines for explicit human recovery.

    The parser remains conservative. This helper gives the reviewer a controlled
    escape hatch when a real product line was left unused, without presenting
    payment-terminal, summary, or already-parsed item rows as purchases.
    """

    excluded_lines = excluded_lines or set()
    candidates: list[dict[str, object]] = []
    for fallback_index, decision in enumerate(line_decisions):
        role = str(decision.get("role") or "ignored")
        if role not in {"ignored", "missing_item_candidate"}:
            continue
        # Parser decisions are already numbered from 1, matching the
        # ``source_line`` of parser missing-item candidates. Adding 1 here
        # made every line appear twice in the recovery list.
        source_line = int(decision.get("index", fallback_index + 1))
        if source_line in excluded_lines:
            continue
        text = str(decision.get("text") or "").strip(" :-")
        if (
            not text
            or len(text) > 120
            or OCR_RECOVERY_NOISE.search(text)
            or re.search(r"\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\b", text)
            or re.search(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b", text)
            or re.search(r"\b[A-Z][A-Z .'-]+,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?\b", text)
            or not any(character.isalpha() for character in text)
        ):
            continue
        amount_match = OCR_RECOVERY_AMOUNT.search(text)
        line_total = (
            float(amount_match.group("amount").replace(",", ""))
            if amount_match
            else None
        )
        name = (
            text[: amount_match.start()].strip(" :-")
            if amount_match
            else text
        )
        marker_match = re.search(r"\b(FW|F|T)\s*$", name, re.IGNORECASE)
        tax_marker = marker_match.group(1).upper() if marker_match else None
        if marker_match:
            name = name[: marker_match.start()].strip(" :-")
        name = re.sub(r"^\d{1,3}[.)]?\s+(?=[A-Za-z])", "", name).strip()
        # Give a recovered row the cleanup a parsed row gets: UPC in its own
        # column, OCR noise after the UPC dropped, Walmart X/N kept (RL-38).
        cleaned = clean_recovered_line(name, store_name)
        name = cleaned["name"]
        product_code = cleaned["product_code"]
        if cleaned["tax_marker"]:
            tax_marker = cleaned["tax_marker"]
        elif product_code and store_name == "Walmart":
            # Walmart's F after the UPC is food eligibility, not tax status.
            tax_marker = None
        words = re.findall(r"[A-Za-z][A-Za-z'-]*", name)
        # A UPC marks a product row, so a one-word name ("CHEESECAKE") counts.
        if len(words) < (1 if product_code else 2) or not name:
            continue
        candidates.append(
            {
                "name": name,
                "product_code": product_code,
                "confidence": float(decision.get("confidence") or 0.0),
                "source_line": source_line,
                "supporting_text": text,
                "quantity": 1.0,
                "unit_price": line_total,
                "line_total": line_total,
                "tax_marker": tax_marker,
            }
        )
    return candidates


def generate_backup_download() -> bytes:
    """Build a fresh verified archive only when requested, off the page thread."""

    started = time.monotonic()
    terminal_status("BACKUP", "Preparing full backup for download.")
    try:
        data = build_backup_zip(
            DATABASE_PATH,
            source_archive_dir=SOURCE_ARCHIVE_DIR,
        )
    except (OSError, ValueError) as exc:
        terminal_status("BACKUP", f"Failed: {exc}")
        raise
    terminal_status(
        "BACKUP",
        f"Ready: {len(data)} bytes in {time.monotonic() - started:.1f}s.",
    )
    return data


def plain_markdown(text: str) -> str:
    """Escape ``$`` so Streamlit shows amounts as text, not LaTeX math (RL-66)."""

    return text.replace("$", "\\$")


def render_local_ai_preview(active: dict, version: object) -> list[EvidenceItem]:
    """An explicitly requested comparison.

    Returns the suggestions the user chose to add to the review table on this
    run (usually none). Nothing is added or saved without that choice, and the
    user still reviews and saves the receipt as usual.
    """

    ocr_text = str(st.session_state.get("ocr_text") or "")
    if not ocr_text.strip():
        return []
    with st.expander("Local AI second opinion (experimental)"):
        st.caption(
            "Ask a model installed on this computer to interpret the OCR text. "
            "Its rows are only suggestions: your review table changes only when "
            "you select Add to review table. It cannot recover text that OCR "
            "never captured."
        )
        model = st.selectbox(
            "Local AI model", LOCAL_MODELS,
            help="Install the selected model in Ollama on the computer running TalliUpper.",
            key="local_llm_model",
        )
        preview_id = (
            str(version), hashlib.sha256(ocr_text.encode("utf-8")).hexdigest(), model,
        )
        if st.button("Get local AI second opinion", key=f"local_llm_run_{version}"):
            st.session_state.pop("local_llm_preview", None)
            try:
                terminal_status("LOCAL AI", "Starting optional OCR interpretation.")
                terminal_status(
                    "LOCAL AI",
                    "Loading the model and reading the receipt; on a slower "
                    "computer this can take several minutes.",
                )
                with st.spinner(
                    "Checking OCR text with local AI. On a slower computer this can "
                    "take several minutes; watch the terminal window for progress…"
                ):
                    preview = suggest_items_locally(
                        ocr_text, model=model,
                        on_progress=lambda message: terminal_status("LOCAL AI", message),
                    )
                st.session_state.local_llm_preview = (preview_id, preview)
                terminal_status(
                    "LOCAL AI",
                    f"Finished in {preview.elapsed_seconds:.1f}s; "
                    f"{len(preview.items)} source-backed suggestions; "
                    f"{len(preview.rejected)} rejected.",
                )
            except LocalLlmError as exc:
                terminal_status("LOCAL AI", "Unavailable; normal receipt review remains active.")
                st.warning(str(exc))
        saved_preview = st.session_state.get("local_llm_preview")
        if not saved_preview or saved_preview[0] != preview_id:
            return []
        preview = saved_preview[1]
        parser_items = active.get("items", [])
        parser_sum = sum(
            (Decimal(str(item.get("line_total") or 0)) for item in parser_items),
            Decimal("0.00"),
        )
        st.write(plain_markdown(
            f"Original parser: {len(parser_items)} rows, ${parser_sum:.2f}. "
            f"Local AI: {len(preview.items)} source-backed rows, ${preview.item_sum:.2f}."
        ))
        if len(parser_items) != len(preview.items) or parser_sum != preview.item_sum:
            st.warning("The two readings disagree. Compare each proposed row with the photo.")
        else:
            st.caption("Matching counts and sums do not establish that the individual rows are correct.")
        to_add: list[EvidenceItem] = []
        if preview.items:
            ocr_lines = ocr_text.splitlines()
            added_state = st.session_state.get("local_llm_added")
            added = (
                set(added_state[1])
                if added_state and added_state[0] == preview_id else set()
            )
            st.dataframe(
                pd.DataFrame([
                    {
                        "Proposed item": split_product_code(item.name)[0],
                        "UPC / SKU": split_product_code(item.name)[1] or "",
                        "Printed line price": str(item.line_total),
                        "OCR evidence": item.source_text,
                        "Status": (
                            "Added to review table"
                            if suggestion_key(item) in added else "Not added"
                        ),
                    }
                    for item in preview.items
                ]),
                hide_index=True, width="stretch",
            )
            # Only rows that passed the OCR source checks are offered, and each
            # is checked again against the current OCR text before it is used.
            addable = [
                item for item in preview.items
                if suggestion_key(item) not in added
                and is_still_source_backed(item, ocr_lines)
            ]
            if addable:
                st.caption(
                    "Compare a proposed row with the photo first. Adding it puts "
                    "the name, any printed UPC / SKU and the line price in the "
                    "review table with quantity 1. Then check it, add the tax "
                    "marker and category if needed, and save as usual."
                )
            for item in addable:
                line_key = "_".join(str(part) for part in suggestion_key(item))
                if st.button(
                    plain_markdown(
                        f"Add to review table: {split_product_code(item.name)[0]} "
                        f"— ${item.line_total:.2f}"
                    ),
                    key=f"local_llm_add_{version}_{line_key}",
                    width="stretch",
                ):
                    to_add = [item]
            if len(addable) > 1 and st.button(
                f"Add all {len(addable)} suggested rows to review table",
                key=f"local_llm_add_all_{version}",
                width="stretch",
            ):
                to_add = list(addable)
        else:
            st.info("Local AI produced no suggestions that passed the OCR source checks.")
        for reason in preview.rejected:
            st.warning(plain_markdown(reason))
        subtotal = active.get("subtotal")
        if subtotal is not None:
            difference = Decimal(str(subtotal)) - preview.item_sum
            st.caption(plain_markdown(
                f"Printed subtotal minus proposed item sum: ${difference:.2f}."
            ))
        st.caption(
            "Only descriptions and printed row amounts are evaluated in this pilot. "
            "Quantity, unit prices, discounts and tax still need normal review. "
            "OCR source checks establish that the quoted text exists; they do not "
            "prove that an amount belongs to an item."
        )
        st.caption(
            f"Model: {preview.model} · {preview.elapsed_seconds:.1f}s · "
            f"Installed model digest: {preview.model_digest}"
        )
    return to_add


def add_local_ai_rows(
    edited_item_rows: pd.DataFrame,
    items: list[EvidenceItem],
    *,
    ocr_text: str,
    line_decisions: list[dict[str, object]],
    recovered_lines: list[int],
) -> tuple[list[dict[str, object]], list[int]]:
    """Insert chosen suggestions where they were printed; return rows and lines.

    Raises LocalLlmError when a suggestion no longer matches the OCR text.
    """

    base_columns = [
        "Receipt order", "Item", "Product code", "Category",
        "Qty", "Line total", "Tax marker", "Unit price", "Discount",
    ]
    rows = edited_item_rows.drop(
        columns=["Delete", "Merge", "Review check"], errors="ignore",
    ).to_dict(orient="records")
    lines = ocr_text.splitlines()
    new_rows = sorted(
        (review_row_from_suggestion(item, lines) for item in items),
        key=lambda row: int(row["source_line"]),
    )
    recovered = [int(line) for line in recovered_lines]
    for row in new_rows:
        source_line = int(row.pop("source_line"))
        position = printed_position(source_line, line_decisions, recovered, len(rows))
        rows.insert(position, row)
        recovered.append(source_line)
    for number, row in enumerate(rows, start=1):
        row["Receipt order"] = number
    return [{column: row.get(column) for column in base_columns} for row in rows], recovered


def set_active_receipt(
    receipt: ReceiptExtraction,
    *,
    image_hash: str | None = None,
    extraction_method: str = "manual",
    ocr_text: str = "",
    line_decisions: list[dict[str, object]] | None = None,
    missing_item_candidates: list[dict[str, object]] | None = None,
    source_files: list[dict[str, object]] | None = None,
    state_key: str | None = None,
    recovered_source_lines: list[int] | None = None,
) -> None:
    st.session_state.pop("local_llm_preview", None)
    st.session_state.pop("local_llm_added", None)
    # OCR lines the reviewer already added, so later ones keep printed order.
    # Row edits on the same receipt keep the list; a new reading resets it.
    if recovered_source_lines is None:
        same_reading = (line_decisions or []) == st.session_state.get("line_decisions", [])
        recovered_source_lines = (
            st.session_state.get("recovered_source_lines", []) if same_reading else []
        )
    st.session_state.recovered_source_lines = list(recovered_source_lines)
    st.session_state.active_receipt = receipt.model_dump()
    st.session_state.active_image_hash = image_hash
    st.session_state.active_method = extraction_method
    st.session_state.ocr_text = ocr_text
    st.session_state.line_decisions = line_decisions or []
    st.session_state.missing_item_candidates = missing_item_candidates or []
    if source_files is not None:
        st.session_state.active_source_files = source_files
    revision = int(st.session_state.get("receipt_revision", 0)) + 1
    st.session_state.receipt_revision = revision
    # A durable job id keeps widget keys stable when Safari reconnects to the
    # same review, while preventing an earlier receipt's values from being
    # applied to a different job that also starts in a fresh browser session.
    st.session_state.receipt_version = (
        f"{state_key[:20]}_{revision}" if state_key else revision
    )


def clear_current_receipt() -> None:
    """Discard the in-progress review without deleting any saved receipt."""

    preserved_revision = int(st.session_state.get("receipt_uploader_revision", 0)) + 1
    for key in (
        "active_receipt",
        "active_image_hash",
        "active_method",
        "active_source_files",
        "line_decisions",
        "missing_item_candidates",
        "recovered_source_lines",
        "ocr_text",
        "receipt_processing_job",
        "restored_receipt_job",
        "receipt_version",
        "receipt_revision",
        "review_fingerprint",
        "last_saved_fingerprint",
        "last_saved_receipt_id",
        "receipt_update_notice",
        "local_llm_preview",
        "local_llm_added",
    ):
        st.session_state.pop(key, None)
    st.session_state.receipt_uploader_revision = preserved_revision
    if "job" in st.query_params:
        del st.query_params["job"]


# Session keys the Saved receipts page creates for one receipt. Each one ends
# with the receipt id (and, for item rows, a row number after it).
PER_RECEIPT_STATE_PREFIXES = (
    "confirm_delete_",
    "confirm_delete_saved_item_",
    "delete_saved_item_choice_",
    "apply_all_category_",
)


def forget_deleted_receipt(receipt_id: int) -> None:
    """Drop every session value that still points at a deleted receipt (RL-44).

    The Scan tab remembers the receipt it just saved, and the Saved receipts
    page keeps confirmation boxes and editor values keyed by receipt id. After a
    delete, none of those may survive, or the next screen can show details,
    "Saved as" messages, or confirmations that belong to the deleted receipt.
    """

    receipt_id = int(receipt_id)
    if st.session_state.get("last_saved_receipt_id") is not None and int(
        st.session_state["last_saved_receipt_id"]
    ) == receipt_id:
        st.session_state.pop("last_saved_receipt_id", None)
        st.session_state.pop("last_saved_fingerprint", None)
        st.session_state.pop("receipt_update_notice", None)
    suffix = str(receipt_id)
    for key in list(st.session_state.keys()):
        key_text = str(key)
        for prefix in PER_RECEIPT_STATE_PREFIXES:
            if not key_text.startswith(prefix):
                continue
            remainder = key_text[len(prefix):]
            if remainder == suffix or remainder.startswith(f"{suffix}_"):
                st.session_state.pop(key, None)
                break


def current_receipt_source_changed(
    current_image_hash: str | None,
    active_image_hash: str | None,
    current_sources: list[dict[str, object]],
    active_sources: list[dict[str, object]],
) -> bool:
    """Return whether a newly selected source differs from the active review.

    A restored durable job remains valid when Safari no longer exposes the file
    uploader value. Raw source hashes also avoid false changes caused by image
    preparation differences across reconnects.
    """

    if current_image_hash is None or current_image_hash == active_image_hash:
        return False

    def source_hashes(sources: list[dict[str, object]]) -> list[str]:
        hashes: list[str] = []
        for source in sources:
            data = source.get("data")
            if not isinstance(data, (bytes, bytearray)):
                return []
            hashes.append(hashlib.sha256(bytes(data)).hexdigest())
        return hashes

    current_hashes = source_hashes(current_sources)
    active_hashes = source_hashes(active_sources)
    if current_hashes and active_hashes and current_hashes == active_hashes:
        # The same PDF can intentionally point at a different selected page,
        # so its rendered-image hash remains the authoritative identity.
        if any(
            str(source.get("filename") or "").casefold().endswith(".pdf")
            for source in current_sources
        ):
            return True
        return False
    return True


def render_original_receipt_viewer(*, key: str) -> None:
    """Keep the uploaded receipt available beside correction controls."""

    source_files = list(st.session_state.get("active_source_files") or [])
    if not source_files:
        return

    with st.container(border=True):
        show_original = st.toggle(
            "View original receipt",
            key=key,
            help=(
                "Reopens the receipt already stored for this review. "
                "It does not upload or process the receipt again."
            ),
        )
        st.caption(
            "Use the original to verify an OCR line or manual price. "
            "Your current corrections stay in place."
        )
        if not show_original:
            return

        for source in source_files:
            try:
                previews = cached_receipt_source_previews(source)
            except (InvalidImageError, OSError, ValueError) as exc:
                st.error(f"Original receipt could not be displayed: {exc}")
                continue
            for preview_data, caption in previews:
                st.image(preview_data, caption=caption, width=450)


def restore_receipt_job(job_id: str) -> bool:
    """Restore a completed server-owned job into this browser session."""

    payload = get_receipt_job_result(DATABASE_PATH, job_id)
    if not payload:
        return False
    set_active_receipt(
        ReceiptExtraction.model_validate(payload["receipt"]),
        image_hash=str(payload.get("image_hash") or ""),
        extraction_method=str(
            payload.get("extraction_method") or "local-paddleocr"
        ),
        ocr_text=str(payload.get("ocr_text") or ""),
        line_decisions=list(payload.get("line_decisions") or []),
        missing_item_candidates=list(
            payload.get("missing_item_candidates") or []
        ),
        source_files=get_receipt_job_sources(DATABASE_PATH, job_id),
        state_key=job_id,
    )
    st.session_state.restored_receipt_job = job_id
    terminal_status("RESTORED", f"{job_id[:8]} | Review restored after reconnect.")
    return True


@st.fragment(run_every="2s")
def render_receipt_job_status(job_id: str) -> None:
    """Poll one durable job without restarting or resubmitting it."""

    job = get_receipt_job(DATABASE_PATH, job_id)
    if job is None:
        st.error("This receipt job could not be found on the server.")
        return
    status = str(job["status"])
    if status == "review":
        st.success("Receipt reading finished. Opening the review screen...")
        st.rerun()
    if status == "failed":
        st.error(str(job.get("error") or job["message"]))
        render_ocr_diagnostic_download(job_id)
        st.caption(
            "This attempt stopped safely. The receipt was not submitted again."
        )
        return
    with st.status(
        str(job["stage"]),
        expanded=True,
        state="running" if status in ACTIVE_JOB_STATUSES else "complete",
    ):
        st.write(str(job["message"]))
        st.caption(
            f"Job {job_id[:8]} · last server update {job['updated_at']}"
        )
        st.info(
            "This job belongs to the server. You may refresh Safari or reconnect "
            "without submitting the receipt again."
        )


def render_ocr_diagnostic_download(job_id: str) -> None:
    """Offer an explicit, local download without retaining export files."""
    diagnostic = get_receipt_job_diagnostic(DATABASE_PATH, job_id)
    if diagnostic is None:
        st.caption("OCR diagnostic is available for 30 minutes in this running app after processing.")
        return
    st.download_button(
        "Download OCR Diagnostic JSON",
        data=json.dumps(diagnostic, indent=2, ensure_ascii=False),
        file_name=f"ocr-diagnostic-{job_id[:8]}.json",
        mime="application/json",
        key=f"ocr_diagnostic_{job_id}",
        on_click="ignore",
        help=(
            "OCR troubleshooting data. Card brands, banks, card numbers and approval "
            "codes are replaced with placeholders automatically. Review it before sharing."
        ),
    )
    st.caption(
        "No receipt image is included. Card and payment details are masked automatically; "
        "redaction is best-effort, so review before sharing."
    )


def build_edited_receipt(version: int, rows: pd.DataFrame) -> ReceiptExtraction:
    items: list[ReceiptItem] = []
    ordered_rows = rows.copy()
    if "Receipt order" in ordered_rows.columns:
        ordered_rows["_receipt_sort"] = pd.to_numeric(
            ordered_rows["Receipt order"], errors="coerce"
        )
        ordered_rows = ordered_rows.sort_values(
            "_receipt_sort", na_position="last", kind="stable"
        )
    for row in ordered_rows.to_dict(orient="records"):
        name = str(row.get("Item", "") or "").strip()
        if not name or name == "nan":
            raise ValueError("Every item row needs a name. Delete unused rows.")
        quantity = row.get("Qty", 1.0)
        unit_price = row.get("Unit price")
        discount = row.get("Discount")
        line_total = row.get("Line total", 0.0)
        product_code_value = row.get("Product code")
        product_code = (
            None
            if pd.isna(product_code_value) or not str(product_code_value).strip()
            else str(product_code_value).strip()
        )
        category_value = row.get("Category")
        category = (
            "Uncategorized"
            if pd.isna(category_value) or not str(category_value).strip()
            else str(category_value).strip()
        )
        if pd.isna(quantity) or not math.isfinite(float(quantity)) or float(quantity) <= 0:
            raise ValueError(f"Enter a positive quantity for {name}.")
        if pd.isna(line_total) or not math.isfinite(float(line_total)):
            raise ValueError(f"Enter the line price for {name}.")
        items.append(
            ReceiptItem(
                tax_marker=row.get("Tax marker") if row.get("Tax marker") in TAX_MARKERS else None,
                category=category,
                name=name,
                product_code=product_code,
                quantity=1.0 if pd.isna(quantity) else float(quantity),
                unit_price=None if pd.isna(unit_price) else float(unit_price),
                discount=None if pd.isna(discount) else float(discount),
                line_total=0.0 if pd.isna(line_total) else float(line_total),
            )
        )

    return ReceiptExtraction(
        store_name=st.session_state[f"store_{version}"].strip(),
        transaction_date=parse_optional_date(st.session_state[f"date_{version}"]),
        currency=st.session_state[f"currency_{version}"].strip() or "USD",
        items=items,
        subtotal=parse_optional_money(
            st.session_state[f"subtotal_{version}"], "Subtotal"
        ),
        tax=parse_optional_money(st.session_state[f"tax_{version}"], "Tax"),
        total=parse_optional_money(st.session_state[f"total_{version}"], "Total"),
        confidence=float(st.session_state.active_receipt.get("confidence", 0.0)),
        notes=list(st.session_state.active_receipt.get("notes", [])),
    )


st.set_page_config(page_title="TalliUpper", page_icon="🧾", layout="wide")
st.markdown(
    """
    <style>
      .block-container {max-width: 1120px; padding-top: 2rem;}
      [data-testid="stMetricValue"] {font-size: 1.6rem;}
      .receipt-note {color: #5f6b66; font-size: 0.92rem;}
      .receipt-print-only {display: none;}
      /* Phones: use the full width and keep inputs at 16px so iPhone Safari
         does not zoom the page whenever a text box is tapped. */
      @media (max-width: 640px) {
        .block-container {padding: 1rem 0.75rem 3rem;}
        [data-testid="stTextInput"] input,
        [data-testid="stNumberInput"] input,
        [data-testid="stDateInput"] input,
        [data-testid="stTextArea"] textarea,
        [data-baseweb="select"] input {font-size: 16px !important;}
        [data-testid="stImage"] img {max-width: 100% !important; height: auto !important;}
        [data-testid="stFormSubmitButton"] button,
        [data-testid="stButton"] button {min-height: 44px;}
      }
      .receipt-comparison-table {
        width: 100%;
        border-collapse: collapse;
        table-layout: fixed;
        margin: 10px 0 18px;
      }
      .receipt-comparison-table th,
      .receipt-comparison-table td {
        border: 1px solid #aebbc5;
        padding: 7px;
        text-align: left;
        overflow-wrap: anywhere;
      }
      .receipt-comparison-table th {background: #edf3f8;}
      .receipt-comparison-table th:first-child {width: 52%;}
      .receipt-comparison-table th:not(:first-child) {width: 16%;}
      /* Editable values must look editable on first use. */
      [data-testid="stTextInput"] [data-baseweb="input"] {
        background-color: #ffffff;
        border: 1px solid #6b7280;
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.08);
      }
      [data-testid="stTextInput"] [data-baseweb="input"]:focus-within {
        border-color: #0068c9;
        box-shadow: 0 0 0 2px rgba(0, 104, 201, 0.22);
      }
      /* Date ranges and unchecked boxes must remain visible on a white page. */
      [data-testid="stDateInput"] [data-baseweb="input"] {
        background-color: #ffffff;
        border: 1px solid #6b7280;
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.08);
      }
      [data-testid="stDateInput"] [data-baseweb="input"]:focus-within {
        border-color: #0068c9;
        box-shadow: 0 0 0 2px rgba(0, 104, 201, 0.22);
      }
      [data-testid="stCheckbox"] [data-baseweb="checkbox"] > div:first-child {
        background-color: #ffffff;
        border: 2px solid #6b7280;
      }
      @media (prefers-color-scheme: dark) {
        [data-testid="stTextInput"] [data-baseweb="input"],
        [data-testid="stDateInput"] [data-baseweb="input"] {
          background-color: #1f2937;
          border-color: #9ca3af;
        }
      }
      @media print {
        @page {size: landscape; margin: 10mm;}
        [class*="st-key-items_"] [data-testid="stDataFrame"] {
          display: none !important;
        }
        .receipt-print-only {display: block !important; margin: 10px 0 18px;}
        .receipt-print-table {
          width: 100%;
          border-collapse: collapse;
          table-layout: fixed;
          font-size: 9pt;
        }
        .receipt-print-table th,
        .receipt-print-table td {
          border: 1px solid #aebbc5;
          padding: 4px;
          text-align: left;
          vertical-align: top;
          overflow-wrap: anywhere;
          word-break: break-word;
        }
        .receipt-print-table th {background: #edf3f8;}
        .receipt-print-table th:nth-child(1) {width: 6%;}
        .receipt-print-table th:nth-child(2) {width: 38%;}
        .receipt-print-table th:nth-child(3) {width: 18%;}
        .receipt-print-table th:nth-child(4) {width: 20%;}
        .receipt-print-table th:nth-child(5) {width: 7%;}
        .receipt-print-table th:nth-child(6) {width: 11%;}
        .receipt-print-table tr {break-inside: avoid;}
        .receipt-comparison-table {font-size: 9pt;}
      }
    </style>
    """,
    unsafe_allow_html=True,
)

initialize_database(DATABASE_PATH)
initialize_receipt_jobs(DATABASE_PATH)
current_receipt_job_id = str(st.query_params.get("job") or "")
current_receipt_job = (
    get_receipt_job(DATABASE_PATH, current_receipt_job_id)
    if current_receipt_job_id
    else None
)
if (
    current_receipt_job is not None
    and str(current_receipt_job["status"]) in ACTIVE_JOB_STATUSES
):
    # This is also the recovery path after a server restart. The in-process
    # guard prevents duplicate worker futures during normal Streamlit reruns.
    submit_local_receipt_job(DATABASE_PATH, current_receipt_job_id)
if (
    current_receipt_job is not None
    and str(current_receipt_job["status"]) == "review"
    and st.session_state.get("restored_receipt_job") != current_receipt_job_id
):
    restore_receipt_job(current_receipt_job_id)
server_job_active = bool(
    current_receipt_job is not None
    and str(current_receipt_job["status"]) in ACTIVE_JOB_STATUSES
)
category_options = list_categories(DATABASE_PATH)

hero_copy, hero_art = st.columns([3, 2], gap="large")
with hero_copy:
    st.title("🧾 TalliUpper")
    st.caption(
        "Turn receipt photos into reviewed, searchable spending data—locally by default."
    )
    st.markdown(f"**Our mission:** {MISSION_STATEMENT}")
with hero_art:
    st.image(
        str(TALLIUPPER_MASCOT_PATH),
        width=280,
        caption="TalliUpper receipt detective",
    )

environment_key = os.getenv("OPENAI_API_KEY", "").strip()
with st.sidebar:
    st.header("Processing")
    processing_mode = st.radio(
        "Method",
        [PROCESSING_LOCAL, PROCESSING_CLOUD],
        help="Local OCR keeps the image on this computer. Cloud vision is an optional fallback.",
    )

    if processing_mode == PROCESSING_LOCAL:
        selected_language = st.selectbox("Receipt language", list(LANGUAGES))
        language = LANGUAGES[selected_language]
        api_key = ""
        model = DEFAULT_MODEL
        st.success("Private mode: the receipt image stays on this computer")
        st.caption(
            "The first local scan downloads the OCR models. Later scans can run offline."
        )
    else:
        language = "en"
        if environment_key:
            st.success("API key loaded from your computer")
            api_key = environment_key
        else:
            api_key = st.text_input(
                "OpenAI API key",
                type="password",
                help="Used for this session only. TalliUpper does not save the key.",
            )
        with st.expander("Advanced"):
            model = st.text_input("Vision model", value=DEFAULT_MODEL)
        st.caption("In this mode, the prepared receipt image is sent for extraction.")

    st.divider()
    st.subheader("Expense categories")
    st.caption(
        "Categories are applied to individual items. Custom categories stay on this computer."
    )
    with st.form("create_expense_category", clear_on_submit=True):
        new_category = st.text_input(
            "Create a category",
            placeholder="For example: Workshop supplies",
        )
        create_category_clicked = st.form_submit_button("Add category")
    if create_category_clicked:
        try:
            created_category = create_category(DATABASE_PATH, new_category)
            st.success(f'Added category: {created_category}')
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

    st.divider()
    st.caption(
        "Corrected records are stored in a local SQLite database. Original receipt "
        "files are saved only when you choose that option before saving."
    )

scan_tab, history_tab, expenses_tab, insights_tab, export_tab, learn_tab = st.tabs(
    [
        "Scan or enter",
        "Saved receipts",
        "Expense totals",
        "Item insights",
        "Export center",
        "Learn the pipeline",
    ]
)

with scan_tab:
    st.caption("Add a receipt → 1. Items → 2. Calculate → 3. Printed amounts → 4. Compare → 5. Verify")
    if st.session_state.get("active_receipt") or current_receipt_job_id:
        reset_column, reset_note = st.columns([1, 3])
        with reset_column:
            if st.button(
                "Start another receipt",
                key="start_another_receipt_top",
                width="stretch",
            ):
                clear_current_receipt()
                st.rerun()
        with reset_note:
            st.caption(
                "Clears this in-progress review. Saved receipts are not deleted."
            )
    left, right = st.container(), st.container()

    with left:
        if current_receipt_job is not None and (
            server_job_active or str(current_receipt_job["status"]) == "failed"
        ):
            st.subheader("Current receipt job")
            render_receipt_job_status(current_receipt_job_id)
        st.subheader("Start here: add a receipt")
        source_mode = st.radio(
            "Image source",
            ["Upload an image or PDF", "Take a photo", "Try included sample"],
            horizontal=True,
            label_visibility="collapsed",
            disabled=server_job_active,
        )
        uploaded_files = []
        raw_pages: list[tuple[bytes, str, dict[str, object]]] = []
        prepared_pages: list[bytes] = []
        valid_source_files: list[dict[str, object]] = []
        source_filename: str | None = None

        if source_mode == "Take a photo":
            captured_file = st.camera_input(
                "Take a clear, straight-on photo",
                disabled=server_job_active,
                key=(
                    "receipt_camera_"
                    f"{st.session_state.get('receipt_uploader_revision', 0)}"
                ),
            )
            if captured_file is not None:
                uploaded_files = [captured_file]
        elif source_mode == "Upload an image or PDF":
            upload_selection = st.file_uploader(
                "Choose one or more images from the same receipt",
                type=["jpg", "jpeg", "png", "webp", "heic", "heif", "pdf"],
                accept_multiple_files=True,
                help="Select images in top-to-bottom receipt order. Use a separate scan for each purchase.",
                disabled=server_job_active,
                key=(
                    "receipt_upload_"
                    f"{st.session_state.get('receipt_uploader_revision', 0)}"
                ),
            )
            if upload_selection:
                uploaded_files = (
                    list(upload_selection)
                    if isinstance(upload_selection, (list, tuple))
                    else [upload_selection]
                )
        else:
            raw_pages = [
                (
                    SAMPLE_RECEIPT_PATH.read_bytes(),
                    SAMPLE_RECEIPT_PATH.name,
                    {
                        "data": SAMPLE_RECEIPT_PATH.read_bytes(),
                        "filename": SAMPLE_RECEIPT_PATH.name,
                        "media_type": "image/png",
                    },
                )
            ]
            st.caption("This synthetic example contains no real purchase data.")

        for uploaded_index, uploaded_file in enumerate(uploaded_files):
            uploaded_bytes = uploaded_file.getvalue()
            uploaded_name = uploaded_file.name
            source_file = {
                "data": uploaded_bytes,
                "filename": uploaded_name,
                "media_type": getattr(uploaded_file, "type", "") or "",
            }
            if uploaded_name.lower().endswith(".pdf"):
                try:
                    pdf_hash = hashlib.sha256(uploaded_bytes).hexdigest()
                    page_count = cached_pdf_page_count(uploaded_bytes)
                    all_pages = page_count == 1
                    if page_count > 1:
                        all_pages = st.checkbox(
                            f"Read all {page_count} PDF pages as one receipt",
                            value=page_count <= 12,
                            key=f"pdf_all_pages_{uploaded_index}_{pdf_hash}",
                            help="Use this for one order or receipt spanning several pages.",
                        )
                    if all_pages:
                        selected_pages = range(1, page_count + 1)
                        st.caption(
                            f"All {page_count} PDF pages will be read in order. "
                            "Review the full item list and printed totals before saving."
                        )
                    else:
                        page_label = (
                            "PDF page to read"
                            if len(uploaded_files) == 1
                            else f"PDF page to read: {uploaded_name}"
                        )
                        selected_page = int(
                            st.number_input(
                                page_label, min_value=1, max_value=page_count,
                                value=1, step=1,
                                key=f"pdf_page_{uploaded_index}_{pdf_hash}",
                            )
                        )
                        selected_pages = [selected_page]
                        st.warning(
                            f"Only page {selected_page} of {page_count} will be read. "
                            "Other pages and their totals will be omitted."
                        )
                    for page_number in selected_pages:
                        raw_pages.append(
                            (
                                cached_render_pdf_page(uploaded_bytes, page_number),
                                f"{uploaded_name} (page {page_number})",
                                source_file,
                            )
                        )
                except InvalidImageError as exc:
                    st.error(f"{uploaded_name}: {exc}")
            else:
                raw_pages.append((uploaded_bytes, uploaded_name, source_file))

        valid_names: list[str] = []
        for page_number, (page_bytes, page_name, source_file) in enumerate(
            raw_pages, start=1
        ):
            try:
                prepared, _ = prepare_receipt_image(page_bytes)
                prepared_pages.append(prepared)
                valid_names.append(page_name)
                if source_file not in valid_source_files:
                    valid_source_files.append(source_file)
                st.image(
                    prepared,
                    caption=(
                        f"Receipt image {page_number} of {len(raw_pages)}: "
                        f"{page_name}"
                    ),
                    width=450,
                )
            except InvalidImageError as exc:
                st.error(f"{page_name}: {exc}")

        image_hash: str | None = None
        if prepared_pages:
            digest = hashlib.sha256()
            for prepared in prepared_pages:
                digest.update(len(prepared).to_bytes(8, "big"))
                digest.update(prepared)
            image_hash = digest.hexdigest()
            source_filename = " + ".join(valid_names)
            if len(prepared_pages) > 1:
                st.info(
                    f"{len(prepared_pages)} images will be read as one receipt, "
                    "in the order shown above."
                )

        cloud_key_missing = processing_mode == PROCESSING_CLOUD and not api_key
        cloud_multi_image = (
            processing_mode == PROCESSING_CLOUD and len(prepared_pages) > 1
        )
        extract_col, manual_col = st.columns(2)
        with extract_col:
            processing_job = st.session_state.get("receipt_processing_job")
            processing_active = bool(processing_job) or server_job_active
            extract_clicked = st.button(
                "Read receipt",
                type="primary",
                width="stretch",
                disabled=not prepared_pages
                or cloud_key_missing
                or cloud_multi_image
                or processing_active,
            )
        with manual_col:
            manual_clicked = st.button(
                "Enter manually",
                width="stretch",
                disabled=processing_active,
            )

        if prepared_pages and cloud_key_missing:
            st.info(
                "Enter an API key in the sidebar, switch to Local OCR, or enter manually."
            )
        if cloud_multi_image:
            st.info(
                "Reading several images together currently uses Local OCR. "
                "Switch to Local OCR, or upload one image for cloud vision."
            )

        if (
            extract_clicked
            and prepared_pages
            and processing_mode == PROCESSING_LOCAL
            and image_hash
        ):
            job_id, created = create_local_receipt_job(
                DATABASE_PATH,
                image_hash=image_hash,
                prepared_pages=prepared_pages,
                language=language,
                source_files=valid_source_files,
            )
            st.query_params["job"] = job_id
            if created:
                terminal_status(
                    "QUEUED",
                    f"{job_id[:8]} | Durable receipt job created.",
                )
            else:
                terminal_status(
                    "RECONNECT",
                    f"{job_id[:8]} | Reusing the active job for this receipt.",
                )
            submit_local_receipt_job(DATABASE_PATH, job_id)
            st.rerun()

        if (
            extract_clicked
            and prepared_pages
            and processing_mode == PROCESSING_CLOUD
        ):
            request_id = image_hash[:8] if image_hash else "unknown"
            st.session_state.receipt_processing_job = {
                "image_hash": image_hash,
                "page_count": len(prepared_pages),
                "request_id": request_id,
            }
            terminal_status(
                "QUEUED",
                f"{request_id} | {len(prepared_pages)} receipt image"
                f"{'s' if len(prepared_pages) != 1 else ''} submitted once.",
            )
            # Render the locked processing screen in a fresh pass before the
            # CPU-heavy OCR call begins. This gives mobile Safari time to paint
            # the status card instead of appearing blank.
            st.rerun()

        processing_job = st.session_state.get("receipt_processing_job")
        processing_matches_upload = bool(
            processing_job
            and prepared_pages
            and processing_job.get("image_hash") == image_hash
        )
        if processing_job and not processing_matches_upload:
            st.session_state.pop("receipt_processing_job", None)
            st.error(
                "The selected receipt changed before reading began. "
                "Please select it again and press Read receipt once."
            )

        if processing_matches_upload:
            request_id = str(processing_job.get("request_id") or "unknown")
            processing_started = time.perf_counter()
            terminal_status(
                "START",
                f"{request_id} | Processing began; submission controls are locked.",
            )
            working_overlay = show_talli_working_overlay(
                TALLI_READING_RECEIPT_PATH
                if len(prepared_pages) == 1
                else TALLI_READING_PAGES_PATH,
                "Talli is reading your receipt..."
                if len(prepared_pages) == 1
                else f"Talli is reading {len(prepared_pages)} receipt images...",
            )
            receipt_status = st.status(
                "Reading receipt - please wait",
                expanded=True,
                state="running",
            )
            try:
                with receipt_status:
                    st.write(
                        f"Processing {len(prepared_pages)} receipt image"
                        f"{'s' if len(prepared_pages) != 1 else ''}. "
                        "Keep this page open while TalliUpper identifies items and totals."
                    )
                    st.info(
                        "Your receipt has already been submitted. Upload, Read receipt, "
                        "and manual-entry controls are locked until this attempt finishes."
                    )
                    st.write("Step 1 of 4 complete: receipt image prepared.")
                    if processing_mode == PROCESSING_LOCAL:
                        st.write(
                            "Step 2 of 4: loading local OCR and reading receipt lines. "
                            "This may take several minutes on this computer..."
                        )
                        terminal_status(
                            "OCR",
                            f"{request_id} | Loading local models and reading text.",
                        )
                        local_result = (
                            process_receipt_locally(
                                prepared_pages[0],
                                language=language,
                            )
                            if len(prepared_pages) == 1
                            else process_receipt_pages_locally(
                                prepared_pages,
                                language=language,
                            )
                        )
                        st.write(
                            "Step 3 of 4 complete: receipt lines organized into "
                            "items and printed totals."
                        )
                        terminal_status(
                            "REVIEW",
                            f"{request_id} | {len(local_result.ocr.lines)} OCR lines; "
                            f"{len(local_result.parsed.receipt.items)} item rows prepared.",
                        )
                        set_active_receipt(
                            apply_remembered_categories(
                                DATABASE_PATH, local_result.parsed.receipt
                            ),
                            image_hash=image_hash,
                            extraction_method="local-paddleocr",
                            ocr_text=local_result.ocr.text,
                            line_decisions=[
                                asdict(decision)
                                for decision in local_result.parsed.decisions
                            ],
                            missing_item_candidates=[
                                asdict(candidate)
                                for candidate in local_result.parsed.missing_item_candidates
                            ],
                            source_files=valid_source_files,
                        )
                    else:
                        st.write("Sending the prepared image to the selected vision model...")
                        terminal_status(
                            "OCR",
                            f"{request_id} | Sending the prepared image to cloud vision.",
                        )
                        extracted = extract_receipt(
                            prepared_pages[0],
                            api_key=api_key,
                            mime_type="image/jpeg",
                            model=model.strip() or DEFAULT_MODEL,
                        )
                        set_active_receipt(
                            apply_remembered_categories(DATABASE_PATH, extracted),
                            image_hash=image_hash,
                            extraction_method="openai-vision",
                            source_files=valid_source_files,
                        )
                st.write("Step 4 of 4 complete: review is ready.")
                terminal_status(
                    "SUCCESS",
                    f"{request_id} | Receipt review ready in "
                    f"{time.perf_counter() - processing_started:.1f}s.",
                )
                receipt_status.update(
                    label="Receipt ready for review",
                    state="complete",
                    expanded=False,
                )
                ready_icon, ready_message = st.columns([1, 4])
                with ready_icon:
                    st.image(str(TALLI_RECEIPT_READY_PATH), width=88)
                with ready_message:
                    st.success("Receipt read. Check every field before saving.")
            except (InvalidImageError, LocalOcrError, ReceiptExtractionError) as exc:
                terminal_status(
                    "ERROR",
                    f"{request_id} | {type(exc).__name__}: {exc}",
                )
                receipt_status.update(
                    label="Receipt could not be read",
                    state="error",
                    expanded=True,
                )
                st.error(str(exc))
            finally:
                st.session_state.pop("receipt_processing_job", None)
                terminal_status(
                    "UNLOCKED",
                    f"{request_id} | Controls unlocked after "
                    f"{time.perf_counter() - processing_started:.1f}s.",
                )
                working_overlay.empty()

        if manual_clicked:
            set_active_receipt(
                blank_receipt(),
                image_hash=image_hash,
                extraction_method="manual",
                source_files=valid_source_files,
            )

        with st.expander("Tips for a better photo"):
            st.markdown(
                "- Put the receipt on a dark, flat surface.\n"
                "- Include all four corners and keep the camera parallel.\n"
                "- Avoid shadows and glare, especially over the totals.\n"
                "- For a very long receipt, use a high-resolution photo."
            )

    with right:
        st.subheader("1. Review item names, quantities, and prices")
        active = st.session_state.get("active_receipt")
        if not active:
            st.info(
                "The extracted receipt will appear here for correction before it is saved."
            )
        else:
            version = st.session_state.get("receipt_version", 1)
            active_method = st.session_state.get("active_method", "unknown")
            active_categories = [
                str(item.get("category", "")).strip()
                for item in active.get("items", [])
                if str(item.get("category", "")).strip()
            ]
            item_category_options = list(
                dict.fromkeys([*category_options, *active_categories])
            )
            st.caption(f"Current extraction method: `{active_method}`")
            if current_receipt_job_id:
                render_ocr_diagnostic_download(current_receipt_job_id)

            category_notice = st.session_state.pop(
                "review_category_notice", None
            )
            if category_notice:
                st.success(category_notice)
            with st.expander("Add a new expense category"):
                st.caption(
                    "Create a reusable category without leaving this receipt. "
                    "For a subcategory, use a name such as Pet Care / Medication."
                )
                with st.form(
                    f"create_review_category_{version}", clear_on_submit=True
                ):
                    review_category = st.text_input(
                        "New category or subcategory",
                        placeholder="For example: Pet Care / Medication",
                    )
                    create_review_category = st.form_submit_button(
                        "Add category to this receipt"
                    )
                if create_review_category:
                    try:
                        created_category = create_category(
                            DATABASE_PATH, review_category
                        )
                        st.session_state.review_category_notice = (
                            f"Added category: {created_category}. "
                            "It is now available in the Expense category column."
                        )
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))

            if current_receipt_source_changed(
                image_hash,
                st.session_state.get("active_image_hash"),
                valid_source_files,
                st.session_state.get("active_source_files", []),
            ):
                st.warning("The photo changed. Select “Read receipt” to replace these details.")

            st.info("Check every item against the photo. Correct names, quantities, and line prices; add missing rows. Line price is the amount charged for the whole row after discounts.")
            local_ai_rows_to_add = render_local_ai_preview(active, version)
            item_rows = pd.DataFrame(
                [
                    {
                        "Item": item.get("name", ""),
                        "Product code": item.get("product_code"),
                        "Category": item.get("category", "Uncategorized"),
                        "Qty": item.get("quantity", 1.0),
                        "Tax marker": item.get("tax_marker"),
                        "Unit price": item.get("unit_price"),
                        "Discount": item.get("discount"),
                        "Line total": item.get("line_total", 0.0),
                    }
                    for item in active.get("items", [])
                ],
                columns=[
                    "Item", "Product code", "Category", "Qty", "Line total",
                    "Tax marker", "Unit price", "Discount",
                ],
            )
            review_rows_key = f"review_rows_{version}"
            pending_review_rows = st.session_state.get(review_rows_key)
            if pending_review_rows is not None:
                item_rows = pd.DataFrame(
                    pending_review_rows,
                    columns=[
                        "Receipt order", "Item", "Product code", "Category",
                        "Qty", "Line total", "Tax marker", "Unit price",
                        "Discount",
                    ],
                )
            else:
                item_rows.insert(
                    0, "Receipt order", range(1, len(item_rows) + 1)
                )
            # Keep the review grid aligned with the printed invoice. Duplicate
            # suggestions remain visible in Review check without moving rows.
            item_rows = item_rows.sort_values(
                "Receipt order", kind="stable"
            ).reset_index(drop=True)
            review_records = item_rows.to_dict(orient="records")
            duplicate_labels = potential_duplicate_labels(review_records)
            split_labels = possible_split_labels(review_records)
            item_rows["Review check"] = compact_review_labels(review_records)
            item_rows["Delete"] = False
            item_rows["Merge"] = False
            item_rows = item_rows[
                # Item name and price come first so they stay visible on a
                # phone without scrolling sideways.
                [
                    "Receipt order", "Item", "Line total", "Qty", "Delete",
                    "Merge", "Review check", "Category", "Product code",
                    "Tax marker", "Unit price", "Discount",
                ]
            ]
            editor_revision = st.session_state.get(
                f"item_editor_revision_{version}", 0
            )
            st.caption("Rows are shown in printed receipt order (1, 2, 3, ...).")
            edited_item_rows = st.data_editor(
                item_rows,
                key=f"items_{version}_{editor_revision}",
                num_rows="dynamic",
                width="stretch",
                hide_index=True,
                disabled=["Review check", "Receipt order"],
                column_config={
                    "Receipt order": st.column_config.NumberColumn(
                        "Row",
                        help="Original receipt order. Merge uses this order even when duplicate groups are displayed together.",
                        width="small",
                    ),
                    "Delete": st.column_config.CheckboxColumn(
                        "Delete",
                        help="Select an incorrect or duplicated row, then use Delete selected items below.",
                        width="small",
                    ),
                    "Merge": st.column_config.CheckboxColumn(
                        "Merge",
                        help="Select a confirmed extra OCR copy or split-item fragment. Duplicate copies collapse to one quantity and price; split fragments join the suggested row.",
                        width="small",
                    ),
                    "Review check": st.column_config.TextColumn(
                        "Review check",
                        help="Short duplicate and possible-merge suggestions. Compare the row with the original receipt before taking action.",
                        width="medium",
                    ),
                    "Tax marker": st.column_config.SelectboxColumn("Tax marker", options=list(TAX_MARKERS), help="H-E-B uses T for taxable and F/FW for non-taxable items. Walmart prints X (taxed) or N (not taxed) after the price. Leave blank when unclear. Other stores may use different codes."),
                    "Item": st.column_config.TextColumn(
                        "Item name", required=True, width="medium"
                    ),
                    "Product code": st.column_config.TextColumn(
                        "UPC / SKU",
                        help="The printed UPC, SKU, or other product code. Leave blank when absent or unclear.",
                    ),
                    "Category": st.column_config.SelectboxColumn(
                        "Expense category",
                        options=item_category_options,
                        required=True,
                        help="Choose Uncategorized when you are not ready to classify an item.",
                        width="medium",
                    ),
                    "Qty": st.column_config.NumberColumn(
                        "Quantity", min_value=0.001, format="%.3f", required=True,
                        width="small",
                    ),
                    "Unit price": st.column_config.NumberColumn(
                        "Unit price", format="$%.2f"
                    ),
                    "Discount": st.column_config.NumberColumn(
                        "Discount", min_value=0.0, format="$%.2f"
                    ),
                    "Line total": st.column_config.NumberColumn(
                        "Line price", format="$%.2f", required=True, width="small"
                    ),
                },
            )
            if local_ai_rows_to_add:
                try:
                    ai_rows, ai_lines = add_local_ai_rows(
                        edited_item_rows,
                        local_ai_rows_to_add,
                        ocr_text=str(st.session_state.get("ocr_text") or ""),
                        line_decisions=st.session_state.get("line_decisions", []),
                        recovered_lines=st.session_state.get(
                            "recovered_source_lines", []
                        ),
                    )
                except LocalLlmError as exc:
                    st.error(str(exc))
                else:
                    st.session_state[review_rows_key] = ai_rows
                    st.session_state.recovered_source_lines = ai_lines
                    st.session_state[f"item_editor_revision_{version}"] = (
                        editor_revision + 1
                    )
                    preview_id = st.session_state["local_llm_preview"][0]
                    added_state = st.session_state.get("local_llm_added")
                    already = (
                        set(added_state[1])
                        if added_state and added_state[0] == preview_id else set()
                    )
                    st.session_state.local_llm_added = (
                        preview_id,
                        already | {suggestion_key(item) for item in local_ai_rows_to_add},
                    )
                    names = ", ".join(
                        f"{split_product_code(item.name)[0]} (${item.line_total:.2f})"
                        for item in local_ai_rows_to_add
                    )
                    st.session_state.item_add_notice = (
                        f"Added from Local AI: {names}, quantity 1. Check each row "
                        "against the photo, fill in the tax marker and "
                        "category if needed, then save as usual."
                    )
                    st.rerun()
            st.markdown(
                build_printable_item_table_html(
                    edited_item_rows.to_dict(orient="records")
                ),
                unsafe_allow_html=True,
            )

            with st.expander("Edit one item (phone-friendly)"):
                st.caption(
                    "Use this editor when the item table is hard to use on a "
                    "phone. Pick a row, correct it, then apply. Changes return "
                    "to the table before the receipt is saved."
                )
                mobile_edit_rows = list(range(len(edited_item_rows)))
                if mobile_edit_rows:
                    mobile_edit_row = st.selectbox(
                        "Item to edit",
                        mobile_edit_rows,
                        format_func=lambda index: (
                            f"Row {safe_receipt_order_label(edited_item_rows.iloc[index]['Receipt order'], index)}: "
                            f"{edited_item_rows.iloc[index]['Item']}"
                        ),
                        key=f"mobile_edit_row_{version}_{editor_revision}",
                    )
                    selected_edit_row = edited_item_rows.iloc[mobile_edit_row]

                    def optional_cell(column: str) -> object:
                        value = selected_edit_row.get(column)
                        return None if pd.isna(value) else value

                    current_mobile_category = (
                        str(optional_cell("Category") or "").strip()
                        or "Uncategorized"
                    )
                    current_tax_marker = optional_cell("Tax marker")
                    tax_marker_options = [None, *TAX_MARKERS]
                    current_quantity = optional_cell("Qty")
                    # A form keeps the phone keyboard open while typing instead
                    # of reloading the page after every field.
                    with st.form(
                        f"mobile_item_editor_{version}_{editor_revision}_"
                        f"{mobile_edit_row}"
                    ):
                        mobile_item_name = st.text_input(
                            "Item name", value=str(optional_cell("Item") or "")
                        )
                        mobile_line_total = st.text_input(
                            "Line price (charged for the whole row)",
                            value=money_text(optional_cell("Line total")),
                        )
                        mobile_quantity = st.text_input(
                            "Quantity",
                            value=(
                                ""
                                if current_quantity is None
                                else f"{float(current_quantity):g}"
                            ),
                        )
                        mobile_category_value = st.selectbox(
                            "Expense category",
                            item_category_options,
                            index=(
                                item_category_options.index(current_mobile_category)
                                if current_mobile_category in item_category_options
                                else 0
                            ),
                        )
                        mobile_tax_marker = st.selectbox(
                            "Tax marker",
                            tax_marker_options,
                            index=(
                                tax_marker_options.index(current_tax_marker)
                                if current_tax_marker in tax_marker_options
                                else 0
                            ),
                            format_func=lambda marker: marker or "None or unclear",
                        )
                        mobile_unit_price = st.text_input(
                            "Unit price (optional)",
                            value=money_text(optional_cell("Unit price")),
                        )
                        mobile_discount = st.text_input(
                            "Discount (optional)",
                            value=money_text(optional_cell("Discount")),
                        )
                        mobile_product_code = st.text_input(
                            "UPC / SKU (optional)",
                            value=str(optional_cell("Product code") or ""),
                        )
                        apply_mobile_edit = st.form_submit_button(
                            "Apply changes to this item", width="stretch"
                        )
                    if apply_mobile_edit:
                        try:
                            edited_name = mobile_item_name.strip()
                            if not edited_name:
                                raise ValueError("Enter an item name.")
                            edited_line_total = parse_optional_money(
                                mobile_line_total, "Line price"
                            )
                            if edited_line_total is None:
                                raise ValueError("Enter the line price.")
                            try:
                                edited_quantity = float(mobile_quantity.strip())
                            except ValueError:
                                edited_quantity = float("nan")
                            if not math.isfinite(edited_quantity) or edited_quantity <= 0:
                                raise ValueError(
                                    "Quantity must be a positive number, such as 1 or 2.5."
                                )
                            edited_unit_price = parse_optional_money(
                                mobile_unit_price, "Unit price"
                            )
                            edited_discount = parse_optional_money(
                                mobile_discount, "Discount"
                            )
                            if edited_discount is not None and edited_discount < 0:
                                raise ValueError("Discount cannot be negative.")
                        except ValueError as exc:
                            st.error(str(exc))
                        else:
                            base_columns = [
                                "Receipt order", "Item", "Product code", "Category",
                                "Qty", "Line total", "Tax marker", "Unit price",
                                "Discount",
                            ]
                            preserved_rows = edited_item_rows.drop(
                                columns=["Delete", "Merge", "Review check"],
                                errors="ignore",
                            ).copy()
                            for column, value in {
                                "Item": edited_name,
                                "Line total": edited_line_total,
                                "Qty": edited_quantity,
                                "Category": mobile_category_value,
                                "Tax marker": mobile_tax_marker,
                                "Unit price": edited_unit_price,
                                "Discount": edited_discount,
                                "Product code": mobile_product_code.strip() or None,
                            }.items():
                                preserved_rows[column] = preserved_rows[column].astype(object)
                                preserved_rows.iloc[
                                    mobile_edit_row,
                                    preserved_rows.columns.get_loc(column),
                                ] = value
                            st.session_state[review_rows_key] = preserved_rows[
                                base_columns
                            ].to_dict(orient="records")
                            st.session_state[f"item_editor_revision_{version}"] = (
                                editor_revision + 1
                            )
                            st.session_state.review_category_notice = (
                                f"Updated row "
                                f"{safe_receipt_order_label(selected_edit_row['Receipt order'], mobile_edit_row)}: "
                                f"{edited_name}, {safe_line_price_label(edited_line_total)}."
                            )
                            st.rerun()
                else:
                    st.caption("Add an item before editing it here.")

            item_add_notice = st.session_state.pop("item_add_notice", None)
            if item_add_notice:
                st.success(plain_markdown(item_add_notice))
            delete_notice = st.session_state.pop("item_delete_notice", None)
            if delete_notice:
                st.success(delete_notice)
            merge_notice = st.session_state.pop("item_merge_notice", None)
            if merge_notice:
                st.success(merge_notice)

            named_rows = edited_item_rows["Item"].fillna("").astype(str)
            valid_name_mask = named_rows.str.strip().ne("") & named_rows.ne("nan")
            # Building this explicitly as a Boolean Series avoids pandas' Arrow
            # string dtype preserving the source dtype through ``Series.map``.
            # On Linux CI that turned True/False results into strings and made
            # the logical ``&`` below fail at runtime.
            gratuity_mask = pd.Series(
                [is_gratuity_item_name(value) for value in named_rows.tolist()],
                index=named_rows.index,
                dtype=bool,
            ) & valid_name_mask
            found_count = int(valid_name_mask.sum())
            purchased_row_count = int((valid_name_mask & ~gratuity_mask).sum())
            expected_count = printed_item_count(
                st.session_state.get("ocr_text", "")
            )
            # Some stores (H-E-B) print a unit count: "3 Ea." counts as 3.
            purchase_rows = edited_item_rows[valid_name_mask & ~gratuity_mask]
            purchased_unit_total = purchased_unit_count(
                purchase_rows["Qty"].tolist() if "Qty" in purchase_rows else []
            )
            count_matches = printed_count_matches(
                expected_count, purchased_row_count, purchased_unit_total
            )

            duplicate_groups = sorted(
                {
                    str(label)
                    for label in duplicate_labels
                    if str(label).strip()
                }
            )
            if duplicate_groups:
                duplicate_icon, duplicate_message = st.columns([1, 5])
                with duplicate_icon:
                    st.image(str(TALLI_COMPARING_RECEIPTS_PATH), width=84)
                with duplicate_message:
                    if (
                        expected_count is not None
                        and count_matches
                    ):
                        st.info(
                            f"{len(duplicate_groups)} repeated-item group"
                            f"{'s' if len(duplicate_groups) != 1 else ''} found, and "
                            "the purchase-row count matches the receipt. These may be "
                            "intentional repeat purchases. Compare the rows before "
                            "deleting or merging anything."
                        )
                    else:
                        st.warning(
                            f"{len(duplicate_groups)} potential duplicate group"
                            f"{'s' if len(duplicate_groups) != 1 else ''} found. "
                            "Compare the grouped rows with the receipt. Check Delete beside "
                            "an extra OCR copy, or check Merge to collapse confirmed OCR "
                            "copies into one row without increasing quantity or price."
                        )

            split_candidates = sum(bool(str(label).strip()) for label in split_labels)
            if split_candidates:
                st.info(
                    f"{split_candidates} possible split-item fragment"
                    f"{'s' if split_candidates != 1 else ''} found. "
                    "Compare each fragment with its suggested original receipt row. "
                    "Check Merge only after confirming the match."
                )

            selected_for_deletion = edited_item_rows["Delete"].fillna(False).astype(bool)
            selected_delete_count = int(selected_for_deletion.sum())
            selected_merge_count = int(
                edited_item_rows["Merge"].fillna(False).astype(bool).sum()
            )
            merge_preview_rows: list[dict[str, object]] | None = None
            merge_ready_preview: list[dict[str, object]] | None = None
            merge_preview_count = 0
            merge_before_subtotal = round(
                pd.to_numeric(
                    edited_item_rows["Line total"], errors="coerce"
                ).fillna(0.0).sum(),
                2,
            )
            merge_after_subtotal = merge_before_subtotal
            merge_would_move_away = False
            if selected_merge_count:
                try:
                    merge_ready_preview = assign_checked_review_merges(
                        edited_item_rows.to_dict(orient="records")
                    )
                    merge_preview_rows, merge_preview_count = (
                        merge_selected_review_rows(merge_ready_preview)
                    )
                    merge_after_subtotal = round(
                        sum(
                            float(row.get("Line total") or 0.0)
                            for row in merge_preview_rows
                        ),
                        2,
                    )
                    st.caption(
                        f"Merge preview: {merge_preview_count} row"
                        f"{'s' if merge_preview_count != 1 else ''} will be absorbed; "
                        f"item subtotal ${merge_before_subtotal:,.2f} -> "
                        f"${merge_after_subtotal:,.2f}. Duplicate OCR copies keep "
                        "one quantity and one line price."
                    )
                    try:
                        printed_subtotal_for_merge = parse_optional_money(
                            str(st.session_state.get(f"subtotal_{version}", "")),
                            "Subtotal",
                        )
                    except ValueError:
                        printed_subtotal_for_merge = None
                    if printed_subtotal_for_merge is not None:
                        before_difference = abs(
                            merge_before_subtotal - printed_subtotal_for_merge
                        )
                        after_difference = abs(
                            merge_after_subtotal - printed_subtotal_for_merge
                        )
                        merge_would_move_away = (
                            after_difference > before_difference + 0.01
                        )
                        if merge_would_move_away:
                            st.warning(
                                "This merge would move the item subtotal farther "
                                "from the printed subtotal. Review the selected rows "
                                "before merging."
                            )
                except ValueError as exc:
                    st.warning(f"Merge preview unavailable: {exc}")
            st.markdown("#### Apply item corrections")
            st.caption(
                "Delete removes an incorrect OCR row. Merge collapses a confirmed "
                "duplicate without double-counting, or joins a split description "
                "while retaining the charged line price."
            )
            delete_action, merge_action = st.columns(2)
            with delete_action:
                delete_clicked = st.button(
                    (
                        f"Delete selected items ({selected_delete_count})"
                        if selected_delete_count
                        else "Delete selected items"
                    ),
                    disabled=selected_delete_count == 0,
                    width="stretch",
                    key=f"delete_items_{version}",
                )
            with merge_action:
                merge_clicked = st.button(
                    (
                        f"Merge selected items ({selected_merge_count})"
                        if selected_merge_count
                        else "Merge selected items"
                    ),
                    disabled=(
                        selected_merge_count == 0 or merge_would_move_away
                    ),
                    width="stretch",
                    key=f"merge_items_{version}",
                )

            if delete_clicked:
                try:
                    kept_rows = edited_item_rows.loc[
                        ~selected_for_deletion
                    ].drop(
                        columns=[
                            "Delete", "Merge", "Review check",
                        ],
                        errors="ignore",
                    )
                    updated = build_edited_receipt(version, kept_rows)
                    printed_fields = {
                        field: st.session_state[f"{field}_{version}"]
                        for field in (
                            "subtotal",
                            "tax",
                            "total",
                            "store",
                            "date",
                            "currency",
                        )
                    }
                    set_active_receipt(
                        updated,
                        image_hash=st.session_state.get("active_image_hash"),
                        extraction_method=active_method,
                        ocr_text=st.session_state.get("ocr_text", ""),
                        line_decisions=st.session_state.get("line_decisions", []),
                        missing_item_candidates=st.session_state.get(
                            "missing_item_candidates", []
                        ),
                    )
                    new_version = st.session_state.receipt_version
                    for field, value in printed_fields.items():
                        st.session_state[f"{field}_{new_version}"] = value
                    st.session_state.item_delete_notice = (
                        f"Deleted {selected_delete_count} selected item"
                        f"{'s' if selected_delete_count != 1 else ''}. "
                        "Review the item count and totals again."
                    )
                    st.rerun()
                except (ValueError, ValidationError) as exc:
                    st.error(str(exc))

            if merge_clicked:
                try:
                    if merge_ready_preview is None or merge_preview_rows is None:
                        merge_ready_preview = assign_checked_review_merges(
                            edited_item_rows.to_dict(orient="records")
                        )
                        merged_rows, merged_count = merge_selected_review_rows(
                            merge_ready_preview
                        )
                    else:
                        merged_rows = merge_preview_rows
                        merged_count = merge_preview_count
                    merged_frame = pd.DataFrame(merged_rows).drop(
                        columns=[
                            "Delete", "Merge", "Merge into row", "Merge mode",
                            "Review check",
                        ],
                        errors="ignore",
                    )
                    updated = build_edited_receipt(version, merged_frame)
                    merged_pairs = [
                        (
                            int(float(row["Receipt order"])),
                            int(float(row["Merge into row"])),
                            str(row.get("Merge mode") or "split"),
                        )
                        for row in merge_ready_preview
                        if row.get("Merge into row") is not None
                    ]
                    action_detail = ", ".join(
                        f"row {source} -> row {target} ({mode})"
                        for source, target, mode in merged_pairs
                    )
                    updated.notes.append(
                        "Review merge: "
                        f"{action_detail}; item subtotal "
                        f"${merge_before_subtotal:,.2f} -> "
                        f"${merge_after_subtotal:,.2f}."
                    )
                    printed_fields = {
                        field: st.session_state[f"{field}_{version}"]
                        for field in (
                            "subtotal", "tax", "total", "store", "date", "currency"
                        )
                    }
                    set_active_receipt(
                        updated,
                        image_hash=st.session_state.get("active_image_hash"),
                        extraction_method=active_method,
                        ocr_text=st.session_state.get("ocr_text", ""),
                        line_decisions=st.session_state.get("line_decisions", []),
                        missing_item_candidates=st.session_state.get(
                            "missing_item_candidates", []
                        ),
                    )
                    new_version = st.session_state.receipt_version
                    for field, value in printed_fields.items():
                        st.session_state[f"{field}_{new_version}"] = value
                    st.session_state.item_merge_notice = (
                        f"Merged {merged_count} selected item"
                        f"{'s' if merged_count != 1 else ''}. "
                        f"Item subtotal changed from ${merge_before_subtotal:,.2f} "
                        f"to ${merge_after_subtotal:,.2f}. Review the combined names, "
                        "quantities, unit prices, and line prices."
                    )
                    st.rerun()
                except (ValueError, ValidationError) as exc:
                    st.error(str(exc))

            st.metric(
                "Charge rows entered" if gratuity_mask.any() else "Item rows entered",
                found_count,
            )
            if gratuity_mask.any():
                st.caption(
                    f"{purchased_row_count} purchase/service row"
                    f"{'s' if purchased_row_count != 1 else ''} + "
                    f"{int(gratuity_mask.sum())} tip/gratuity row"
                    f"{'s' if int(gratuity_mask.sum()) != 1 else ''}."
                )
            if expected_count is not None:
                st.caption(f"Receipt says: {expected_count} items purchased.")
                if not count_matches:
                    st.warning(
                        f"Count differs: {purchased_row_count} purchase rows "
                        f"({purchased_unit_total} units) entered; receipt lists "
                        f"{expected_count}. Check missing or combined items and quantities."
                    )
            else:
                st.caption("Printed item count was not identified. Check the number of purchased rows against the receipt.")
            if purchased_row_count and (
                expected_count is None or not count_matches
            ):
                with st.expander(
                    "Split a combined OCR item",
                    expanded=(
                        expected_count is not None
                        and purchased_row_count < expected_count
                    ),
                ):
                    st.caption(
                        "If OCR joined two purchases in one row, compare the receipt photo "
                        "and enter both names and charged line prices. Nothing is saved until "
                        "you verify the receipt."
                    )
                    split_row = st.selectbox(
                        "Combined item row",
                        list(range(len(edited_item_rows))),
                        format_func=lambda index: (
                            f"Row {safe_receipt_order_label(edited_item_rows.iloc[index]['Receipt order'], index)}: "
                            f"{edited_item_rows.iloc[index]['Item']}"
                        ),
                        key=f"split_row_{version}_{editor_revision}",
                    )
                    selected_combined = edited_item_rows.iloc[split_row]
                    st.caption(
                        f"Current OCR row: {selected_combined['Item']} — "
                        f"line price {safe_line_price_label(selected_combined['Line total'])}"
                    )
                    with st.form(f"split_combined_form_{version}_{editor_revision}_{split_row}"):
                        first_name = st.text_input("First purchased item name")
                        first_price = st.text_input("First charged line price")
                        second_name = st.text_input("Second purchased item name")
                        second_price = st.text_input("Second charged line price")
                        split_confirmed = st.form_submit_button(
                            "Replace combined row with two items"
                        )
                    if split_confirmed:
                        try:
                            names = [first_name.strip(), second_name.strip()]
                            if not all(names) or names[0].casefold() == names[1].casefold():
                                raise ValueError("Enter two different purchased item names.")
                            prices = [
                                parse_optional_money(first_price, "First line price"),
                                parse_optional_money(second_price, "Second line price"),
                            ]
                            if any(price is None or price < 0 for price in prices):
                                raise ValueError("Enter both charged line prices (zero is allowed).")
                            if expected_count is not None and purchased_row_count + 1 > expected_count:
                                raise ValueError(
                                    "Splitting this row would exceed the printed item count."
                                )
                            preserved = edited_item_rows.drop(
                                columns=["Delete", "Merge", "Review check"],
                                errors="ignore",
                            ).copy()
                            original = preserved.iloc[split_row].copy()
                            first = original.copy()
                            second = original.copy()
                            first["Item"], second["Item"] = names
                            first["Line total"], second["Line total"] = prices
                            for row in (first, second):
                                row["Qty"] = 1.0
                                row["Unit price"] = row["Line total"]
                                row["Discount"] = None
                                row["Product code"] = None
                                row["Tax marker"] = None
                            preserved = pd.concat(
                                [
                                    preserved.iloc[:split_row],
                                    pd.DataFrame([first, second]),
                                    preserved.iloc[split_row + 1:],
                                ],
                                ignore_index=True,
                            )
                            preserved["Receipt order"] = range(1, len(preserved) + 1)
                            updated = build_edited_receipt(version, preserved)
                            printed_fields = {
                                field: st.session_state.get(f"{field}_{version}", "")
                                for field in (
                                    "subtotal", "tax", "total", "store", "date", "currency"
                                )
                            }
                            set_active_receipt(
                                updated,
                                image_hash=st.session_state.get("active_image_hash"),
                                extraction_method=active_method,
                                ocr_text=st.session_state.get("ocr_text", ""),
                                line_decisions=st.session_state.get("line_decisions", []),
                                missing_item_candidates=st.session_state.get(
                                    "missing_item_candidates", []
                                ),
                            )
                            new_version = st.session_state.receipt_version
                            for field, value in printed_fields.items():
                                st.session_state[f"{field}_{new_version}"] = value
                            st.session_state.item_add_notice = (
                                "Split the combined row. Check both item names, tax markers, "
                                "prices, printed subtotal, and receipt count before saving."
                            )
                            st.rerun()
                        except (ValueError, ValidationError) as exc:
                            st.error(str(exc))

            render_original_receipt_viewer(
                key=f"review_original_receipt_{version}"
            )

            add_selected_candidates = False
            selected_candidate_rows = pd.DataFrame()
            add_missing = False
            missing_candidates = st.session_state.get("missing_item_candidates", [])
            missing_source_lines = {
                int(candidate["source_line"])
                for candidate in missing_candidates
            }
            unused_ocr_candidates = ocr_line_recovery_candidates(
                st.session_state.get("line_decisions", []),
                excluded_lines=missing_source_lines,
                store_name=active.get("store_name"),
            )
            entered_item_names = {
                " ".join(str(name).casefold().split())
                for name in edited_item_rows["Item"].fillna("")
                if str(name).strip() and str(name) != "nan"
            }
            already_recovered = {
                int(line) for line in st.session_state.get("recovered_source_lines", [])
            }

            def _already_entered(candidate: dict[str, object]) -> bool:
                # A row with a UPC is hidden only once it was added: the same
                # product can be bought twice (two FRENCH BREAD rows, RL-38).
                if candidate.get("product_code"):
                    return False
                name = " ".join(str(candidate["name"]).casefold().split())
                return name in entered_item_names

            unused_ocr_candidates = [
                candidate
                for candidate in unused_ocr_candidates
                if int(candidate["source_line"]) not in already_recovered
                and not _already_entered(candidate)
            ]
            count_shortfall = (
                expected_count is not None
                and not count_matches
                and max(purchased_row_count, purchased_unit_total) < expected_count
            )
            current_printed_subtotal = st.session_state.get(
                f"subtotal_{version}", active.get("subtotal")
            )
            try:
                current_printed_subtotal = float(current_printed_subtotal)
            except (TypeError, ValueError):
                current_printed_subtotal = None
            current_purchase_subtotal = round(
                float(
                    pd.to_numeric(
                        edited_item_rows.loc[
                            ~edited_item_rows["Item"].fillna("").astype(str).map(
                                is_gratuity_item_name
                            ),
                            "Line total",
                        ],
                        errors="coerce",
                    ).sum()
                ),
                2,
            )
            subtotal_candidate_review = (
                expected_count is None
                and bool(missing_candidates)
                and current_printed_subtotal is not None
                and current_printed_subtotal - current_purchase_subtotal > 0.02
            )
            active_missing_candidates = (
                missing_candidates
                if count_shortfall or subtotal_candidate_review
                else []
            )
            candidates = [
                clean_candidate(candidate, active.get("store_name"))
                for candidate in [*active_missing_candidates, *unused_ocr_candidates]
            ]
            if count_shortfall or subtotal_candidate_review or unused_ocr_candidates:
                with st.container(border=True):
                    if count_shortfall:
                        recovery_heading = (
                            f"Recover missing items ({expected_count - max(purchased_row_count, purchased_unit_total)} remaining)"
                        )
                    elif subtotal_candidate_review:
                        recovery_heading = "Review possible missing items"
                    else:
                        recovery_heading = "Review unused OCR lines"
                    st.subheader(recovery_heading)
                    if count_shortfall or subtotal_candidate_review:
                        st.warning(
                            "The printed count or subtotal indicates possible missing "
                            "items. Compare the photo with the table and recover a "
                            "purchase that is missing."
                        )
                    else:
                        st.info(
                            "These product-like OCR lines were not used as item rows. "
                            "Use this list when the parser selected the wrong text or "
                            "left a real purchase out."
                        )
                    if candidates:
                        st.info(
                            "Select the OCR lines that are purchased items. Correct the "
                            "name, quantity, line price, and tax marker before adding them."
                        )
                        candidate_rows = pd.DataFrame(
                            [
                                {
                                    "Add": False,
                                    "OCR line": candidate["source_line"],
                                    "Item name": candidate["name"],
                                    "Category": suggest_category(
                                        DATABASE_PATH,
                                        item_name=candidate["name"],
                                        store_name=active.get("store_name", ""),
                                    ),
                                    "Qty": candidate.get("quantity", 1.0),
                                    "Unit price": candidate.get("unit_price"),
                                    "Line price": candidate.get("line_total"),
                                    "Tax marker": candidate.get("tax_marker") or "",
                                    "Supporting OCR": candidate.get("supporting_text", ""),
                                }
                                for candidate in candidates
                            ]
                        )
                        selected_candidate_rows = st.data_editor(
                            candidate_rows,
                            key=f"candidate_items_{version}",
                            hide_index=True,
                            width="stretch",
                            disabled=["OCR line", "Supporting OCR"],
                            column_config={
                                "Add": st.column_config.CheckboxColumn(
                                    "Add item",
                                    help="Select this OCR line after comparing it with the receipt photo.",
                                ),
                                "OCR line": st.column_config.NumberColumn("Line"),
                                "Item name": st.column_config.TextColumn(
                                    "Item name", required=True, width="medium"
                                ),
                                "Category": st.column_config.SelectboxColumn(
                                    "Expense category",
                                    options=item_category_options,
                                    required=True,
                                ),
                                "Qty": st.column_config.NumberColumn(
                                    "Quantity", min_value=0.001, format="%.3f"
                                ),
                                "Line price": st.column_config.NumberColumn(
                                    "Line price", min_value=0.0, format="$%.2f"
                                ),
                                "Unit price": st.column_config.NumberColumn(
                                    "Unit price", min_value=0.0, format="$%.2f"
                                ),
                                "Tax marker": st.column_config.SelectboxColumn(
                                    "Tax marker", options=["", *TAX_MARKERS]
                                ),
                                "Supporting OCR": st.column_config.TextColumn(
                                    "Nearby weight/price text", width="large"
                                ),
                            },
                        )
                        add_selected_candidates = st.button(
                            "Add selected OCR lines as items",
                            type="primary",
                        )
                        if add_selected_candidates:
                            # Show the problem beside the table where it must be fixed.
                            candidate_error = selected_candidate_error(
                                selected_candidate_rows.to_dict(orient="records"),
                                expected_count,
                                purchased_row_count,
                            )
                            if candidate_error:
                                st.error(candidate_error)
                                add_selected_candidates = False
                        st.caption(
                            "A nearby weighted-price line may be prefilled. Blank prices "
                            "were not readable and must be entered by the user."
                        )
                        if expected_count is not None and purchased_row_count >= expected_count:
                            st.caption(
                                "If the table already has the printed number of items, "
                                "delete the incorrect row first, then add the correct OCR "
                                "line here."
                            )
                    else:
                        st.caption("The count alone cannot identify the missing product or explain why it was missed. OCR may not have recognized the text, or the parser may not have linked its description and price. No name or price is guessed.")

                    st.caption(
                        "If the correct purchase is not listed, use Add an item "
                        "manually below."
                    )

            manual_form_prefix = f"manual_item_{version}"
            if st.session_state.pop(
                f"{manual_form_prefix}_reset_after_add", False
            ):
                st.session_state[f"{manual_form_prefix}_name"] = ""
                st.session_state[f"{manual_form_prefix}_quantity"] = 1.0
                st.session_state[f"{manual_form_prefix}_price"] = ""
                st.session_state[f"{manual_form_prefix}_marker"] = "Unknown"
                st.session_state[f"{manual_form_prefix}_category"] = (
                    "Uncategorized"
                    if "Uncategorized" in item_category_options
                    else item_category_options[0]
                )

            with st.expander(
                "Add an item manually", expanded=purchased_row_count == 0
            ):
                st.caption(
                    "Use this whenever OCR misses an item, finds only part of it, "
                    "or leaves its price blank. Existing table edits are preserved."
                )
                with st.form(manual_form_prefix):
                    manual_name = st.text_input(
                        "Manual item name",
                        key=f"{manual_form_prefix}_name",
                    )
                    manual_quantity = st.number_input(
                        "Manual item quantity",
                        min_value=0.001,
                        value=1.0,
                        step=1.0,
                        key=f"{manual_form_prefix}_quantity",
                    )
                    manual_price = st.text_input(
                        "Manual item line price",
                        placeholder="Amount charged for this entire row, e.g. 5.97",
                        key=f"{manual_form_prefix}_price",
                    )
                    manual_marker = st.selectbox(
                        "Manual item tax marker (optional)",
                        ["Unknown", *TAX_MARKERS],
                        key=f"{manual_form_prefix}_marker",
                    )
                    manual_category = st.selectbox(
                        "Manual item expense category",
                        item_category_options,
                        key=f"{manual_form_prefix}_category",
                    )
                    add_manual_item = st.form_submit_button("Add item manually")
                st.caption(
                    "A zero-dollar reward or promotional item is valid. Adding an "
                    "item updates the review table but does not save the receipt."
                )

            st.subheader("2. Calculate the item subtotal")
            amounts = pd.to_numeric(edited_item_rows["Line total"], errors="coerce")
            line_sum = round(float(amounts.sum()), 2)
            gratuity_sum = round(float(amounts[gratuity_mask].sum()), 2)
            purchase_subtotal = round(line_sum - gratuity_sum, 2)
            st.metric(
                "Calculated purchase/service subtotal",
                f"{purchase_subtotal:,.2f}",
            )
            if gratuity_sum:
                st.metric("Detected tip / gratuity", f"{gratuity_sum:,.2f}")
                st.caption(
                    "The tip remains part of the final expense but is excluded from "
                    "the merchant's printed purchase/service subtotal comparison."
                )
            st.caption("Quantity is already included in each line price. Do not multiply it again. Complete blank prices before saving.")
            marked_taxable = round(float(amounts[edited_item_rows["Tax marker"].isin(["T", "X"])].sum()), 2)
            marked_untaxed = round(float(amounts[edited_item_rows["Tax marker"].isin(["F", "FW", "N"])].sum()), 2)
            st.caption(f"Marked taxed (T/X): {marked_taxable:.2f} · Marked not taxed (F/FW/N): {marked_untaxed:.2f}. Marker meanings vary by store. Blank means unknown. Tax is still read from the receipt; no rate is assumed.")
            category_amounts = pd.DataFrame(
                {
                    "Category": edited_item_rows["Category"].fillna(
                        "Uncategorized"
                    ),
                    "Amount": amounts.fillna(0.0),
                }
            )
            category_amounts["Category"] = category_amounts["Category"].replace(
                "", "Uncategorized"
            )
            category_summary = (
                category_amounts.groupby("Category", as_index=False)["Amount"]
                .sum()
                .sort_values(["Amount", "Category"], ascending=[False, True])
            )
            with st.expander("Category breakdown for this receipt"):
                st.dataframe(
                    category_summary,
                    hide_index=True,
                    width="stretch",
                    column_config={
                        "Category": "Expense category",
                        "Amount": st.column_config.NumberColumn(
                            "Amount", format="$%.2f"
                        ),
                    },
                )
            st.subheader("3. Check the amounts printed on the receipt")
            st.info("Correct the printed subtotal, sales tax, and total below. Sales tax is a money amount, not a percentage (for example, enter 0.49 for $0.49). Enter 0.00 for tax only when the receipt has no tax.")
            inferred_notes = " ".join(active.get("notes", []))
            totals = st.columns(3)
            for column, field, label in zip(totals, ("subtotal", "tax", "total"), ("Printed subtotal", "Printed sales tax", "Printed total")):
                value = active.get(field)
                if (field == "subtotal" and "Subtotal was inferred" in inferred_notes) or (field == "total" and "Total was inferred" in inferred_notes):
                    value = None
                with column:
                    st.text_input(label, key=f"{field}_{version}", **({} if f"{field}_{version}" in st.session_state else {"value": money_text(value)}))
            with st.expander("Store, date, and currency (optional details)"):
                field_left, field_right = st.columns(2)
                with field_left:
                    merchants = list_merchants(DATABASE_PATH)
                    current_name = st.session_state.get(f"store_{version}", active.get("store_name", ""))
                    match = next((name for name in merchants if " ".join(name.casefold().split()) == " ".join(current_name.casefold().split())), None)
                    if match:
                        st.caption(f"Merchant found in saved receipts: {match}")
                    elif merchants:
                        st.caption("New or unrecognized merchant. Choose a receipt suggestion, a saved merchant, or edit the name below.")

                    receipt_merchant_suggestions = merchant_line_suggestions(
                        st.session_state.get("line_decisions", []),
                        merchants,
                    )
                    if receipt_merchant_suggestions:
                        def use_receipt_merchant():
                            choice = st.session_state[
                                f"receipt_merchant_choice_{version}"
                            ]
                            if choice:
                                st.session_state[f"store_{version}"] = choice

                        st.selectbox(
                            "Merchant suggestions from receipt",
                            [None, *receipt_merchant_suggestions],
                            format_func=lambda name: (
                                name or "Keep the current merchant name"
                            ),
                            key=f"receipt_merchant_choice_{version}",
                            on_change=use_receipt_merchant,
                            help=(
                                "TalliUpper ranks known merchant names, business-type "
                                "words, and likely store numbers. Choose a line only "
                                "after comparing it with the receipt."
                            ),
                        )

                    def use_saved_merchant():
                        choice = st.session_state[f"merchant_choice_{version}"]
                        if choice:
                            st.session_state[f"store_{version}"] = choice
                    st.selectbox("Use a saved merchant", [None, *merchants],
                                 format_func=lambda name: name or "Keep or enter a merchant name",
                                 key=f"merchant_choice_{version}", on_change=use_saved_merchant)
                    st.session_state.setdefault(
                        f"store_{version}", active.get("store_name", "")
                    )
                    st.text_input(
                        "Store name",
                        key=f"store_{version}",
                        help="Include a location or store number if useful, for example Rockler — Austin — Store #123.",
                    )
                    st.text_input(
                        "Transaction date (YYYY-MM-DD)",
                        value=active.get("transaction_date") or "",
                        key=f"date_{version}",
                    )
                with field_right:
                    st.text_input(
                        "Currency",
                        value=active.get("currency", "USD"),
                        key=f"currency_{version}",
                    )
            if add_selected_candidates:
                try:
                    selected = selected_candidate_rows[
                        selected_candidate_rows["Add"].fillna(False)
                    ]
                    if selected.empty:
                        raise ValueError("Select at least one OCR line to add.")
                    if expected_count is not None and purchased_row_count + len(selected) > expected_count:
                        raise ValueError(
                            "Too many OCR lines are selected for the printed item count."
                        )
                    updated = build_edited_receipt(version, edited_item_rows)
                    selected_lines: set[int] = set()
                    recovered_lines = [
                        int(line)
                        for line in st.session_state.get("recovered_source_lines", [])
                    ]
                    candidate_codes = {
                        int(candidate["source_line"]): candidate.get("product_code")
                        for candidate in candidates
                    }
                    selected_records = sorted(
                        selected.to_dict(orient="records"),
                        key=lambda record: int(record["OCR line"]),
                    )
                    for row in selected_records:
                        name = str(row.get("Item name", "") or "").strip()
                        if not name or name == "nan":
                            raise ValueError("Every selected OCR line needs an item name.")
                        price = row.get("Line price")
                        if pd.isna(price):
                            raise ValueError(
                                f"Enter the line price for selected item {name}."
                            )
                        quantity = row.get("Qty", 1.0)
                        if pd.isna(quantity) or float(quantity) <= 0:
                            raise ValueError(
                                f"Enter a positive quantity for selected item {name}."
                            )
                        marker = row.get("Tax marker")
                        category = str(
                            row.get("Category", "") or "Uncategorized"
                        )
                        unit_price = row.get("Unit price")
                        source_line = int(row["OCR line"])
                        # Place the row where it was printed, not at the end.
                        position = printed_position(
                            source_line,
                            st.session_state.get("line_decisions", []),
                            recovered_lines,
                            len(updated.items),
                        )
                        updated.items.insert(
                            position,
                            ReceiptItem(
                                name=name,
                                product_code=candidate_codes.get(source_line),
                                category=category,
                                quantity=float(quantity),
                                line_total=float(price),
                                unit_price=(
                                    None
                                    if pd.isna(unit_price)
                                    else float(unit_price)
                                ),
                                discount=None,
                                tax_marker=marker if marker in TAX_MARKERS else None,
                            ),
                        )
                        recovered_lines.append(source_line)
                        selected_lines.add(source_line)
                    fields = {
                        field: st.session_state[f"{field}_{version}"]
                        for field in ("subtotal", "tax", "total")
                    }
                    remaining_candidates = [
                        candidate
                        for candidate in st.session_state.get(
                            "missing_item_candidates", []
                        )
                        if int(candidate["source_line"]) not in selected_lines
                    ]
                    set_active_receipt(
                        updated,
                        image_hash=st.session_state.get("active_image_hash"),
                        extraction_method=active_method,
                        ocr_text=st.session_state.get("ocr_text", ""),
                        line_decisions=st.session_state.get("line_decisions", []),
                        missing_item_candidates=remaining_candidates,
                        recovered_source_lines=recovered_lines,
                    )
                    for field, value in fields.items():
                        st.session_state[
                            f"{field}_{st.session_state.receipt_version}"
                        ] = value
                    st.rerun()
                except (ValueError, ValidationError) as exc:
                    st.error(str(exc))
            elif add_manual_item:
                try:
                    if not manual_name.strip():
                        raise ValueError("Enter the manual item's name.")
                    manual_amount = parse_optional_money(
                        manual_price, "Manual item line price"
                    )
                    if manual_amount is None:
                        raise ValueError("Enter the manual item's line price.")

                    base_columns = [
                        "Receipt order", "Item", "Product code", "Category",
                        "Qty", "Line total", "Tax marker", "Unit price",
                        "Discount",
                    ]
                    preserved_rows = edited_item_rows.drop(
                        columns=["Delete", "Merge", "Review check"],
                        errors="ignore",
                    ).copy()
                    receipt_orders = pd.to_numeric(
                        preserved_rows.get("Receipt order"), errors="coerce"
                    )
                    next_receipt_order = (
                        int(receipt_orders.max()) + 1
                        if receipt_orders.notna().any()
                        else 1
                    )
                    manual_row = {
                        "Receipt order": next_receipt_order,
                        "Item": manual_name.strip(),
                        "Product code": None,
                        "Category": manual_category,
                        "Qty": float(manual_quantity),
                        "Line total": manual_amount,
                        "Tax marker": (
                            None if manual_marker == "Unknown" else manual_marker
                        ),
                        "Unit price": None,
                        "Discount": None,
                    }
                    preserved_rows = pd.concat(
                        [preserved_rows, pd.DataFrame([manual_row])],
                        ignore_index=True,
                    )
                    for column in base_columns:
                        if column not in preserved_rows:
                            preserved_rows[column] = None
                    st.session_state[review_rows_key] = preserved_rows[
                        base_columns
                    ].to_dict(orient="records")
                    st.session_state[f"item_editor_revision_{version}"] = (
                        editor_revision + 1
                    )
                    currency = (
                        st.session_state.get(
                            f"currency_{version}", active.get("currency", "USD")
                        ).strip()
                        or "USD"
                    )
                    st.session_state.item_add_notice = (
                        f"Added {manual_name.strip()} — quantity "
                        f"{float(manual_quantity):g} — {currency} "
                        f"{manual_amount:.2f}. The entry form is ready for the next item."
                    )
                    st.session_state[
                        f"{manual_form_prefix}_reset_after_add"
                    ] = True
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
            st.subheader("4. Compare calculated and printed amounts")
            corrected = None
            review_error = None
            try:
                corrected = build_edited_receipt(version, edited_item_rows)
                if not corrected.items:
                    raise ValueError("Add at least one item in step 1.")
                if any(value is None for value in (corrected.subtotal, corrected.tax, corrected.total)):
                    raise ValueError("Complete all three printed amounts in step 3.")
            except (ValueError, ValidationError) as exc:
                review_error = str(exc)
            mismatch = False
            if review_error:
                st.warning(review_error)
            else:
                calculated_total = round(
                    purchase_subtotal + gratuity_sum + corrected.tax, 2
                )
                savings = printed_order_savings(corrected.notes)
                expected_purchases = round(
                    corrected.subtotal - savings, 2
                ) if savings is not None else corrected.subtotal
                subtotal_difference = round(
                    purchase_subtotal - expected_purchases, 2
                )
                total_difference = round(calculated_total - corrected.total, 2)
                amount_status = amount_check_status(
                    subtotal_difference, total_difference
                )
                amounts_differ = amount_status == "differ"
                count_differs = not count_matches
                mismatch = amounts_differ or count_differs
                if savings is not None:
                    st.caption(
                        f"Printed subtotal ${corrected.subtotal:,.2f} "
                        f"− printed savings ${savings:,.2f} = "
                        f"expected purchases ${expected_purchases:,.2f}. "
                        "Compare corrected line prices with the after-savings amount."
                    )
                if marked_taxable > 0 and corrected.tax:
                    # Informational only: the rate printed on the receipt is
                    # the authority, so no rate is assumed or enforced here.
                    effective_rate = corrected.tax / marked_taxable * 100
                    st.caption(
                        f"Tax check: the printed tax ${corrected.tax:,.2f} is "
                        f"{effective_rate:.2f}% of the ${marked_taxable:,.2f} marked "
                        "taxed (T/X). If that matches the rate printed on the "
                        "receipt, the tax markers are consistent."
                    )
                comparison_rows = [
                    {
                        "Check": (
                            "Purchases after printed savings"
                            if savings is not None else "Purchase/service subtotal"
                        ),
                        "Calculated": purchase_subtotal,
                        "Printed": expected_purchases,
                        "Difference": subtotal_difference,
                    },
                ]
                if gratuity_sum:
                    comparison_rows.append(
                        {
                            "Check": "Tip / gratuity",
                            "Calculated": gratuity_sum,
                            "Printed": gratuity_sum,
                            "Difference": 0.0,
                        }
                    )
                comparison_rows.append(
                    {
                        "Check": "Purchases + gratuity + printed sales tax",
                        "Calculated": calculated_total,
                        "Printed": corrected.total,
                        "Difference": total_difference,
                    }
                )
                printable_comparison = pd.DataFrame(comparison_rows).copy()
                for column in ("Calculated", "Printed", "Difference"):
                    printable_comparison[column] = printable_comparison[column].map(
                        lambda value: f"{float(value):.2f}"
                    )
                comparison_html = printable_comparison.to_html(
                    index=False,
                    border=0,
                    classes="receipt-comparison-table",
                )
                st.markdown(
                    comparison_html,
                    unsafe_allow_html=True,
                )
                if amounts_differ:
                    st.warning("Amounts differ. Recheck missing items, quantities, prices, discounts, and tax. Acknowledge any remaining difference in step 5.")
                elif count_differs:
                    st.warning(
                        f"Amounts match, but the item count differs: {purchased_row_count} rows "
                        f"({purchased_unit_total} units) entered; receipt lists {expected_count}. "
                        "Check quantities and combined rows, or acknowledge the difference in step 5."
                    )
                elif amount_status == "near":
                    st.warning(
                        "Amounts are within 2 cents but not exact. A small difference "
                        "like this is often one misread digit, especially when the "
                        "receipt prints tax on several lines. Compare the printed "
                        "subtotal, each tax line, and the total with the photo before saving."
                    )
                else:
                    st.success("Amounts match. Verify the item details against the receipt before saving.")

            st.subheader("Print or download this review before saving")
            st.caption(
                "Use this report to compare the current edited item list with the "
                "original receipt. Open the downloaded file and select Print this list."
            )
            if corrected is not None:
                review_report = build_review_report_html(
                    corrected,
                    expected_count=expected_count,
                )
                st.download_button(
                    "Print review list",
                    data=review_report,
                    file_name=review_report_filename(corrected),
                    mime="text/html",
                    on_click="ignore",
                    width="stretch",
                    key=f"print_review_{version}",
                )
            else:
                st.button(
                    "Print review list",
                    disabled=True,
                    width="stretch",
                    key=f"print_review_disabled_{version}",
                )
                st.caption("Complete the item rows before creating the review report.")

            st.subheader("5. Verify and save")
            save_guidance = st.empty()
            fingerprint = hashlib.sha256((edited_item_rows.to_json() + str([
                st.session_state.get(f"{field}_{version}") for field in ("subtotal", "tax", "total", "store", "date", "currency")
            ]) + str(image_hash)).encode()).hexdigest()
            if st.session_state.get("review_fingerprint") != fingerprint:
                st.session_state[f"difference_{version}"] = False
                st.session_state.review_fingerprint = fingerprint

            stale_image = current_receipt_source_changed(
                image_hash,
                st.session_state.get("active_image_hash"),
                valid_source_files,
                st.session_state.get("active_source_files", []),
            )
            duplicate_matches = (
                find_duplicate_receipts(
                    DATABASE_PATH,
                    corrected,
                    st.session_state.get("active_image_hash"),
                )
                if corrected and not stale_image
                else []
            )
            exact_duplicate = next(
                (match for match in duplicate_matches if match["exact"]),
                None,
            )
            just_saved_id = (
                int(st.session_state["last_saved_receipt_id"])
                if (
                    st.session_state.get("last_saved_fingerprint")
                    == fingerprint
                    and st.session_state.get("last_saved_receipt_id")
                    is not None
                )
                else None
            )
            if just_saved_id is not None and get_receipt(
                DATABASE_PATH, just_saved_id
            ) is None:
                # RL-44: the receipt saved from this review was deleted later.
                # Show the review as unsaved again instead of "Saved as RL-...".
                forget_deleted_receipt(just_saved_id)
                just_saved_id = None
            if just_saved_id is not None:
                saved_reference = f"RL-{just_saved_id:06d}"
                st.session_state["saved_receipt_selector"] = just_saved_id
                save_guidance.success(
                    f"Saved successfully as {saved_reference}."
                )
                st.info(
                    "Open Saved receipts to review the stored record, or choose "
                    "a new image to scan another receipt."
                )
            elif exact_duplicate is not None:
                existing_id = int(exact_duplicate["id"])
                existing_reference = f"RL-{existing_id:06d}"
                st.session_state["saved_receipt_selector"] = existing_id
                save_guidance.success(
                    f"Already saved as {existing_reference}."
                )
                st.info(
                    "No additional copy will be created. If you corrected this "
                    "review, update the existing saved receipt below."
                )
                update_notice = st.session_state.pop(
                    "receipt_update_notice", None
                )
                if update_notice:
                    st.success(update_notice)
                update_ready = not review_error and not mismatch and corrected is not None
                st.caption(
                    "Updating preserves the receipt number and any archived original "
                    "files while replacing its reviewed fields and item rows."
                )
                if st.button(
                    "Update saved receipt",
                    type="primary",
                    disabled=not update_ready,
                    width="stretch",
                    key=f"update_saved_{existing_id}_{version}",
                ):
                    corrected.notes = list(corrected.notes) + [
                        "User updated the existing saved receipt after review."
                    ]
                    update_receipt(DATABASE_PATH, existing_id, corrected)
                    st.session_state["receipt_update_notice"] = (
                        f"Updated {existing_reference} with the reviewed corrections."
                    )
                    st.session_state["saved_receipt_selector"] = existing_id
                    st.rerun()
                if not update_ready:
                    st.caption(
                        "Correct the remaining amount or item differences before "
                        "updating the saved receipt."
                    )
                if st.button(
                    "Start another receipt",
                    width="stretch",
                    key=f"start_another_duplicate_{existing_id}_{version}",
                ):
                    clear_current_receipt()
                    st.rerun()
            else:
                unknown_markers = edited_item_rows.loc[
                    ~edited_item_rows["Tax marker"].isin(TAX_MARKERS)
                    & ~gratuity_mask,
                    "Item",
                ].dropna().astype(str)
                unknown_markers = unknown_markers[
                    unknown_markers.str.strip() != ""
                ]
                if not unknown_markers.empty:
                    st.warning(
                        "Tax markers need review for: "
                        + ", ".join(unknown_markers)
                        + ". Check the receipt and fill in any printed markers. "
                        "Leave a marker unknown if none is readable; matching totals "
                        "do not verify these fields."
                    )

                st.info(
                    "Not saved yet. Check every item name, quantity, line price, tax "
                    "marker, and printed amount. When everything is correct, the "
                    "button below will verify and save the receipt in one step."
                )
                accepted_difference = False
                if mismatch:
                    accepted_difference = st.checkbox(
                        "I reviewed the remaining differences and want to save "
                        "these amounts as entered.",
                        key=f"difference_{version}",
                    )
                available_source_files = st.session_state.get(
                    "active_source_files", []
                )
                save_original_files = st.checkbox(
                    "Save a local copy of the original receipt files",
                    value=False,
                    disabled=not available_source_files,
                    key=f"save_originals_{version}",
                    help=(
                        "Stores the original upload beside the local TalliUpper "
                        "database so you can verify or download it later."
                    ),
                )
                if available_source_files:
                    st.caption(
                        f"{len(available_source_files)} original file(s) available. "
                        "They remain on this computer and are not uploaded by this option."
                    )
                else:
                    st.caption(
                        "No original file is available for this manual entry. Existing "
                        "saved receipts are not backfilled automatically."
                    )
                stale_image = current_receipt_source_changed(
                    image_hash,
                    st.session_state.get("active_image_hash"),
                    valid_source_files,
                    st.session_state.get("active_source_files", []),
                )
                if stale_image:
                    st.warning(
                        "The image changed. Read it or start manual entry again "
                        "before saving."
                    )
                ready = (
                    not review_error
                    and (not mismatch or accepted_difference)
                    and not stale_image
                )
                if stale_image:
                    save_guidance.info(
                        "Next step: read the changed receipt image, or choose Enter "
                        "manually, before verifying and saving."
                    )
                elif review_error:
                    save_guidance.info("Before you can save: " + review_error)
                elif mismatch and not accepted_difference:
                    save_guidance.info(
                        "Next step: correct the differences in steps 1-4, or check "
                        "the remaining-differences checkbox below to acknowledge "
                        "them before saving."
                    )
                else:
                    save_guidance.info(
                        "Final step: review the information above, then click Verify "
                        "and save receipt. That single action confirms your review "
                        "and saves the receipt."
                    )

                allow_duplicate = False
                if duplicate_matches:
                    duplicate_ids = ", ".join(
                        f"RL-{int(match['id']):06d}"
                        for match in duplicate_matches
                    )
                    st.warning(
                        f"Possible duplicate: {duplicate_ids} has the same store, "
                        "date, currency, and total. Check Saved receipts before "
                        "continuing."
                    )
                    allow_duplicate = st.checkbox(
                        "This is a separate purchase. Save it as another receipt.",
                        key=f"duplicate_{fingerprint}_{duplicate_ids}",
                    )
                    ready = ready and allow_duplicate

                if st.button(
                    "Verify and save receipt",
                    type="primary",
                    width="stretch",
                    disabled=not ready,
                ):
                    corrected.notes = list(corrected.notes) + [
                        "User verified item details and printed amounts."
                    ]
                    if allow_duplicate:
                        corrected.notes.append(
                            "User confirmed this is a separate purchase despite "
                            "matching store, date, currency, and total."
                        )
                    if mismatch:
                        corrected.notes.append(
                            "User acknowledged differences between calculated and "
                            "printed amounts."
                        )
                    try:
                        archived_filename = " + ".join(
                            str(source.get("filename") or "receipt source")
                            for source in available_source_files
                        )
                        receipt_id = save_receipt(
                            DATABASE_PATH,
                            corrected,
                            source_filename=archived_filename or source_filename,
                            image_sha256=st.session_state.get(
                                "active_image_hash"
                            ),
                            extraction_method=active_method,
                            allow_possible_duplicate=allow_duplicate,
                            source_files=(
                                available_source_files
                                if save_original_files
                                else None
                            ),
                            source_archive_dir=SOURCE_ARCHIVE_DIR,
                        )
                        archive_note = (
                            f" Original files saved: "
                            f"{len(available_source_files)}."
                            if save_original_files
                            else ""
                        )
                        receipt_reference = f"RL-{receipt_id:06d}"
                        st.session_state["last_saved_fingerprint"] = fingerprint
                        st.session_state["last_saved_receipt_id"] = receipt_id
                        st.session_state["saved_receipt_selector"] = receipt_id
                        st.session_state["saved_receipt_notice"] = (
                            f"Saved successfully as {receipt_reference}."
                            f"{archive_note}"
                        )
                        completed_job_id = str(st.query_params.get("job") or "")
                        if completed_job_id:
                            try:
                                finish_receipt_job(
                                    DATABASE_PATH, completed_job_id, receipt_id
                                )
                            except sqlite3.Error as exc:
                                terminal_status(
                                    "WARNING",
                                    f"{completed_job_id[:8]} | Receipt saved, but "
                                    f"job cleanup failed: {exc}",
                                )
                            del st.query_params["job"]
                        st.rerun()
                    except DuplicateReceiptError as exc:
                        st.warning(str(exc))
                    except (OSError, ValueError) as exc:
                        st.error(f"The receipt was not saved: {exc}")

            if active.get("notes"):
                with st.expander("Extraction notes"):
                    st.caption("These are the original automatic extraction results, before your corrections. The reviewed table and comparison above show the current amounts.")
                    for note in active["notes"]:
                        st.write(f"• {note}")

            decisions = st.session_state.get("line_decisions", [])
            if decisions:
                with st.expander("How the local parser classified each OCR line"):
                    decision_frame = pd.DataFrame(decisions)
                    decision_frame["confidence"] = (
                        decision_frame["confidence"] * 100
                    )
                    st.dataframe(
                        decision_frame,
                        hide_index=True,
                        width="stretch",
                        column_config={
                            "index": "Line",
                            "text": "OCR text",
                            "confidence": st.column_config.NumberColumn(
                                "OCR confidence", format="%.0f%%"
                            ),
                            "role": "Parser role",
                            "reason": "Why",
                        },
                    )
                    if st.session_state.get("ocr_text"):
                        st.code(st.session_state.ocr_text, language=None)

with history_tab:
    history_heading, history_refresh = st.columns([4, 1])
    with history_heading:
        st.subheader("Saved receipts")
        st.caption(
            "Find a receipt by its RL reference, correct its details, or safely "
            "remove an unwanted saved copy."
        )
    with history_refresh:
        if st.button(
            "Refresh receipts",
            type="primary",
            key="refresh_saved_receipts",
            help="Reload the saved receipt list from the local database.",
            width="stretch",
        ):
            st.rerun()
    with st.expander("View database tables"):
        st.caption("Inspect the SQLite records saved on this computer. This viewer does not change or delete data.")
        database_table = st.selectbox("Database table", ["receipts", "receipt_items", "receipt_sources", "expense_categories"],
            format_func=lambda name: {"receipts": "Receipts", "receipt_items": "Receipt items", "receipt_sources": "Original receipt files", "expense_categories": "Expense categories"}[name])
        if st.button("Load database table"):
            # The table name comes only from this fixed allowlist, not free-form SQL.
            queries = {name: f'SELECT * FROM "{name}" LIMIT 500' for name in
                       ("receipts", "receipt_items", "receipt_sources", "expense_categories")}
            with sqlite3.connect(DATABASE_PATH.resolve().as_uri() + "?mode=ro", uri=True) as connection:
                database_rows = pd.read_sql_query(queries[database_table], connection)
            st.dataframe(database_rows, hide_index=True, width="stretch")
            st.caption(f"Showing {len(database_rows)} records (up to 500).")
    summaries = list_receipts(DATABASE_PATH)
    if st.session_state.get("saved_receipt_notice"):
        st.success(st.session_state.pop("saved_receipt_notice"))
    if not summaries:
        st.info("No receipts have been saved yet.")
    else:
        maintenance_warnings = personal_finance_export_warnings(DATABASE_PATH)
        if maintenance_warnings:
            with st.expander(
                f"Saved-data checks ({len(maintenance_warnings)})", expanded=True
            ):
                st.caption(
                    "Use each RL reference below to open and correct the affected receipt. "
                    "These checks do not change saved data automatically."
                )
                for warning in maintenance_warnings:
                    st.warning(warning)
        else:
            st.success("Saved-data checks found no missing dates, empty receipts, suspicious merchant names, or likely duplicate transactions.")

        summary_frame = pd.DataFrame(summaries)
        summary_frame.insert(
            0,
            "reference",
            summary_frame["id"].map(lambda value: f"RL-{value:06d}"),
        )
        summary_frame = summary_frame.drop(columns=["id"])
        summary_frame["confidence"] = summary_frame["confidence"] * 100
        st.dataframe(
            summary_frame,
            width="stretch",
            hide_index=True,
            column_config={
                "reference": "Reference",
                "date": "Date",
                "store": "Store",
                "items": "Items",
                "source_files": "Original files",
                "subtotal": st.column_config.NumberColumn(
                    "Subtotal", format="$%.2f"
                ),
                "tax": st.column_config.NumberColumn("Tax", format="$%.2f"),
                "total": st.column_config.NumberColumn("Total", format="$%.2f"),
                "currency": "Currency",
                "confidence": st.column_config.ProgressColumn(
                    "Confidence", min_value=0.0, max_value=100.0, format="%.0f%%"
                ),
                "method": "Method",
            },
        )

        summaries_by_id = {row["id"]: row for row in summaries}
        receipt_ids = list(summaries_by_id)
        preferred_receipt_id = st.session_state.get("saved_receipt_selector")
        selected_index = (
            receipt_ids.index(preferred_receipt_id)
            if preferred_receipt_id in receipt_ids
            else 0
        )
        selector_identity = "_".join(str(receipt_id) for receipt_id in receipt_ids)
        selector_identity += f"_{st.session_state.get('receipt_details_revision', 0)}"
        selected_id = st.selectbox(
            "Open saved receipt",
            receipt_ids,
            index=selected_index,
            format_func=lambda value: (
                f"RL-{value:06d} — {summaries_by_id[value]['store']} — "
                f"{summaries_by_id[value]['date'] or 'date unknown'} — "
                f"{summaries_by_id[value]['currency']} "
                f"{summaries_by_id[value]['total'] or 0:,.2f}"
            ),
            key=f"saved_receipt_selector_widget_{selector_identity}",
        )
        st.session_state["saved_receipt_selector"] = int(selected_id)
        selected = get_receipt(DATABASE_PATH, int(selected_id))
        if selected:
            receipt_reference = f"RL-{selected_id:06d}"
            if st.session_state.get("receipt_details_notice"):
                st.success(st.session_state.pop("receipt_details_notice"))
            if st.session_state.get("saved_item_notice"):
                st.success(st.session_state.pop("saved_item_notice"))
            with st.expander("Edit receipt details"):
                st.caption(
                    "Correct the merchant name, location, store number, or date. "
                    "Amounts and item rows will not change."
                )
                with st.form(f"edit_receipt_details_{selected_id}"):
                    merchant = st.text_input("Merchant name / location / store number", value=selected["store_name"])
                    saved_transaction_date = st.text_input(
                        "Transaction date (YYYY-MM-DD)",
                        value=selected["transaction_date"] or "",
                        placeholder="2026-09-13",
                    )
                    save_details = st.form_submit_button("Save receipt details")
                if save_details:
                    try:
                        update_receipt_details(
                            DATABASE_PATH,
                            int(selected_id),
                            merchant=merchant,
                            transaction_date=saved_transaction_date,
                        )
                        st.session_state["receipt_details_notice"] = (
                            f"{receipt_reference} details updated."
                        )
                        # Recreate the selector so its displayed merchant/date label
                        # refreshes while saved_receipt_selector retains the selection.
                        st.session_state["receipt_details_revision"] = (
                            st.session_state.get("receipt_details_revision", 0) + 1
                        )
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
            if st.session_state.get("saved_category_notice"):
                st.success(st.session_state.pop("saved_category_notice"))
            st.markdown(
                f"**{selected['store_name']}** · "
                f"{selected['transaction_date'] or 'Date unknown'} · "
                f"{selected['currency']} {selected['total']:.2f} · "
                f"`{selected['extraction_method']}`"
            )
            st.dataframe(
                pd.DataFrame(selected["items"]),
                width="stretch",
                hide_index=True,
            )
            saved_item_total = round(
                math.fsum(
                    float(item.get("line_total") or 0)
                    for item in selected["items"]
                ),
                2,
            )
            printed_subtotal = selected.get("subtotal")
            if printed_subtotal is None:
                st.caption(
                    f"Saved item rows total {selected['currency']} "
                    f"{saved_item_total:,.2f}. No printed subtotal is stored."
                )
            else:
                saved_item_difference = round(
                    saved_item_total - float(printed_subtotal), 2
                )
                reconciliation_message = (
                    f"Saved item rows: {selected['currency']} "
                    f"{saved_item_total:,.2f} · Printed subtotal: "
                    f"{selected['currency']} {float(printed_subtotal):,.2f}"
                )
                if abs(saved_item_difference) <= 0.01:
                    st.success(reconciliation_message + " · Balanced")
                else:
                    st.warning(
                        reconciliation_message
                        + f" · Difference: {selected['currency']} "
                        + f"{saved_item_difference:+,.2f}"
                    )
            if not selected["items"]:
                st.warning(
                    f"{receipt_reference} has no saved item rows. If it is not a valid "
                    "transaction, delete it below. Otherwise, scan the original receipt "
                    "again before deleting this record."
                )
            elif len(selected["items"]) > 1:
                with st.expander("Delete a saved item row"):
                    st.caption(
                        "Use this for OCR text, rewards messages, or other rows that "
                        "are not purchased items. The printed subtotal, tax, and total "
                        "remain unchanged for comparison with the receipt."
                    )
                    saved_item_indexes = list(range(len(selected["items"])))
                    delete_item_order = st.selectbox(
                        "Select the row to delete",
                        saved_item_indexes,
                        format_func=lambda item_index: (
                            f"Row {item_index + 1}: "
                            f"{selected['items'][item_index]['name']} — "
                            f"{selected['currency']} "
                            f"{float(selected['items'][item_index]['line_total']):,.2f}"
                        ),
                        key=f"delete_saved_item_choice_{selected_id}",
                    )
                    delete_item = selected["items"][delete_item_order]
                    confirm_item_delete = st.checkbox(
                        f"Delete row {delete_item_order + 1}: "
                        f"{delete_item['name']}",
                        key=(
                            f"confirm_delete_saved_item_{selected_id}_"
                            f"{delete_item_order}"
                        ),
                    )
                    if st.button(
                        "Delete selected item row",
                        type="primary",
                        disabled=not confirm_item_delete,
                        key=f"delete_saved_item_{selected_id}",
                        width="stretch",
                    ):
                        try:
                            deleted_item = delete_receipt_item(
                                DATABASE_PATH,
                                int(selected_id),
                                int(delete_item_order),
                            )
                            st.session_state["saved_item_notice"] = (
                                f"Deleted row {delete_item_order + 1}: "
                                f"{deleted_item['deleted_item_name']}. "
                                f"{deleted_item['remaining_item_count']} saved "
                                "item(s) remain."
                            )
                            st.rerun()
                        except ValueError as exc:
                            st.error(str(exc))
            elif selected["items"]:
                st.caption(
                    "This receipt has one item. Delete the entire saved receipt "
                    "instead if that row is not a valid purchase."
                )
            st.download_button(
                "Download this receipt as compact PDF",
                data=build_saved_receipt_pdf(selected),
                file_name=saved_receipt_pdf_filename(selected),
                mime="application/pdf",
                on_click="ignore",
                width="stretch",
                type="primary",
                key=f"saved_receipt_pdf_{selected_id}",
                help="Creates a focused receipt report instead of printing the entire app screen.",
            )
            archived_sources = list_receipt_sources(
                DATABASE_PATH,
                int(selected_id),
                source_archive_dir=SOURCE_ARCHIVE_DIR,
            )
            if archived_sources:
                with st.expander(
                    f"Original receipt files ({len(archived_sources)})"
                ):
                    st.caption(
                        "TalliUpper verifies each file against the checksum recorded when "
                        "the receipt was saved."
                    )
                    for source in archived_sources:
                        source_label = (
                            f"{source['source_order']}. {source['original_filename']} "
                            f"({source['size_bytes'] / 1024:,.1f} KB)"
                        )
                        if not source["exists"]:
                            st.error(f"{source_label} — file missing")
                            continue
                        if not source["checksum_ok"]:
                            st.error(f"{source_label} — integrity check failed")
                            continue
                        st.success(f"{source_label} — verified")
                        try:
                            source_metadata, source_data = read_receipt_source(
                                DATABASE_PATH,
                                int(source["id"]),
                                source_archive_dir=SOURCE_ARCHIVE_DIR,
                            )
                            if source_metadata["media_type"] in {
                                "image/jpeg",
                                "image/png",
                                "image/webp",
                            }:
                                st.image(
                                    source_data,
                                    caption=source_metadata["original_filename"],
                                    width=450,
                                )
                            st.download_button(
                                f"Download {source_metadata['original_filename']}",
                                data=source_data,
                                file_name=source_metadata["original_filename"],
                                mime=source_metadata["media_type"],
                                key=f"download_source_{source['id']}",
                                on_click="ignore",
                                width="stretch",
                            )
                        except (OSError, ValueError) as exc:
                            st.error(str(exc))
            else:
                st.info(
                    "No original receipt files were saved for this record. Existing "
                    "receipts are not backfilled automatically."
                )
            with st.expander("Change saved item categories"):
                st.caption(
                    "Edit categories directly beside each item, or assign one "
                    "category to every item on this receipt."
                )
                st.caption(
                    "Saved choices are automatically suggested for matching item "
                    "names on future receipts."
                )
                category_options_now = list_categories(DATABASE_PATH)
                saved_category_rows = pd.DataFrame(
                    [
                        {
                            "Receipt row": index + 1,
                            "Item": item["name"],
                            "Line price": item["line_total"],
                            "Category": item["category"],
                        }
                        for index, item in enumerate(selected["items"])
                    ]
                )
                with st.form(f"saved_categories_{selected_id}"):
                    edited_saved_categories = st.data_editor(
                        saved_category_rows,
                        hide_index=True,
                        width="stretch",
                        disabled=["Receipt row", "Item", "Line price"],
                        column_config={
                            "Receipt row": st.column_config.NumberColumn(
                                "Row", width="small"
                            ),
                            "Item": st.column_config.TextColumn(
                                "Item name", width="large"
                            ),
                            "Line price": st.column_config.NumberColumn(
                                "Line price", format="$%.2f", width="small"
                            ),
                            "Category": st.column_config.SelectboxColumn(
                                "Expense category",
                                options=category_options_now,
                                required=True,
                                width="medium",
                            ),
                        },
                    )
                    apply_all_category = st.selectbox(
                        "Category for all items",
                        category_options_now,
                        key=f"apply_all_category_{selected_id}",
                    )
                    category_actions = st.columns(2)
                    save_category_edits = category_actions[0].form_submit_button(
                        "Save category edits",
                        type="primary",
                    )
                    apply_category_to_all = category_actions[
                        1
                    ].form_submit_button("Apply category to all items")

                if save_category_edits or apply_category_to_all:
                    try:
                        if apply_category_to_all:
                            changes = {
                                apply_all_category: list(
                                    range(len(selected["items"]))
                                )
                            }
                        else:
                            changes: dict[str, list[int]] = {}
                            for index, row in edited_saved_categories.iterrows():
                                category = str(row["Category"]).strip()
                                if category != selected["items"][index]["category"]:
                                    changes.setdefault(category, []).append(
                                        int(index)
                                    )
                            if not changes:
                                raise ValueError(
                                    "No category changes were entered."
                                )

                        changed_count = 0
                        for category, item_indexes in changes.items():
                            update_item_categories(
                                DATABASE_PATH,
                                int(selected_id),
                                item_indexes,
                                category,
                            )
                            changed_count += len(item_indexes)
                        st.session_state["saved_category_notice"] = (
                            f"Updated {changed_count} item(s). Expense totals and "
                            "CSV export now use the new categories."
                        )
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))

                with st.form(f"new_saved_category_{selected_id}"):
                    new_saved_category = st.text_input(
                        "New expense category name"
                    )
                    create_saved_category = st.form_submit_button(
                        "Create category"
                    )
                if create_saved_category:
                    try:
                        created_category = create_category(
                            DATABASE_PATH, new_saved_category
                        )
                        st.session_state["saved_category_notice"] = (
                            f"Created {created_category}. It is now available in "
                            "the category editor."
                        )
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))

            with st.expander("Delete this saved receipt"):
                st.error(
                    f"This permanently removes {receipt_reference} and all of its saved "
                    f"item rows and {len(archived_sources)} archived original file(s). "
                    "It does not affect other receipts."
                )
                confirm_delete = st.checkbox(
                    f"I understand and want to delete {receipt_reference}",
                    key=f"confirm_delete_{selected_id}",
                )
                if st.button(
                    "Delete saved receipt",
                    type="primary",
                    disabled=not confirm_delete,
                    key=f"delete_saved_receipt_{selected_id}",
                    width="stretch",
                ):
                    try:
                        deleted = delete_receipt(
                            DATABASE_PATH,
                            int(selected_id),
                            source_archive_dir=SOURCE_ARCHIVE_DIR,
                        )
                        forget_deleted_receipt(int(selected_id))
                        remaining_receipt_ids = [
                            receipt_id
                            for receipt_id in receipt_ids
                            if receipt_id != selected_id
                        ]
                        if remaining_receipt_ids:
                            st.session_state["saved_receipt_selector"] = (
                                remaining_receipt_ids[0]
                            )
                        else:
                            st.session_state.pop("saved_receipt_selector", None)
                        st.session_state["saved_receipt_notice"] = (
                            f"Deleted RL-{deleted['id']:06d} ({deleted['store_name']}) "
                            f"and {deleted['source_count']} archived original file(s)."
                        )
                        if deleted["source_cleanup_warning"]:
                            st.session_state["saved_receipt_notice"] += (
                                " " + deleted["source_cleanup_warning"]
                            )
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))

        st.download_button(
            "Download all data as CSV",
            data=export_csv(DATABASE_PATH).encode("utf-8-sig"),
            file_name="receipt_history.csv",
            mime="text/csv",
            on_click="ignore",
            width="stretch",
        )
        st.download_button(
            "Download full backup (database + original files)",
            data=generate_backup_download,
            file_name=f"receiptlens-backup-{date.today().isoformat()}.zip",
            mime="application/zip",
            on_click="ignore",
            key="download_full_backup",
            width="stretch",
            help="Builds and verifies a fresh backup when you click.",
        )
        st.caption(
            "The backup is checksum-verified but is not password-encrypted. Keep it "
            "in a protected folder or trusted encrypted storage."
        )

with expenses_tab:
    st.subheader("Expense totals")
    st.caption(
        "Item spending for the selected dates. Uses item line prices after "
        "discounts; receipt-level sales tax is not included."
    )
    expense_group = st.radio(
        "Group expenses by",
        ["Expense category", "Item name"],
        horizontal=True,
    )
    expense_date_mode = st.radio(
        "Expense dates",
        ["All dates", "This year", "Choose a range"],
        horizontal=True,
        help=(
            "All dates includes dated and undated receipts. Date filters exclude "
            "receipts whose transaction date is unknown."
        ),
    )
    expense_start_date = expense_end_date = None
    expense_range_ready = expense_date_mode != "Choose a range"
    expense_summaries = list_receipts(DATABASE_PATH)
    expense_dated_receipts = []
    for summary in expense_summaries:
        try:
            if summary["date"]:
                expense_dated_receipts.append(date.fromisoformat(summary["date"]))
        except ValueError:
            continue
    if expense_date_mode == "This year":
        current_year = date.today().year
        expense_start_date = date(current_year, 1, 1).isoformat()
        expense_end_date = date(current_year, 12, 31).isoformat()
        st.caption(f"Showing receipts dated in {current_year}.")
    elif expense_date_mode == "Choose a range":
        if expense_dated_receipts:
            expense_selected_dates = st.date_input(
                "Expense date range",
                value=(
                    min(expense_dated_receipts),
                    max(expense_dated_receipts),
                ),
                min_value=min(expense_dated_receipts),
                max_value=max(expense_dated_receipts),
            )
            if isinstance(expense_selected_dates, (tuple, list)):
                if len(expense_selected_dates) == 2:
                    expense_start_date = expense_selected_dates[0].isoformat()
                    expense_end_date = expense_selected_dates[1].isoformat()
                    expense_range_ready = True
                elif len(expense_selected_dates) == 1:
                    st.info(
                        "Select an ending date to complete the expense range. "
                        "Totals will appear after both dates are selected."
                    )
            else:
                expense_start_date = expense_end_date = (
                    expense_selected_dates.isoformat()
                )
                expense_range_ready = True
        else:
            st.warning("No saved receipt has a valid transaction date.")
    expense_rows = (
        expense_totals(
            DATABASE_PATH,
            by_item=expense_group == "Item name",
            start_date=expense_start_date,
            end_date=expense_end_date,
        )
        if expense_range_ready
        else []
    )
    expense_rows = [
        row for row in expense_rows
        if abs(float(row["Amount"])) >= 0.005
    ]
    if expense_range_ready and not expense_rows:
        st.info("No saved item expenses match the selected dates.")
    elif expense_range_ready:
        expense_frame = pd.DataFrame(expense_rows)
        expense_currency = st.selectbox(
            "Expense currency",
            sorted(expense_frame["Currency"].unique()),
        )
        expense_frame = expense_frame[
            expense_frame["Currency"] == expense_currency
        ]
        st.metric(
            "Total item expenses",
            f"{expense_currency} {expense_frame['Amount'].sum():,.2f}",
        )
        st.bar_chart(
            expense_frame,
            x="Expense",
            y="Amount",
            horizontal=True,
        )
        st.dataframe(
            expense_frame[["Expense", "Amount"]],
            hide_index=True,
            width="stretch",
            column_config={
                "Amount": st.column_config.NumberColumn(
                    f"Amount ({expense_currency})",
                    format="%.2f",
                )
            },
        )
    st.info(
        "For totals such as Cat food or Water, create those expense categories "
        "in the sidebar and assign them to items before saving. Item name groups "
        "identical saved names."
    )

with insights_tab:
    st.subheader("Item insights")
    st.caption(
        "Find an individual product across saved receipts and compare its recorded "
        "prices. Receipt histories stay read-only and on this computer."
    )
    st.info(
        "Names are matched without regard to letter case or extra spaces. Different "
        "spellings stay separate unless you explicitly group them as one product."
    )
    alias_notice = st.session_state.pop("item_alias_notice", None)
    if alias_notice:
        st.success(alias_notice)
    package_notice = st.session_state.pop("package_size_notice", None)
    if package_notice:
        st.success(package_notice)

    with st.expander("Group equivalent recorded item names"):
        st.caption(
            "Create a user-approved product name without rewriting any original "
            "receipt line. Select only descriptions that represent the same product."
        )
        alias_search = st.text_input(
            "Find recorded names to group",
            placeholder="For example: egg",
            key="item_alias_search",
        )
        alias_options = list_recorded_item_names(DATABASE_PATH, alias_search)
        with st.form("item_alias_group_form"):
            selected_aliases = st.multiselect(
                "Recorded item names",
                alias_options,
                help="Select at least two saved names for the same product.",
            )
            canonical_product_name = st.text_input(
                "Canonical product name",
                placeholder="For example: Large eggs, 18 count",
            )
            save_alias_group = st.form_submit_button(
                "Save product-name group", type="primary"
            )
        if save_alias_group:
            try:
                saved_group_name = save_item_alias_group(
                    DATABASE_PATH,
                    canonical_product_name,
                    selected_aliases,
                )
            except ValueError as exc:
                st.error(str(exc))
            else:
                st.success(
                    f'Saved "{saved_group_name}" with '
                    f"{len(selected_aliases)} recorded names."
                )

        existing_alias_groups = list_item_alias_groups(DATABASE_PATH)
        if existing_alias_groups:
            group_names = [
                group["Canonical name"] for group in existing_alias_groups
            ]
            selected_group_name = st.selectbox(
                "Existing product-name group",
                group_names,
            )
            selected_group = next(
                group
                for group in existing_alias_groups
                if group["Canonical name"] == selected_group_name
            )
            st.caption(
                "Recorded names: "
                + " · ".join(selected_group["Recorded aliases"])
            )
            if st.button("Remove selected product-name group"):
                removed_count = remove_item_alias_group(
                    DATABASE_PATH, selected_group_name
                )
                st.session_state["item_alias_notice"] = (
                    f'Removed "{selected_group_name}" grouping preference '
                    f"for {removed_count} recorded names. Receipt data was not changed."
                )
                st.rerun()

    item_search = st.text_input(
        "Search saved item names",
        placeholder="For example: eggs, paper towels, or Dr Pepper",
        key="item_insights_search",
    )
    st.caption("Results update automatically as you type.")
    if not item_search.strip():
        st.info("Enter part of an item name to view its purchase and price history.")
    else:
        matching_item_names = list_item_names(DATABASE_PATH, item_search)
        if not matching_item_names:
            st.warning(f'No saved item name contains "{item_search.strip()}".')
        else:
            selected_item_name = st.selectbox(
                "Matching saved item",
                matching_item_names,
            )
            item_history = item_price_history(DATABASE_PATH, selected_item_name)
            history_frame = pd.DataFrame(item_history)
            selected_currency = st.selectbox(
                "Currency",
                sorted(history_frame["Currency"].unique()),
            )
            currency_history = history_frame[
                history_frame["Currency"] == selected_currency
            ].copy()

            with st.expander("Set package size for a purchase"):
                st.caption(
                    "Enter the contents of one purchased package. TalliUpper already "
                    "includes the saved purchase quantity; for example, quantity 2 × "
                    "18 count means 36 items. Original receipt values are not changed."
                )
                package_rows = currency_history.to_dict("records")
                package_labels = []
                for row in package_rows:
                    date_label = (
                        str(row["Date"])
                        if pd.notna(row["Date"])
                        and str(row["Date"]).strip()
                        else "date unknown"
                    )
                    receipt_item_number = int(row["_item_order"]) + 1
                    package_labels.append(
                        f"{row['Reference']} · {date_label} · "
                        f"{row['Merchant']} · receipt item {receipt_item_number} · "
                        f"{row['Recorded item']}"
                    )
                selected_package_label = st.selectbox(
                    "Saved purchase row",
                    package_labels,
                    key="package_size_row",
                )
                selected_package_row = package_rows[
                    package_labels.index(selected_package_label)
                ]
                existing_package_amount = selected_package_row["Package amount"]
                existing_package_unit = selected_package_row["Package unit"]
                with st.form("package_size_form"):
                    package_amount = st.number_input(
                        "Package amount per purchased unit",
                        min_value=0.01,
                        value=(
                            float(existing_package_amount)
                            if pd.notna(existing_package_amount)
                            else 1.0
                        ),
                        step=1.0,
                    )
                    package_unit = st.selectbox(
                        "Package unit",
                        PACKAGE_UNITS,
                        index=(
                            PACKAGE_UNITS.index(existing_package_unit)
                            if existing_package_unit in PACKAGE_UNITS
                            else 0
                        ),
                    )
                    package_actions = st.columns(2)
                    save_package = package_actions[0].form_submit_button(
                        "Save package size", type="primary"
                    )
                    clear_package = package_actions[1].form_submit_button(
                        "Clear package size"
                    )
                if save_package or clear_package:
                    try:
                        update_item_package_size(
                            DATABASE_PATH,
                            int(selected_package_row["_receipt_id"]),
                            int(selected_package_row["_item_order"]),
                            package_amount if save_package else None,
                            package_unit if save_package else None,
                        )
                    except ValueError as exc:
                        st.error(str(exc))
                    else:
                        if save_package:
                            message = (
                                f"Saved {package_amount:g} {package_unit} for "
                                f"{selected_package_row['Reference']}."
                            )
                        else:
                            message = (
                                "Cleared package size for "
                                f"{selected_package_row['Reference']}."
                            )
                        st.session_state["package_size_notice"] = message
                        st.rerun()

            purchase_count = len(currency_history)
            total_line_spending = float(currency_history["Line price"].sum())
            lowest_price = float(currency_history["Comparable price"].min())
            dated_history = currency_history[
                currency_history["Date"].notna()
                & (currency_history["Date"].astype(str).str.strip() != "")
            ].copy()
            latest_price = (
                float(
                    dated_history.sort_values(
                        ["Date", "Reference"], kind="stable"
                    ).iloc[-1]["Comparable price"]
                )
                if not dated_history.empty
                else None
            )

            metrics = st.columns(4)
            metrics[0].metric("Matching purchases", f"{purchase_count}")
            metrics[1].metric(
                "Recorded line spending",
                f"{selected_currency} {total_line_spending:,.2f}",
            )
            metrics[2].metric(
                "Lowest comparable price",
                f"{selected_currency} {lowest_price:,.2f}",
            )
            metrics[3].metric(
                "Most recent dated price",
                (
                    f"{selected_currency} {latest_price:,.2f}"
                    if latest_price is not None
                    else "Date unavailable"
                ),
            )

            st.caption(
                "Comparable price uses the saved unit price when available; otherwise "
                "it divides the line price by quantity. Package-normalized comparisons "
                "appear after package content is entered for a purchase."
            )
            if not dated_history.empty:
                dated_history["Date"] = pd.to_datetime(
                    dated_history["Date"], errors="coerce"
                )
                chart_data = dated_history.dropna(subset=["Date"])[
                    ["Date", "Comparable price"]
                ]
                if not chart_data.empty:
                    if chart_data["Date"].nunique() == 1:
                        st.scatter_chart(
                            chart_data,
                            x="Date",
                            y="Comparable price",
                        )
                        st.caption(
                            "Only one purchase date is available, so values are "
                            "shown as points. A trend line will appear after a "
                            "purchase from another date is saved."
                        )
                    else:
                        st.line_chart(
                            chart_data,
                            x="Date",
                            y="Comparable price",
                        )
            charted_purchase_count = len(chart_data) if not dated_history.empty else 0
            uncharted_purchase_count = purchase_count - charted_purchase_count
            if uncharted_purchase_count:
                st.caption(
                    f"{uncharted_purchase_count} matching purchase"
                    f"{'s are' if uncharted_purchase_count != 1 else ' is'} listed "
                    "below but not charted because the transaction date is unavailable."
                )

            normalized_history = currency_history.dropna(
                subset=["Normalized price", "Normalized unit"]
            ).copy()
            if not normalized_history.empty:
                st.markdown("#### Package-normalized comparison")
                normalized_units = sorted(
                    normalized_history["Normalized unit"].unique()
                )
                normalized_unit = (
                    st.selectbox("Normalized unit", normalized_units)
                    if len(normalized_units) > 1
                    else normalized_units[0]
                )
                normalized_unit_history = normalized_history[
                    normalized_history["Normalized unit"] == normalized_unit
                ].copy()
                normalized_dated = normalized_unit_history[
                    normalized_unit_history["Date"].notna()
                    & (
                        normalized_unit_history["Date"]
                        .astype(str)
                        .str.strip()
                        != ""
                    )
                ].copy()
                normalized_latest = (
                    float(
                        normalized_dated.sort_values(
                            ["Date", "Reference"], kind="stable"
                        ).iloc[-1]["Normalized price"]
                    )
                    if not normalized_dated.empty
                    else None
                )
                normalized_metrics = st.columns(3)
                normalized_metrics[0].metric(
                    "Normalized coverage",
                    f"{len(normalized_unit_history)} of {purchase_count}",
                )
                normalized_metrics[1].metric(
                    f"Lowest price per {normalized_unit}",
                    f"{selected_currency} {normalized_unit_history['Normalized price'].min():,.4f}",
                )
                normalized_metrics[2].metric(
                    f"Most recent price per {normalized_unit}",
                    (
                        f"{selected_currency} {normalized_latest:,.4f}"
                        if normalized_latest is not None
                        else "Date unavailable"
                    ),
                )
                if not normalized_dated.empty:
                    normalized_dated["Date"] = pd.to_datetime(
                        normalized_dated["Date"], errors="coerce"
                    )
                    normalized_chart = normalized_dated.dropna(subset=["Date"])[
                        ["Date", "Normalized price"]
                    ]
                    if not normalized_chart.empty:
                        if normalized_chart["Date"].nunique() == 1:
                            st.scatter_chart(
                                normalized_chart,
                                x="Date",
                                y="Normalized price",
                            )
                            st.caption(
                                "Only one package-normalized purchase date is "
                                "available, so values are shown as points."
                            )
                        else:
                            st.line_chart(
                                normalized_chart,
                                x="Date",
                                y="Normalized price",
                            )
                normalized_chart_count = (
                    len(normalized_chart) if not normalized_dated.empty else 0
                )
                normalized_uncharted_count = (
                    len(normalized_unit_history) - normalized_chart_count
                )
                if normalized_uncharted_count:
                    st.caption(
                        f"{normalized_uncharted_count} package-normalized purchase"
                        f"{'s are' if normalized_uncharted_count != 1 else ' is'} "
                        "not charted because the transaction date is unavailable."
                    )

            st.dataframe(
                currency_history.drop(columns=["_receipt_id", "_item_order"]),
                hide_index=True,
                width="stretch",
                column_config={
                    "Line price": st.column_config.NumberColumn(
                        f"Line price ({selected_currency})", format="%.2f"
                    ),
                    "Comparable price": st.column_config.NumberColumn(
                        f"Comparable price ({selected_currency})", format="%.2f"
                    ),
                    "Normalized price": st.column_config.NumberColumn(
                        f"Normalized price ({selected_currency})", format="%.4f"
                    ),
                },
            )
            st.caption(
                "Every matching saved item row is included. Remove unwanted duplicate "
                "receipts before interpreting totals or price changes."
            )

with export_tab:
    st.subheader("Export center")
    st.caption(
        "Preview reviewed receipt data before downloading it as a CSV file. "
        "The export does not change or delete saved receipts."
    )
    export_summaries = list_receipts(DATABASE_PATH)
    if not export_summaries:
        st.info("Save a verified receipt before creating an export.")
    else:
        export_format = st.radio(
            "CSV format",
            [
                "Itemized items",
                "Receipt summaries",
                "Personal-finance transactions (beta)",
            ],
            horizontal=True,
            help=(
                "Itemized exports contain one row per purchase item. Receipt "
                "summaries contain one row per saved receipt. Personal-finance "
                "transactions use a Quicken Simplifi-compatible CSV schema, with "
                "one expense transaction per receipt."
            ),
        )
        if export_format == "Personal-finance transactions (beta)":
            st.info(
                "This beta export is checked against Quicken Simplifi's published "
                "CSV structure, but it has not been imported into a paid Quicken "
                "account. Mixed receipts use the category ‘Mixed purchase’ and "
                "list category totals in Notes."
            )
        available_stores = sorted(
            {row["store"] for row in export_summaries}, key=str.casefold
        )
        selected_stores = st.multiselect(
            "Merchants",
            available_stores,
            default=available_stores,
            help="Clear a merchant to leave its receipts out of this export.",
        )

        date_mode = st.radio(
            "Transaction dates",
            ["All dates", "Choose a range"],
            horizontal=True,
            help="All dates also includes receipts whose transaction date is unknown.",
        )
        start_date = end_date = None
        dated_receipts = []
        for summary in export_summaries:
            try:
                if summary["date"]:
                    dated_receipts.append(date.fromisoformat(summary["date"]))
            except ValueError:
                continue
        if date_mode == "Choose a range":
            if dated_receipts:
                selected_dates = st.date_input(
                    "Date range",
                    value=(min(dated_receipts), max(dated_receipts)),
                    min_value=min(dated_receipts),
                    max_value=max(dated_receipts),
                )
                if isinstance(selected_dates, (tuple, list)):
                    if len(selected_dates) == 2:
                        start_date, end_date = (
                            selected_dates[0].isoformat(),
                            selected_dates[1].isoformat(),
                        )
                else:
                    start_date = end_date = selected_dates.isoformat()
                st.caption(
                    "Receipts with an unknown transaction date are excluded from "
                    "a date-range export."
                )
            else:
                st.warning(
                    "No saved receipt has a valid transaction date. Choose All dates "
                    "to export undated receipts."
                )

        selected_categories = None
        if export_format == "Itemized items":
            available_categories = list_categories(DATABASE_PATH)
            selected_categories = st.multiselect(
                "Expense categories",
                available_categories,
                default=available_categories,
                help="Clear a category to leave those item rows out of this export.",
            )
            export_text = export_csv(
                DATABASE_PATH,
                start_date=start_date,
                end_date=end_date,
                stores=selected_stores,
                categories=selected_categories,
            )
            export_filename = "receiptlens-itemized-export.csv"
        elif export_format == "Receipt summaries":
            export_text = export_receipt_summary_csv(
                DATABASE_PATH,
                start_date=start_date,
                end_date=end_date,
                stores=selected_stores,
            )
            export_filename = "receiptlens-receipt-summary-export.csv"
        else:
            export_text = export_personal_finance_csv(
                DATABASE_PATH,
                start_date=start_date,
                end_date=end_date,
                stores=selected_stores,
            )
            export_filename = "receiptlens-personal-finance-export.csv"
            export_warnings = personal_finance_export_warnings(
                DATABASE_PATH,
                start_date=start_date,
                end_date=end_date,
                stores=selected_stores,
            )
            if export_warnings:
                st.markdown("#### Review before downloading")
                for warning in export_warnings:
                    st.warning(warning)

        export_preview = pd.read_csv(io.StringIO(export_text))
        if export_format == "Itemized items":
            st.info(
                "This preview contains one row per purchased item. A receipt ID "
                "normally repeats when that receipt contains several items. Use "
                "Item number to identify each row within the receipt."
            )
        if export_preview.empty:
            st.warning("No saved receipt data matches the selected filters.")
        else:
            st.markdown("#### Export preview")
            st.dataframe(
                export_preview.head(200),
                hide_index=True,
                width="stretch",
            )
            if len(export_preview) > 200:
                st.caption(
                    f"Showing the first 200 of {len(export_preview)} export rows."
                )
            else:
                st.caption(f"{len(export_preview)} row(s) ready to download.")
            st.download_button(
                "Download filtered CSV",
                data=export_text.encode("utf-8-sig"),
                file_name=export_filename.replace("receiptlens", "talliupper"),
                mime="text/csv",
                on_click="ignore",
                width="stretch",
            )

with learn_tab:
    st.subheader("How TalliUpper processes a receipt")
    st.markdown(
        """
1. **Image preparation** rotates the phone image correctly, reduces excessive
   resolution, and converts it to a predictable format.
2. **OCR** finds text regions and returns text, coordinates, and confidence scores.
3. **Receipt parsing** applies readable Python rules for dates, money, summary
   labels, quantities, discounts, and item lines.
4. **Validation** compares item totals with the subtotal and compares subtotal plus
   tax with the final total.
5. **Human review** remains the final authority before the record is saved.
6. **Persistence** stores corrected fields in SQLite and makes them exportable as CSV.

When you use Local OCR or Manual entry, TalliUpper keeps receipt processing on
this computer. Only the optional OpenAI vision mode sends a prepared receipt
image for extraction, and only when you select that mode.
"""
    )

