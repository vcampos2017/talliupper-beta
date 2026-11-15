"""Privacy-conscious, local-only OCR diagnostic exports."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from .local_ocr import OcrLine, OcrResult
from .parser import ParseResult

_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}(?!\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_ADDRESS = re.compile(r"\b\d{1,6}\s+[\w .'-]+\s+(?:ST(?:REET)?|RD|ROAD|AVE(?:NUE)?|BLVD|BOULEVARD|DR|DRIVE|LN|LANE|HWY|HIGHWAY)\b", re.I)
_LOYALTY = re.compile(r"\b(?:loyalty|member(?:ship)?|rewards?|club)\s*(?:#|no\.?|id)?\s*[A-Z0-9-]{4,}\b", re.I)
# Card numbers printed with the leading digits blanked out ("XXXXXXXXXXX2732",
# "************1234", "xxxx-xxxx-xxxx-1234"). The last four are still personal.
_MASKED_CARD = re.compile(r"(?<![A-Z0-9])(?:[X*#•][ -]?){4,}\d{2,6}\b", re.I)
# "ACCT # 1234", "Card No: ...1234", "ending in 1234", "Last 4: 1234".
_CARD_LAST_FOUR = re.compile(
    r"\b(ACCT|ACCOUNT|CARD|ENDING(?:\s+IN)?|LAST\s*(?:4|FOUR))\s*(?:#|NO\.?|NUM(?:BER)?)?\s*[:#.]?\s*\.{0,3}\d{4}\b",
    re.I,
)
# Approval and terminal codes on the tender block. A label that can also be a
# plain word (AUTH, APPROVAL) only counts when its code contains a digit, so
# "APPROVED" stays readable.
_PAYMENT_CODE = re.compile(
    r"\b("
    r"AUTH(?:ORI[SZ]ATION)?(?:\s*(?:CODE|CD|NO\.?|NUM(?:BER)?))?"
    r"|APPR(?:OVAL)?(?:\s*(?:CODE|CD|NO\.?|NUM(?:BER)?))?"
    r"|TRANS(?:ACTION)?\s*(?:ID|NO\.?|NUM(?:BER)?|#)"
    r"|TERM(?:INAL)?\s*(?:ID|NO\.?|#)"
    r"|MERCH(?:ANT)?\s*(?:ID|NO\.?|#)"
    r"|REF(?:ERENCE)?\s*(?:NO\.?|NUM(?:BER)?|#)"
    r"|(?:REF|AID|MID|TID|TVR|TSI|ARQC|ARPC|IAD|ARC|TC|AC|CVM)(?=\s*[:#])"
    r")\s*[:#]?\s*(?=[A-Z0-9-]*\d)[A-Z0-9-]{2,80}\b",
    re.I,
)

# Card networks, banks and payment apps printed on the tender line. Together
# with the store, date and items they say a lot about one person's habits, so
# exports and library fixtures replace them with a neutral "CREDIT CARD" (the
# parser still sees a card tender). The brand is never needed to fix the parser.
PAYMENT_BRAND = re.compile(
    r"\b(?:VISA|MASTER\s?CARD|MASTER\s?CRD|MASTER\s?CD|MASTRCRD|MSTRCRD|MSTRCD|MCARD|AMEX|AMERICAN\s+EXPRESS|"
    r"DISCOVER|DINERS(?:\s+CLUB)?|JCB|UNION\s?PAY|INTERAC|"
    r"CAPITAL\s+ONE|CHASE|CITI(?:BANK)?|WELLS\s+FARGO|BANK\s+OF\s+AMERICA|"
    r"U\.?S\.?\s+BANK|SYNCHRONY|BARCLAYS|NAVY\s+FEDERAL|USAA|"
    r"APPLE\s?PAY|GOOGLE\s?PAY|SAMSUNG\s?PAY|PAYPAL|VENMO|CASH\s?APP)\b",
    re.IGNORECASE,
)
PAYMENT_PLACEHOLDER = "CREDIT CARD"
_REPEATED_PLACEHOLDER = re.compile(
    r"CREDIT CARD(?:\s*\(\s*CREDIT CARD\s*\)|\s+CREDIT(?:\s+CARD)?)+", re.IGNORECASE
)


def mask_payment_brands(text: str) -> str:
    """Replace card brands, issuing banks and payment apps with "CREDIT CARD"."""
    masked = PAYMENT_BRAND.sub(PAYMENT_PLACEHOLDER, text)
    return _REPEATED_PLACEHOLDER.sub(PAYMENT_PLACEHOLDER, masked)


# Receipt dates such as 11-03-26 or 10/22/2026. They are protected while the
# number patterns run, so a long store number printed just before the date is
# not merged with it into one "card number" (that hid the H-E-B date, RL-72).
_DATE = re.compile(r"(?<!\d)\d{1,2}[/-]\d{1,2}[/-](?:\d{4}|\d{2})(?!\d)")
_DATE_SLOT = "\x00{}\x00"


def redact_text(value: object) -> str:
    """Best-effort removal of personal and payment identifiers before export.

    Item names, prices, tax and totals are left alone so the export is still
    useful for parser fixes. Payment details are replaced in full, never
    partially, so exports can't be pieced together about how someone pays.
    """
    text = str(value or "")
    dates: list[str] = []

    def _protect(match: re.Match[str]) -> str:
        dates.append(match.group(0))
        return _DATE_SLOT.format(len(dates) - 1)

    text = _DATE.sub(_protect, text)
    for pattern, replacement in (
        (_EMAIL, "[REDACTED EMAIL]"),
        # Labelled payment codes first, so a long terminal or merchant ID is
        # called a payment code rather than a phone or card number.
        (_PAYMENT_CODE, r"\1 [REDACTED PAYMENT CODE]"),
        (_CARD, "[REDACTED PAYMENT NUMBER]"),
        (_MASKED_CARD, "[REDACTED PAYMENT NUMBER]"),
        (_CARD_LAST_FOUR, r"\1 [REDACTED PAYMENT NUMBER]"),
        (_PHONE, "[REDACTED PHONE]"),
        (_ADDRESS, "[REDACTED ADDRESS]"),
        (_LOYALTY, "[REDACTED LOYALTY ID]"),
    ):
        text = pattern.sub(replacement, text)
    for index, date in enumerate(dates):
        text = text.replace(_DATE_SLOT.format(index), date)
    return mask_payment_brands(text)


def _line(line: OcrLine) -> dict[str, object]:
    return {"text": redact_text(line.text), "confidence": line.confidence, "box": line.box}


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


@dataclass(slots=True)
class OcrDiagnosticTrace:
    """Ephemeral pipeline observations, deliberately containing no image bytes."""

    image_dimensions: list[tuple[int, int]] = field(default_factory=list)
    language: str = "en"
    standard: OcrResult | None = None
    enhanced: OcrResult | None = None
    enhanced_triggered: bool = False
    enhanced_reason: str | None = None
    merged: OcrResult | None = None
    merged_origins: list[dict[str, object]] = field(default_factory=list)
    stage: str = "queued"


class DiagnosticPipelineError(RuntimeError):
    """Pipeline error retaining a bounded, in-memory-only diagnostic trace."""

    def __init__(self, error: Exception, trace: OcrDiagnosticTrace):
        super().__init__(str(error))
        self.original_error = error
        self.trace = trace


def _app_version() -> str:
    try:
        return version("talliupper")
    except PackageNotFoundError:
        return "local checkout"


def build_ocr_diagnostic(
    trace: OcrDiagnosticTrace,
    *,
    parsed: ParseResult | None = None,
    error: object | None = None,
) -> dict[str, Any]:
    """Build a JSON-safe export; callers should retain it only on request."""
    return {
        "format": "talliupper-ocr-diagnostic-v1",
        "privacy_notice": "Card brands, banks, card numbers and approval codes are masked automatically, along with phone numbers, emails, addresses and loyalty IDs. Redaction is best-effort: review this JSON before sharing.",
        "metadata": {
            "application_version": _app_version(),
            "ocr_version": "PaddleOCR PP-OCRv5",
            "image_dimensions": [list(size) for size in trace.image_dimensions],
            "language": trace.language,
            "processing_stage": trace.stage,
            "enhanced_ocr": {"triggered": trace.enhanced_triggered, "reason": trace.enhanced_reason},
        },
        "ocr_passes": {
            "standard": None if trace.standard is None else {"engine": trace.standard.engine, "lines": [_line(line) for line in trace.standard.lines]},
            "enhanced": None if trace.enhanced is None else {"engine": trace.enhanced.engine, "lines": [_line(line) for line in trace.enhanced.lines]},
            "merged": None if trace.merged is None else {"engine": trace.merged.engine, "lines": [_line(line) for line in trace.merged.lines], "origins": _redact_value(trace.merged_origins)},
        },
        "parser": None if parsed is None else {
            "classifications": [{**asdict(item), "text": redact_text(item.text), "reason": redact_text(item.reason)} for item in parsed.decisions],
            "ignored_lines": [{"index": item.index, "text": redact_text(item.text), "reason": redact_text(item.reason)} for item in parsed.decisions if item.role == "ignored"],
            "missing_item_candidates": [{**asdict(item), "name": redact_text(item.name), "supporting_text": redact_text(item.supporting_text)} for item in parsed.missing_item_candidates],
            "parsed_items": [{**item.model_dump(mode="json"), "name": redact_text(item.name), "product_code": "[REDACTED PRODUCT CODE]" if item.product_code else None} for item in parsed.receipt.items],
        },
        "error": None if error is None else redact_text(error),
    }
