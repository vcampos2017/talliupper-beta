from io import BytesIO

import pytest
from PIL import Image

from receipt_reader.image_utils import InvalidImageError, prepare_receipt_image


def test_image_is_resized_and_converted_to_jpeg() -> None:
    source = BytesIO()
    Image.new("RGBA", (800, 400), (255, 255, 255, 128)).save(source, format="PNG")

    prepared, mime_type = prepare_receipt_image(source.getvalue(), max_dimension=300)

    with Image.open(BytesIO(prepared)) as result:
        assert result.format == "JPEG"
        assert max(result.size) == 300
        assert result.mode == "RGB"
    assert mime_type == "image/jpeg"


def test_invalid_image_is_rejected() -> None:
    with pytest.raises(InvalidImageError):
        prepare_receipt_image(b"not an image")


def test_default_resize_limit_keeps_local_ocr_work_bounded() -> None:
    source = BytesIO()
    Image.new("RGB", (2_600, 60), "white").save(source, format="PNG")

    prepared, _ = prepare_receipt_image(source.getvalue())

    with Image.open(BytesIO(prepared)) as result:
        assert max(result.size) == 2_500
