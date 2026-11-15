from io import BytesIO
from pathlib import Path
import time

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

from receipt_reader.image_utils import InvalidImageError
from receipt_reader.pdf_utils import pdf_page_count, render_pdf_page


def sample_pdf():
    output = BytesIO()
    Image.new("RGB", (200, 300), "red").save(
        output, format="PDF", save_all=True,
        append_images=[Image.new("RGB", (200, 300), "blue")])
    return output.getvalue()


def test_pdf_selected_page_and_size_limit():
    data = sample_pdf()
    assert pdf_page_count(data) == 2
    for page, channel in [(1, 0), (2, 2)]:
        with Image.open(BytesIO(render_pdf_page(data, page))) as image:
            assert image.format == "JPEG"
            assert max(image.size) <= 4000
            assert image.getpixel((50, 50))[channel] > 200


@pytest.mark.parametrize("data", [b"", b"not a PDF", b"%PDF-1.7 damaged"])
def test_bad_pdf_is_readable_error(data):
    with pytest.raises(InvalidImageError):
        pdf_page_count(data)
    with pytest.raises(InvalidImageError):
        render_pdf_page(data)


@pytest.mark.parametrize("page", [0, 3])
def test_missing_page_is_rejected(page):
    with pytest.raises(InvalidImageError, match="Choose a page"):
        render_pdf_page(sample_pdf(), page)


def _wait_for_receipt_result(app, previous_hash=None):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        app.run()
        try:
            current_hash = app.session_state["active_image_hash"]
        except KeyError:
            current_hash = None
        if current_hash and current_hash != previous_hash:
            return
        time.sleep(0.02)
    raise AssertionError("Background receipt job did not reach review in time")


def test_pdf_upload_preview_and_page_change(tmp_path, monkeypatch):
    import streamlit as st
    import receipt_reader.local_pipeline as pipeline
    from types import SimpleNamespace
    from receipt_reader.local_ocr import OcrLine, OcrResult
    from receipt_reader.parser import parse_receipt

    uploaded = BytesIO(sample_pdf())
    uploaded.name = "receipt.PDF"
    monkeypatch.setenv("RECEIPT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(st, "file_uploader", lambda *args, **kwargs: uploaded)
    lines = (OcrLine("SHOP", .99), OcrLine("ITEM 1.00", .99))
    monkeypatch.setattr(pipeline, "process_receipt_locally", lambda *args, **kwargs:
                        SimpleNamespace(parsed=parse_receipt(lines), ocr=OcrResult(lines, "test")))
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py", default_timeout=30).run()
    assert not app.exception
    assert any("All 2 PDF pages" in caption.value for caption in app.caption)
    next(c for c in app.checkbox if c.label == "Read all 2 PDF pages as one receipt").set_value(False).run()
    assert any(n.label == "PDF page to read" for n in app.number_input)
    next(b for b in app.button if b.label == "Read receipt").click().run()
    assert not app.exception
    _wait_for_receipt_result(app)
    assert not app.exception
    old_hash = app.session_state["active_image_hash"]
    # AppTest resets the monkeypatched uploader's PDF checkbox after the
    # app changes its query parameter for the durable job. Model the real
    # post-review interaction explicitly, instead of treating that test-only
    # reset as a lost upload or a production rerun regression.
    next(
        c for c in app.checkbox if c.label == "Read all 2 PDF pages as one receipt"
    ).set_value(False).run()
    page_picker = next(
        (n for n in app.number_input if n.label == "PDF page to read"), None
    )
    assert page_picker is not None
    page_picker.set_value(2).run()
    assert not app.exception
    assert any("image changed" in w.value.lower() for w in app.warning)
    assert next(b for b in app.button if b.label == "Verify and save receipt").disabled
    next(b for b in app.button if b.label == "Read receipt").click().run()
    assert not app.exception
    _wait_for_receipt_result(app, previous_hash=old_hash)
    assert not app.exception
    assert app.session_state["active_image_hash"] != old_hash
