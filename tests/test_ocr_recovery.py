"""RL-38: rows recovered from unused OCR lines get the same cleanup as parsed rows.

Modeled on the faded Walmart receipt of 2026-10-11 (saved as RL-000073).
OCR text and boxes come from the Dell's OCR diagnostic export; UPCs and
prices the export redacted were restored from the receipt photo.
"""

from __future__ import annotations

import pytest

from receipt_reader.local_ocr import OcrLine, OcrResult
from receipt_reader.local_pipeline import process_receipt_locally
from receipt_reader.ocr_recovery import (
    clean_candidate,
    clean_recovered_line,
    printed_position,
    split_product_code,
)
from receipt_reader.parser import parse_receipt


def _lines(rows):
    return tuple(OcrLine(text, confidence, box) for text, confidence, box in rows)


HEADER = [
    ("Walmart", 0.99, (661, 29, 1187, 151)),
    ("WM Supercenter", 0.99, (732, 159, 1113, 210)),
    ("# ITEMSOLD 11", 0.93, (701, 470, 1115, 530)),
]
FOOTER = [
    ("SUBTOTAL 78.50", 0.99, (898, 1512, 1388, 1569)),
    ("TAX1 8.2500% 4.02", 0.99, (707, 1565, 1388, 1626)),
    ("TOTAL 82.52", 0.99, (975, 1618, 1387, 1678)),
    ("10/11/26 13:37:41", 0.99, (633, 1993, 1198, 2048)),
]
STANDARD = _lines(
    HEADER
    + [
        ("LDSSGLZH0NBN 024300865400F Cv 2", 0.84, (407, 824, 1405, 889)),
        ("CHEESECAKE 194346717820F 6 NNX", 0.99, (414, 880, 1443, 938)),
        ("GLASS WIPES 0706121750107", 0.99, (412, 929, 1387, 992)),
        ("FRENCH BREAD L 200989000000 F 1.47", 0.89, (410, 980, 1389, 1046)),
        ("FRENCH BREAD 限 200989000000 1. SVA 47 NZ", 0.72, (413, 1038, 1442, 1096)),
        ("CHEESECAKE 194346717840 F 7.98 N", 0.99, (412, 1086, 1443, 1152)),
        ("DRY SAUSAGE 038616900010 F 8.47 N", 0.99, (412, 1143, 1440, 1204)),
        ("SITE MERCH 889698929570 14.88", 0.99, (411, 1196, 1404, 1259)),
        ("SITE MERCH 889698929560 14.88", 0.99, (413, 1248, 1400, 1310)),
        ("AVOCADO 750032608690 F", 0.99, (413, 1305, 1143, 1361)),
        ("3 AT 1 FOR 0.56 1.68 N", 0.98, (541, 1354, 1447, 1415)),
        ("LATCH BOXES 073149097330 14.98 X", 0.98, (413, 1408, 1447, 1469)),
    ]
    + FOOTER
)
ENHANCED = _lines(
    HEADER
    + [
        ("LDSSGLZH0NBN 024300865400F Cv NN", 0.82, (409, 827, 1442, 887)),
        ("CHEESECAKE 194346717820 F 6 2", 0.99, (412, 882, 1398, 939)),
        ("GLASS WIPES 070612175010 2 97 X", 0.82, (411, 933, 1443, 992)),
        ("FRENCH BREAD L 200989000000 F 1 47 N", 0.95, (412, 980, 1441, 1045)),
        ("FRENCH BREAD 200989000000 F 1 47 N", 0.95, (413, 1038, 1440, 1097)),
        ("CHEESECAKE 194346717840 人 98 N", 0.94, (412, 1092, 1440, 1151)),
        ("DRY SAUSAGE 038616900010 F 8.47 N", 0.99, (413, 1141, 1440, 1204)),
        ("SITE MERCH 889698929570 14.88 X", 0.99, (410, 1192, 1451, 1259)),
        ("SITE MERCH 889698929560 14.88 X", 0.98, (412, 1249, 1443, 1310)),
        ("AVOCADO 750032608690 F", 0.99, (413, 1306, 1146, 1364)),
        ("3 AT 1 FOR 0.56 1.68 N", 0.99, (540, 1356, 1447, 1417)),
        ("LATCH BOXES 073149097330 14.98 X", 0.99, (411, 1410, 1448, 1470)),
    ]
    + FOOTER
)


@pytest.fixture
def walmart_result(monkeypatch):
    def fake_extract(image_bytes, *, language="en", enhanced=False):
        return OcrResult(lines=ENHANCED if enhanced else STANDARD, engine="test OCR")

    monkeypatch.setattr("receipt_reader.local_pipeline.extract_text_locally", fake_extract)
    return process_receipt_locally(b"receipt")


