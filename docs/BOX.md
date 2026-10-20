# Box adapter

Connects open-doc-review to Box. The adapter reads files and writes **metadata and comments beside
them**. It has no delete, move, rename or upload call, so it never changes a document.

```bash
pip install 'open-doc-review[box]'   # adds box-sdk-gen
```

## 1. Create the Box app

In the Box Developer Console, create a **Custom App** with **Server Authentication (Client
Credentials Grant)**.

| Setting | Value |
|---|---|
| App access level | App Access Only |
| Application scopes | Read all files and folders, Write all files and folders (needed for metadata and comments), Manage enterprise properties (only for `events: enterprise`) |
| Advanced features | none |

Have a Box admin authorize the app (Admin Console → Integrations → Platform Apps Manager). Then
share the folder you want reviewed (`root_folder_id`) with the app's service account as
**Editor**. Don't use Co-owner: the adapter never needs it.

Two auth modes:

- `enterprise_id` acts as the app's service account. This is the usual choice.
- `user_id` acts as one managed user. Use it when that user, rather than the service account, owns the content.

## 2. Configure

```yaml
ecm:
  adapter: box
  options:
    client_id: ${BOX_CLIENT_ID}
    client_secret: ${BOX_CLIENT_SECRET}
    enterprise_id: ${BOX_ENTERPRISE_ID}     # or user_id: ${BOX_USER_ID}
    root_folder_id: "123456789"             # nothing outside this folder is read, found or written
    events: folder_scan                     # default; or: enterprise (needs admin rights)
    inbox_folder_id: "123456790"            # folder_scan only; default = root_folder_id
    templates:
      classification: docClassification
      review: docReview
      fields:                               # one template per document type in your config
        invoice: docFieldsInvoice
        purchase_order: docFieldsPurchaseOrder
```

Put secrets in environment variables, never in the YAML. When an option is left out, the adapter
reads `BOX_CLIENT_ID`, `BOX_CLIENT_SECRET`, `BOX_ENTERPRISE_ID` and `BOX_USER_ID` from the environment.

| Option | Default | Meaning |
|---|---|---|
| `root_folder_id` | (required) | Scope for `poll`, `get` and `find`. A file outside it is ignored by `poll` and refused by `get`. |
| `templates` | (required) | The metadata template key behind each namespace (see §3). |
| `events` | `folder_scan` | How new work is found (see §4). |
| `inbox_folder_id` | `root_folder_id` | The folder `folder_scan` lists. It must be inside `root_folder_id`; the first poll refuses to run otherwise. |
| `scope` | `enterprise` | Metadata scope of your templates. |
| `review_trigger_field` | `requested` | Field on the review template that a person sets to `true` to ask for a re-review. |
| `include_new_versions` | `false` | `enterprise` mode: also treat a new version of a file (`EDIT`) as a new document. |

Run `docreview check` after setup. It signs in, confirms that the root folder (and the inbox) can
be read, and confirms that every document type has a template and that each template defines
every key listed in §3. It prints one line per problem.

## 3. Metadata templates

Each namespace the pipeline writes maps to one enterprise metadata template. **Field keys must
equal the keys the pipeline writes**: Box rejects a key that the template does not define.

| Namespace | Template (example key) | Fields (key: Box type) |
|---|---|---|
| `classification` | `docClassification` | `doc_type`: string, `classified_at`: string, `run_id`: string, `fields_version`: string, `processed_version`: string, `failed_version`: string, `failure_count`: float, `last_error`: string |
| `review` | `docReview` | `outcome`: string, `finding_counts`: string, `reviewed_version`: string, `reviewed_at`: string, `run_id`: string, `references`: string, `requested`: enum (`true`, `false`) |
| `fields` | one per document type, e.g. `docFieldsInvoice` | one field per entry in that type's `fields:` config, with the same `name` as key |

Notes:

- Structured values (`finding_counts`, `references`) are stored as JSON text and read back as
  objects. Booleans are stored as `"true"` / `"false"`. Timestamps (`classified_at`, `reviewed_at`)
  are ISO-8601 strings.
- Config type → Box type: `string` → string, `number` / `integer` → float, `date` → date,
  `boolean` → string. Box returns numbers as floats (`failure_count` reads back as `2.0`).
- Date fields: the pipeline writes `YYYY-MM-DD`. When the Box field is a date field, the adapter
  writes `YYYY-MM-DDT00:00:00Z` and reads it back as `YYYY-MM-DD`. It learns field types from the
  template schema, read once per template per process. String fields are stored as written.
- A value the pipeline writes as empty (`None`) removes that field from the file, so a value from an
  older version does not survive into a newer one.
- `processed_version` is written when a full pass succeeds; `failed_version`, `failure_count` and
  `last_error` when one fails. The pipeline skips a version after 3 failures.
- **Reference match fields must be string fields.** A field used in a reference rule's `match:` is
  matched case-insensitively, ignoring surrounding whitespace, and Box only does that on strings.
