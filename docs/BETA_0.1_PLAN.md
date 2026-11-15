# Beta 0.1 plan — receipt reading track

Target release: **November 15, 2026.**

Jira is the source of truth for all beta work: issues labeled `beta-0-1` in
the TalliUpper QA Portfolio project (`RL-`). This page covers the
receipt-reading track and the checks that gate the release.

## Done

- Merchant format profiles, with H-E-B as the first one (PR #72).
- Unit-aware item counts, and the step 4 banner fix (PR #72).
- Receipt library, scoring script, and add-a-receipt helper, seeded with the
  H-E-B receipt from October 25 (this PR).

## Week of Oct 26 — fill the library

Add the receipts behind the open parsing bugs to `tests/receipt_library/`, so
each fix is proven by a real receipt:

| Bug | Store | Problem |
|-----|-------|---------|
| RL-38 | Walmart | Recovered rows keep UPC/OCR noise, lose tax marker and order |
| RL-41 | UPS Store | Merchant read as the city/state/ZIP line |
| RL-42 | Sherwin-Williams | Unit price read as quantity on a discounted line |
| RL-43 | Petco, Rockler | "Reg Price" in item name; "3 Items" count; 1-cent tax misread |

Mark each one `"known_issue": "RL-xx"` until it is fixed, then run
`python scripts/score_receipts.py --save-baseline`.

## Weeks of Nov 2 and Nov 9 — fix by frequency

- Fix the bugs above, most-scanned stores first (Walmart, H-E-B).
- Each fix removes that receipt's `known_issue` and must not move any other
  receipt away from PASS (`score_receipts.py --compare`).
- Add more of your own everyday receipts as they come in, especially H-E-B
  and Walmart.
- Optional if time allows: move Walmart's rules out of `parser.py` into a
  profile.

## Nov 12 — parser freeze

After this date only fixes for receipts in the library go in.

## Release checklist (Nov 13–15)

- [ ] CI green on `main` (pytest on 3.11 and 3.12, Windows dependency check).
- [ ] `python scripts/score_receipts.py` exits 0: no reviewed receipt FAILs.
- [ ] Every remaining KNOWN receipt has an open Jira issue and is listed in
      the release notes as a known limitation.
- [ ] Each fixture's `ocr.json` and `expected.json` checked for personal data.
- [ ] Tester instructions published (below, and in
      [RELEASE_NOTES_0.1.md](RELEASE_NOTES_0.1.md)).

## For beta testers: reporting a misread receipt

1. On the review page, use **Download OCR Diagnostic JSON**. It contains the
   text TalliUpper read, not the photo.
2. Open the file and check it for anything personal, including the card
   brand or bank (replace with `CREDIT CARD`). Redaction is best-effort.
3. Send the file with a short note on what was wrong, only if you agree to it
   being kept as an anonymized test receipt.

Each report becomes a library receipt, so the same mistake can't come back.
