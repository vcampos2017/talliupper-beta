"""Printable pre-save review report for a corrected receipt."""

from __future__ import annotations

from html import escape
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .models import ReceiptExtraction
from .walmart_invoice import printed_order_savings
from .review import (
    is_gratuity_item_name,
    possible_split_labels,
    potential_duplicate_labels,
)


def _money(value: float | None) -> str:
    return "—" if value is None else "$" + f"{value:,.2f}"


def review_report_filename(receipt: ReceiptExtraction) -> str:
    merchant = re.sub(r"[^a-z0-9]+", "-", receipt.store_name.casefold()).strip("-")
    date = receipt.transaction_date or "undated"
    return f"receipt-review-{date}-{merchant or 'merchant'}.html"


def build_printable_item_table_html(
    rows: Sequence[Mapping[str, Any]],
) -> str:
    """Return a print-only HTML item table that does not depend on a grid canvas."""

    body_rows: list[str] = []

    def cell_text(value: object, fallback: str = "") -> str:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return fallback
        cleaned = str(value).strip()
        return cleaned if cleaned and cleaned.casefold() != "nan" else fallback

    for number, row in enumerate(rows, start=1):
        receipt_order = row.get("Receipt order")
        try:
            display_number = str(int(float(receipt_order)))
        except (TypeError, ValueError):
            display_number = str(number)
        quantity = row.get("Qty", 1)
        try:
            quantity_text = f"{float(quantity):g}"
        except (TypeError, ValueError):
            quantity_text = ""
        try:
            line_price_value = float(row.get("Line total"))
            line_price = (
                "—" if not math.isfinite(line_price_value) else _money(line_price_value)
            )
        except (TypeError, ValueError):
            line_price = "—"
        body_rows.append(
            "<tr>"
            f"<td>{escape(display_number)}</td>"
            f"<td>{escape(cell_text(row.get('Item')))}</td>"
            f"<td>{escape(cell_text(row.get('Product code')))}</td>"
            f"<td>{escape(cell_text(row.get('Category'), 'Uncategorized'))}</td>"
            f"<td>{escape(quantity_text)}</td>"
            f"<td>{escape(line_price)}</td>"
            "</tr>"
        )
    return (
        '<div class="receipt-print-only">'
        '<h3>Reviewed charge rows</h3>'
        '<table class="receipt-print-table">'
        '<thead><tr><th>Row</th><th>Item or charge</th><th>UPC / SKU</th>'
        '<th>Category</th><th>Qty</th><th>Line price</th></tr></thead>'
        f"<tbody>{''.join(body_rows)}</tbody></table></div>"
    )