- `requested`: a person sets it to `true` to queue the file for review (`events: enterprise` only).
  The pipeline writes `requested: false` with every review result, which clears it. If you point
  `review_trigger_field` at a field the pipeline doesn't write, the adapter removes that field
  when it writes the review result instead.

Create the templates with this script. It is a one-off and needs an admin-authorized app.
`templates.json` is a list of `{"key", "display_name", "fields": [{"key", "type", "options"?}]}`:

```python
import json, os
from box_sdk_gen import BoxCCGAuth, BoxClient, CCGConfig, CreateMetadataTemplateFields
from box_sdk_gen import CreateMetadataTemplateFieldsOptionsField as Option

client = BoxClient(BoxCCGAuth(CCGConfig(
    os.environ["BOX_CLIENT_ID"], os.environ["BOX_CLIENT_SECRET"], enterprise_id=os.environ["BOX_ENTERPRISE_ID"])))
for t in json.load(open("templates.json")):
    client.metadata_templates.create_metadata_template(
        "enterprise", t["display_name"], template_key=t["key"],
        fields=[CreateMetadataTemplateFields(type=f["type"], key=f["key"], display_name=f.get("display_name", f["key"]),
                                             options=[Option(key=o) for o in f["options"]] if "options" in f else None)
                for f in t["fields"]])
    print("created", t["key"])
```

```json
[
  {"key": "docClassification", "display_name": "Document classification",
   "fields": [{"key": "doc_type", "type": "string"}, {"key": "classified_at", "type": "string"},
              {"key": "run_id", "type": "string"}, {"key": "fields_version", "type": "string"},
              {"key": "processed_version", "type": "string"}, {"key": "failed_version", "type": "string"},
              {"key": "failure_count", "type": "float"}, {"key": "last_error", "type": "string"}]},
  {"key": "docReview", "display_name": "Document review",
   "fields": [{"key": "outcome", "type": "string"}, {"key": "finding_counts", "type": "string"},
              {"key": "reviewed_version", "type": "string"}, {"key": "reviewed_at", "type": "string"},
              {"key": "run_id", "type": "string"}, {"key": "references", "type": "string"},
              {"key": "requested", "type": "enum", "options": ["true", "false"], "display_name": "Review requested"}]},
  {"key": "docFieldsInvoice", "display_name": "Invoice fields",
   "fields": [{"key": "invoice_number", "type": "string"}, {"key": "po_number", "type": "string"},
              {"key": "total", "type": "float"}, {"key": "invoice_date", "type": "date"}]}
]
```

## 4. Events

| `events` | Needs | Picks up | Cursor |
|---|---|---|---|
| `enterprise` | an admin-authorized app with *Manage enterprise properties* | `UPLOAD` under the root folder, plus `METADATA_INSTANCE_UPDATE` on the review template while `requested` is `true` | the event stream position |
| `folder_scan` | read access only | files in `inbox_folder_id` (one level, no subfolders) whose current version is not the classification's `processed_version`. Such a file is offered on every poll until that version is processed, so a new version is picked up too. | JSON `{"seen": {file_id: version_id}}` (processed versions) |

`enterprise` reads the `admin_logs_streaming` stream. On the first run (no cursor) it only records
the current position and processes nothing that came before. Enterprise events can arrive a few
minutes late; that's how Box works. `folder_scan` does not see re-review requests.

## 5. What each call does in Box

| Port method | Box API |
|---|---|
| `get` | `GET /files/{id}` (name, version, parent, path) |
| `download` | `GET /files/{id}/content?version=` (pinned to the version that was read) |
| `read_metadata` / `write_metadata` | `GET` / `POST`, then on 409 `PUT` (JSON-Patch `add` / `replace`) on `/files/{id}/metadata/enterprise/{template}` |
| `find` | `POST /metadata_queries/execute_read`, with every value bound as a query parameter, under the scope folder (or the root). Results are sorted newest first in the adapter. |
| `post_comment` / `list_comments` | `POST /comments`, `GET /files/{id}/comments` |

The `fields` namespace has no doc type of its own, so the adapter reads the file's `doc_type` from
the classification template to find the template (one extra read when it hasn't seen the file yet).

## 6. Known limits

- `events: enterprise` needs an admin-authorized app (*Manage enterprise properties*). Without
  admin rights, use `folder_scan`.
- The event stream does not replay an event whose processing failed. Either use `folder_scan`
  (it offers every file whose current version isn't processed again on the next poll) or re-run that file with
  `docreview process <id>`.
- Two Box behaviors are assumed but have not been checked against a live Box: that
  `admin_logs_streaming` accepts `stream_position=now` on the first run, and that Box's `ILIKE`
  honors backslash escapes for `%` and `_`. If the escapes are ignored, `find` still returns only
  exact matches, because the adapter filters the results itself.
- A document reviewed before its reference arrives is re-reviewed automatically when the reference is
  processed, for rules whose `match` values are bare `{field}` placeholders. For other rules, set
  `requested` to `true` on it once the reference is there.
