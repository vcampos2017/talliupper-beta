"""Generate a synthetic receipt image for demos and local OCR experiments."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "sample_data" / "sample_receipt.png"

REGULAR_FONTS = [
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"),
    Path("C:/Windows/Fonts/consola.ttf"),
    Path("/System/Library/Fonts/Menlo.ttc"),
]
BOLD_FONTS = [
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"),
    Path("C:/Windows/Fonts/consolab.ttf"),
    Path("/System/Library/Fonts/Menlo.ttc"),
]


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in BOLD_FONTS if bold else REGULAR_FONTS:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def centered(draw: ImageDraw.ImageDraw, text: str, y: int, chosen_font) -> None:
    left, _, right, _ = draw.textbbox((0, 0), text, font=chosen_font)
    draw.text(((1000 - (right - left)) / 2, y), text, fill="#191919", font=chosen_font)


def main() -> None:
    image = Image.new("RGB", (1000, 1500), "#dce3df")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((123, 73, 903, 1443), radius=10, fill="#9aa29d")
    draw.rounded_rectangle((105, 55, 885, 1425), radius=10, fill="#fffdf6")

    title_font = font(48, bold=True)
    body_font = font(31)
    small_font = font(23)

    centered(draw, "FRESH MART", 115, title_font)
    centered(draw, "123 MAIN ST", 185, body_font)
    centered(draw, "SAN ANTONIO TX", 230, body_font)
    draw.line((155, 300, 835, 300), fill="#444444", width=2)

    lines = [
        "09/03/2026 18:42",
        "",
        "WHOLE MILK 3.49",
        "BREAD 2 @ 2.00 4.00",
        "COUPON -0.50",
        "BANANAS 1.25 @ 0.59 0.74",
        "",
        "SUBTOTAL 7.73",
        "TAX 0.62",
        "TOTAL 8.35",
        "",
        "VISA 8.35",
    ]

    y = 345
    for line in lines:
        draw.text((155, y), line, fill="#191919", font=body_font)
        y += 66 if line else 38

    draw.line((155, y + 5, 835, y + 5), fill="#444444", width=2)
    centered(draw, "THANK YOU", y + 55, body_font)
    centered(draw, "SYNTHETIC TEST RECEIPT", y + 115, small_font)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUTPUT, format="PNG", optimize=True)
    print(f"Created {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
