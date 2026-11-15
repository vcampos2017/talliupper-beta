from __future__ import annotations

from receipt_reader.local_ocr import OcrLine, OcrResult
from receipt_reader.local_pipeline import process_receipt_locally, process_receipt_pages_locally


def test_ordered_receipt_images_are_parsed_as_one_document(monkeypatch) -> None:
    pages = {
        b"top": OcrResult(
            lines=(
                OcrLine("SAMPLE MARKET", 0.99, (10, 10, 180, 30)),
                OcrLine("FIRST ITEM 2.00", 0.98, (10, 50, 220, 70)),
            ),
            engine="test OCR",
        ),
        b"bottom": OcrResult(
            lines=(
                OcrLine("SECOND ITEM 3.00", 0.97, (10, 10, 220, 30)),
                OcrLine("SUBTOTAL 5.00", 0.99, (10, 50, 220, 70)),
                OcrLine("TAX 0.40", 0.99, (10, 90, 220, 110)),
                OcrLine("TOTAL 5.40", 0.99, (10, 130, 220, 150)),
            ),
            engine="test OCR",
        ),
    }

    monkeypatch.setattr(
        "receipt_reader.local_pipeline.extract_text_locally",
        lambda image_bytes, language="en": pages[image_bytes],
    )

    result = process_receipt_pages_locally([b"top", b"bottom"])

    assert result.ocr.engine == "test OCR; 2 ordered images"
    assert result.ocr.lines[2].box == (10, 180, 220, 200)
    assert [item.name for item in result.parsed.receipt.items] == [
        "FIRST ITEM",
        "SECOND ITEM",
    ]
    assert result.parsed.receipt.subtotal == 5.00
    assert result.parsed.receipt.tax == 0.40
    assert result.parsed.receipt.total == 5.40


def test_multi_image_pipeline_requires_at_least_one_image() -> None:
    try:
        process_receipt_pages_locally([])
    except ValueError as exc:
        assert str(exc) == "Select at least one receipt image."
    else:
        raise AssertionError("Expected an empty-image error")

def test_repeated_boundary_lines_are_removed_without_deleting_single_repeats(
    monkeypatch,
) -> None:
    pages = {
        b"top": OcrResult(
            lines=(
                OcrLine("SHOP", 0.99),
                OcrLine("ITEM A 2.00", 0.99),
                OcrLine("ITEM B 3.00", 0.99),
                OcrLine("ITEM C 4.00", 0.99),
            ),
            engine="test OCR",
        ),
        b"bottom": OcrResult(
            lines=(
                OcrLine("ITEM B 3.00", 0.98),
                OcrLine("ITEM C 4.00", 0.98),
                OcrLine("ITEM D 5.00", 0.99),
                OcrLine("SUBTOTAL 14.00", 0.99),
            ),
            engine="test OCR",
        ),
    }
    monkeypatch.setattr(
        "receipt_reader.local_pipeline.extract_text_locally",
        lambda image_bytes, language="en": pages[image_bytes],
    )

    result = process_receipt_pages_locally([b"top", b"bottom"])

    assert [item.name for item in result.parsed.receipt.items] == [
        "ITEM A",
        "ITEM B",
        "ITEM C",
        "ITEM D",
    ]
    assert sum(item.line_total for item in result.parsed.receipt.items) == 14.00
    assert any(
        "Removed 2 repeated OCR lines" in note
        for note in result.parsed.receipt.notes
    )