def test_tax_flags_lost_in_first_pass_are_read_from_enhanced_pass(walmart_result):
    items = walmart_result.parsed.receipt.items
    assert [(item.name, item.tax_marker, item.line_total) for item in items] == [
        ("FRENCH BREAD", "N", 1.47),
        ("CHEESECAKE", "N", 7.98),
        ("DRY SAUSAGE", "N", 8.47),
        ("SITE MERCH", "X", 14.88),
        ("SITE MERCH", "X", 14.88),
        ("AVOCADO", "N", 1.68),
        ("LATCH BOXES", "X", 14.98),
    ]
    assert any("enhanced OCR pass" in note for note in walmart_result.parsed.receipt.notes)
    flagged = [o for o in walmart_result.diagnostic.merged_origins if o.get("tax_flag_from_enhanced")]
    assert len(flagged) == 5  # GLASS WIPES, FRENCH BREAD L, FRENCH BREAD 限, 2x SITE MERCH


def test_unused_walmart_lines_split_name_upc_and_tax_flag(walmart_result):
    text_of = {decision.index: decision.text for decision in walmart_result.parsed.decisions}
    unpriced = [
        text_of[index]
        for index, decision in enumerate(walmart_result.parsed.decisions, start=1)
        if decision.role in {"ignored", "missing_item_candidate"}
        and any(character.isdigit() for character in decision.text)
        and "SOLD" not in decision.text
    ]
    assert [clean_recovered_line(text, "Walmart") for text in unpriced] == [
        {"name": "LDSSGLZH0NBN", "product_code": "024300865400", "tax_marker": None},
        # The smudged "6 NNX" price column gives no reliable flag.
        {"name": "CHEESECAKE", "product_code": "194346717820", "tax_marker": None},
        {"name": "GLASS WIPES", "product_code": "0706121750107", "tax_marker": "X"},
        {"name": "FRENCH BREAD", "product_code": "200989000000", "tax_marker": "N"},
    ]


def test_parser_missing_item_candidates_are_cleaned_too(walmart_result):
    from dataclasses import asdict

    candidate = next(
        asdict(c) for c in walmart_result.parsed.missing_item_candidates
        if c.name.startswith("LDSS")
    )
    cleaned = clean_candidate(candidate, "Walmart")
    assert (cleaned["name"], cleaned["product_code"]) == ("LDSSGLZH0NBN", "024300865400")
    assert cleaned["source_line"] == candidate["source_line"]
    assert clean_candidate({"name": "WM Supercenter"}, "Walmart") == {"name": "WM Supercenter"}


def test_recovered_rows_return_to_printed_positions(walmart_result):
    decisions = [
        {"index": d.index, "role": d.role, "text": d.text}
        for d in walmart_result.parsed.decisions
    ]
    items = [item.name for item in walmart_result.parsed.receipt.items]
    recovered: list[int] = []
    # Added out of order on purpose; each lands where it was printed.
    for text in ("FRENCH BREAD 限", "LDSSGLZH0NBN", "GLASS WIPES"):
        source_line = next(d["index"] for d in decisions if d["text"].startswith(text))
        items.insert(printed_position(source_line, decisions, recovered, len(items)), text)
        recovered.append(source_line)
    cheesecake_line = next(
        d["index"] for d in decisions if d["text"].startswith("CHEESECAKE 194346717820F")
    )
    items.insert(printed_position(cheesecake_line, decisions, recovered, len(items)), "CHEESECAKE 6.72")
    assert items == [
        "LDSSGLZH0NBN", "CHEESECAKE 6.72", "GLASS WIPES", "FRENCH BREAD",
        "FRENCH BREAD 限", "CHEESECAKE", "DRY SAUSAGE", "SITE MERCH",
        "SITE MERCH", "AVOCADO", "LATCH BOXES",
    ]


def test_tax_check_reads_the_printed_rate_after_recovery(walmart_result):
    taxed = sum(i.line_total for i in walmart_result.parsed.receipt.items if i.tax_marker == "X")
    taxed += 3.97  # GLASS WIPES, recovered with its X flag
    assert taxed == pytest.approx(48.71)
    assert round(4.02 / taxed * 100, 2) == pytest.approx(8.25, abs=0.01)


def test_walmart_stray_letter_before_upc_is_dropped_but_other_stores_keep_it():
    walmart = parse_receipt([OcrLine("Walmart", 0.99), OcrLine("FRENCH BREAD L 200989000000 F 1.47 N", 0.99)])
    assert walmart.receipt.items[0].name == "FRENCH BREAD"
    other = parse_receipt([OcrLine("Corner Shop", 0.99), OcrLine("VITAMIN C 012345678905 4.99", 0.99)])
    assert other.receipt.items[0].name == "VITAMIN C"


def test_non_walmart_lines_keep_tax_flag_empty_and_plain_names_untouched():
    assert clean_recovered_line("GLASS WIPES 0706121750107 X", "Target")["tax_marker"] is None
    assert clean_recovered_line("Garlic Naan", "Lucky Noodle") == {
        "name": "Garlic Naan", "product_code": None, "tax_marker": None,
    }


