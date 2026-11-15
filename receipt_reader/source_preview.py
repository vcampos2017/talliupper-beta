"""Prepare original receipt sources for safe display during review."""

from __future__ import annotations

from collections.abc import Mapping

from .image_utils import InvalidImageError, prepare_receipt_image
from .pdf_utils import pdf_page_count, render_pdf_page


def receipt_source_previews(
    source: Mapping[str, object],
) -> list[tuple[bytes, str]]:
    """Return display-ready images and captions for one stored receipt source."""

    raw_data = source.get("data")
    if not isinstance(raw_data, (bytes, bytearray)) or not raw_data:
        raise InvalidImageError("The original receipt file is no longer available.")

    data = bytes(raw_data)
    filename = str(source.get("filename") or "Original receipt")
    media_type = str(source.get("media_type") or "").casefold()
    is_pdf = filename.casefold().endswith(".pdf") or media_type == "application/pdf"

    if is_pdf:
        page_count = pdf_page_count(data)
        return [
            (
                render_pdf_page(data, page_number),
                f"{filename} — page {page_number} of {page_count}",
            )
            for page_number in range(1, page_count + 1)
        ]

    prepared, _ = prepare_receipt_image(data)
    return [(prepared, filename)]