def test_incomplete_walmart_read_gets_one_enhanced_recovery_pass(monkeypatch) -> None:
    initial = OcrResult(
        lines=(
            OcrLine("Walmart", 0.99, (20, 10, 250, 35)),
            OcrLine("# ITEMS ?OLD 3", 0.85, (20, 45, 250, 70)),
            OcrLine("ITEM C 333333333333 F 7.00 N", 0.98, (20, 125, 700, 150)),
            OcrLine("SUBTOTAL 12.00", 0.99, (400, 165, 700, 190)),
            OcrLine("TAX1 0.99", 0.99, (400, 205, 700, 230)),
            OcrLine("TOTAL 12.99", 0.99, (400, 245, 700, 270)),
        ),
        engine="test OCR",
    )
    enhanced = OcrResult(
        lines=(
            OcrLine("Walmart", 0.99, (20, 10, 250, 35)),
            OcrLine("# ITEMS SOLD 3", 0.99, (20, 45, 250, 70)),
            OcrLine("ITEM A 111111111111 F 2.00 N", 0.97, (20, 85, 700, 110)),
            OcrLine("ITEM B 222222222222 F 3.00 N", 0.97, (20, 105, 700, 130)),
            OcrLine("ITEM C 333333333333 F 7.00 N", 0.98, (20, 125, 700, 150)),
            OcrLine("SUBTOTAL 12.00", 0.99, (400, 165, 700, 190)),
            OcrLine("TAX1 0.99", 0.99, (400, 205, 700, 230)),
            OcrLine("TOTAL 12.99", 0.99, (400, 245, 700, 270)),
        ),
        engine="test OCR; enhanced recovery pass",
    )
    calls: list[bool] = []

    def fake_extract(image_bytes, *, language="en", enhanced=False):
        calls.append(enhanced)
        return enhanced_result if enhanced else initial

    enhanced_result = enhanced
    monkeypatch.setattr(
        "receipt_reader.local_pipeline.extract_text_locally",
        fake_extract,
    )

    result = process_receipt_locally(b"receipt")

    assert calls == [False, True]
    assert [item.name for item in result.parsed.receipt.items] == [
        "ITEM A",
        "ITEM B",
        "ITEM C",
    ]
    assert sum(item.line_total for item in result.parsed.receipt.items) == 12.00
    assert "conditional enhanced recovery" in result.ocr.engine
    assert result.diagnostic is not None
    assert result.diagnostic.standard is initial
    assert result.diagnostic.enhanced is enhanced
    assert result.diagnostic.merged is result.ocr
    assert result.diagnostic.enhanced_triggered is True
    assert result.diagnostic.merged_origins[2]["origin"] == "enhanced_added"
    assert result.diagnostic.merged.lines[2].box == (20, 85, 700, 110)
    assert any(
        "added 2 purchase rows" in note
        for note in result.parsed.receipt.notes
    )


def test_complete_receipt_does_not_run_enhanced_recovery(monkeypatch) -> None:
    complete = OcrResult(
        lines=(
            OcrLine("Walmart", 0.99),
            OcrLine("# ITEMS SOLD 1", 0.99),
            OcrLine("ITEM A 111111111111 F 2.00 N", 0.99),
            OcrLine("SUBTOTAL 2.00", 0.99),
            OcrLine("TAX1 0.17", 0.99),
            OcrLine("TOTAL 2.17", 0.99),
        ),
        engine="test OCR",
    )
    calls: list[bool] = []

    def fake_extract(image_bytes, *, language="en", enhanced=False):
        calls.append(enhanced)
        return complete

    monkeypatch.setattr(
        "receipt_reader.local_pipeline.extract_text_locally",
        fake_extract,
    )

    result = process_receipt_locally(b"receipt")

    assert calls == [False]
    assert [item.name for item in result.parsed.receipt.items] == ["ITEM A"]


def test_enhanced_rows_replace_a_first_pass_region_spanning_several_rows(
    monkeypatch,
) -> None:
    """Modeled on a faded Walmart receipt read on 2026-10-23.

    The first pass returned five printed rows as one tall region with their
    words interleaved.  The enhanced pass separated them, but row matching let
    only one separated line compete with the tall region, so the rest were
    discarded and the first read was kept.
    """

    blob = (
        "ITEM A ITEM B ITEM C 111111111111 222222222222 F F 2.00 3 N N "
        "ITEM D ITEM E 444444444444 5.00 X 6 N"
    )
    initial = OcrResult(
        lines=(
            OcrLine("Walmart", 0.99, (20, 10, 250, 60)),
            OcrLine("# ITEMSOLD 6", 0.93, (20, 70, 250, 120)),
            OcrLine(blob, 0.88, (20, 130, 700, 400)),
            OcrLine("ITEM F 666666666666 F 7.00 N", 0.99, (20, 395, 700, 450)),
            OcrLine("SUBTOTAL 25.00", 0.99, (400, 460, 700, 510)),
            OcrLine("TAX1 8.2500 % 0.41", 0.99, (400, 515, 700, 565)),
            OcrLine("TOTAL 25.41", 0.99, (400, 570, 700, 620)),
        ),
        engine="test OCR",
    )
    enhanced = OcrResult(
        lines=(
            OcrLine("Walmart", 0.99, (20, 10, 250, 60)),
            OcrLine("# ITEMS SOLD 6", 0.99, (20, 70, 250, 120)),
            OcrLine("ITEM A 111111111111 F 2.00 N", 0.95, (20, 135, 700, 185)),
            OcrLine("ITEM B 222222222222 F 3.00 N", 0.95, (20, 188, 700, 238)),
            OcrLine("ITEM C 333333333333 F 4.00 N", 0.95, (20, 241, 700, 291)),
            OcrLine("ITEM D 444444444444 5.00 X", 0.95, (20, 294, 700, 344)),
            OcrLine("ITEM E 555555555555 F 4 00 N", 0.80, (20, 347, 700, 397)),
            OcrLine("ITEM F 666666666666 F 7.00 N", 0.99, (20, 397, 700, 450)),
            OcrLine("SUBTOTAL 25.00", 0.99, (400, 460, 700, 510)),
            OcrLine("TAX1 8.2500 % 0.41", 0.99, (400, 515, 700, 565)),
            OcrLine("TOTAL 25.41", 0.99, (400, 570, 700, 620)),
        ),
        engine="test OCR; enhanced recovery pass",
    )

    def fake_extract(image_bytes, *, language="en", enhanced=False):
        return enhanced_result if enhanced else initial

    enhanced_result = enhanced
    monkeypatch.setattr(
        "receipt_reader.local_pipeline.extract_text_locally", fake_extract
    )

    result = process_receipt_locally(b"receipt")

    names = [item.name for item in result.parsed.receipt.items]
    assert names == ["ITEM A", "ITEM B", "ITEM C", "ITEM D", "ITEM F"]
    assert result.ocr.text.count(blob) == 0
    assert "ITEM E 555555555555 F 4 00 N" in result.ocr.text
    origins = result.diagnostic.merged_origins
    split = [origin for origin in origins if origin["origin"] == "enhanced_split"]
    assert [origin["enhanced_line"] for origin in split] == [3, 4, 5, 6, 7]
    assert all(origin["standard_line"] == 3 for origin in split)
    taxed = [i.name for i in result.parsed.receipt.items if i.tax_marker == "X"]
    assert taxed == ["ITEM D"]


