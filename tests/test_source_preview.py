from io import BytesIO

from PIL import Image
import pytest

from receipt_reader.image_utils import InvalidImageError
from receipt_reader.source_preview import receipt_source_previews


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (200, 400), "white").save(output, format="PNG")
    return output.getvalue()


def test_image_source_is_prepared_for_review_without_changing_the_source() -> None:
    original = _png_bytes()

    previews = receipt_source_previews(
        {
            "data": original,
            "filename": "rockler-receipt.png",
            "media_type": "image/png",
        }
    )

    assert len(previews) == 1
    prepared, caption = previews[0]
    assert caption == "rockler-receipt.png"
    assert prepared.startswith(b"\xff\xd8")
    assert original.startswith(b"\x89PNG")


@pytest.mark.parametrize("missing_data", [None, b""])
def test_missing_source_data_has_a_clear_recovery_error(missing_data: object) -> None:
    with pytest.raises(
        InvalidImageError,
        match="original receipt file is no longer available",
    ):
        receipt_source_previews(
            {
                "data": missing_data,
                "filename": "receipt.png",
                "media_type": "image/png",
            }
        )
