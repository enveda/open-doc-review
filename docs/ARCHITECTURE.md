# Architecture

```
src/docreview/
  models.py       value objects: DocumentRef, Event, Finding, ReviewResult, ...
  config.py       YAML → validated Config
  prompts.py      your Markdown prompts: lookup across folders, {{ variables }}, front matter
  llm.py          model calls: request building, schema-enforced JSON, refusal handling, audit
  audit.py        append-only JSONL record of every model call
  pipeline.py     classify → extract → find references → review → write back; outcome rules
  cli.py          check / run / process / review
  ecm/base.py     the port (ECMAdapter)
  ecm/local.py    folder-on-disk adapter
  ecm/box.py      Box adapter
  ecm/sharepoint.py  SharePoint Online adapter (experimental)
  windows.py      page windows for long PDFs
```

## One document, one pass

1. **Download once per pass.** Every step of the pass uses the same bytes, tied to one version.
2. **Classify**, unless a document type is already recorded. A person's value always wins.
3. **Extract** the type's fields, once per version. Corrections a person makes are kept until a new version arrives.
4. **Find references.** Each rule fills its `match` from the extracted fields and calls
   `adapter.find`. The engine records whether nothing, one document, or several documents matched. With several, it
   uses the newest and says so.
5. **Review.** One model call (one per page window for long PDFs) with your system prompt, your review prompt, the document and
   every reference found. The JSON schema is enforced by the API.
6. **Derive the outcome** from the findings and the reference gaps (see CONFIGURATION.md).
7. **Write back.** One comment per finding at or above `comment_min_severity`, never repeated, and
   a summary comment. Review metadata records the outcome, counts, version and the reference ids used.

`run` polls in two phases. First every new document is classified and extracted, then reviews
run, so a batch containing both an invoice and its purchase order reviews correctly in a single poll.
Each phase is its own pass, so a document is downloaded twice in that poll.

**Retries.** A pass that completes records `processed_version`. Adapters offer a document again only when
its version changes or a person requests a review. A pass that fails records `failed_version`,
`failure_count` and `last_error`, and the document is retried on later polls up to `max_attempts`. After
that it is skipped until it changes; `docreview process <id>` always runs it.

**Late references.** When a newly extracted document is a reference for documents already reviewed
without it, for example a purchase order arriving after its invoice, those documents are re-reviewed in the
same poll. This works for rules whose `match` values are bare `{field}` placeholders. For other rules,
request a review by hand.

**Long documents.** PDFs longer than `review.window_pages` are reviewed one page window at a time (see below).

## Design rules

- **Advisory only.** The ECM port has no delete, move, rename or overwrite. The model holds no
  credentials and has no tools; it receives documents and returns JSON.
- **Deterministic around the model.** The engine decides which references are attached, what the
  outcome is, and what gets posted. The model supplies findings, not decisions.
- **Never guess silently.** A missing reference is reported, never replaced by something similar. A
  missing extracted value is `null`, not inferred. An ambiguous reference match is flagged.
- **Everything auditable.** Each model call is logged: model requested and used, effort, the full
  prompt text with each file's SHA-256 and front matter, the attachments' SHA-256 (never their bytes), the output
  (extracted values and findings), token usage and timing. A failed call is logged too.
- **Config over code.** Document types, fields, references and prompts are data. Adding a document
  type is a YAML edit plus one Markdown file.

## Extending

- A new ECM: see ADAPTERS.md.
- Another model provider: implement `ModelClient.complete` (`llm.py`), which takes a `ModelRequest`
  and returns a `ModelResponse` whose data matches `request.schema`.
- Long documents: PDFs longer than `review.window_pages` (default 100) are reviewed in page windows.
  Each window is one call with the same references attached; page numbers are mapped back to the whole
  document and the findings are merged. A finding that needs two windows at once (page 40 contradicts
  page 160) can be missed: put such cross-checks in the extracted fields, which every window sees.
  References are always attached whole, so keep them within the model's request limits (about 32 MB).
