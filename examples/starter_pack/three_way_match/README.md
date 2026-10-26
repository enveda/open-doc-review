# Starter pack: three-way match

**What it checks.** An invoice is paid only when it agrees with its purchase order (what was agreed)
AND its goods receipt (what actually arrived). The invoice has two required references, both found
by `po_number`. The goods receipt is itself reviewed against its purchase order.

**Planted problems** (`expected.yaml` lists them for `docreview eval`):

| Document | Problems |
|---|---|
| `inbox/INV-3001.txt` | bills 1,000 washers when 800 were accepted; nut price 0.06 vs 0.05 agreed; a handling charge not on the order; the stated total 230.00 does not add up (225.00) |
| `inbox/GR-2002.txt` | received before the order date; 650 cable ties against 500 ordered; a cable gland not on the order; 20 damaged clips still counted as accepted |

`GR-2001.txt` is clean: its short delivery of washers is recorded correctly, and is the reason the
invoice must not bill all 1,000.

**Run it.** From this folder: `docreview check`, then `docreview run --once` (needs `ANTHROPIC_API_KEY`),
or `docreview eval` to score the results against `expected.yaml`.

**Adapt it.**
- Rename the fields to your documents' wording, and add tolerances your payment policy allows (for
  example a price tolerance) to the "What NOT to flag" section of `prompts/review/invoice.md`.
- If your receipts are matched by receipt number rather than PO number, change the `Goods receipt`
  rule's `match`.
- Fill in `owner` and `approved_by` in each prompt's front matter, and align the checks and severity
  definitions with your own procedures before relying on any result.
