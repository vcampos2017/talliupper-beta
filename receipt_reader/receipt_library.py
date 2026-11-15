"""Receipt library: real receipts kept as regression fixtures, and a scorer.

Each fixture is a folder under ``tests/receipt_library/<merchant>/<receipt-id>/``:

* ``ocr.json`` — the OCR lines for that receipt. Either TalliUpper's own
  "Download OCR Diagnostic JSON" export (``talliupper-ocr-diagnostic-v1``) or
  the simple form ``{"lines": [{"text": ..., "confidence": ..., "box": ...}]}``.
* ``expected.json`` — what a correct reading of the receipt is, written by a
  person looking at the photo.

The parser is run on the OCR lines and compared with ``expected.json`` check by
check. ``scripts/score_receipts.py`` prints a scoreboard across the whole
library; ``tests/test_receipt_library.py`` fails CI when a reviewed fixture
that used to pass stops passing.

No receipt images are stored. Fixtures must be redacted before they are
committed (see ``tests/receipt_library/README.md``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .local_ocr import OcrLine
# Brand masking lives with the diagnostic redaction so exports and library
# fixtures follow one rule. Re-exported for the library script and tests.
from .ocr_diagnostic import PAYMENT_BRAND, PAYMENT_PLACEHOLDER, mask_payment_brands  # noqa: F401
from .parser import ParseResult, parse_receipt, printed_count_matches, purchased_unit_count

LIBRARY_ROOT = Path(__file__).resolve().parents[1] / "tests" / "receipt_library"
# Printed amounts are read, not calculated, so they must match to the cent.
# A one-cent difference is how a misread digit shows up (RL-43: Rockler tax
# $0.89 read for $0.90); the half cent only absorbs float rounding.
MONEY_TOLERANCE = 0.005

# Checks reported for every fixture, in display order.
CHECKS = (
    "store",
    "date",
    "item_count",
    "names",
    "quantities",
    "line_totals",
    "tax_markers",
    "subtotal",
    "tax",
    "total",
    "reconciles",
    "printed_count",
    "no_false_missing",
)


@dataclass(frozen=True, slots=True)
class Fixture:
    path: Path
    merchant: str
    receipt_id: str
    lines: tuple[OcrLine, ...]
    expected: dict[str, Any]

    @property
    def name(self) -> str:
        return f"{self.merchant}/{self.receipt_id}"

    @property
    def reviewed(self) -> bool:
        return bool(self.expected.get("reviewed"))

    @property
    def known_issue(self) -> str | None:
        return self.expected.get("known_issue") or None


@dataclass(slots=True)
class FixtureScore:
    fixture: Fixture
    checks: dict[str, bool | None] = field(default_factory=dict)
    details: dict[str, str] = field(default_factory=dict)

    @property
    def scored(self) -> list[str]:
        return [name for name, result in self.checks.items() if result is not None]

    @property
    def failed(self) -> list[str]:
        return [name for name, result in self.checks.items() if result is False]

    @property
    def passed(self) -> bool:
        return not self.failed


def _box(value: Any) -> tuple[int, int, int, int] | None:
    if not value:
        return None
    if isinstance(value[0], (list, tuple)):
        # Four corner points: reduce to x1, y1, x2, y2.
        xs = [point[0] for point in value]
        ys = [point[1] for point in value]
        return (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
    return tuple(int(v) for v in value[:4])  # type: ignore[return-value]


def ocr_lines_from_json(payload: dict[str, Any]) -> tuple[OcrLine, ...]:
    """Read OCR lines from a diagnostic export or the simple fixture form."""

    if payload.get("format") == "talliupper-ocr-diagnostic-v1":
        passes = payload.get("ocr_passes") or {}
        chosen = passes.get("merged") or passes.get("standard") or passes.get("enhanced")
        raw_lines = (chosen or {}).get("lines") or []
    else:
        raw_lines = payload.get("lines") or []
    return tuple(
        OcrLine(
            str(line.get("text", "")),
            float(line.get("confidence", 0.9)),
            _box(line.get("box")),
        )
        for line in raw_lines
        if str(line.get("text", "")).strip()
    )


def load_fixture(path: Path) -> Fixture:
    ocr = json.loads((path / "ocr.json").read_text(encoding="utf-8"))
    expected = json.loads((path / "expected.json").read_text(encoding="utf-8"))
    return Fixture(
        path=path,
        merchant=path.parent.name,
        receipt_id=path.name,
        lines=ocr_lines_from_json(ocr),
        expected=expected,
    )


def discover_fixtures(root: Path = LIBRARY_ROOT) -> list[Fixture]:
    return [
        load_fixture(expected.parent)
        for expected in sorted(root.glob("*/*/expected.json"))
        if (expected.parent / "ocr.json").exists()
    ]


def _money_equal(actual: float | None, expected: float | None) -> bool:
    if actual is None or expected is None:
        return actual is None and expected is None
    return abs(float(actual) - float(expected)) <= MONEY_TOLERANCE


def _normalize(name: str) -> str:
    return " ".join(str(name).upper().split())


def score_fixture(fixture: Fixture, result: ParseResult | None = None) -> FixtureScore:
    """Compare the parser's reading of a fixture with its expected result.

    A check is ``None`` (not scored) when expected.json leaves that field out,
    so a fixture can be added before every detail has been written down.
    """

    result = result or parse_receipt(list(fixture.lines))
    receipt = result.receipt
    expected = fixture.expected
    score = FixtureScore(fixture)
    checks = score.checks
    details = score.details

    def record(name: str, ok: bool | None, detail: str = "") -> None:
        checks[name] = ok
        if ok is False and detail:
            details[name] = detail

    record(
        "store",
        receipt.store_name == expected["store_name"] if "store_name" in expected else None,
        f"got {receipt.store_name!r}",
    )
    record(
        "date",
        receipt.transaction_date == expected["transaction_date"]
        if "transaction_date" in expected else None,
        f"got {receipt.transaction_date!r}",
    )

    expected_items = expected.get("items")
    items = receipt.items
    if expected_items is None:
        for name in ("item_count", "names", "quantities", "line_totals", "tax_markers"):
            record(name, None)
    else:
        record("item_count", len(items) == len(expected_items),
               f"got {len(items)} rows, expected {len(expected_items)}")
        pairs = list(zip(items, expected_items))
        same_length = len(items) == len(expected_items)
        bad_names = [
            f"{_normalize(item.name)!r} vs {_normalize(want['name'])!r}"
            for item, want in pairs
            if "name" in want and _normalize(item.name) != _normalize(want["name"])
        ]
        record("names", same_length and not bad_names, "; ".join(bad_names[:3]))
        bad_qty = [
            f"{_normalize(want.get('name', '?'))}: {item.quantity:g} vs {want['quantity']:g}"
            for item, want in pairs
            if "quantity" in want and abs(item.quantity - float(want["quantity"])) > 1e-6
        ]
        record("quantities", same_length and not bad_qty, "; ".join(bad_qty[:3]))
        bad_totals = [
            f"{_normalize(want.get('name', '?'))}: {item.line_total:.2f} vs {want['line_total']:.2f}"
            for item, want in pairs
            if "line_total" in want and not _money_equal(item.line_total, want["line_total"])
        ]
        record("line_totals", same_length and not bad_totals, "; ".join(bad_totals[:3]))
        marker_pairs = [(item, want) for item, want in pairs if "tax_marker" in want]
        bad_markers = [
            f"{_normalize(want.get('name', '?'))}: {item.tax_marker} vs {want['tax_marker']}"
            for item, want in marker_pairs
            if item.tax_marker != want["tax_marker"]
        ]
        record("tax_markers", (same_length and not bad_markers) if marker_pairs else None,
               "; ".join(bad_markers[:3]))

    for name in ("subtotal", "tax", "total"):
        record(
            name,
            _money_equal(getattr(receipt, name), expected[name]) if name in expected else None,
            f"got {getattr(receipt, name)}, expected {expected.get(name)}",
        )

    item_sum = round(sum(item.line_total for item in items), 2)
    record(
        "reconciles",
        _money_equal(item_sum, receipt.subtotal) if receipt.subtotal is not None and items else False,
        f"items add to {item_sum:.2f}, subtotal {receipt.subtotal}",
    )

    printed = expected.get("printed_item_count")
    units = purchased_unit_count(item.quantity for item in items)
    record(
        "printed_count",
        printed_count_matches(printed, len(items), units) if printed is not None else None,
        f"{len(items)} rows / {units} units vs printed {printed}",
    )
    record(
        "no_false_missing",
        not result.missing_item_candidates if expected_items is not None
        and checks.get("item_count") else None,
        f"{len(result.missing_item_candidates)} missing-item prompt(s) on a complete receipt",
    )
    return score


def score_library(root: Path = LIBRARY_ROOT) -> list[FixtureScore]:
    return [score_fixture(fixture) for fixture in discover_fixtures(root)]
