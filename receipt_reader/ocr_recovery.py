"""Clean and place OCR lines that the reviewer recovers by hand.

When the parser cannot read a row's price, the review screen offers the raw
OCR line so the reviewer can add it.  Those lines need the same cleanup as
rows the parser read directly: product code in its own column, OCR noise after
the code removed, Walmart's X/N tax flag kept, and the row placed where it was
printed instead of at the end of the table (RL-38).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
from typing import Any


# A product name, then an 8-14 digit UPC.  Walmart prints its food-eligibility
# F right after the code and OCR often glues it on ("024300865400F").
NAME_THEN_CODE = re.compile(
    r"^(?P<name>.*?[A-Za-z].*?)\s+(?P<code>\d{8,14})(?:F(?![A-Za-z]))?(?=\s|$)(?P<rest>.*)$"
)
WALMART_TRAILING_FLAG = re.compile(r"(?:^|\s)(?P<flag>X|N)\s*$")
ITEM_ROLES = {"item"}


def _readable_name(text: str) -> str:
    """Drop characters an English receipt never prints (OCR misreads)."""

    ascii_only = "".join(character for character in text if ord(character) < 128)
    return re.sub(r"\s+", " ", ascii_only).strip(" :-")


# One standalone 8-14 digit UPC/SKU anywhere in a description.  Walmart's
# food-eligibility F may be glued to the end of it ("024300865400F").
STANDALONE_CODE = re.compile(r"(?<![\w.$/-])(?P<code>\d{8,14})F?(?![\w.$/-])")
MIN_NAME_LETTERS = 2


def split_product_code(text: str) -> tuple[str, str | None]:
    """Move one printed product code out of an item description.

    Receipts print the UPC/SKU before the name (Home Depot) or after it
    (Walmart).  When the description starts or ends with the only standalone
    8-14 digit code in it, return the description without it and the code;
    everything else (words, tax markers) is kept as printed.  Prices, dates
    and part numbers with dots, dashes or slashes are never treated as codes,
    and the text is left alone when it is ambiguous (two codes) or when no
    real name would remain.
    """

    original = re.sub(r"\s+", " ", text).strip()
    matches = list(STANDALONE_CODE.finditer(original))
    if len(matches) != 1:
        return original, None
    match = matches[0]
    if match.start() != 0 and match.end() != len(original):
        return original, None
    name = (original[: match.start()] + " " + original[match.end():])
    name = re.sub(r"\s+", " ", name).strip(" :-")
    if sum(character.isalpha() for character in name) < MIN_NAME_LETTERS:
        return original, None
    return name, match.group("code")


def clean_recovered_line(text: str, store_name: str | None) -> dict[str, Any]:
    """Split one unused OCR line into name, product code and tax flag.

    Only the name and code are taken from the line; anything OCR read after
    the code is noise from a faded price column and is left out of the name.
    A Walmart X (taxed) or N (not taxed) is kept only when it stands alone at
    the end of the line, so a smudge such as ``NNX`` is never guessed at.
    """

    match = NAME_THEN_CODE.match(text.strip())
    if match is None:
        return {"name": _readable_name(text), "product_code": None, "tax_marker": None}
    marker = None
    if store_name == "Walmart":
        flag = WALMART_TRAILING_FLAG.search(match.group("rest"))
        marker = flag.group("flag") if flag else None
    return {
        "name": _readable_name(match.group("name")),
        "product_code": match.group("code"),
        "tax_marker": marker,
    }


def printed_position(
    source_line: int,
    line_decisions: Iterable[Mapping[str, Any]],
    recovered_lines: Iterable[int],
    item_count: int,
) -> int:
    """Return the table index where a recovered OCR line was printed.

    Parsed items keep the order of their OCR lines, so the row belongs after
    every parsed or earlier-recovered item printed above it.
    """

    above = sum(
        1
        for decision in line_decisions
        if str(decision.get("role")) in ITEM_ROLES
        and int(decision.get("index", 0)) < source_line
    )
    above += sum(1 for line in recovered_lines if int(line) < source_line)
    return max(0, min(above, item_count))


def clean_candidate(
    candidate: Mapping[str, Any], store_name: str | None
) -> dict[str, Any]:
    """Return a copy of a missing-item candidate with its name cleaned up."""

    cleaned = dict(candidate)
    if cleaned.get("product_code"):
        return cleaned
    parts = clean_recovered_line(str(candidate.get("name") or ""), store_name)
    if parts["product_code"] is None:
        return cleaned
    cleaned["name"] = parts["name"]
    cleaned["product_code"] = parts["product_code"]
    if parts["tax_marker"]:
        cleaned["tax_marker"] = parts["tax_marker"]
    elif store_name == "Walmart" and cleaned.get("tax_marker") == "F":
        # Walmart's F after the UPC is food eligibility, not tax status.
        cleaned["tax_marker"] = None
    return cleaned
