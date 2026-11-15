from io import BytesIO

from pypdfium2 import PdfDocument

from receipt_reader.saved_pdf import (
    build_saved_receipt_pdf,
    saved_receipt_pdf_filename,
)


def _saved_walmart_receipt(item_count: int = 36) -> dict[str, object]:
    return {
        "id": 11,
        "store_name": "Walmart Blanco",
        "transaction_date": "2026-09-05",
        "currency": "USD",
        "subtotal": 211.43,
        "tax": 5.51,
        "total": 216.94,
        "source_filename": "IMG_7068.PNG + IMG_7069.PNG",
        "extraction_method": "local-paddleocr",
        "items": [
            {
                "name": f"Walmart item {number} with a useful product description",
                "product_code": f"00000000{number:04d}",
                "category": "Groceries" if number % 3 else "Household",
                "quantity": 1.0,
                "line_total": 5.00 + number / 100,
            }
            for number in range(1, item_count + 1)
        ],
    }


def test_saved_receipt_pdf_is_valid_and_compact_for_walmart_size() -> None:
    pdf_bytes = build_saved_receipt_pdf(_saved_walmart_receipt())

    assert pdf_bytes.startswith(b"%PDF-")
    document = PdfDocument(BytesIO(pdf_bytes))
    assert 1 <= len(document) <= 3


def test_saved_receipt_pdf_filename_is_readable_and_specific() -> None:
    assert saved_receipt_pdf_filename(_saved_walmart_receipt()) == (
        "receipt-2026-09-05-walmart-blanco-11.pdf"
    )
