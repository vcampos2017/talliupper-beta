from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from receipt_reader.extractor import ReceiptExtractionError, extract_receipt
from receipt_reader.models import ReceiptExtraction


def extracted_receipt() -> ReceiptExtraction:
    return ReceiptExtraction(
        store_name="Corner Shop",
        transaction_date="2026-09-03",
        currency="USD",
        items=[],
        subtotal=10.0,
        tax=0.8,
        total=10.8,
        confidence=0.9,
        notes=[],
    )


def test_extract_receipt_sends_image_and_requests_structured_output() -> None:
    parsed = extracted_receipt()
    fake_client = MagicMock()
    fake_client.responses.parse.return_value = SimpleNamespace(output_parsed=parsed)

    with patch("receipt_reader.extractor.OpenAI", return_value=fake_client):
        result = extract_receipt(
            b"jpeg bytes",
            api_key="test-key",
            mime_type="image/jpeg",
            model="test-vision-model",
        )

    assert result == parsed
    kwargs = fake_client.responses.parse.call_args.kwargs
    assert kwargs["store"] is False
    assert kwargs["model"] == "test-vision-model"
    assert kwargs["text_format"] is ReceiptExtraction
    image_part = kwargs["input"][1]["content"][1]
    assert image_part["type"] == "input_image"
    assert image_part["detail"] == "original"
    assert image_part["image_url"].startswith("data:image/jpeg;base64,")


def test_extract_receipt_requires_api_key() -> None:
    with pytest.raises(ReceiptExtractionError, match="API key"):
        extract_receipt(b"jpeg bytes", api_key="")