def test_normal_height_lines_are_not_treated_as_multirow_regions(monkeypatch) -> None:
    from receipt_reader.local_pipeline import _merge_ocr_passes_with_origins

    primary = OcrResult(
        lines=(
            OcrLine("ITEM A 111111111111 F 2.00 N", 0.99, (20, 100, 700, 150)),
            OcrLine("ITEM B 222222222222 F 3.00 N", 0.99, (20, 155, 700, 205)),
        ),
        engine="test",
    )
    recovery = OcrResult(
        lines=(
            OcrLine("ITEM A 111111111111 F 2.00 N", 0.90, (20, 101, 700, 126)),
            OcrLine("ITEM A 111111111111 F 2.00 N", 0.90, (20, 126, 700, 149)),
        ),
        engine="test",
    )

    merged, origins = _merge_ocr_passes_with_origins(primary, recovery)

    assert [origin["origin"] for origin in origins] == ["standard", "standard"]
    assert [line.text for line in merged.lines] == [
        "ITEM A 111111111111 F 2.00 N",
        "ITEM B 222222222222 F 3.00 N",
    ]


def test_rows_read_as_one_tall_line_are_replaced_by_separate_enhanced_rows() -> None:
    from receipt_reader.local_pipeline import _merge_ocr_passes_with_origins
    from receipt_reader.parser import parse_receipt

    standard = OcrResult(
        lines=(
            OcrLine("# ITEMS SOLD 4", 0.95, (20, 40, 400, 95)),
            OcrLine("ITEM A ITEM B ITEM C 1.00 2.00 3.00 N N N", 0.89, (20, 100, 700, 270)),
            OcrLine("ITEM D 444444444444 F 4.00 N", 0.99, (20, 265, 700, 320)),
            OcrLine("SUBTOTAL 10.00", 0.99, (400, 330, 700, 385)),
        ),
        engine="test OCR",
    )
    enhanced = OcrResult(
        lines=(
            OcrLine("# ITEMS SOLD 4", 0.95, (20, 40, 400, 95)),
            OcrLine("ITEM A 111111111111 F 1.00 N", 0.93, (20, 104, 700, 158)),
            OcrLine("ITEM B 222222222222 F 2.00 N", 0.93, (20, 158, 700, 212)),
            OcrLine("ITEM C 333333333333 F 3.00 N", 0.93, (20, 212, 700, 266)),
            # A tall enhanced line must not replace a cleanly read row.
            OcrLine("ITEM D SUBTOTAL 4.00 10.00 N", 0.99, (20, 265, 700, 385)),
            OcrLine("SUBTOTAL 10.00", 0.99, (400, 330, 700, 385)),
        ),
        engine="test OCR; enhanced",
    )

    merged, origins = _merge_ocr_passes_with_origins(standard, enhanced)

    texts = [line.text for line in merged.lines]
    assert "ITEM A ITEM B ITEM C 1.00 2.00 3.00 N N N" not in texts
    assert texts[1:4] == [
        "ITEM A 111111111111 F 1.00 N",
        "ITEM B 222222222222 F 2.00 N",
        "ITEM C 333333333333 F 3.00 N",
    ]
    assert [origin["origin"] for origin in origins[1:4]] == ["enhanced_split"] * 3
    assert "ITEM D 444444444444 F 4.00 N" in texts
    assert "ITEM D SUBTOTAL 4.00 10.00 N" not in texts
    parsed = parse_receipt(merged.lines)
    assert [item.name for item in parsed.receipt.items] == ["ITEM A", "ITEM B", "ITEM C", "ITEM D"]


