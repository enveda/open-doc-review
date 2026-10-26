# Metadata: what gets written on each document

The engine writes three groups of metadata on every document it processes, plus comments. This page
shows each group with example values from the two sample documents, and how each ECM stores them.

**Don't write templates or columns by hand.** Generate them from your config:

```bash
docreview templates                     # the setup for the adapter in your config (Box template JSON,
                                        # SharePoint PnP script, Alfresco content model, ...)
docreview templates --adapter sharepoint  # the same config, rendered for another ECM
docreview templates --format json       # the generic schema, for an adapter of your own
```

Re-run it whenever you add a document type or a field. The list of keys lives in one place in the code
(`docreview/schema.py`), and a test fails if the engine ever writes a key that isn't listed there, so
generated templates always match what is written.

## 1. Classification: what the document is, and where processing stands

Written at the **classify** step, and updated as the document moves through the pipeline.

| Key | Type | Example (invoice) | Meaning |
|---|---|---|---|
| `doc_type` ✎ | string | `invoice` | The document type id from your config. Set it by hand to override the model; the engine never re-classifies a type a person set. |
| `classified_at` | string | `2026-10-20T14:02:11+00:00` | When it was classified. |
| `run_id` | string | `a41f0c9e2b7d` | Links to the matching lines in the audit log. |
| `fields_version` | string | `3f9c0d1e2a7b` | The document version the fields were extracted from. |
| `processed_version` | string | `3f9c0d1e2a7b` | The last version processed to the end. A different current version means "process again". |
| `failed_version`, `failure_count`, `last_error` | string, number, string | (absent) | Only present after a failed pass. Polling stops retrying after `max_attempts` failures on one version. |

✎ = a person may edit it in the ECM, and the engine respects the edit.

## 2. Fields: the values extracted from the document

Written at the **extract** step. **You define these per document type** in `docreview.yaml`, so they are
the part that differs from one deployment to another. All are person-editable (✎): a correction is kept
until a new version of the document arrives.

**Invoice** (`doc_types.invoice.fields`):

| Key | Type | Example |
|---|---|---|
| `invoice_number` | string | `INV-2041` |
| `po_number` | string | `PO-7730` |
| `supplier` | string | `Acme Widgets Co.` |
| `invoice_date` | date | `2026-09-18` |
| `total` | number | `1460.0` |
| `currency` | string | `USD` |

**Executed batch record** (`doc_types.executed_batch_record.fields`):

| Key | Type | Example |
|---|---|---|
| `batch_number` | string | `B24-0117` |
| `mbr_number` | string | `MBR-0042` |
| `mbr_version` | string | `3.0` |
| `product` | string | `Examplamide Tablets 50 mg` |
| `manufacture_date` | date | `2026-09-22` |

Fields that a reference rule matches on (`po_number`, `mbr_number`, `mbr_version`) are the ones the
engine *searches* by. Index them in your ECM; `docreview templates` marks them.

Choosing fields:

- Extract what the review or the reference search needs, plus what people will filter on.
  Every field costs a little extraction effort and adds a column or template field.
- Use `string` for identifiers, even numeric-looking ones (a lot "0042" must keep its zero).
- Write a precise `description` for each field. The model reads it, for example
  "The purchase order number the invoice cites, exactly as printed".

## 3. Review: the latest review's result

Written at the **review** step.

| Key | Type | Example (invoice) | Meaning |
|---|---|---|---|
| `outcome` | string | `blocking_findings` | One of `no_findings`, `minor_findings`, `major_findings`, `blocking_findings`, `insufficient_data` (a required reference was not found). Set by fixed rules from the findings, never by the model. |
| `finding_counts` | json | `{"blocker": 1, "major": 3, "minor": 0, "info": 0}` | Findings by severity. |
| `reviewed_version` | string | `3f9c0d1e2a7b` | The version the review covers. |
| `reviewed_at` | string | `2026-10-20T14:02:40+00:00` | When it ran. |
| `run_id` | string | `c09d5e1a7f3b` | Links to the audit log. |
| `references` | json | `{"Purchase order": "purchase_orders/PO-7730.txt"}` | The documents it was compared with, by rule name (`null` = not found). |
| `requested` ✎ | boolean | `false` | Set it to `true` to ask for a re-review. The engine resets it to `false`. |

The useful filters in an ECM are `outcome` (for example "show me everything with blocking findings") and
`requested` (the re-review queue).

## Comments

One comment per finding at or above `review.comment_min_severity`, plus one summary comment per review:

```
[MAJOR] Quantity exceeds purchase order
Line 1 bills 550; PO-7730 ordered 500. · compared with Purchase order
```

The same finding is never posted twice on the same document.

## How each ECM stores it

| ECM | Classification and review | Fields | Comments |
|---|---|---|---|
| Local folder, Amazon S3 | keys in a sidecar `<document>.docreview.json` | in the same sidecar | in the same sidecar |
| Box | one metadata template each (`docClassification`, `docReview`) | one template per document type | native Box comments |
| SharePoint Online | a JSON text column each, plus `DocType` and `ReviewRequested` columns for filtering | one column per field | a text column (Graph has no file-comment API) |
| Veeva Vault | a custom object record per document version; the controlled document is not edited | fields on that record | a text field on that record |
| M-Files | properties on the object (property ids from your config) | one property per field | the built-in Comment property |
| Alfresco | properties of a `dr:documentReview` aspect | one aspect property per field | native Alfresco comments |

Each adapter's guide in `docs/` has the details and its known limits. To store metadata in another ECM,
see [ADAPTERS.md](ADAPTERS.md); `docreview templates --format json` gives you the full key list to map.