def test_review_screen_adds_recovered_rows_in_printed_order(
    tmp_path, monkeypatch, walmart_result
) -> None:
    from dataclasses import asdict
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=30).run()
    parsed = walmart_result.parsed
    app.session_state["active_receipt"] = parsed.receipt.model_dump()
    app.session_state["receipt_version"] = 1
    app.session_state["active_image_hash"] = None
    app.session_state["active_method"] = "local-paddleocr"
    app.session_state["receipt_revision"] = 1
    app.session_state["ocr_text"] = walmart_result.ocr.text
    app.session_state["line_decisions"] = [asdict(d) for d in parsed.decisions]
    app.session_state["missing_item_candidates"] = [
        asdict(c) for c in parsed.missing_item_candidates
    ]
    app.run()
    assert not app.exception
    table = app.dataframe[1].value
    rows = {name: position for position, name in enumerate(table["Item name"])}
    assert {"LDSSGLZH0NBN", "CHEESECAKE", "GLASS WIPES", "FRENCH BREAD"} <= set(rows)
    glass = table.loc[rows["GLASS WIPES"]]
    assert glass["Tax marker"] == "X"
    assert "0706121750107" not in glass["Item name"]

    edits = {
        rows["LDSSGLZH0NBN"]: {"Add": True, "Line price": 2.00, "Tax marker": "N"},
        rows["GLASS WIPES"]: {"Add": True, "Line price": 3.97},
    }
    app.session_state["candidate_items_1"] = {
        "edited_rows": edits, "added_rows": [], "deleted_rows": [],
    }
    next(b for b in app.button if b.label == "Add selected OCR lines as items").click()
    app.run()
    assert not app.exception
    items = app.session_state["active_receipt"]["items"]
    assert [(i["name"], i["product_code"], i["tax_marker"]) for i in items[:4]] == [
        ("LDSSGLZH0NBN", "024300865400", "N"),
        ("GLASS WIPES", "0706121750107", "X"),
        ("FRENCH BREAD", items[2]["product_code"], "N"),
        ("CHEESECAKE", items[3]["product_code"], "N"),
    ]
    assert len(items) == 9

    # Second round: the rows printed between ones already placed.
    table = app.dataframe[1].value
    assert "GLASS WIPES" not in set(table["Item name"])
    names = list(table["Item name"])
    edits = {
        names.index("CHEESECAKE"): {"Add": True, "Line price": 6.72, "Tax marker": "N"},
        names.index("FRENCH BREAD"): {"Add": True, "Line price": 1.47},
    }
    app.session_state["candidate_items_2"] = {
        "edited_rows": edits, "added_rows": [], "deleted_rows": [],
    }
    next(b for b in app.button if b.label == "Add selected OCR lines as items").click()
    app.run()
    assert not app.exception
    items = app.session_state["active_receipt"]["items"]
    assert [(i["name"], i["line_total"], i["tax_marker"]) for i in items] == [
        ("LDSSGLZH0NBN", 2.00, "N"),
        ("CHEESECAKE", 6.72, "N"),
        ("GLASS WIPES", 3.97, "X"),
        ("FRENCH BREAD", 1.47, "N"),
        ("FRENCH BREAD", 1.47, "N"),
        ("CHEESECAKE", 7.98, "N"),
        ("DRY SAUSAGE", 8.47, "N"),
        ("SITE MERCH", 14.88, "X"),
        ("SITE MERCH", 14.88, "X"),
        ("AVOCADO", 1.68, "N"),
        ("LATCH BOXES", 14.98, "X"),
    ]
    assert sum(i["line_total"] for i in items) == pytest.approx(78.50)
    assert sum(i["line_total"] for i in items if i["tax_marker"] == "X") == pytest.approx(48.71)


@pytest.mark.parametrize(("text", "expected"), [
    ("095624930055 BASE MDF <A>", ("BASE MDF <A>", "095624930055")),
    ("GV MILK 007874235187F", ("GV MILK", "007874235187")),
    ("LATCH BOXES 12345678", ("LATCH BOXES", "12345678")),
    # Left alone: no code, a code mid-name, two codes, or nothing but a code.
    ("PAPER TOWEL", ("PAPER TOWEL", None)),
    ("ITEM 0012345678 BLUE", ("ITEM 0012345678 BLUE", None)),
    ("123456789012 456789012345 BOLT", ("123456789012 456789012345 BOLT", None)),
    ("095624930055", ("095624930055", None)),
    ("1234567 SCREWS", ("1234567 SCREWS", None)),
    ("601-10171-0000 SLING", ("601-10171-0000 SLING", None)),
])
def test_split_product_code_is_conservative(text, expected):
    assert split_product_code(text) == expected
