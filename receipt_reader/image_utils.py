"""Receipt image preparation utilities."""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:  # Pillow still handles the other supported formats.
    pass


class InvalidImageError(ValueError):
    """Raised when uploaded bytes are not a usable image."""


def prepare_receipt_image(
    image_bytes: bytes,
    *,
    max_dimension: int = 2_500,
) -> tuple[bytes, str]:
    """Rotate, resize, and encode a receipt as a high-quality JPEG.

    The resize protects against very large phone images while retaining enough
    detail for small receipt text. The original image is never modified.
    """

    if not image_bytes:
        raise InvalidImageError("The selected image is empty.")

    try:
        with Image.open(BytesIO(image_bytes)) as opened:
            image = ImageOps.exif_transpose(opened)
            image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise InvalidImageError("The selected file is not a readable image.") from exc

    if image.width < 50 or image.height < 50:
        raise InvalidImageError("The image is too small to read as a receipt.")

    if image.mode in {"RGBA", "LA"}:
        background = Image.new("RGB", image.size, "white")
        alpha = image.getchannel("A")
        background.paste(image.convert("RGB"), mask=alpha)
        image = background
    else:
        image = image.convert("RGB")

    image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)

    output = BytesIO()
    image.save(output, format="JPEG", quality=92, optimize=True)
    return output.getvalue(), "image/jpeg"