def build_review_report_html(
    receipt: ReceiptExtraction,
    *,
    expected_count: int | None = None,
) -> str:
    """Return a standalone HTML report the user can open and print before saving."""
    calculated_subtotal = round(
        sum(
            item.line_total
            for item in receipt.items
            if not is_gratuity_item_name(item.name)
        ),
        2,
    )
    gratuity_total = round(
        sum(
            item.line_total
            for item in receipt.items
            if is_gratuity_item_name(item.name)
        ),
        2,
    )
    savings = printed_order_savings(receipt.notes)
    expected_purchases = (
        None if receipt.subtotal is None else
        round(receipt.subtotal - (savings or 0.0), 2)
    )
    subtotal_difference = (
        None if expected_purchases is None else
        round(calculated_subtotal - expected_purchases, 2)
    )
    duplicate_rows = [
        {
            "Item": item.name,
            "Product code": item.product_code,
            "Qty": item.quantity,
            "Line total": item.line_total,
        }
        for item in receipt.items
    ]
    duplicate_labels = potential_duplicate_labels(duplicate_rows)
    split_labels = possible_split_labels(duplicate_rows)

    item_rows = []
    for number, (item, duplicate_label, split_label) in enumerate(
        zip(receipt.items, duplicate_labels, split_labels), start=1
    ):
        warnings = [label for label in (duplicate_label, split_label) if label]
        warning = "".join(
            f'<span class="warning">{escape(label)}</span>' for label in warnings
        )
        item_rows.append(
            "<tr>"
            f"<td>{number}</td>"
            f"<td>{escape(item.name)}{warning}</td>"
            f"<td>{escape(item.product_code or '')}</td>"
            f"<td>{escape(item.category)}</td>"
            f"<td>{item.quantity:g}</td>"
            f"<td>{_money(item.line_total)}</td>"
            "</tr>"
        )

    count_check = (
        "Printed item count unavailable"
        if expected_count is None
        else (
            f"{sum(not is_gratuity_item_name(item.name) for item in receipt.items)} "
            f"entered / {expected_count} printed"
        )
    )
    difference_text = (
        "Printed purchase amount unavailable"
        if subtotal_difference is None
        else _money(subtotal_difference)
    )
    mismatch_class = (
        "mismatch"
        if subtotal_difference is not None and abs(subtotal_difference) > 0.02
        else "match"
    )

    savings_cards = (
        '<div class="card"><div class="label">Printed savings</div>'
        f'<div class="value">−{_money(savings)}</div></div>'
        '<div class="card"><div class="label">Expected purchases after savings</div>'
        f'<div class="value">{_money(expected_purchases)}</div></div>'
        if savings is not None else ""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>TalliUpper pre-save review</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 32px; color: #17202a; }}
h1 {{ margin-bottom: 4px; }}
.subtitle {{ color: #53616f; margin-top: 0; }}
.summary {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin: 20px 0; }}
.card {{ border: 1px solid #ccd4dc; border-radius: 8px; padding: 12px; }}
.label {{ color: #53616f; font-size: 12px; text-transform: uppercase; }}
.value {{ font-size: 18px; font-weight: 700; margin-top: 4px; }}
table {{ width: 100%; border-collapse: collapse; table-layout: fixed; font-size: 12px; }}
th, td {{ border: 1px solid #ccd4dc; padding: 6px; text-align: left; vertical-align: top; overflow-wrap: anywhere; }}
th {{ background: #edf3f8; }}
th:nth-child(1), td:nth-child(1) {{ width: 5%; }}
th:nth-child(2), td:nth-child(2) {{ width: 39%; }}
th:nth-child(3), td:nth-child(3) {{ width: 14%; }}
th:nth-child(4), td:nth-child(4) {{ width: 20%; }}
th:nth-child(5), td:nth-child(5) {{ width: 7%; white-space: nowrap; }}
th:nth-child(6), td:nth-child(6) {{ width: 15%; white-space: nowrap; }}
.warning {{ display: block; color: #a04400; font-size: 11px; font-weight: 700; }}
.mismatch {{ color: #b42318; }}
.match {{ color: #16794b; }}
.print-button {{ padding: 10px 16px; margin-bottom: 16px; }}
.footer {{ margin-top: 18px; color: #53616f; font-size: 12px; }}
@page {{ size: Letter portrait; margin: 12mm; }}
@media print {{
  .print-button {{ display: none; }}
  body {{ margin: 12mm; }}
  thead {{ display: table-header-group; }}
  tr {{ break-inside: avoid; }}
}}
</style>
</head>
<body>
<button class="print-button" onclick="window.print()">Print this list</button>
<h1>TalliUpper pre-save review</h1>
<p class="subtitle">Not yet saved — compare this report with the original receipt.</p>
<p><strong>Store:</strong> {escape(receipt.store_name or "Unknown")}<br>
<strong>Date:</strong> {escape(receipt.transaction_date or "Unknown")}<br>
<strong>Currency:</strong> {escape(receipt.currency)}</p>
<div class="summary">
  <div class="card"><div class="label">Items</div><div class="value">{escape(count_check)}</div></div>
  <div class="card"><div class="label">Reviewed purchases</div><div class="value">{_money(calculated_subtotal)}</div></div>
  <div class="card"><div class="label">Tip / gratuity</div><div class="value">{_money(gratuity_total)}</div></div>
  <div class="card"><div class="label">Printed subtotal before savings</div><div class="value">{_money(receipt.subtotal)}</div></div>
  {savings_cards}
  <div class="card"><div class="label">Purchase difference</div><div class="value {mismatch_class}">{difference_text}</div></div>
  <div class="card"><div class="label">Printed tax</div><div class="value">{_money(receipt.tax)}</div></div>
  <div class="card"><div class="label">Printed total</div><div class="value">{_money(receipt.total)}</div></div>
</div>
<table>
<thead><tr><th>Row</th><th>Item</th><th>UPC / SKU</th><th>Category</th><th>Qty</th><th>Line price</th></tr></thead>
<tbody>{''.join(item_rows)}</tbody>
</table>
<p class="footer">Generated from the current edited TalliUpper table before saving. Correct the app—not this printout—if anything differs from the original receipt.</p>
</body>
</html>"""
