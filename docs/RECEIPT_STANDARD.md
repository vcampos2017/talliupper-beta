# How TalliUpper reads receipts

TalliUpper reads receipts in three layers. Each layer only handles what the
one before it couldn't.

```
OCR lines
   │
   ▼
1. Merchant format profile   (only if the store prints an unusual layout)
   rewrites odd layouts into the standard one-line shape
   │
   ▼
2. Standard parser           (every receipt)
   items, quantities, totals, tax, item count, checks
   │
   ▼
3. Human review              (every receipt)
   the user confirms or corrects; local AI second opinion is optional
```

## 1. The standard

The standard parser (`receipt_reader/parser.py`) expects **one purchase per
line**: a name, optionally a quantity and unit price, then the charged line
total, with an optional tax marker:

```
KEWPIE MAYONNAISE              F    5.37
LINDT EXCELLENCE SEA SALT 2@3.97 T  7.94
```

Everything after that — subtotal, tax, total, printed item count, missing-item
checks, the review steps — happens once, here, for every store.

## 2. Merchant format profiles

A store whose receipt is not one purchase per line gets a profile in
`receipt_reader/vendors/` (see `docs/ARCHITECTURE.md`). Rules for profiles:

- **Translate, don't parse.** A profile only rewrites its layout into the
  standard shape. It never computes totals or decides what to save.
- **Only run when sure.** A profile runs only when its `detect` matches that
  store, so it cannot change any other store's results.
- **Only change what reconciles.** Join or rewrite lines only when the math
  checks (3 × 2.77 = 8.31). Otherwise leave the line for human review.
- **Don't guess meanings.** Unknown markers (H-E-B's `Q`) stay blank until
  their meaning is known.
- **Earn it with a real receipt.** Add a profile only when a real receipt
  fails the standard parser, and add that receipt to the receipt library.
- **Group by layout, not brand.** If several stores print the same layout,
  one profile named for the layout can serve them all.
- **Keep it small.** A profile that keeps growing usually means the rule
  belongs in the standard parser for everyone.

Current profiles: H-E-B (`heb.py`). Walmart rules still live in `parser.py`
and `walmart_invoice.py` and can move to a profile later.

## 3. Human review

The parser never silently guesses. When something doesn't reconcile, the
review page says so and the user decides. The receipt prints its own answer
key — subtotal, tax, total, item count — so a reading that reconciles is
probably right, and one that doesn't is flagged.

## Keeping it working: the receipt library

Every receipt that was misread becomes a permanent test in
`tests/receipt_library/` (see its README):

1. Download the OCR Diagnostic JSON for the misread receipt.
2. `python scripts/add_receipt_to_library.py file.json --merchant <store>`
3. Correct `expected.json` against the photo, set `"reviewed": true`.
4. Fix the parser or add a profile until it passes.
5. `python scripts/score_receipts.py --compare` to confirm nothing else broke.

CI runs every reviewed receipt on every push. Fix the stores people scan most
first.
