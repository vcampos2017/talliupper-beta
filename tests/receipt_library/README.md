# Receipt library

Real receipts kept as regression tests. Every receipt that TalliUpper once
misread goes here with the correct answer, so a fix stays fixed while the next
merchant is worked on.

```
tests/receipt_library/
  heb/
    2026-10-25-four-items/
      ocr.json        OCR lines (no image)
      expected.json   the correct reading, checked against the photo
  baseline.json       last saved scoreboard (optional)
```

## Adding a receipt

1. In TalliUpper, read the receipt and use **Download OCR Diagnostic JSON**.
2. From the repository folder:

   ```
   python scripts/add_receipt_to_library.py path\to\diagnostic.json --merchant heb
   ```

3. Open the new `expected.json` next to the receipt photo. Correct every item
   name, quantity, line total, tax marker, and the printed subtotal, tax, total
   and item count. Write anything unusual about the layout in `layout_notes`.
4. Set `"reviewed": true` and fill in `reviewed_by`.
5. Check `ocr.json` line by line for personal data (see below), then commit.

A draft (`"reviewed": false`) is scored but never fails CI. A reviewed receipt
the parser can't read yet gets `"known_issue": "short reason"`; it shows as
KNOWN on the scoreboard until it is fixed, then remove the note.

## Scoring

```
python scripts/score_receipts.py --details
```

Run it before and after a parser change. Save a baseline with
`--save-baseline`; `--compare` then lists every receipt whose status changed.
A change is an improvement only when receipts move to PASS and none move away.

Statuses: **PASS** every check right · **FAIL** a reviewed receipt is wrong
(blocks CI) · **KNOWN** wrong, with a recorded known issue · **DRAFT** not
reviewed yet.

Checks: store, date, item count, names, quantities, line totals, tax markers,
subtotal, tax, total, items reconcile to the subtotal, printed item count, and
no missing-item prompts on a complete receipt. Leave a field out of
`expected.json` to skip that check.

## Privacy

Never commit receipt images. Before committing `ocr.json` and
`expected.json`, remove or mask:

- card numbers, approval/auth codes, account or loyalty numbers
- names, phone numbers, emails, street addresses
- card brands, issuing banks and payment apps (Visa, Mastercard, Amex,
  Capital One, Apple Pay and so on): replace them with `CREDIT CARD`.
  `add_receipt_to_library.py` does this for you. Together with the store,
  date and items, they help piece together one person's habits.
- transaction and barcode numbers (keep date digits if the store encodes the
  date there, as H-E-B does: `XXXX XXXX 1025 2616 XXXX XXX`)

TalliUpper's redaction is best-effort and also masks long digit runs, which can
remove H-E-B's encoded date. Restore only the date digits by hand when needed.
`tests/test_receipt_library.py` rejects files containing card-length digit
runs (13-19 digits, or 4-4-4-4 style groups), emails, phone numbers, or card
brand and bank names as a
backstop, not a guarantee. A 12-digit product UPC printed next to its price,
as on Walmart receipts, is allowed.

## What to add first

Fix by frequency: the stores scanned most (H-E-B, Walmart) matter more than a
store seen once. Add every receipt beta testers report as wrong, after
redaction and with their permission.
