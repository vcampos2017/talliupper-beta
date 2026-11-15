# TalliUpper architecture

TalliUpper is intentionally split into small stages. OCR answers “what text is
visible?” The parser answers “what does that text mean on a receipt?” Keeping
those questions separate makes the project easier to test, explain, and improve.

## Data flow

| Stage | Input | Output | Main file |
|---|---|---|---|
| Capture | Camera or uploaded image | Original image bytes | `app.py` |
| Preparation | Original image | Rotated, resized JPEG bytes | `receipt_reader/image_utils.py` |
| Local OCR | Prepared image | Text, confidence, bounding box per line | `receipt_reader/local_ocr.py` |
| Parsing | Ordered OCR lines | Merchant, date, items, totals, notes | `receipt_reader/parser.py` |
| Review | Proposed structured record | Human-corrected and categorized record | `app.py` |
| Persistence | Corrected record | SQLite rows and CSV export | `receipt_reader/database.py` |

## Local privacy boundary

In the default mode, all runtime stages execute on the same computer as the
Streamlit process. Receipt images are kept in memory and are not inserted into
SQLite. The first scan needs internet access so PaddleOCR can download its model
files; after they are cached, OCR can run without uploading receipt images.

The optional OpenAI vision mode is a separate fallback. Selecting it and clicking
**Read receipt** sends the prepared image to that service. The UI calls out this
difference before extraction.

## OCR adapter

`local_ocr.py` is the only module that knows PaddleOCR's result format. It converts
the engine's `rec_texts`, `rec_scores`, and geometry into the small `OcrLine`
dataclass. Text regions that overlap vertically are combined from left to right,
so a separately detected item name, tax marker, and price become one visual row.
The rest of the app therefore does not depend on Paddle-specific objects.

The engine is created lazily and cached by language. Importing the application or
running parser tests does not download a model.

## Rule parser

`parser.py` uses explicit regular expressions and ordered rules:

1. Pick a likely merchant line near the top.
2. normalize a recognized date to `YYYY-MM-DD`.
3. locate subtotal, tax, and final-total labels, including conservative recovery
   when OCR drops a decimal point or decorative total label.
4. treat text-plus-price lines before the summary as item candidates.
5. recognize `quantity @ unit price` and attach discount lines to the previous item.
6. compare arithmetic and add review notes when values disagree.

Every OCR line receives a role and a plain-language reason. Those decisions appear
in the app's review panel, which is useful when changing a rule.

## Storage model

SQLite uses a one-to-many relationship: one `receipts` row has zero or more
`receipt_items` rows. Each item stores its own expense category, so a single
store transaction can be divided among groceries, household, pet care, and other
purposes. Built-in and user-created category names are stored in
`expense_categories`. Money is stored as integer cents to avoid floating-point
rounding problems in persistence. `extraction_method` records whether a receipt
came from local OCR, cloud vision, or manual entry.

## Extension points

- Add store-specific cleanup rules before the generic parser.
- Extend row reconstruction for genuinely multi-column receipt layouts.
- Add currency and non-US date detection.
- Store parser corrections as labeled examples for evaluation.
- Replace the rule parser with a local document model while keeping the same
  `ReceiptExtraction` boundary.

## Vendor profiles

`receipt_reader/vendors/` holds merchant-specific layout rules. The general
parser handles shared layouts; a profile runs only when its `detect` matches
the OCR lines, so it cannot change how other stores parse. A profile can join
rows into the one-row shape the parser reads (`preprocess`) and mark lines
such as coupon notes as item metadata (`metadata_reason`).

- `heb.py` — joins H-E-B's two-line quantity rows (`3 Ea. @ 1/ 2.77  8.31`)
  to their item name and treats `COUPON … orig … Ea.` notes as metadata.

To add a store, write a module that exposes `PROFILE` and add it to
`_profiles()` in `vendors/__init__.py`, with a test built from a real receipt.
Older Walmart rules still live in `parser.py` and `walmart_invoice.py`; they can
move into a profile later.
