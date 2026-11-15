import json
import sys
from types import SimpleNamespace

import pytest

from receipt_reader.local_ocr import LocalOcrError, _load_engine, paddle_results_to_lines


def test_paddle_result_adapter_normalizes_and_sorts_lines() -> None:
    raw_result = {
        "res": {
            "rec_texts": ["  Second   line ", "First line", ""],
            "rec_scores": [1.2, 0.81, 0.4],
            "rec_boxes": [[20, 50, 180, 70], [5, 10, 150, 30], [0, 0, 1, 1]],
        }
    }

    lines = paddle_results_to_lines([raw_result])

    assert [line.text for line in lines] == ["First line", "Second line"]
    assert lines[0].confidence == pytest.approx(0.81)
    assert lines[1].confidence == 1.0
    assert lines[0].box == (5, 10, 150, 30)


def test_paddle_result_adapter_accepts_documented_json_property_and_polygons() -> None:
    payload = {
        "res": {
            "rec_texts": ["TOTAL 8.35"],
            "rec_scores": [0.97],
            "rec_polys": [[[12, 40], [200, 40], [200, 65], [12, 65]]],
        }
    }
    raw_result = SimpleNamespace(json=json.dumps(payload))

    lines = paddle_results_to_lines([raw_result])

    assert lines[0].text == "TOTAL 8.35"
    assert lines[0].box == (12, 40, 200, 65)


def test_paddle_result_adapter_merges_fragments_on_the_same_visual_row() -> None:
    raw_result = {
        "res": {
            "rec_texts": ["2.98", "SAMPLE EGGS", "F"],
            "rec_scores": [0.96, 0.98, 0.94],
            "rec_boxes": [
                [620, 103, 700, 128],
                [20, 100, 450, 126],
                [540, 102, 560, 127],
            ],
        }
    }

    lines = paddle_results_to_lines([raw_result])

    assert [line.text for line in lines] == ["SAMPLE EGGS F 2.98"]
    assert lines[0].box == (20, 100, 700, 128)
    assert lines[0].confidence == pytest.approx(0.96)


def test_paddle_result_adapter_keeps_weight_details_and_next_item_separate() -> None:
    raw_result = {
        "res": {
            "rec_texts": [
                "PRODUCE",
                "WEIGHTED",
                "0.25",
                "@ 1.24 FW",
                "0.20 Lbs",
                "5.97",
                "T",
                "NEXT ITEM",
            ],
            "rec_scores": [0.95] * 8,
            "rec_boxes": [
                [150, 100, 330, 124],
                [20, 102, 140, 126],
                [620, 136, 700, 160],
                [300, 134, 480, 158],
                [45, 132, 190, 156],
                [620, 169, 700, 193],
                [540, 167, 560, 191],
                [20, 165, 300, 189],
            ],
        }
    }

    lines = paddle_results_to_lines([raw_result])

    assert [line.text for line in lines] == [
        "WEIGHTED PRODUCE",
        "0.20 Lbs @ 1.24 FW 0.25",
        "NEXT ITEM T 5.97",
    ]


def test_paddle_result_adapter_rejects_unknown_shape() -> None:
    with pytest.raises(LocalOcrError, match="unfamiliar"):
        paddle_results_to_lines([["legacy", "shape"]])


