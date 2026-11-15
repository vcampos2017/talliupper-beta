# TalliUpper Beta 0.1 — release notes

**Version:** v0.1.0, for the closed beta. Planned release: November 15, 2026.
Code freeze: end of November 12, 2026.

TalliUpper turns a receipt photo into a reviewed, itemized expense record on
your own computer. Beta 0.1 is for a small group of testers, so expect rough
edges and please report anything that reads wrong (see
[Reporting a misread receipt](#reporting-a-misread-receipt)).

## What you can do in Beta 0.1

- Take a photo or upload a receipt (JPG, PNG, WebP, HEIC, PDF), including
  long receipts split across several photos.
- Read it with local OCR (the default). TalliUpper does not upload the image.
- Review and correct the store, date, items, quantities, prices, discounts,
  tax and total, with warnings when the amounts don't add up.
- Add items the parser missed from the unused OCR lines, or type them in.
- Categorize each item, then save, reopen and export to CSV or PDF.
- Use it from a phone browser (tested on iPhone Safari and Chrome) when the
  phone can reach the computer running TalliUpper.
- Optional, experimental: a Local AI second opinion that suggests rows backed
  by the OCR text. You choose whether to add them. See
  [LOCAL_AI_EXPERIMENT.md](LOCAL_AI_EXPERIMENT.md).

To install, follow [Quick start on Windows](../README.md#quick-start-on-windows)
in the README.

## Known limitations

Always check the review page before saving. These receipts in the test library
still need a manual fix, and each one is tracked:

| Receipt | What happens | What to do | Ticket |
| --- | --- | --- | --- |
| Lucky Noodle House (date and time printed together) | The date is left blank. | Enter the date on the review page. | RL-57 |
| Rockler (tax printed on three lines) | OCR reads one tax line a cent low, and the app warns about a $0.01 difference. | Correct the tax to match the receipt. | RL-67 |
| Walmart (faded thermal paper) | Rows too faded to show a price are offered as missing items with no price. | Add those rows and type in their prices. | RL-68 |
| Sherwin-Williams | The store name is saved as "SHERWIN-WILLIAMS." | Edit the name if you like. Items and totals are correct. | RL-69 |

Also:

- Amounts on a saved receipt can only be changed by rerunning it and choosing
  **Update saved receipt** (RL-45, after the beta).
- Payment splits and rewards such as Walmart Cash aren't recorded separately
  (RL-39, after the beta).
- Faded paper, glare, skew and multi-column receipts reduce accuracy. Currency
  defaults to USD, and dates favor US and ISO formats.

## Reporting a misread receipt

1. On the review page, choose **Download OCR Diagnostic JSON**. The file holds
   the text TalliUpper read, not the photo.
2. Open the file and check it for anything personal. Redaction is best-effort.
   Look especially for:
   - names (yours, a cashier's, a server's)
   - street addresses, phone numbers and emails
   - card digits, approval or auth codes, account or loyalty numbers
   - card brand, bank or payment app (for example "Visa", "Capital One",
     "Apple Pay"). Replace these with `CREDIT CARD`.
3. Send the file with a short note on what was wrong, but only if you agree to
   it being kept as an anonymized test receipt.

Each report becomes a test receipt in the library, so that mistake can't come
back unnoticed.