# RL-81: Petco 2026-09-11, live Dell rerun 2026-11-04 (job c57df8a4). The
# first read was complete, but "3 Items" was compared with one row (qty 3),
# so an enhanced pass ran and its worse tax/total rows replaced good ones.
PETCO_STANDARD = OcrResult(
    lines=(
        OcrLine("petco", 0.99, (564, 10, 1249, 223)),
        OcrLine("09/11/26 9:16AM Reg 002 Trans XXXXXX", 0.93, (321, 558, 1215, 580)),
        OcrLine("000266523 S0PH-30# REFILL FF A 43.47 T", 0.99, (333, 718, 1245, 768)),
        OcrLine("Reg Price: 3@ 14.49", 0.99, (734, 771, 1145, 828)),
        OcrLine("Taxable Total ymdonoa eedbe se 43.47", 0.82, (597, 866, 1251, 923)),
        OcrLine("Subtotal 43.47", 0.99, (700, 913, 1253, 968)),
        OcrLine("Tax TX 8.25% podo n atime 3.59", 0.83, (617, 960, 1262, 1018)),
        OcrLine("TOTAL raqomoldo lon 47.06", 0.89, (768, 1008, 1266, 1067)),
        OcrLine("on e1 anoada 3 Items es bruter llute evieoer lliw", 0.77, (387, 1903, 1285, 1953)),
    ),
    engine="test OCR",
)
PETCO_ENHANCED = OcrResult(
    lines=(
        OcrLine("petco", 0.99, (564, 10, 1249, 223)),
        OcrLine("09/11/26 9:16AM Reg 002 Trans XXXXXX", 0.93, (321, 558, 1215, 580)),
        OcrLine("000266523 S0PH-30# REFILL FF A 43.47 T", 0.99, (333, 718, 1245, 768)),
        OcrLine("Reg Price: 3@ 14.49", 0.99, (734, 771, 1145, 828)),
        OcrLine("Taxable Totalorymo mo eebed bego 43.47", 0.84, (596, 871, 1253, 926)),
        OcrLine("Subtotal 43.47", 0.99, (700, 913, 1253, 968)),
        OcrLine("Tax TX 8.25%", 0.997, (614, 973, 880, 1021)),
        OcrLine(
            "ovolled TOTAL Se18a omoldo 01047.06 61691694± 05769 .8020",
            0.80,
            (686, 1013, 1266, 1079),
        ),
        OcrLine("3 Items ae bnutei llut e evieser lliw", 0.89, (658, 1905, 1285, 1947)),
    ),
    engine="test OCR enhanced",
)


def test_complete_read_with_unit_count_skips_enhanced_pass(monkeypatch) -> None:
    calls = []

    def fake_ocr(image_bytes, language="en", enhanced=False):
        calls.append(enhanced)
        return PETCO_ENHANCED if enhanced else PETCO_STANDARD

    monkeypatch.setattr("receipt_reader.local_pipeline.extract_text_locally", fake_ocr)

    result = process_receipt_locally(b"petco")
    receipt = result.parsed.receipt

    assert calls == [False]
    assert [(item.name, item.quantity, item.line_total) for item in receipt.items] == [
        ("S0PH-30# REFILL FF A", 3, 43.47)
    ]
    assert (receipt.subtotal, receipt.tax, receipt.total) == (43.47, 3.59, 47.06)


def test_merge_keeps_a_row_whose_cleaner_read_dropped_its_amount() -> None:
    from receipt_reader.local_pipeline import _merge_ocr_passes

    merged = _merge_ocr_passes(PETCO_STANDARD, PETCO_ENHANCED)
    texts = [line.text for line in merged.lines]

    assert "Tax TX 8.25% podo n atime 3.59" in texts
    assert "Tax TX 8.25%" not in texts
    assert "TOTAL raqomoldo lon 47.06" in texts


def test_recovery_that_loses_printed_amounts_is_rejected() -> None:
    from receipt_reader.local_pipeline import _recovery_improved
    from receipt_reader.parser import parse_receipt

    initial = parse_receipt(_petco_without_item(PETCO_STANDARD).lines)
    worse = parse_receipt(PETCO_ENHANCED.lines)

    # The worse read "adds" a row but loses the tax and total and overshoots
    # the subtotal, so it must not replace the first read.
    assert worse.receipt.total is None
    assert not _recovery_improved(initial, worse)


def _petco_without_item(ocr: OcrResult) -> OcrResult:
    return OcrResult(
        lines=tuple(line for line in ocr.lines if "REFILL" not in line.text),
        engine=ocr.engine,
    )
