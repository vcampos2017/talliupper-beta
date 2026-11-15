# Learning guide

This project is easiest to understand by following one synthetic receipt through
the pipeline. None of the first three exercises needs an API key.

## 1. Run the deterministic parser test

```bash
python -m pytest tests/test_parser.py -q
```

Open `tests/test_parser.py` beside `receipt_reader/parser.py`. The test supplies
perfect OCR lines directly, so a failure points to receipt interpretation rather
than image recognition.

Try changing `COUPON -0.50` to `SAVINGS -0.50`. The existing discount expression
should still recognize it. Then try a label the parser does not know, watch the
test fail, and add that label to `DISCOUNT_WORDS`.

## 2. Inspect the OCR boundary

```bash
python -m pytest tests/test_local_ocr.py -q
```

These tests use small fake PaddleOCR payloads. They demonstrate how engine output
is normalized, confidence is clamped to 0–1, and separate text regions on the
same visual row are combined in left-to-right reading order without downloading
a real model.

## 3. Generate the sample again

```bash
python scripts/generate_sample_receipt.py
```

The script recreates `sample_data/sample_receipt.png`. Upload that image in the
app and compare the editable result with `sample_data/expected_receipt.json`.

## 4. Make one controlled parser improvement

Choose one receipt from a store you use. Reduce the raw OCR shown under **How the
local parser classified each OCR line** to a synthetic example that reproduces
the mistake without publishing personal purchase data. Fix only one kind of
mistake at a time—for example, a new tax label or quantity format—and rerun all
tests after each change.

This is a useful development loop:

1. save a failing OCR-text example;
2. write the expected structured fields;
3. make the smallest rule change;
4. run `python -m pytest -q`;
5. confirm older receipt formats still pass.

## 5. Separate OCR errors from parser errors

- If the raw OCR text is wrong, work in `local_ocr.py`, improve the photo, or try a
  different PaddleOCR language/model.
- If the raw OCR text is correct but a field is wrong, work in `parser.py`.
- If the reviewed result is right but history/export is wrong, work in
  `database.py`.

That diagnosis habit is the main lesson behind TalliUpper's modular design.
