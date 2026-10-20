# open-doc-review

Advisory AI document review that bolts onto the content-management system (ECM) you already use.

When a document lands in your ECM, open-doc-review:

1. **classifies** it into one of the document types you define,
2. **extracts** the fields you define for that type into metadata,
3. **finds the reference documents** it must agree with (by matching extracted fields: an invoice's
   PO number finds the purchase order),
4. **reviews** it against those references with Claude, using **your** prompts, and
5. **writes the findings beside the document**: one comment per finding (severity, page, what was
   compared), a summary comment, and review metadata.

A reference that arrives later triggers a re-review, and long PDFs are reviewed in page windows.

It never edits, moves, renames or deletes a document. The ECM port has no method that could. Every
finding is advisory: a person decides what to do with it.

```
   your ECM ──poll──▶ classify ─▶ extract ─▶ find references ─▶ review ─▶ comments + metadata ──▶ your ECM
     (adapter)          │            │              │              │
                        └──── your prompts + your doc-type config ─┘      every model call ─▶ audit log
```

## What you bring

| You provide | Where | Example |
|---|---|---|
| Document types and their fields | `docreview.yaml` → `doc_types` | invoice: `invoice_number`, `po_number`, `total` |
| How to find references | `doc_types.<type>.review.references` | invoice → purchase order where `po_number` matches |
| Prompts, in your own words and style | a folder of Markdown files | `prompts/review/invoice.md` |
| A connection to your ECM | an adapter: `local`, `box`, or your own | see [docs/ADAPTERS.md](docs/ADAPTERS.md) |

The project ships **no domain prompts**. The example in `examples/generic/` (supplier invoices
checked against purchase orders) shows the shape; replace it with your own.

## Quick start (no ECM needed)

```bash
git clone <this repo> && cd open-doc-review
uv sync                                   # or: pip install -e .
export ANTHROPIC_API_KEY=...              # or: ant auth login
cd examples/generic
uv run docreview check                    # validates config, prompts and the adapter
uv run docreview run --once               # processes library/ ; results land in *.docreview.json
```

Open `library/inbox/INV-2041.txt.docreview.json` to see the classification, the extracted fields,
the review outcome and the comments. The sample invoice has four deliberate problems against its
purchase order. `logs/ai_calls.jsonl` records every model call.

Other commands:

```bash
docreview run                    # poll forever (every 60 s; --interval to change)
docreview process <document-id>  # one document: classify, extract, review if not yet reviewed
docreview review <document-id>   # review now, even if this version was already reviewed
```

Set `review.dry_run: true` to run real model calls while writing nothing to the ECM. It's useful
for trying a prompt change on live documents.

## Connecting your ECM

- **Local folder** (`adapter: local`): documents on disk, results in sidecar JSON files. For trying it out and for tests.
- **Box** (`adapter: box`, `pip install 'open-doc-review[box]'`): metadata templates and comments. See [docs/BOX.md](docs/BOX.md).
- **SharePoint Online** (`adapter: sharepoint`, `pip install 'open-doc-review[sharepoint]'`, *experimental*): library columns. See [docs/SHAREPOINT.md](docs/SHAREPOINT.md).
- **Anything else** (Alfresco, OpenText, M-Files, Documentum, an S3 bucket ...): implement eight
  methods. See [docs/ADAPTERS.md](docs/ADAPTERS.md). `adapter: your_package.module:YourAdapter` loads it, with no fork needed.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how a document flows through, and the design rules
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md): every config key
- [docs/PROMPTS.md](docs/PROMPTS.md): writing your own prompts, and the variables available
- [docs/ADAPTERS.md](docs/ADAPTERS.md): writing an adapter for your ECM
- [docs/BOX.md](docs/BOX.md): setting up the Box adapter
- [docs/SHAREPOINT.md](docs/SHAREPOINT.md): setting up the SharePoint adapter (experimental)
- [docs/DEPLOY.md](docs/DEPLOY.md): running it as a service, rolling it out, cost

## Before you use it on real documents

- Documents are sent to the Claude API. Check that this is allowed for your content, and review
  your data-retention settings with Anthropic.
- Give the adapter's service account the least access that works: read, plus write for metadata and comments.
- Findings come from a model. Validate the prompts and configuration on documents with known
  problems before anyone relies on the output, and keep a person in the loop.

## License

Apache License 2.0. See [LICENSE](LICENSE).
