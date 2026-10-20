# Starter pack: policy / SOP checklist

**What it checks.** A standalone review with **no reference documents**: a controlled procedure is
checked against the document-control rules written in its review prompt. Required sections are
present, an owner and an effective date are named, the review date is not already past, and defined
terms are used consistently.

**Planted problems** (`expected.yaml` lists them for `docreview eval`):

| Document | Problems |
|---|---|
| `inbox/SOP-0007.txt` | no Responsibilities section; the owner is blank; the next review date 2025-03-01 is before the 2026-03-01 effective date; the defined term "Controlled Copy" appears as "master copy" in step 4.2 |

**Run it.** From this folder: `docreview check`, then `docreview run --once` (needs `ANTHROPIC_API_KEY`),
or `docreview eval` to score the results against `expected.yaml`.

**Adapt it.**
- Put your own required sections and review cycle in `prompts/review/procedure.md`. Everything this
  review knows comes from that prompt, so it is your document-control policy, written as checks.
- Add document types for other controlled documents (work instructions, forms), each with its own
  checklist prompt.
- Fill in `owner` and `approved_by` in the prompt's front matter, and align the checks and severity
  definitions with your own policy before relying on any result.