def test_engine_uses_cpu_and_mobile_detector(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_paddle_ocr(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setitem(
        sys.modules,
        "paddleocr",
        SimpleNamespace(PaddleOCR=fake_paddle_ocr),
    )
    _load_engine.cache_clear()
    try:
        _load_engine("en")
    finally:
        _load_engine.cache_clear()

    assert captured["device"] == "cpu"
    assert captured["ocr_version"] == "PP-OCRv5"
    assert captured["text_detection_model_name"] == "PP-OCRv5_mobile_det"
    assert captured["use_doc_orientation_classify"] is False
    assert captured["use_textline_orientation"] is False


def test_prediction_error_does_not_blame_photo(monkeypatch, caplog) -> None:
    from io import BytesIO

    from PIL import Image

    from receipt_reader import local_ocr

    def fail(_):
        raise RuntimeError("backend failure")

    monkeypatch.setattr(
        local_ocr, "_load_engine", lambda _: SimpleNamespace(predict=fail)
    )
    output = BytesIO()
    Image.new("RGB", (20, 20), "white").save(output, format="PNG")
    with pytest.raises(LocalOcrError, match="software error"):
        local_ocr.extract_text_locally(output.getvalue())
    assert "backend failure" in caplog.text



def test_prediction_reports_progress_to_terminal(monkeypatch, capsys) -> None:
    from io import BytesIO

    from PIL import Image

    from receipt_reader import local_ocr

    raw_result = {
        "res": {
            "rec_texts": ["TOTAL 8.35"],
            "rec_scores": [0.97],
            "rec_boxes": [[12, 40, 200, 65]],
        }
    }
    monkeypatch.setattr(
        local_ocr,
        "_load_engine",
        lambda _: SimpleNamespace(predict=lambda __: [raw_result]),
    )
    output = BytesIO()
    Image.new("RGB", (220, 90), "white").save(output, format="PNG")

    result = local_ocr.extract_text_locally(output.getvalue())

    terminal = capsys.readouterr().out
    assert result.lines[0].text == "TOTAL 8.35"
    assert "INPUT" in terminal
    assert "220x90 pixels" in terminal
    assert "READ" in terminal
    assert "Recognized 1 text lines" in terminal


def test_enhanced_recovery_uses_grayscale_contrast_pass(monkeypatch) -> None:
    from io import BytesIO

    from PIL import Image, ImageDraw

    from receipt_reader import local_ocr

    captured = {}

    def predict(image_array):
        captured["array"] = image_array
        return [{
            "res": {
                "rec_texts": ["ITEM 2.00"],
                "rec_scores": [0.97],
                "rec_boxes": [[10, 10, 120, 30]],
            }
        }]

    monkeypatch.setattr(
        local_ocr,
        "_load_engine",
        lambda _: SimpleNamespace(predict=predict),
    )
    image = Image.new("RGB", (180, 60), (224, 210, 190))
    ImageDraw.Draw(image).text((10, 10), "ITEM 2.00", fill=(90, 80, 70))
    output = BytesIO()
    image.save(output, format="PNG")

    result = local_ocr.extract_text_locally(output.getvalue(), enhanced=True)

    image_array = captured["array"]
    assert (image_array[:, :, 0] == image_array[:, :, 1]).all()
    assert (image_array[:, :, 1] == image_array[:, :, 2]).all()
    assert "enhanced recovery pass" in result.engine


def _receipt_rows(curl: int = 0, smudge: bool = True) -> dict:
    """Seven printed rows 54px apart, like a phone photo of a Walmart receipt."""
    rows = [
        ("ROW ONE", "111111111111", "F", None),
        ("ROW TWO", "222222222222", "F", None),
        ("GLASS WIPES", "333333333333", None, "3.97"),
        ("FRENCH BREAD", "444444444444", "F", "1.47"),
        ("FRENCH BREAD", "444444444444", "F", "1.47"),
        ("CHEESECAKE", "555555555555", "F", "7.98"),
        ("DRY SAUSAGE", "666666666666", "F", "8.47"),
    ]
    texts, boxes = [], []
    for index, (name, code, marker, price) in enumerate(rows):
        top = 822 + 54 * index
        parts = [(name, 410, 700), (code, 720, 1010)]
        if marker:
            parts.append((marker, 1030, 1050))
        if price:
            parts.append((price, 1250, 1380))
        parts.append(("N", 1410, 1440))
        for text, left, right in parts:
            shift = curl * (left - 410) // 1000
            texts.append(text)
            boxes.append([left, top + shift, right, top + 56 + shift])
    if smudge:
        # A crease blurs two prices into one region spanning both rows.
        texts.append("Cv 6 2")
        boxes.append([1250, 822, 1380, 932])
    return {"res": {"rec_texts": texts, "rec_scores": [0.95] * len(texts), "rec_boxes": boxes}}


@pytest.mark.parametrize("curl", [0, 20, 50])
def test_smudged_region_and_curled_paper_do_not_merge_neighboring_rows(curl) -> None:
    lines = paddle_results_to_lines([_receipt_rows(curl)])

    assert len(lines) == 7
    assert lines[0].text.startswith("ROW ONE 111111111111 F")
    assert "Cv 6 2" in lines[0].text
    assert lines[1].text == "ROW TWO 222222222222 F N"
    assert lines[2].text == "GLASS WIPES 333333333333 3.97 N"
    assert lines[6].text == "DRY SAUSAGE 666666666666 F 8.47 N"
    # No row box may stretch across its neighbor.
    assert all(line.box[3] - line.box[1] < 54 * 2 + 1 for line in lines)
