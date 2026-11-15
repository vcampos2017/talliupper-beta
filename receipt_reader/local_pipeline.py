"""Composition root for the local receipt-processing pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import re
from statistics import median

from PIL import Image

from .local_ocr import OcrLine, OcrResult, extract_text_locally
from .ocr_diagnostic import DiagnosticPipelineError, OcrDiagnosticTrace
from .parser import (
    ParseResult,
    amount_at_end,
    parse_receipt,
    printed_item_count,
    purchased_unit_count,
)
from .review import is_gratuity_item_name


@dataclass(frozen=True, slots=True)
class LocalPipelineResult:
    ocr: OcrResult
    parsed: ParseResult
    diagnostic: OcrDiagnosticTrace | None = None


def _purchased_items(parsed: ParseResult):
    return [
        item
        for item in parsed.receipt.items
        if not is_gratuity_item_name(item.name)
    ]


def _subtotal_shortfall(parsed: ParseResult) -> float:
    subtotal = parsed.receipt.subtotal
    if subtotal is None:
        return 0.0
    item_sum = sum(item.line_total for item in _purchased_items(parsed))
    return max(0.0, round(subtotal - item_sum, 2))


def _printed_count(ocr: OcrResult) -> int | None:
    return next(
        (
            count
            for line in ocr.lines
            if (count := printed_item_count(line.text)) is not None
        ),
        None,
    )


def _purchased_units(parsed: ParseResult) -> int:
    """Rows counted the way printed counts usually are (``3@14.49`` is 3)."""

    return purchased_unit_count(item.quantity for item in _purchased_items(parsed))


def _below_printed_count(ocr: OcrResult, parsed: ParseResult) -> bool:
    # A printed count may count rows or units. Units are never fewer than
    # rows, so only a unit count below the printed one means a row is missing
    # (RL-81: Petco "3 Items" for one row of qty 3 triggered a needless retry).
    expected = _printed_count(ocr)
    return expected is not None and _purchased_units(parsed) < expected


def _needs_enhanced_recovery(ocr: OcrResult, parsed: ParseResult) -> bool:
    return bool(
        _below_printed_count(ocr, parsed) or _subtotal_shortfall(parsed) > 0.02
    )


def _same_visual_row(first: OcrLine, second: OcrLine) -> bool:
    if first.box is None or second.box is None:
        return False
    first_center = (first.box[1] + first.box[3]) / 2
    second_center = (second.box[1] + second.box[3]) / 2
    first_height = max(1, first.box[3] - first.box[1])
    second_height = max(1, second.box[3] - second.box[1])
    return abs(first_center - second_center) <= max(first_height, second_height) * 0.55


def _ocr_line_quality(line: OcrLine) -> float:
    readable = sum(character.isalnum() for character in line.text)
    return (line.confidence * 10) + min(readable, 100) / 20


# A line this many typical line-heights tall almost certainly holds several
# printed rows that were read as one.
MULTI_ROW_HEIGHT = 1.8


def _line_height(line: OcrLine) -> int:
    return max(1, line.box[3] - line.box[1]) if line.box is not None else 0


def _split_multirow_blobs(
    primary: OcrResult, recovery: OcrResult
) -> tuple[list[OcrLine], list[dict[str, object]], set[int]]:
    """Replace a first-pass region that swallowed several printed rows.

    The standard pass sometimes returns one tall region covering many receipt
    rows, with their words interleaved.  Row matching alone lets only one
    recovery line compete with that region, so the other separated rows were
    discarded.  When the recovery pass has two or more lines whose centers sit
    inside such a tall region, those lines replace it.
    """

    heights = sorted(_line_height(line) for line in primary.lines if line.box is not None)
    merged: list[OcrLine] = []
    origins: list[dict[str, object]] = []
    consumed: set[int] = set()
    typical = heights[len(heights) // 2] if heights else 0
    for index, line in enumerate(primary.lines, start=1):
        inside: list[tuple[int, OcrLine]] = []
        if typical and line.box is not None and _line_height(line) >= typical * MULTI_ROW_HEIGHT:
            top, bottom = line.box[1], line.box[3]
            for recovery_index, candidate in enumerate(recovery.lines, start=1):
                if recovery_index in consumed or candidate.box is None:
                    continue
                if _line_height(candidate) >= typical * MULTI_ROW_HEIGHT:
                    continue  # a tall recovery line is not a separated row
                center = (candidate.box[1] + candidate.box[3]) / 2
                overlaps_horizontally = (
                    candidate.box[0] < line.box[2] and candidate.box[2] > line.box[0]
                )
                if top <= center <= bottom and overlaps_horizontally:
                    inside.append((recovery_index, candidate))
        if len(inside) >= 2:
            for recovery_index, candidate in inside:
                consumed.add(recovery_index)
                merged.append(candidate)
                origins.append(
                    {
                        "origin": "enhanced_split",
                        "standard_line": index,
                        "enhanced_line": recovery_index,
                        "discarded_text": line.text,
                    }
                )
            continue
        merged.append(line)
        origins.append({"origin": "standard", "standard_line": index})
    return merged, origins, consumed


def _merge_ocr_passes_with_origins(
    primary: OcrResult, recovery: OcrResult
) -> tuple[OcrResult, list[dict[str, object]]]:
    """Merge a second view by row position without duplicating real purchases.

    When the first pass read several printed rows as one tall line and the
    second pass read them separately, the separate rows replace the tall one.
    """

    heights = [_line_height(line) for line in (*primary.lines, *recovery.lines) if line.box]
    typical = median(heights) if heights else 0

    def is_multi_row(line: OcrLine) -> bool:
        return bool(typical and line.box and _line_height(line) > typical * MULTI_ROW_HEIGHT)

    merged, origins, consumed = _split_multirow_blobs(primary, recovery)
    for recovery_index, candidate in enumerate(recovery.lines, start=1):
        if recovery_index in consumed:
            continue
        matches = [
            (index, existing)
            for index, existing in enumerate(merged)
            if _same_visual_row(existing, candidate)
        ]
        if not matches:
            merged.append(candidate)
            origins.append({"origin": "enhanced_added", "enhanced_line": recovery_index})
            continue

        index, existing = min(
            matches,
            key=lambda pair: abs(
                ((pair[1].box[1] + pair[1].box[3]) / 2)  # type: ignore[index]
                - ((candidate.box[1] + candidate.box[3]) / 2)  # type: ignore[index]
            ),
        )
        # A tall multi-row candidate must not replace a cleanly separated row.
        better = _ocr_line_quality(candidate) > _ocr_line_quality(existing)
        # Never trade a row's printed amount for a cleaner read without one
        # (RL-81: "Tax TX 8.25% ... 3.59" was replaced by "Tax TX 8.25%").
        loses_amount = (
            amount_at_end(existing.text) is not None
            and amount_at_end(candidate.text) is None
        )
        if (
            better
            and not loses_amount
            and not (is_multi_row(candidate) and not is_multi_row(existing))
        ):
            merged[index] = candidate
            origins[index] = {
                "origin": "enhanced_substitution",
                "standard_line": origins[index].get("standard_line"),
                "enhanced_line": recovery_index,
                "discarded_text": existing.text,
            }
        else:
            origins[index].setdefault("discarded_enhanced_lines", []).append(
                {"line": recovery_index, "text": candidate.text}
            )

    ordered = sorted(zip(merged, origins), key=lambda entry: (entry[0].y, entry[0].x))
    result = OcrResult(
        lines=tuple(line for line, _ in ordered),
        engine=f"{primary.engine}; conditional enhanced recovery",
    )
    return result, [origin for _, origin in ordered]


def _merge_ocr_passes(primary: OcrResult, recovery: OcrResult) -> OcrResult:
    """Compatibility wrapper for callers that need only merged OCR."""
    return _merge_ocr_passes_with_origins(primary, recovery)[0]


WALMART_TEXT = re.compile(r"\bWAL\s*[-*]?\s*MART\b|\bWALMART\b", re.IGNORECASE)
WALMART_TAX_FLAG_AT_END = re.compile(r"(?:^|\s|\d)(?P<flag>X|N)\s*$")
UPC_IN_ROW = re.compile(r"\d{8,14}")


def _borrow_walmart_tax_flags(
    base: OcrResult, other: OcrResult
) -> tuple[OcrResult, list[int]]:
    """Copy Walmart's trailing X/N from the other pass onto the same row.

    The X/N column sits at the right edge and fades first, so one pass can
    read it while the other drops it (RL-38: both SITE MERCH rows lost their
    X in the first pass).  Only product rows (with a UPC) that have no flag of
    their own receive one, and only from the closest line on the same row.
    Returns the updated OCR and the 1-based lines that received a flag.
    """

    if not any(WALMART_TEXT.search(line.text) for line in base.lines[:20]):
        return base, []
    lines = list(base.lines)
    changed: list[int] = []
    for index, line in enumerate(lines):
        if (
            line.box is None
            or not UPC_IN_ROW.search(line.text)
            or WALMART_TAX_FLAG_AT_END.search(line.text)
        ):
            continue
        same_row = [
            candidate
            for candidate in other.lines
            if UPC_IN_ROW.search(candidate.text) and _same_visual_row(line, candidate)
        ]
        if not same_row:
            continue
        center = (line.box[1] + line.box[3]) / 2
        closest = min(
            same_row,
            key=lambda candidate: abs(
                (candidate.box[1] + candidate.box[3]) / 2 - center  # type: ignore[index]
            ),
        )
        flag = WALMART_TAX_FLAG_AT_END.search(closest.text)
        if flag is None:
            continue
        lines[index] = OcrLine(f"{line.text} {flag.group('flag')}", line.confidence, line.box)
        changed.append(index + 1)
    return OcrResult(lines=tuple(lines), engine=base.engine), changed


def _items_exceed_subtotal(parsed: ParseResult) -> bool:
    subtotal = parsed.receipt.subtotal
    if subtotal is None:
        return False
    item_sum = sum(item.line_total for item in _purchased_items(parsed))
    return item_sum > subtotal + 0.02


def _recovery_lost_printed_amounts(initial: ParseResult, recovered: ParseResult) -> bool:
    """True when the retry dropped or overshot amounts the first read had right."""

    for field in ("subtotal", "tax", "total"):
        if (
            getattr(initial.receipt, field) is not None
            and getattr(recovered.receipt, field) is None
        ):
            return True
    return _items_exceed_subtotal(recovered) and not _items_exceed_subtotal(initial)


def _recovery_improved(initial: ParseResult, recovered: ParseResult) -> bool:
    if _recovery_lost_printed_amounts(initial, recovered):
        return False
    initial_items = len(_purchased_items(initial))
    recovered_items = len(_purchased_items(recovered))
    initial_shortfall = _subtotal_shortfall(initial)
    recovered_shortfall = _subtotal_shortfall(recovered)
    return bool(
        recovered_shortfall + 0.02 < initial_shortfall
        or (
            recovered_items > initial_items
            and recovered_shortfall <= initial_shortfall + 0.02
        )
    )


def process_receipt_locally(
    image_bytes: bytes,
    *,
    language: str = "en",
) -> LocalPipelineResult:
    """Run local OCR, then independently parse the recognized lines."""

    trace = OcrDiagnosticTrace(language=language, stage="standard_ocr")
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            trace.image_dimensions.append(image.size)
    except Exception:
        # OCR reports the user-facing image error; metadata is optional.
        pass
    try:
        ocr_result = extract_text_locally(image_bytes, language=language)
        trace.standard = ocr_result
        trace.stage = "parsing_standard_ocr"
        parsed_result = parse_receipt(ocr_result.lines)

        if _needs_enhanced_recovery(ocr_result, parsed_result):
            trace.enhanced_triggered = True
            trace.enhanced_reason = (
                "identified items below printed count"
                if _below_printed_count(ocr_result, parsed_result)
                else "parsed item subtotal is below printed subtotal"
            )
            trace.stage = "enhanced_ocr"
            recovery_ocr = extract_text_locally(image_bytes, language=language, enhanced=True)
            trace.enhanced = recovery_ocr
            combined_ocr, origins = _merge_ocr_passes_with_origins(ocr_result, recovery_ocr)
            combined_ocr, flagged = _borrow_walmart_tax_flags(combined_ocr, recovery_ocr)
            for line_number in flagged:
                origins[line_number - 1]["tax_flag_from_enhanced"] = True
            trace.merged, trace.merged_origins = combined_ocr, origins
            trace.stage = "parsing_merged_ocr"
            combined_parsed = parse_receipt(combined_ocr.lines)
            if _recovery_improved(parsed_result, combined_parsed):
                added_rows = len(_purchased_items(combined_parsed)) - len(_purchased_items(parsed_result))
                combined_parsed.receipt.notes.append(
                    "An enhanced local OCR recovery pass improved an incomplete "
                    f"first read and added {max(0, added_rows)} purchase "
                    f"{'row' if added_rows == 1 else 'rows'}."
                )
                trace.stage = "complete"
                return LocalPipelineResult(ocr=combined_ocr, parsed=combined_parsed, diagnostic=trace)
            # The merge did not add purchases, but the enhanced pass may still
            # have read tax flags the first pass lost.
            ocr_result, flagged = _borrow_walmart_tax_flags(ocr_result, recovery_ocr)
            if flagged:
                parsed_result = parse_receipt(ocr_result.lines)
                parsed_result.receipt.notes.append(
                    f"Walmart X/N tax flags on {len(flagged)} "
                    f"{'row' if len(flagged) == 1 else 'rows'} were read from an "
                    "enhanced OCR pass; verify them against the photo."
                )
        else:
            flagged = []
        trace.merged = ocr_result
        trace.merged_origins = [
            {"origin": "standard", "standard_line": index + 1}
            | ({"tax_flag_from_enhanced": True} if index + 1 in flagged else {})
            for index in range(len(ocr_result.lines))
        ]
        trace.stage = "complete"
        return LocalPipelineResult(ocr=ocr_result, parsed=parsed_result, diagnostic=trace)
    except Exception as exc:
        if isinstance(exc, DiagnosticPipelineError):
            raise
        trace.stage = f"failed:{trace.stage}"
        raise DiagnosticPipelineError(exc, trace) from exc


def _normalized_ocr_line(text: str) -> str:
    """Normalize harmless OCR spacing and punctuation differences."""

    return re.sub(r"[^A-Z0-9]+", "", text.upper())


def _boundary_overlap_size(
    previous_lines: list[OcrLine],
    next_lines: list[OcrLine],
    *,
    maximum: int = 60,
) -> int:
    """Return an exact repeated suffix/prefix shared by consecutive images.

    At least two consecutive lines must match. This avoids deleting a legitimate
    repeated product merely because it appears once at a page boundary.
    """

    largest = min(maximum, len(previous_lines), len(next_lines))
    for size in range(largest, 1, -1):
        previous = previous_lines[-size:]
        following = next_lines[:size]
        if all(
            _normalized_ocr_line(left.text)
            and _normalized_ocr_line(left.text)
            == _normalized_ocr_line(right.text)
            for left, right in zip(previous, following)
        ):
            return size
    return 0


def process_receipt_pages_locally(
    image_pages: list[bytes] | tuple[bytes, ...],
    *,
    language: str = "en",
) -> LocalPipelineResult:
    """Read ordered images as consecutive sections of one receipt.

    Page coordinates are offset before parsing so OCR rows from separate images
    cannot be accidentally merged. The caller must provide pages from top to
    bottom.
    """

    if not image_pages:
        raise ValueError("Select at least one receipt image.")

    trace = OcrDiagnosticTrace(language=language, stage="standard_ocr")
    combined_lines: list[OcrLine] = []
    y_offset = 0
    engines: list[str] = []
    previous_page_lines: list[OcrLine] = []
    overlap_lines_removed = 0

    try:
      for image_bytes in image_pages:
        try:
            with Image.open(BytesIO(image_bytes)) as image:
                trace.image_dimensions.append(image.size)
        except Exception:
            pass
        page = extract_text_locally(image_bytes, language=language)
        engines.append(page.engine)
        page_lines = list(page.lines)
        overlap_size = _boundary_overlap_size(previous_page_lines, page_lines)
        overlap_lines_removed += overlap_size
        lines_to_add = page_lines[overlap_size:]
        page_bottom = max((line.box[3] for line in page_lines if line.box is not None), default=0)
        for line in lines_to_add:
            box = line.box
            if box is not None:
                x1, y1, x2, y2 = box
                box = (x1, y1 + y_offset, x2, y2 + y_offset)
                page_bottom = max(page_bottom, y2)
            combined_lines.append(OcrLine(text=line.text, confidence=line.confidence, box=box))
        y_offset += max(page_bottom, 1) + 100
        previous_page_lines = page_lines
      engine = engines[0] if len(set(engines)) == 1 else " + ".join(dict.fromkeys(engines))
      if len(image_pages) > 1:
          engine = f"{engine}; {len(image_pages)} ordered images"

      ocr_result = OcrResult(lines=tuple(combined_lines), engine=engine)
      trace.standard = trace.merged = ocr_result
      trace.merged_origins = [{"origin": "standard", "standard_line": index + 1} for index in range(len(ocr_result.lines))]
      trace.stage = "parsing_standard_ocr"
      parsed_result = parse_receipt(ocr_result.lines)
      if overlap_lines_removed:
          parsed_result.receipt.notes.append(
              f"Removed {overlap_lines_removed} repeated OCR lines where consecutive "
              "receipt images overlapped. Verify the final item count."
            )
      trace.stage = "complete"
      return LocalPipelineResult(ocr=ocr_result, parsed=parsed_result, diagnostic=trace)
    except Exception as exc:
      trace.stage = f"failed:{trace.stage}"
      raise DiagnosticPipelineError(exc, trace) from exc
