# M-Files adapter

> **Example adapter, not tested against a live system.** It follows the M-Files Web Service (MFWS)
> REST reference and is tested against a fake server only. Every assumption it makes is listed under
> [Known limits](#known-limits). Treat it as a starting point for your own adapter.

```bash
pip install 'open-doc-review[mfiles]'   # adds httpx
```

## Design

The engine's metadata lives in **properties of the document object**:

| Namespace | Property |
|---|---|
| classification | one multi-line text property holding JSON |
| review | one multi-line text property holding JSON, plus a Boolean property for `requested` |
| fields | one property per extracted field (text, number, date or Boolean) |
| `doc_type` | its own text property, holding the type id as configured. **The source of truth:** a person corrects the type here; reads use it over the JSON, and every write keeps it. `find` searches on it. |
| comments | the built-in **Comment** property (id 33): each comment becomes a version comment |

**Property edits need a check-out.** Following the MFWS reference, every write checks the object
out (`PUT …/latest/checkedout` with value 2), sets the properties (`POST …/{version}/properties`),
and checks it in (`PUT …/{version}/checkedout` with value 0). The properties are read **after** the
check-out, so nobody can change them between the read and the write. The check-in happens even when
the update fails. If a person has the document checked out, the check-out fails and the write raises,
so the person's work is never overwritten. Each write therefore creates a new **object** version;
the file is never changed.

**The version is the file's version, not the object's.** The object version number doesn't work:
every property write, including the engine's own, creates a new object version, so the engine would
process each document forever. The adapter uses `{file id}.{file version}`,
which changes only when the file content changes.

## 1. Create the properties

**Generate the list:** `docreview templates` prints every property definition to create for your
config (name, data type, purpose; the fields reference rules search on are marked), and the
`properties:` block for `ecm.options` with an `<id of '…'>` placeholder for each id.

By hand:

In M-Files Admin → the vault → Metadata Structure → Property Definitions, create (names are yours):

| Purpose | Data type | Example name |
|---|---|---|
| classification | Text (multi-line) | DocReview Classification |
| review | Text (multi-line) | DocReview Review |
| doc_type | Text | DocReview Doc Type |
| requested | Boolean (yes/no) | DocReview Review Requested |
| one per extracted field | Text / Number / Date / Boolean | e.g. PO Number, Invoice Date |

Add them to the document classes in scope as optional properties. Note each property definition's
id: the config uses ids. Consider hiding the two JSON properties from the default metadata card.

## 2. Create the integration user

A vault user for the engine with **read** access to documents of those classes and **edit**
permission on their metadata. It does not need delete or destroy rights, and must not have them.

## 3. Configure

```yaml
ecm:
  adapter: mfiles
  options:
    server_url: https://mfiles.example.com/REST
    vault_guid: "{01234567-89AB-CDEF-0123-456789ABCDEF}"
    username: ${MFILES_USERNAME}
    password: ${MFILES_PASSWORD}
    classes: [5, 12]                 # document classes in scope
    properties:
      classification: 1101
      review: 1102
      doc_type: 1103
      requested: 1104
      fields:
        po_number: 1110
        invoice_date: 1111
```

When `username` / `password` are left out, the adapter reads `MFILES_USERNAME` and `MFILES_PASSWORD`.

| Option | Default | Meaning |
|---|---|---|
| `server_url` | (required) | The MFWS base URL, ending in `/REST`. |
| `vault_guid` | (required) | The vault to sign in to. |
| `classes` | (required) | Class ids in scope. Objects of other classes are not polled, returned, downloaded or written. |
| `properties` | (required) | Property definition ids, as above. |
| `object_type` | `0` | The Document object type. |
| `search_limit` | `500` | Maximum results per search. A poll whose search would return more raises, rather than silently skipping documents. |

Run `docreview check`. It signs in, reads every class and checks every property definition's data
type: multi-line text for the JSON properties, text for `doc_type`, Boolean for `requested`.

## 4. How the port maps to MFWS

| Port method | MFWS |
|---|---|
| `poll` | `GET /objects?o=0&p100=<class>` per class, then `GET /objects/0/{id}/latest/properties` for each document. It offers `review_requested` while `requested` is true, and `new_document` while `processed_version` ≠ the file version. Stateless: no cursor. |
| `get` | `GET /objects/0/{id}/latest`. It refuses other classes and objects without a file. |
| `download` | `GET /objects/0/{id}/latest/files/{file id}/content`, after confirming that the file version is still `ref.version`. |
| `read_metadata` / `write_metadata` | Properties, as above. JSON namespaces are read, merged and written; `None` removes a key, or empties a field property. |
| `find` | `GET /objects?o=0&p<doc_type>=…&p<field>=…`, with values passed as query parameters (URL-encoded, never spliced in). Up to 50 hits are re-read and kept only if the class is in scope and every value matches case-insensitively with whitespace trimmed; newest first. M-Files has no folders, so `scope` / `same_folder` does not narrow anything. |
| `post_comment` | Sets property 33 (Comment) through the check-out flow. |
| `list_comments` | `GET /objects/0/{id}/latest/comments`. |

## Known limits

- **Example adapter, not tested against a live M-Files server.** Each of the following is taken from
  the documentation and is unverified:
  - `POST /server/authenticationtokens` with `Username`, `Password` and `VaultGuid` returns
    `{"Value": token}`, sent as `X-Authentication`. On a 401 the adapter signs in again once.
  - `GET /objects?o=0&p100=<class>&p<id>=<value>&limit=N` searches by property equality (text
    compared case-insensitively) and returns `{"Items": [ObjectVersion], "MoreResults": …}`.
    There is no paging, and `MoreResults: true` means results were cut off. `poll` then raises
    (raise `search_limit` or split the scope into more classes); `find` re-checks its first 50 hits.
  - An ObjectVersion carries `ObjVer.ID` / `ObjVer.Version`, `Class`, `Title`, `LastModifiedUtc`
    and `Files[]` with `ID`, `Version`, `Name` and `Extension`. A file's `Version` changes only
    when its content changes.
  - `PUT /objects/{type}/{id}/latest/checkedout` with `{"Value": 2}` checks out and returns the
    checked-out version; `POST /objects/{type}/{id}/{version}/properties` sets the given properties
    and leaves the others alone; `PUT …/{version}/checkedout` with `{"Value": 0}` checks in.
  - A PropertyValue is `{"PropertyDef": id, "TypedValue": {"DataType": n, "HasValue": bool, "Value": v}}`;
    `HasValue: false` empties a property. A date is written as `YYYY-MM-DD` and may read back with
    a time, which the adapter cuts off.
  - Setting property 33 adds a version comment, and `GET …/latest/comments` lists them as
    `[{"VersionComment": {"TypedValue": {"Value": text}}}]`.
  - `GET /structure/properties/{id}` returns `DataType`, and `GET /structure/classes/{id}` returns
    the class.
- Only the first file of a multi-file document is downloaded and versioned.
- `poll` reads the properties of every in-scope document on each run: one call per document.
- `poll` skips a document that is checked out (`ObjectCheckedOut` / `CheckedOutTo` on the search
  result) and logs it once per object version. It is offered again once it is checked in.
- If the property update fails after the check-out, the adapter checks the object in anyway, which
  can leave an object version with no changes. Undoing a check-out is a `DELETE` in MFWS, and this
  adapter never sends `DELETE`.
- `doc_type` is searched exactly as written, so a person who corrects it should type the configured
  id (`purchase_order`). The recheck after the search ignores case.
- Every write and every comment creates an object version, so the object history grows with each
  review.
- A document reviewed before its reference document arrives is re-reviewed automatically when the
  rule matches on a plain field. Otherwise it stays `insufficient_data` until someone sets the
  `requested` property.
- Office documents are downloaded as source files; the adapter does not ask M-Files for a PDF.
