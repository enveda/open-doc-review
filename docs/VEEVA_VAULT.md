# Veeva Vault adapter

> **Example adapter, not tested against a live system.** It follows Veeva's published Vault REST
> API and VQL reference and is tested against a fake Vault only. Every assumption it makes is listed
> under [Known limits](#known-limits). Treat it as a starting point for your own adapter.

> **Validated systems.** A Vault holding GxP records is normally a validated system. Adding a custom
> object, a security profile and an integration user is a configuration change. Your Vault admin and
> quality unit must assess and validate it under your computer system validation (CSV) and
> 21 CFR Part 11 procedures **before** it is used on controlled records. The engine is advisory: a
> person always makes the release decision.

```bash
pip install 'open-doc-review[veeva_vault]'   # adds httpx
```

## Design: the controlled document is never edited

A Vault document is a controlled record, so the adapter **does not change a document or any of its
fields**. Everything the engine writes goes into a custom object, one record per document version:

| What | Where |
|---|---|
| classification, extracted fields, review result | JSON in long-text fields of the record for that version |
| `doc_type`, `processed_version`, `requested` | their own fields, so VQL can filter on them; `doc_type__c` is also where a person corrects the type |
| fields used in reference matching | one text field each (`match_fields`), holding a case-folded copy |
| comments | a long-text field on the record (see below) |

A new document version has no record until the engine writes one. Until then, reads return the
newest earlier record, so a person's `doc_type` correction carries forward the way metadata does in
other systems. A review request (`review_requested__c`) does not carry forward: it was for the earlier version. The first write for the new version creates its own record, and earlier records are
never changed.

**Comments are not Vault annotations.** Annotations are notes placed on a page of the document
version, and they belong to the controlled record that the adapter avoids writing to. In the
documented API a note annotation also needs a placemark (page and position) that an engine finding
doesn't have. Comments therefore go to `comments__c` on the result record: newest first, each
starting with a `[timestamp]` line, and the oldest drop off past `comments_max_chars` (see Known limits). Reviewers can
show the field on the object's page layout, or on a related-object section of the document.

## 1. Create the custom object

Admin → Configuration → Objects → Create. The default name is `docreview_result__c`; any name
works if you set `result_object`.

| Field | Type | Notes |
|---|---|---|
| `name__v` | Text (standard) | The adapter sets it to the version id, e.g. `1234_1_0`. Leave the name editable, not system-managed. |
| `document_id__c` | Text (20) | The document id. |
| `document_version__c` | Text (40) | `{id}_{major}_{minor}`. |
| `doc_type__c` | Text (100) | The document type id, as configured (e.g. `purchase_order`). **The source of truth:** to correct a type, a person edits this field; reads use it over the JSON, and every write keeps it. |
| `processed_version__c` | Text (40) | A copy of the classification's `processed_version`. |
| `review_requested__c` | Yes/No | A person sets it to ask for a re-review; the review write clears it. |
| `classification__c` | Long Text (32,000) | JSON. |
| `review__c` | Long Text (32,000) | JSON. |
| `fields__c` | Long Text (32,000) | JSON. |
| `comments__c` | Long Text (32,000) | Comments, newest first. |
| one Text field per match field | Text (255) | e.g. `po_number__c`, listed in `match_fields`. |

Optional: add a **Document Reference** field (for example `document__c`) if you want the record shown
on the document's page. The adapter does not read or write it; it matches on `document_id__c`.

## 2. Create the integration user

Create a user for the engine with a security profile and permission set that allow:

- **Read** on documents in scope, including *View Content* (to download files). No edit, state
  change, delete or create on documents.
- **Create, Read, Edit** on `docreview_result__c` and its fields. No delete.
- API access (*Vault Owner Actions → API* or your equivalent permission).

Sign-in is a session started with that user's username and password.

## 3. Configure

```yaml
ecm:
  adapter: veeva_vault
  options:
    vault_url: https://myvault.veevavault.com
    username: ${VEEVA_USERNAME}
    password: ${VEEVA_PASSWORD}
    api_version: v24.1
    scope:                       # which documents the engine sees: document field -> allowed values
      type__v: [Specification, Certificate of Analysis]
      status__v: [Approved, Effective]
    match_fields:                # extracted field -> object field used to find references
      specification_number: specification_number__c
      lot_number: lot_number__c
```

When `username` / `password` are left out, the adapter reads `VEEVA_USERNAME` and `VEEVA_PASSWORD`
from the environment.

| Option | Default | Meaning |
|---|---|---|
| `vault_url` | (required) | `https://<vault>.veevavault.com` |
| `scope` | (required) | Document field → list of values. Only matching documents are polled, returned, downloaded or written for. Field names must be Vault names (`…__v`, `…__c`). |
| `api_version` | `v24.1` | The REST API version in the URL. |
| `result_object` | `docreview_result__c` | The custom object. |
| `match_fields` | `{}` | Extracted fields that reference rules match on, each mapped to a text field of the object. |
| `comments_max_chars` | `30000` | Size cap of `comments__c`. |

Run `docreview check`. It signs in, confirms that the object has every field above (plus the match
fields), and runs the scope query once.

## 4. How the port maps to Vault

| Port method | Vault API |
|---|---|
| `poll` | VQL `SELECT … FROM documents WHERE <scope>`, then the result records for those ids (`document_id__c CONTAINS (…)`, 200 per query). It offers `review_requested` when the current version's record has `review_requested__c`, and `new_document` when that record is missing or its `processed_version__c` ≠ the version. Stateless: no cursor. |
| `get` | VQL on `documents` with `id CONTAINS (<id>)` plus the scope. An id outside the scope is refused. |
| `download` | `GET /api/{v}/objects/documents/{id}/versions/{major}/{minor}/file`, pinned to the version in `ref.version`. |
| `read_metadata` | The record for `ref.version`, or the newest earlier record. |
| `write_metadata` | Read, merge, write: `POST /api/{v}/vobjects/{object}` creates the version's record; `PUT` updates it. `None` removes a key. |
| `find` | VQL on the object: `doc_type__c = '…' AND <match field> = '…'` (the type id exactly; match fields folded; `'` and `\` escaped). A document is kept when the record that applies to its current version is among the matching records, and it is still in scope; newest first. Vault has no folders, so `scope` / `same_folder` does not narrow anything. |
| `post_comment` / `list_comments` | `comments__c` on the version's record. |

## Known limits

- **Example adapter, not tested against a live Vault.** Each of the following is taken from the
  documentation and is unverified:
  - `POST /api/{v}/auth` with form fields `username` and `password` returns `sessionId`, which
    goes in the `Authorization` header as it is.
  - Most errors come back as HTTP 200 with `responseStatus: FAILURE`, and an expired session reports
    the error type `INVALID_SESSION_ID`. The adapter signs in again once.
  - `POST /api/{v}/query` with form field `q` runs VQL; further pages are at
    `responseDetails.next_page`, a path relative to the Vault host.
  - VQL: `field CONTAINS ('a', 'b')` works as `IN` on document fields such as `type__v` and
    `status__v`, and on text fields of the object; string literals escape `'` and `\` with a
    backslash; `PAGESIZE 1` limits a query; `ORDER BY created_date__v DESC` works on object records.
  - A `documents` query returns the latest version of each document. Its fields include `filename__v`,
    `major_version_number__v`, `minor_version_number__v` and `version_modified_date__v`.
  - `POST` / `PUT /api/{v}/vobjects/{object}` take a JSON array of records (`PUT` with `id`); a
    JSON `null` clears a field; each row reports its own `responseStatus`.
  - `GET /api/{v}/metadata/vobjects/{object}` lists the object's fields under `object.fields[].name`.
  - The file download returns the bytes with a non-JSON content type.
- Matching is exact on a case-folded, trimmed copy of the value, written by the adapter. Records
  written before a field was added to `match_fields` have no copy until they are written again.
- Long-text fields hold 32,000 characters. A very large review result or extracted-field set will
  not fit, and the save fails.
- Read, merge, write uses no lock: two writers on the same record at once can lose one change.
- `doc_type__c` is matched exactly, so a person who corrects it must type the configured id
  (`purchase_order`, not `Purchase Order`).
- Comment de-duplication only sees the comments still in `comments__c`. Once old comments drop off
  past `comments_max_chars`, a finding that was posted long ago can be posted again. Keep the cap
  high (the field holds 32,000 characters).
- `poll` lists every in-scope document on each run (plus one record query per 200 documents). For a
  large scope, narrow it with `status__v` or a date field, or replace the scan with Vault's
  document-event API.
- A document reviewed before its reference document arrives is re-reviewed automatically when the
  rule matches on a plain field. Otherwise it stays `insufficient_data` until someone sets
  `review_requested__c` on its record.
- Office documents are downloaded as source files; the adapter does not fetch Vault's PDF rendition.
