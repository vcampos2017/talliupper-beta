"""Compact PDF export for a receipt that has already been saved."""

from __future__ import annotations

from html import escape
from io import BytesIO
import re
from typing import Any, Mapping

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .review import is_gratuity_item_name


def _money(value: object, currency: str = "USD") -> str:
    if value is None:
        return "-"
    symbol = "$" if currency.upper() == "USD" else f"{currency.upper()} "
    return f"{symbol}{float(value):,.2f}"


def _text(value: object, fallback: str = "") -> str:
    cleaned = str(value or "").strip()
    return escape(cleaned or fallback)


def saved_receipt_pdf_filename(receipt: Mapping[str, Any]) -> str:
    merchant = re.sub(
        r"[^a-z0-9]+", "-", str(receipt.get("store_name") or "merchant").casefold()
    ).strip("-")
    receipt_date = str(receipt.get("transaction_date") or "undated")
    receipt_id = receipt.get("id")
    suffix = f"-{receipt_id}" if receipt_id is not None else ""
    return f"receipt-{receipt_date}-{merchant or 'merchant'}{suffix}.pdf"


def build_saved_receipt_pdf(receipt: Mapping[str, Any]) -> bytes:
    """Build a compact, printable PDF directly from a saved receipt record."""
    output = BytesIO()
    page_size = landscape(letter)
    document = SimpleDocTemplate(
        output,
        pagesize=page_size,
        leftMargin=0.38 * inch,
        rightMargin=0.38 * inch,
        topMargin=0.38 * inch,
        bottomMargin=0.40 * inch,
        title="TalliUpper saved receipt",
        author="TalliUpper",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReceiptTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=21,
        textColor=colors.HexColor("#17202A"),
        alignment=TA_LEFT,
        spaceAfter=4,
    )
    detail_style = ParagraphStyle(
        "ReceiptDetail",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=8.5,
        leading=10.5,
        textColor=colors.HexColor("#344054"),
    )
    cell_style = ParagraphStyle(
        "ReceiptCell",
        parent=detail_style,
        fontSize=7.4,
        leading=8.6,
        textColor=colors.HexColor("#17202A"),
    )
    header_style = ParagraphStyle(
        "ReceiptHeader",
        parent=cell_style,
        fontName="Helvetica-Bold",
        textColor=colors.white,
    )

    currency = str(receipt.get("currency") or "USD")
    items = list(receipt.get("items") or [])
    calculated_subtotal = round(
        sum(
            float(item.get("line_total") or 0.0)
            for item in items
            if not is_gratuity_item_name(item.get("name"))
        ),
        2,
    )
    gratuity_total = round(
        sum(
            float(item.get("line_total") or 0.0)
            for item in items
            if is_gratuity_item_name(item.get("name"))
        ),
        2,
    )
    heading = f"TalliUpper saved receipt #{receipt.get('id', '')}".rstrip(" #")
    details = (
        f"<b>Store:</b> {_text(receipt.get('store_name'), 'Unknown')} &nbsp;&nbsp; "
        f"<b>Date:</b> {_text(receipt.get('transaction_date'), 'Unknown')} &nbsp;&nbsp; "
        f"<b>Currency:</b> {_text(currency)} &nbsp;&nbsp; "
        f"<b>Items:</b> {len(items)}"
    )
    source_details = (
        f"<b>Source:</b> {_text(receipt.get('source_filename'), 'Not recorded')} &nbsp;&nbsp; "
        f"<b>Method:</b> {_text(receipt.get('extraction_method'), 'Unknown')}"
    )
    summary_data = [
        [
            Paragraph("CALCULATED ITEM SUBTOTAL", header_style),
            Paragraph("TIP / GRATUITY", header_style),
            Paragraph("SAVED SUBTOTAL", header_style),
            Paragraph("SALES TAX", header_style),
            Paragraph("TOTAL", header_style),
        ],
        [
            _money(calculated_subtotal, currency),
            _money(gratuity_total, currency),
            _money(receipt.get("subtotal"), currency),
            _money(receipt.get("tax"), currency),
            _money(receipt.get("total"), currency),
        ],
    ]
    summary = Table(summary_data, colWidths=[1.5 * inch] * 5)
    summary.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#245B78")),
                ("TEXTCOLOR", (0, 1), (-1, 1), colors.HexColor("#17202A")),
                ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 1), (-1, 1), 11),
                ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#EDF3F8")),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#AEBBC5")),
                ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#C7D0D8")),
                ("LEFTPADDING", (0, 0), (-1, -1), 7),
                ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )

    rows: list[list[object]] = [
        [
            Paragraph("#", header_style),
            Paragraph("Item", header_style),
            Paragraph("UPC / SKU", header_style),
            Paragraph("Category", header_style),
            Paragraph("Qty", header_style),
            Paragraph("Line price", header_style),
        ]
    ]
    for number, item in enumerate(items, start=1):
        quantity = float(item.get("quantity") or 0.0)
        rows.append(
            [
                str(number),
                Paragraph(_text(item.get("name"), "Unnamed item"), cell_style),
                Paragraph(_text(item.get("product_code")), cell_style),
                Paragraph(_text(item.get("category"), "Uncategorized"), cell_style),
                f"{quantity:g}",
                _money(item.get("line_total"), currency),
            ]
        )
    item_table = Table(
        rows,
        colWidths=[0.32 * inch, 4.45 * inch, 1.15 * inch, 1.45 * inch, 0.55 * inch, 0.85 * inch],
        repeatRows=1,
        hAlign="LEFT",
    )
    item_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#245B78")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 1), (0, -1), "RIGHT"),
                ("ALIGN", (4, 1), (-1, -1), "RIGHT"),
                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 1), (-1, -1), 7.4),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F7F9FB")]),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#C7D0D8")),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )

    story = [
        KeepTogether(
            [
                Paragraph(heading, title_style),
                Paragraph(details, detail_style),
                Paragraph(source_details, detail_style),
                Spacer(1, 8),
                summary,
                Spacer(1, 10),
            ]
        ),
        item_table,
    ]

    def add_page_footer(canvas, doc) -> None:  # type: ignore[no-untyped-def]
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#667085"))
        canvas.drawString(
            document.leftMargin,
            0.20 * inch,
            "Exported from TalliUpper - Every purchase, understood and owned by you.",
        )
        canvas.drawRightString(
            page_size[0] - document.rightMargin,
            0.20 * inch,
            f"Page {doc.page}",
        )
        canvas.restoreState()

    document.build(story, onFirstPage=add_page_footer, onLaterPages=add_page_footer)
    return output.getvalue()
