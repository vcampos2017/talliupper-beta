"""H-E-B receipt layout.

H-E-B prints a single-unit purchase on one row::

    3 KEWPIE MAYONNAISE              F    5.37

but a multi-unit purchase on two rows: the numbered name, then a quantity row
that carries the charged line total::

    1 QUANTUM THERAZINC ELDRBRR
          3 Ea. @ 1/  2.77               8.31 Q

The general parser read that second row as the item, so the product name was
lost and the quantity became 1. This profile joins the two rows into the
one-row shape the parser already understands (``NAME 3@2.77 8.31``).

It also recognizes the coupon note H-E-B prints under a discounted item::

    COUPON          $1.00  orig   4.97 Ea.

The charged line total above it is already net of that coupon, so the note is
metadata about the previous item, never a separate purchase.

``ITEMS PURCHASED: 7`` on H-E-B receipts counts units (3 + 2 + 1 + 1), not
rows; the parser's unit-aware count check handles that for every store.
"""

from __future__ import annotations

import re

from ..local_ocr import OcrLine
from . import VendorProfile

_HEB_NAME = re.compile(r"\bH\s*[-.]?\s*E\s*[-.]?\s*B\b", re.IGNORECASE)
# Store code or web address: HEB195 (as in "TEXAS HEB195SON") or heb.com.
_HEB_TOKEN = re.compile(r"HEB\d{2,4}|\bHEB\.COM\b", re.IGNORECASE)
_ITEMS_PURCHASED = re.compile(r"\bITEMS\s+PURCHASED\s*:?\s*\d", re.IGNORECASE)

QUANTITY_ROW = re.compile(
    r"^\s*(?P<qty>\d{1,3})?\s*E\s*A\s*\.?\s*@\s*"
    r"(?P<bundle>\d{1,2})\s*/\s*\$?(?P<price>\d{1,5}\.\d{2})\s*"
    r"(?P<pre>FW|F|T|Q)?\s+\$?(?P<total>\d{1,6}\.\d{2})\s*"
    r"(?P<post>FW|F|T|Q)?\s*$",
    re.IGNORECASE,
)
COUPON_NOTE = re.compile(
    r"^\s*COUPON\b.*\bORIG\b.*\bE\s*A\s*\.?\s*$", re.IGNORECASE
)
_PRICE_AT_END = re.compile(r"\d\.\d{2}\s*[A-Z]{0,2}\s*$", re.IGNORECASE)
_SUMMARY = re.compile(r"\b(?:SUB\s*-?\s*TOTAL|TAX|TOTAL)\b", re.IGNORECASE)
# Tax markers the receipt model accepts. H-E-B also prints Q; its meaning is
# not documented here, so it is left blank (unknown) rather than guessed.
_SUPPORTED_MARKERS = {"FW", "F", "T"}


def detect(lines: list[OcrLine]) -> bool:
    """Recognize H-E-B from its name, or from its layout when the logo is unread.

    The H-E-B logo is a graphic, and OCR often misreads it ("E-", "E-B CHUR",
    "FOR qtuttt"). So besides the printed name near the top, H-E-B is also
    recognized from evidence anywhere on the receipt (long receipts print the
    store details in the footer):

    * ``HEB`` inside a token, such as the store code in ``TEXAS HEB195SON``
      or ``heb.com``;
    * its layout: an ``N Ea. @ 1/ price total`` quantity row together with
      the ``ITEMS PURCHASED:`` unit count (RL-60).
    """

    if any(_HEB_NAME.search(line.text) for line in lines[:15]):
        return True
    if any(_HEB_TOKEN.search(line.text) for line in lines):
        return True
    has_quantity_row = any(QUANTITY_ROW.match(line.text) for line in lines)
    has_unit_count = any(_ITEMS_PURCHASED.search(line.text) for line in lines)
    return has_quantity_row and has_unit_count


def _is_name_row(text: str) -> bool:
    stripped = text.strip()
    return (
        bool(stripped)
        and sum(character.isalpha() for character in stripped) >= 3
        and not _PRICE_AT_END.search(stripped)
        and not _SUMMARY.search(stripped)
        and not COUPON_NOTE.match(stripped)
        and not QUANTITY_ROW.match(stripped)
    )


def _quantity(match: re.Match[str]) -> tuple[float, float] | None:
    """Return (quantity, unit price) when the row's arithmetic reconciles."""

    bundle = float(match.group("bundle")) or 1.0
    unit_price = float(match.group("price")) / bundle
    total = float(match.group("total"))
    if unit_price <= 0:
        return None
    if match.group("qty"):
        quantity = float(match.group("qty"))
    else:
        # OCR can drop the quantity digit; recover it only when the charged
        # total divides into a whole number of units.
        quantity = round(total / unit_price)
    if quantity < 1 or abs(quantity * unit_price - total) > 0.02:
        return None
    return quantity, unit_price


def _combined(first: OcrLine, second: OcrLine, text: str) -> OcrLine:
    box = None
    if first.box is not None and second.box is not None:
        box = (
            min(first.box[0], second.box[0]),
            min(first.box[1], second.box[1]),
            max(first.box[2], second.box[2]),
            max(first.box[3], second.box[3]),
        )
    return OcrLine(text, min(first.confidence, second.confidence), box)


def preprocess(lines: list[OcrLine]) -> list[OcrLine]:
    """Join each numbered name row to the quantity row printed under it."""

    result: list[OcrLine] = []
    for line in lines:
        # OCR occasionally returns the same name region twice in a row.
        if (
            result
            and _is_name_row(line.text)
            and " ".join(line.text.split()).casefold()
            == " ".join(result[-1].text.split()).casefold()
        ):
            continue
        match = QUANTITY_ROW.match(line.text)
        if match and result and _is_name_row(result[-1].text):
            parsed = _quantity(match)
            if parsed is not None:
                quantity, unit_price = parsed
                marker = (match.group("post") or match.group("pre") or "").upper()
                marker_text = f" {marker}" if marker in _SUPPORTED_MARKERS else ""
                name_line = result.pop()
                text = (
                    f"{name_line.text.strip()} {quantity:g}@{unit_price:.2f}"
                    f"{marker_text} {float(match.group('total')):.2f}"
                )
                result.append(_combined(name_line, line, text))
                continue
        result.append(line)
    return result


def metadata_reason(text: str) -> str | None:
    if COUPON_NOTE.match(text):
        return (
            "H-E-B coupon note; the item's charged line total above is already "
            "net of this coupon"
        )
    return None


PROFILE = VendorProfile(
    name="H-E-B",
    detect=detect,
    preprocess=preprocess,
    metadata_reason=metadata_reason,
)
