# Local AI second opinion — first experiment

This optional pilot asks a small local model to identify descriptions and printed
line amounts in existing OCR text. It does not alter the normal parser, save
suggestions, or call a cloud service. It runs only when you select **Get local
AI second opinion** in an active receipt review, and it changes the review table
only when you select **Add to review table** on one of its rows.

## Set it up (any tester, Windows)

Each model is a download of a few gigabytes. No Python package, API key or
account is needed.

1. Install Ollama for Windows from <https://ollama.com/download/windows> and
   start it. It runs in the background (look for the llama icon by the clock).
2. Open PowerShell and download the model:

   ```powershell
   ollama pull qwen3.5:4b
   ```

   On a computer with less memory, or if the 4b model is too slow, use the
   smaller one instead (or as well):

   ```powershell
   ollama pull qwen3.5:2b
   ```

3. Check that the model is installed. It should be listed:

   ```powershell
   ollama list
   ```

4. Optional, for a local-only installation: set `OLLAMA_NO_CLOUD=1` in your
   Windows user environment variables and quit/restart Ollama. Keep its server
   bound to localhost. Do not expose Ollama to the network; iPhone requests
   still go through TalliUpper on the Dell.
5. Start TalliUpper and read a receipt with **Local OCR**.

## Use it on a receipt

1. In step 1 of the review, open **Local AI second opinion (experimental)**.
2. Select the model you downloaded, then **Get local AI second opinion**. The
   first run loads the model and can take several minutes on a slower computer;
   the TalliUpper terminal window shows progress.
3. Compare each proposed row and its numbered OCR evidence with the photo.
4. For a row that is correct and missing from the review table, select
   **Add to review table: _name_ — _price_**. When there are several, **Add all
   suggested rows** adds them together. The row is added with quantity 1 where
   it was printed, and the comparison marks it as added. A UPC/SKU printed at
   the start or end of the name goes in the **UPC / SKU** column, not the name.
5. Check the new row in the review table, fill in the UPC/SKU (if it was not
   printed), tax marker and category if needed, then save as usual. Nothing is
   saved automatically.

Only rows that passed the OCR source checks get an Add button, and each one is
checked again against the current OCR text when you add it.

## If it does not work

| Message in TalliUpper | What to do |
| --- | --- |
| Local AI is not running on this computer | Install or start Ollama (step 1 above), then try again. |
| Ollama is running, but the model … is not downloaded | Run the `ollama pull` command shown, or select the other model if the message says it is installed. |
| Local AI had not started answering after 10 minutes | The computer is slow to load this model. Try `qwen3.5:2b`. |
| Local AI stopped responding / took longer than 20 minutes | Try again, or try `qwen3.5:2b`. |

In every case the receipt review is unchanged and you can continue normally.

No extra Python model library, API key, or per-receipt API charge is required.
Ollama and weights are separate installations. TalliUpper never downloads models
automatically. Download size is not the RAM requirement; measure memory and speed
on the Dell before deciding whether either candidate is suitable. The model is
not installed or benchmarked by automated tests.

## Boundaries and checks

- Only `qwen3.5:4b` and `qwen3.5:2b` are selectable. The adapter checks installed
  models and records the selected model's digest for comparing runs.
- Requests go directly to `127.0.0.1:11434` using the standard library, without
  environment proxies, redirect following, or configurable remote URLs.
- The prompt contains OCR text and a fixed schema, not images, credentials,
  saved receipt history, parser answers, or expected answers from tests.
- Pydantic validates structured JSON. Descriptions must quote cited OCR lines;
  prices must exist at specified source occurrences. Descriptions must match whole
  words and contain at least two letters. Reusing a price occurrence or a
  description occurrence for multiple rows is rejected. A trailing minus
  (`2.00-`) is read as a negative amount. Genuine repeated purchases remain separate.
- Sums use `Decimal`. No amount is invented to balance a subtotal.
- Source checks and arithmetic do **not** establish semantic correctness: a model
  can still select a summary or associate the wrong name and price.
- This pilot evaluates descriptions and printed line totals only. Quantities,
  unit prices, discounts, tax and merchant metadata remain outside its scope.
- Missing models, service errors, timeouts and invalid/truncated output leave
  normal review available. There are no automatic retries. Inputs over 12,000
  characters or 160 OCR lines are rejected before network access, never silently
  truncated. Response size and generated output are bounded. Chat replies are
  streamed and tolerate slow, CPU-only computers: Ollama gets up to 10 minutes
  to load the model and read the receipt before its first piece of reply, then
  fails only if silent for 90 seconds, with a 20-minute cap on the whole reply.
  The model stays loaded for 30 minutes so a second try skips loading. The
  terminal shows when the model starts answering. Model-list lookup has a
  separate 3-second timeout.
- Suggestions stay in transient session state and are cleared when changing or
  clearing receipts. A suggestion enters the review table only when the user
  selects its Add button, as an ordinary editable row (name, line price, qty 1);
  it is saved only when the user saves the receipt. Terminal output reports only timing and counts. Suggestions
  do not enter SQLite, OCR diagnostics, CSVs or PDFs. Ollama's own configuration
  and logging remain managed by its installation.

## Evaluate before automatic filling

Keep existing regressions. Reserve receipt images not used to tune the parser or
prompt. Do not commit real receipts or purchase data. Record a manual evaluation
outside the repository:

| Case | Parser correct rows | AI correct rows | Missing / invented rows | Manual corrections | Time | Model digest |
| --- | --- | --- | --- | --- | --- | --- |
| Unfamiliar store / products | | | | | | |
| Split description and price | | | | | | |
| Repeated identical purchases | | | | | | |
| Faint / incomplete OCR | | | | | | |
| Weighted / multi-buy items | | | | | | |
| Discount / zero-price rows | | | | | | |

Ground truth comes from the photo, not the model or a matching total. Count extra
rows and incorrect name/amount pairings as errors even when sums match. Compare
both models on the same held-out images. Keep the feature optional unless it
reduces manual corrections at acceptable latency. Unit tests simulate model
responses; they are not accuracy benchmarks. Real-model acceptance and the
difficult Walmart receipt remain separate manual tasks.

## Existing Walmart change audit

At implementation time, PR #67 remained open. Its new `FRENCH BREAD` strings
were in regression fixtures, not new production matching rules. Production
changes concern OCR region segmentation and printed-count spacing. An older
H-E-B parser repair explicitly matches `AVOCADOS|ONIONS`; that is a separate,
existing product-specific heuristic. This experiment leaves that rule and the
pending Walmart fix unchanged. Evaluate generic replacements before removing
existing behavior.

## Primary references

- Models: <https://ollama.com/library/qwen3.5:4b>
- Structured output: <https://docs.ollama.com/capabilities/structured-outputs>
- Chat API: <https://docs.ollama.com/api/chat>
- Installed models: <https://docs.ollama.com/api/tags>
- Local-only configuration: <https://docs.ollama.com/faq>
