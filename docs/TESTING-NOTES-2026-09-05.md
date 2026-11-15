# Receipt testing milestone - September 5, 2026

## Evidence and scope

Reviewed the H-E-B Run 11 and Target/CVS Run 1 PDF results and sampled frames
from their screen recordings. Audio narration was not transcribed or reviewed.
Observations below do not claim to represent the tester's spoken feedback.
Original recordings and receipts remain local; no customer identifiers or media
are included in this repository.

## Completed milestone

- H-E-B: four reviewed items, matching subtotal/tax/total, successful save.
  Missing-item recovery and manual corrections remain necessary.
- Target/CVS: two rows, including a zero-price item, matching subtotal and total,
  zero sales tax, and successful save. This establishes a second-store workflow
  example, not proof of general OCR accuracy.
- Replaced the small verification checkbox with a prominent full-width
  confirmation button in a bordered review section.
- Added guidance explaining verification, disabled Save states, and next steps
  after saving. Editing receipt details invalidates prior confirmation.
- Clarified money-versus-percentage tax entry and the historical nature of
  extraction notes.

## Follow-up backlog (deferred)

1. Make tax-marker guidance merchant-aware. Target/CVS currently receives the
   H-E-B explanation and warnings about absent F/FW/T markers. Preserve unknown
   markers without assigning H-E-B meanings to another store's symbols.
2. Simplify the missing-item recovery table. Keep selection, name, line price,
   and tax marker visible together; move optional fields and OCR support text
   out of the main editing path. H-E-B video shows names/selection scrolling
   offscreen while prices are edited.
3. Make the saved state consistent. Replace "Continue to Save" after success,
   persist completion across reruns, and review repeat-save behavior to avoid
   accidental duplicate entries.
4. Improve OCR waiting feedback. Record elapsed time and show meaningful status
   without inventing progress percentages. The sampled H-E-B recording shows
   roughly 90 seconds of processing; timings are approximate.
5. Improve merchant-name extraction. Target/CVS required store-field review;
   location/address text should not become the merchant name by default.
6. Clarify where categories are edited versus where the category breakdown is
   only a summary. The recording shows interaction with the summary; whether
   that caused confusion needs confirmation rather than assumption.

## Next testing session

Retain the original H-E-B case as a regression example. Capture the initial
extraction before manual correction on additional receipts. Compare item count,
zero-price rows, printed amounts, store/date extraction, review guidance, and
the final saved result. Use synthetic/anonymized cases for repository tests.
