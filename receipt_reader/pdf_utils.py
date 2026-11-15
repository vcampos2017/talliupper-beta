"""Render one selected PDF receipt page locally for the existing OCR pipeline."""

from io import BytesIO
from threading import RLock

import pypdfium2 as pdfium

from .image_utils import InvalidImageError, prepare_receipt_image

# PDFium calls must not run concurrently across Streamlit sessions.
_PDF_LOCK = RLock()


def pdf_page_count(data: bytes) -> int:
    try:
        with _PDF_LOCK, pdfium.PdfDocument(data) as document:
            count = len(document)
            if not count:
                raise InvalidImageError("The PDF has no pages.")
            return count
    except pdfium.PdfiumError as exc:
        raise InvalidImageError("This PDF could not be opened. Use a readable PDF without a password.") from exc


def render_pdf_page(data: bytes, page_number: int = 1) -> bytes:
    """Return a JPEG at up to 200 DPI, bounded to 4000 pixels per side."""
    try:
        with _PDF_LOCK, pdfium.PdfDocument(data) as document:
            if not 1 <= page_number <= len(document):
                raise InvalidImageError("Choose a page that exists in this PDF.")
            page = document[page_number - 1]
            width, height = page.get_size()
            if min(width, height) <= 0:
                raise InvalidImageError("This PDF page has an invalid size.")
            bitmap = page.render(scale=min(200 / 72, 4000 / max(width, height)))
            try:
                image = bitmap.to_pil()
                output = BytesIO()
                image.save(output, format="PNG")
                return prepare_receipt_image(output.getvalue())[0]
            finally:
                bitmap.close()
    except pdfium.PdfiumError as exc:
        raise InvalidImageError("This PDF page could not be read. Try another PDF or upload a photo.") from exc
