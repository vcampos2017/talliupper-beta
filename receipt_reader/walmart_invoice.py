"""Parse selectable-text Walmart.com order invoices across every PDF page.

This path is limited to the recognizable Walmart order-details layout. Other
PDFs continue through the image OCR pipeline.
"""

from __future__ import annotations

import re
from datetime import datetime

import pypdfium2 as pdfium

from .local_ocr import OcrLine, OcrResult
from .local_pipeline import LocalPipelineResult
from .models import ReceiptExtraction, ReceiptItem
from .parser import LineDecision, ParseResult
from .pdf_utils import _PDF_LOCK

_ITEM_END = re.compile(
    r"(?P<status>Unavailable|(?:\d+\s+shopped)|(?:\d+\s+weight adjusted))?\s*"
    r"Qty\s*(?P<qty>\d+)\s*\$(?P<price>\d+(?:,\d{3})*\.\d{2})\s*$",
    re.IGNORECASE,
)
_MONEY = re.compile(r"^\$?(?P<amount>\d+(?:,\d{3})*\.\d{2})$")
_TOTAL = re.compile(r"^(?P<label>Subtotal|Savings|Taxes|Total|InHome delivery)\s+"
                    r"(?P<amount>-?\$?\d+(?:,\d{3})*\.\d{2})$", re.I)
_ORDER_DATE = re.compile(r"^([A-Za-z]+\s+\d{1,2},?\s+20\d{2})\s+order$", re.I)
_HEADER = re.compile(
    r"^(?:Order details - Walmart\.com|https?://|Page \d+ of \d+|"
    r"Invoice$|Buyer$|Order#|Walmart$)", re.I
)


def parse_walmart_invoice_pdf(data: bytes) -> LocalPipelineResult | None:
    """Return a reviewed draft for a text invoice, or None for another PDF.

    Monetary values on the order page can differ from the final charged total
    (unavailable and adjusted products, savings). Never infer missing discounts.
    """
    with _PDF_LOCK, pdfium.PdfDocument(data) as document:
        pages = []
        for page in document:
            textpage = page.get_textpage()
            try:
                pages.append(textpage.get_text_bounded().replace("\x02", "-"))
            finally:
                textpage.close()
    if not pages or not re.search(r"Walmart\.com", pages[0], re.I) or not re.search(
        r"\bInvoice\b", pages[0], re.I
    ) or not re.search(r"\bQty\s*\d+\s*\$\d", "\n".join(pages), re.I):
        return None

    items = []
    decisions = []
    ocr_lines = []
    totals = {}
    order_date = None
    pending = []
    in_items = False
    unavailable = 0
    buyer_lines_remaining = 0
    for page_number, text in enumerate(pages, start=1):
        for raw in text.splitlines():
            line = " ".join(raw.split()).strip()
            if not line:
                continue
            index = len(ocr_lines)
            ocr_lines.append(OcrLine(line, 1.0))
            role, reason = "ignored", "Invoice heading or metadata"
            date_match = _ORDER_DATE.match(line)
            if date_match and order_date is None:
                try:
                    order_date = datetime.strptime(
                        date_match.group(1).replace(",", ""), "%b %d %Y"
                    ).date().isoformat()
                except ValueError:
                    pass
            total_match = _TOTAL.match(line)
            if total_match:
                pending.clear()
                label = total_match.group("label").casefold()
                totals[label] = float(total_match.group("amount").replace("$", "").replace(",", ""))
                role, reason = "summary", "Printed invoice amount"
                in_items = False
            elif line.casefold() == "buyer":
                buyer_lines_remaining = 2
            elif buyer_lines_remaining:
                buyer_lines_remaining -= 1
            elif _HEADER.match(line) or date_match:
                pass
            elif re.match(r"^\d+\s+shopped\s+Qty", line, re.I) and not pending:
                role, reason = "ignored", "Quantity has no preceding item description"
            elif (match := _ITEM_END.search(line)) and not totals:
                description = line[:match.start()].strip()
                name = " ".join([*pending, description]).strip()
                pending.clear()
                name = re.sub(r"\s+(?:Unavailable|\d+\s+shopped|\d+\s+weight adjusted)$",
                              "", name, flags=re.I)
                if name:
                    status = (match.group("status") or "").strip()
                    if status.casefold() == "unavailable":
                        unavailable += 1
                    qty = float(match.group("qty"))
                    price = float(match.group("price").replace(",", ""))
                    items.append(ReceiptItem(
                        name=name, quantity=qty, unit_price=None,
                        discount=None, line_total=price,
                    ))
                    role, reason = "item", f"Invoice item on page {page_number}; verify displayed price"
                    in_items = True
            elif not totals and (in_items or "order" in text.casefold()) and not (
                _MONEY.match(line) or line.startswith("Order#")
            ):
                pending.append(line)
                role, reason = "description", "Wrapped product description"
            decisions.append(LineDecision(index, line, 1.0, role, reason))

    if not items:
        return None
    notes = [
        f"Read {len(pages)} Walmart invoice pages from selectable PDF text. "
        "Confirm quantities and displayed prices against the invoice."
    ]
    if pending:
        notes.append("Some text after the last priced row was not assigned to an item.")
    if unavailable:
        notes.append(f"{unavailable} listed item(s) say Unavailable; confirm whether they were charged.")
    item_sum = round(sum(item.line_total for item in items), 2)
    subtotal = totals.get("subtotal")
    if subtotal is None or abs(item_sum - subtotal) > 0.01:
        notes.append(
            f"Listed item prices sum to ${item_sum:.2f}; printed subtotal is "
            + (f"${subtotal:.2f}" if subtotal is not None else "missing")
            + ". Review unavailable or adjusted items and discounts before saving."
        )
    if "savings" in totals:
        notes.append(
            f"Printed order savings: ${abs(totals['savings']):.2f}; "
            "compare reviewed line prices with the after-savings amount."
        )
    receipt = ReceiptExtraction(
        store_name="Walmart", transaction_date=order_date, currency="USD",
        items=items, subtotal=subtotal, tax=totals.get("taxes"),
        total=totals.get("total"), confidence=0.8, notes=notes,
    )
    ocr = OcrResult(lines=tuple(ocr_lines), engine="pdf-text-walmart-invoice")
    return LocalPipelineResult(
        ocr=ocr, parsed=ParseResult(receipt=receipt, decisions=tuple(decisions))
    )


_SAVINGS_NOTE = re.compile(r"^Printed order savings: \$(\d+(?:,\d{3})*\.\d{2});")


def printed_order_savings(notes: list[str]) -> float | None:
    """Read the printed savings retained with a Walmart invoice receipt."""
    for note in notes:
        match = _SAVINGS_NOTE.match(note)
        if match:
            return float(match.group(1).replace(",", ""))
    return None
