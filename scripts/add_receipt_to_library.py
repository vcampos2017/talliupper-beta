"""Add a receipt to the receipt library from an OCR Diagnostic JSON download.

Usage (from the repository folder):

    python scripts/add_receipt_to_library.py PATH_TO_DIAGNOSTIC.json --merchant heb

This creates ``tests/receipt_library/<merchant>/<id>/`` with:

* ``ocr.json`` — the OCR lines only (no parser output, no image), redacted
  again with TalliUpper's redaction rules.
* ``expected.json`` — a DRAFT filled in from what the parser reads today,
  marked ``"reviewed": false``.

Then open the receipt photo and correct expected.json until it describes the
receipt exactly. Set ``"reviewed": true`` when it does. Drafts are scored but
never fail CI; reviewed fixtures do.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from receipt_reader.ocr_diagnostic import redact_text  # noqa: E402
from receipt_reader.parser import parse_receipt, printed_item_count  # noqa: E402
from receipt_reader.receipt_library import (  # noqa: E402
    LIBRARY_ROOT,
    mask_payment_brands,
    ocr_lines_from_json,
)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Add a receipt to the receipt library.")
    parser.add_argument("diagnostic", type=Path, help="OCR Diagnostic JSON downloaded from TalliUpper")
    parser.add_argument("--merchant", required=True, help="short folder name, e.g. heb, walmart")
    parser.add_argument("--id", help="receipt folder name (default: today's date + item count)")
    args = parser.parse_args(argv)

    payload = json.loads(args.diagnostic.read_text(encoding="utf-8"))
    lines = ocr_lines_from_json(payload)
    if not lines:
        print("No OCR lines found in that file.")
        return 1

    result = parse_receipt(list(lines))
    receipt = result.receipt
    receipt_id = args.id or f"{receipt.transaction_date or date.today().isoformat()}-{len(receipt.items)}-items"
    folder = LIBRARY_ROOT / _slug(args.merchant) / _slug(receipt_id)
    if folder.exists():
        print(f"{folder.relative_to(PROJECT_ROOT)} already exists; choose another --id.")
        return 1
    folder.mkdir(parents=True)

    ocr = {
        "source": f"TalliUpper OCR Diagnostic JSON ({payload.get('metadata', {}).get('ocr_version', 'unknown OCR')})",
        "redaction": "TalliUpper best-effort redaction applied; card brands and banks replaced with CREDIT CARD. Check every line before committing.",
        "lines": [
            {"text": mask_payment_brands(redact_text(line.text)), "confidence": line.confidence, "box": line.box}
            for line in lines
        ],
    }
    expected = {
        "reviewed": False,
        "reviewed_by": "",
        "layout_notes": "",
        "store_name": receipt.store_name,
        "transaction_date": receipt.transaction_date,
        "items": [
            {
                "name": item.name,
                "quantity": item.quantity,
                "line_total": item.line_total,
                "tax_marker": item.tax_marker,
            }
            for item in receipt.items
        ],
        "subtotal": receipt.subtotal,
        "tax": receipt.tax,
        "total": receipt.total,
        "printed_item_count": printed_item_count("\n".join(line.text for line in lines)),
    }
    (folder / "ocr.json").write_text(json.dumps(ocr, indent=2) + "\n", encoding="utf-8")
    (folder / "expected.json").write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")

    print(f"Created {folder.relative_to(PROJECT_ROOT)}")
    print("Next: compare expected.json with the receipt photo, fix it, and set \"reviewed\": true.")
    print("Then check ocr.json for anything personal before committing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
