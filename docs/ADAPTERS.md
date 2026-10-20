# Writing an adapter for your ECM

An adapter is one class with the eight methods of `docreview.ecm.base.ECMAdapter`, plus a `name`
attribute. It is the only code that talks to your content system. The pipeline never sees your
SDK's objects; it sees `DocumentRef`, `DocumentContent` and plain dicts.

```python
from docreview.models import DocumentContent, DocumentRef, Event

class MyAdapter:
    name = "my-ecm"

    def __init__(self, base_url: str, token: str, _base_dir: str = ".") -> None: ...
    def poll(self, cursor):                       -> (list[Event], new_cursor)
    def get(self, document_id):                   -> DocumentRef
    def download(self, ref):                      -> DocumentContent
    def read_metadata(self, ref, namespace):      -> dict
    def find(self, doc_type, criteria, *, scope=None, limit=20) -> list[DocumentRef]
    def write_metadata(self, ref, namespace, values) -> None
    def post_comment(self, ref, text)             -> None
    def list_comments(self, ref)                  -> list[str]
```

Load it from config with no fork and no registration:

```yaml
ecm:
  adapter: my_package.my_module:MyAdapter
  options: {base_url: "https://ecm.example.com", token: "${MY_ECM_TOKEN}"}
```

Every key under `options` is passed to your constructor as a keyword argument, plus `_base_dir`
(the config file's folder). To publish an adapter as a package, declare an entry point in the
`docreview.adapters` group; users can then write `adapter: my-ecm`.

## What each method must do

| Method | Contract |
|---|---|
| `poll(cursor)` | Return events since `cursor`, and the next cursor (any string; the engine stores it). `Event.kind` is `"new_document"` or `"review_requested"`. Offer a document while `classification.processed_version` differs from its current version, so new versions and failed passes come back; offer `review_requested` while `review.requested` is true. Repeating an event is fine; losing one is not. If your system has no event feed, scan an inbox folder, as the local adapter does. |
| `get(id)` | The current name, version and parent folder (`parent_id`) of a document. Refuse ids outside the area you are configured for. |
| `download(ref)` | The bytes and media type. When `ref.version` is set, return that version or raise. PDF, plain text and PNG/JPEG/GIF/WebP are sent to the model as-is; convert Office files to PDF in the adapter if your ECM offers renditions. |
| `read_metadata` / `write_metadata` | Three namespaces, stored however your ECM stores metadata. A write may replace the namespace or merge into it; either way a `None` value means "remove this key". Writes must be idempotent. |
| `find(doc_type, criteria)` | Documents whose classification is `doc_type` and whose extracted fields equal every value in `criteria`. Compare case-insensitively with trimmed whitespace. Bind values as query parameters; never paste them into a query string. Newest first. `scope` is a `parent_id` (for rules with `same_folder: true`). Adapters without folders (for example Veeva Vault and M-Files) leave `parent_id` as None, so `same_folder` has no effect there. |
| `post_comment` / `list_comments` | Plain-text comments on the document. The engine checks `list_comments` so it never posts the same finding twice. |

## Metadata the engine writes

| Namespace | Keys |
|---|---|
| `classification` | `doc_type`, `classified_at`, `run_id`, `fields_version`, `processed_version`; after a failed pass also `failed_version`, `failure_count` (int), `last_error` |
| `fields` | the fields configured for the document type (string, number, integer, boolean, or a `YYYY-MM-DD` string for dates; any may be null) |
| `review` | `outcome`, `finding_counts` (object), `reviewed_version`, `reviewed_at`, `run_id`, `references` (object: name → document id), `requested` (bool) |

People may edit these values in the ECM, and the engine respects their edits:

- A `doc_type` set by a person is never re-classified.
- Fields a person corrects are kept until a new version arrives.
- Setting `review.requested = true` asks for a re-review. Have `poll` return a `review_requested` event for it.

If your ECM needs a schema (templates, columns, properties), add a `render_schema` classmethod so `docreview templates` can generate it. See `docreview/schema.py`, `ecm/local.py` for the simplest example, and [METADATA.md](METADATA.md).

## Optional: `check`

If your adapter has a `check(self, doc_types: list[str]) -> list[str]` method, `docreview check` calls
it and prints each problem it returns. An empty list means OK. Use it to test the connection,
permissions and metadata schema, so a wrong setting shows up at `check` instead of mid-run.

## Rules for adapters

- **No destructive calls.** Do not add delete, move, rename or content-overwrite methods, even
  privately. The engine is advisory by design, so give its service account the least access that works.
- **Secrets** come from options filled by `${ENV_VAR}` in the YAML, or from the environment. Never hard-code them.
- **Fail loudly.** Raise on errors. The runner logs the failure and carries on with the next document.

## Testing your adapter

Use `tests/test_config_prompts_local.py::test_local_adapter_satisfies_port_and_cannot_delete` as
the pattern: `isinstance(adapter, ECMAdapter)` checks the methods exist. Then run the example
config against a test area of your ECM with `review.dry_run: true` before letting it write.
