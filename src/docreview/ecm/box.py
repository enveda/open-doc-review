"""Box adapter (``pip install open-doc-review[box]``). Setup and template fields: docs/BOX.md.

Reads documents and writes metadata and comments beside them. It has no delete, move, rename or
upload call anywhere: give the service account Editor (not Co-owner) on ``root_folder_id``.
Everything it returns or acts on is under ``root_folder_id``.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from docreview.ecm.base import NS_CLASSIFICATION, NS_FIELDS, NS_REVIEW
from docreview.models import DocumentContent, DocumentRef, Event

_FILE_FIELDS = ["name", "file_version", "modified_at", "parent", "path_collection"]
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")  # a metadata field key, safe to name in a query
_FIND_CAP = 500  # results read before sorting newest first (Box returns them unordered)
_EVENT_PAGES = 5  # pages of the event stream read per poll
_OFF = {"", "0", "false", "no", "off", "none"}
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Keys the pipeline writes; ``check`` confirms the templates define them (Box rejects unknown keys).
CLASSIFICATION_KEYS = (
    "doc_type", "classified_at", "run_id", "fields_version",
    "processed_version", "failed_version", "failure_count", "last_error",
)  # fmt: skip
REVIEW_KEYS = ("outcome", "finding_counts", "reviewed_version", "reviewed_at", "run_id", "references", "requested")


def _sdk():
    try:
        import box_sdk_gen
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError("the Box adapter needs box-sdk-gen: pip install 'open-doc-review[box]'") from exc
    return box_sdk_gen


def _as_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    return obj if isinstance(obj, dict) else obj.to_dict()


def _status(exc: Exception) -> int | None:
    return getattr(getattr(exc, "response_info", None), "status_code", None)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime) or value is None:
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _encode(value: Any) -> Any:
    """Box metadata holds strings, numbers and dates; anything structured is stored as JSON text."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in "{[":
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def _like_literal(value: str) -> str:
    return re.sub(r"([\\%_])", r"\\\1", value)


class BoxAdapter:
    """Box through Client Credentials Grant.

    ``templates`` names the metadata template behind each namespace::

        {"classification": "docClassification", "review": "docReview",
         "fields": {"invoice": "docFieldsInvoice", "purchase_order": "docFieldsPurchaseOrder"}}

    ``events``: ``"folder_scan"`` (default) lists ``inbox_folder_id`` for files whose current version
    has not been processed; ``"enterprise"`` reads the enterprise event stream (needs an admin-scoped app).
    """

    name = "box"

    def __init__(
        self,
        *,
        root_folder_id: str,
        templates: Mapping[str, Any],
        events: str = "folder_scan",
        inbox_folder_id: str | None = None,
        scope: str = "enterprise",
        review_trigger_field: str = "requested",
        include_new_versions: bool = False,
        client_id: str | None = None,
        client_secret: str | None = None,
        enterprise_id: str | None = None,
        user_id: str | None = None,
        client: Any = None,
        _base_dir: Any = None,
    ) -> None:
        if events not in {"enterprise", "folder_scan"}:
            raise ValueError(f"box: events must be 'enterprise' or 'folder_scan', not {events!r}")
        missing = [ns for ns in (NS_CLASSIFICATION, NS_FIELDS, NS_REVIEW) if not templates.get(ns)]
        if missing or not isinstance(templates[NS_FIELDS], Mapping):
            raise ValueError(f"box: templates needs {NS_CLASSIFICATION!r}, {NS_REVIEW!r} and a {NS_FIELDS!r} map")
        if not _KEY.match(review_trigger_field):
            raise ValueError(f"box: review_trigger_field {review_trigger_field!r} is not a valid field key")
        self.root_folder_id = str(root_folder_id)
        self.inbox_folder_id = str(inbox_folder_id or root_folder_id)
        self.templates = dict(templates)
        self.events = events
        self.scope = scope
        self.review_trigger_field = review_trigger_field
        self.include_new_versions = include_new_versions
        self.enterprise_id = enterprise_id or os.environ.get("BOX_ENTERPRISE_ID")
        self._client = client or self._connect(client_id, client_secret, self.enterprise_id, user_id)
        self._doc_types: dict[str, str] = {}  # file id -> doc type, learned from classification reads/writes
        self._field_types: dict[str, dict[str, str]] = {}  # template key -> {field key: Box type}
        self._inbox_checked = False

    @staticmethod
    def _connect(client_id: str | None, client_secret: str | None, enterprise_id: str | None, user_id: str | None):
        sdk = _sdk()
        client_id = client_id or os.environ.get("BOX_CLIENT_ID")
        client_secret = client_secret or os.environ.get("BOX_CLIENT_SECRET")
        user_id = user_id or os.environ.get("BOX_USER_ID")
        if not (client_id and client_secret and (enterprise_id or user_id)):
            raise ValueError("box: set client_id, client_secret and enterprise_id or user_id (or BOX_* env vars)")
        config = sdk.CCGConfig(client_id, client_secret, enterprise_id=enterprise_id, user_id=user_id)
        return sdk.BoxClient(sdk.BoxCCGAuth(config))

    # --- events ---------------------------------------------------------------------------------
    def poll(self, cursor: str | None) -> tuple[list[Event], str | None]:
        return self._poll_stream(cursor) if self.events == "enterprise" else self._poll_folder(cursor)

    def _poll_stream(self, cursor: str | None) -> tuple[list[Event], str | None]:
        sdk = _sdk()
        stream = sdk.GetEventsStreamType.ADMIN_LOGS_STREAMING
        types = ["UPLOAD", "METADATA_INSTANCE_UPDATE"] + (["EDIT"] if self.include_new_versions else [])
        position, wanted = cursor or "now", [sdk.GetEventsEventType(t) for t in types]
        raw: list[dict[str, Any]] = []
        for _ in range(_EVENT_PAGES if cursor else 1):
            page = _as_dict(
                self._client.events.get_events(
                    stream_type=stream, stream_position=position, event_type=wanted, limit=500
                )
            )
            if cursor:  # first run: only learn the current position
                raw += page.get("entries") or []
            position = str(page.get("next_stream_position") or position)
            if not page.get("entries"):
                break
        events, done = [], set()
        for entry in raw:
            event = self._stream_event(entry)
            if event and (event.kind, event.ref.id) not in done:
                done.add((event.kind, event.ref.id))
                events.append(event)
        return events, position

    def _stream_event(self, entry: Mapping[str, Any]) -> Event | None:
        source = entry.get("source") or {}
        if source.get("item_type") != "file" or not source.get("item_id"):
            return None
        kind = entry.get("event_type")
        if kind == "METADATA_INSTANCE_UPDATE":
            if _template_of(entry) != self.templates[NS_REVIEW]:
                return None
            ref = self._scoped(str(source["item_id"]))
            if not ref or str(self.read_metadata(ref, NS_REVIEW).get(self.review_trigger_field, "")).lower() in _OFF:
                return None
            return Event("review_requested", ref)
        ref = self._scoped(str(source["item_id"]))
        return Event("new_document", ref) if ref else None

    def _poll_folder(self, cursor: str | None) -> tuple[list[Event], str | None]:
        if not self._inbox_checked:
            self._check_inbox()
        seen: dict[str, str] = json.loads(cursor)["seen"] if cursor else {}
        now: dict[str, str] = {}
        events = []
        for item in self._folder_files(self.inbox_folder_id):
            version = str((item.get("file_version") or {}).get("id") or "")
            ref = DocumentRef(
                id=str(item["id"]),
                name=item.get("name") or "",
                version=version or None,
                modified_at=_parse_time(item.get("modified_at")),
                parent_id=self.inbox_folder_id,
            )
            if seen.get(ref.id) == version:
                now[ref.id] = version
                continue
            # A read error raises: the whole poll is retried. Box keeps metadata across versions, so a
            # new version still carries the old doc_type but not a matching processed_version.
            if str(self.read_metadata(ref, NS_CLASSIFICATION).get("processed_version") or "") == version:
                now[ref.id] = version  # processed: not looked at again until a new version arrives
            else:
                events.append(Event("new_document", ref))  # offered every poll until it is processed
        return events, json.dumps({"seen": now}, sort_keys=True)

    def _check_inbox(self) -> None:
        if self.inbox_folder_id != self.root_folder_id:
            info = _as_dict(self._client.folders.get_folder_by_id(self.inbox_folder_id, fields=["path_collection"]))
            ids = [str(f.get("id")) for f in (info.get("path_collection") or {}).get("entries") or []]
            if self.root_folder_id not in ids:
                raise ValueError(f"box: inbox_folder_id {self.inbox_folder_id} is not inside {self.root_folder_id}")
        self._inbox_checked = True

    def _folder_files(self, folder_id: str) -> Iterable[dict[str, Any]]:
        marker = None
        while True:
            page = _as_dict(
                self._client.folders.get_folder_items(
                    folder_id, fields=["name", "file_version", "modified_at"], usemarker=True, marker=marker, limit=1000
                )
            )
            yield from (e for e in page.get("entries") or [] if e.get("type") == "file")
            marker = page.get("next_marker")
            if not marker:
                return

    # --- reads ----------------------------------------------------------------------------------
    def get(self, document_id: str) -> DocumentRef:
        ref = self._scoped(document_id)
        if ref is None:
            raise LookupError(f"box: file {document_id} is not under folder {self.root_folder_id}")
        return ref

    def _scoped(self, file_id: str) -> DocumentRef | None:
        """The file as a ref, or None when it is gone or outside ``root_folder_id``."""
        try:
            info = _as_dict(self._client.files.get_file_by_id(file_id, fields=_FILE_FIELDS))
        except Exception as exc:
            if _status(exc) == 404:
                return None
            raise
        chain = (info.get("path_collection") or {}).get("entries") or []
        ids = [str(f.get("id")) for f in chain]
        if self.root_folder_id not in ids:
            return None
        below_root = chain[ids.index(self.root_folder_id) :]
        return DocumentRef(
            id=str(info.get("id") or file_id),
            name=info.get("name") or "",
            version=(info.get("file_version") or {}).get("id"),
            modified_at=_parse_time(info.get("modified_at")),
            parent_id=str((info.get("parent") or {}).get("id") or ids[-1]),
            location="/".join(f.get("name") or "" for f in below_root),
        )

    def download(self, ref: DocumentRef) -> DocumentContent:
        stream = self._client.downloads.download_file(ref.id, version=ref.version)
        data = stream.read() if stream is not None else b""
        media_type = mimetypes.guess_type(ref.name)[0] or "application/octet-stream"
        return DocumentContent(ref=ref, data=data, media_type=media_type)

    def read_metadata(self, ref: DocumentRef, namespace: str) -> dict[str, Any]:
        template = self._template(ref, namespace)
        if template is None:
            return {}
        values = self._read_instance(ref.id, template)
        if namespace == NS_CLASSIFICATION and values.get("doc_type"):
            self._doc_types[ref.id] = values["doc_type"]
        types = self._types(template) if values else {}
        return {k: str(v)[:10] if types.get(k) == "date" and v else _decode(v) for k, v in values.items()}

    def _read_instance(self, file_id: str, template: str) -> dict[str, Any]:
        try:
            raw = _as_dict(self._client.file_metadata.get_file_metadata_by_id(file_id, self.scope, template))
        except Exception as exc:
            if _status(exc) == 404:
                return {}
            raise
        extra = raw.get("extra_data") if isinstance(raw.get("extra_data"), dict) else {}
        return {k: v for k, v in {**raw, **extra}.items() if not k.startswith("$") and k != "extra_data"}

    def find(
        self, doc_type: str, criteria: Mapping[str, str], *, scope: str | None = None, limit: int = 20
    ) -> list[DocumentRef]:
        template = self._fields_template(doc_type)
        want = {k: str(v).strip() for k, v in criteria.items()}
        bad = [k for k in want if not _KEY.match(k)]
        if bad or not want:
            raise ValueError(f"box: find needs criteria with valid field keys (got {bad or 'none'})")
        keys = list(want)
        query = " AND ".join(f"{k} ILIKE :v{i}" for i, k in enumerate(keys))
        params = {f"v{i}": _like_literal(want[k]) for i, k in enumerate(keys)}
        md_scope = self._query_scope()
        fields = ["name", "file_version", "modified_at", "created_at", "parent"]
        fields += [f"metadata.{md_scope}.{template}.{k}" for k in keys]
        matches, marker = [], None
        while len(matches) < _FIND_CAP:
            page = _as_dict(
                self._client.search.search_by_metadata_query(
                    f"{md_scope}.{template}",
                    str(scope or self.root_folder_id),
                    query=query,
                    query_params=params,
                    limit=100,
                    marker=marker,
                    fields=fields,
                )
            )
            for item in page.get("entries") or []:
                values = ((item.get("metadata") or {}).get(md_scope) or {}).get(template) or {}
                # ILIKE is the recall; exact equality after case/whitespace folding is the match
                if item.get("type") == "file" and all(
                    str(values.get(k, "")).strip().casefold() == v.casefold() for k, v in want.items()
                ):
                    matches.append(item)
            marker = page.get("next_marker")
            if not marker:
                break
        matches.sort(key=lambda i: str(i.get("modified_at") or i.get("created_at") or ""), reverse=True)
        return [
            DocumentRef(
                id=str(i["id"]),
                name=i.get("name") or "",
                version=(i.get("file_version") or {}).get("id"),
                modified_at=_parse_time(i.get("modified_at")),
                parent_id=(i.get("parent") or {}).get("id"),
            )
            for i in matches[:limit]
        ]

    # --- writes ---------------------------------------------------------------------------------
    def write_metadata(self, ref: DocumentRef, namespace: str, values: Mapping[str, Any]) -> None:
        template = self._template(ref, namespace)
        if template is None:
            raise ValueError(f"box: no {namespace!r} template for {ref.name!r} (classify it first)")
        types = self._types(template)
        data = {k: _encode(v) for k, v in values.items() if v is not None}
        for k, v in data.items():  # the pipeline writes days; a Box date field takes a timestamp
            if types.get(k) == "date" and isinstance(v, str) and _DAY.match(v):
                data[k] = f"{v}T00:00:00Z"
        cleared = [k for k, v in values.items() if v is None]
        try:
            self._client.file_metadata.create_file_metadata_by_id(ref.id, self.scope, template, data)
        except Exception as exc:
            if _status(exc) != 409:
                raise
            self._update(ref.id, template, data, cleared, clear_trigger=namespace == NS_REVIEW)
        if namespace == NS_CLASSIFICATION and values.get("doc_type"):
            self._doc_types[ref.id] = str(values["doc_type"])

    def _update(
        self, file_id: str, template: str, data: Mapping[str, Any], cleared: list[str], *, clear_trigger: bool
    ) -> None:
        sdk = _sdk()
        body, op = sdk.UpdateFileMetadataByIdRequestBody, sdk.UpdateFileMetadataByIdRequestBodyOpField
        current = self._read_instance(file_id, template)
        ops = [body(op=op.REPLACE if k in current else op.ADD, path=f"/{k}", value=v) for k, v in data.items()]
        ops += [body(op=op.REMOVE, path=f"/{k}") for k in cleared if k in current]  # None clears a stale value
        trigger = self.review_trigger_field
        if clear_trigger and trigger not in data and str(current.get(trigger, "")).lower() not in _OFF:
            # The pipeline writes ``requested: false`` itself; a custom trigger field it doesn't write is
            # cleared here, or the next metadata event would ask for the same review again.
            ops.append(body(op=op.REMOVE, path=f"/{trigger}"))
        if ops:
            self._client.file_metadata.update_file_metadata_by_id(file_id, self.scope, template, ops)

    def post_comment(self, ref: DocumentRef, text: str) -> None:
        sdk = _sdk()
        item = sdk.CreateCommentItem(id=ref.id, type=sdk.CreateCommentItemTypeField.FILE)
        self._client.comments.create_comment(text, item)

    def list_comments(self, ref: DocumentRef) -> Iterable[str]:
        offset, texts = 0, []
        while True:
            page = _as_dict(
                self._client.comments.get_file_comments(ref.id, fields=["message"], limit=1000, offset=offset)
            )
            entries = page.get("entries") or []
            texts += [e.get("message") or "" for e in entries]
            offset += len(entries)
            if not entries or offset >= int(page.get("total_count") or 0):
                return texts

    # --- setup check (called by ``docreview check``) -----------------------------------------------
    def check(self, doc_types: list[str]) -> list[str]:
        """Problems with credentials, folders and templates; an empty list means ready."""
        try:
            self._client.users.get_user_me(fields=["login"])
        except Exception as exc:
            return [f"box: cannot sign in ({exc}); check client_id, client_secret and enterprise_id / user_id"]
        problems = []
        try:
            self._client.folders.get_folder_by_id(self.root_folder_id, fields=["name"])
            if self.events == "folder_scan":
                self._check_inbox()
        except ValueError as exc:
            problems.append(str(exc))
        except Exception as exc:
            problems.append(
                f"box: cannot read folder {self.root_folder_id} or the inbox ({exc}); share it with the app"
            )
        trigger = () if self.review_trigger_field in REVIEW_KEYS else (self.review_trigger_field,)
        wanted = [(NS_CLASSIFICATION, self.templates[NS_CLASSIFICATION], CLASSIFICATION_KEYS)]
        wanted.append((NS_REVIEW, self.templates[NS_REVIEW], REVIEW_KEYS + trigger))
        for doc_type in doc_types:
            if doc_type in self.templates[NS_FIELDS]:
                wanted.append((f"fields of {doc_type!r}", self.templates[NS_FIELDS][doc_type], ()))
            else:
                problems.append(f"box: templates.fields has no template for document type {doc_type!r}")
        for label, template, keys in wanted:
            try:
                types = self._types(template, fresh=True)
            except Exception as exc:
                problems.append(f"box: metadata template {template!r} ({label}) cannot be read ({exc})")
                continue
            missing = [k for k in keys if k not in types]
            if missing:
                problems.append(f"box: metadata template {template!r} ({label}) has no field {', '.join(missing)}")
        return problems

    # --- helpers --------------------------------------------------------------------------------
    def _template(self, ref: DocumentRef, namespace: str) -> str | None:
        if namespace != NS_FIELDS:
            return self.templates.get(namespace)
        doc_type = self._doc_types.get(ref.id) or self.read_metadata(ref, NS_CLASSIFICATION).get("doc_type")
        return self._fields_template(doc_type) if doc_type else None

    def _fields_template(self, doc_type: str) -> str:
        try:
            return self.templates[NS_FIELDS][doc_type]
        except KeyError:
            raise ValueError(f"box: templates.fields has no template for document type {doc_type!r}") from None

    def _types(self, template: str, *, fresh: bool = False) -> dict[str, str]:
        """The template's field types, read once per process (``{}`` for fields Box doesn't list)."""
        if fresh or template not in self._field_types:
            schema = _as_dict(self._client.metadata_templates.get_metadata_template(self.scope, template))
            self._field_types[template] = {f.get("key"): f.get("type") for f in schema.get("fields") or []}
        return self._field_types[template]

    def _query_scope(self) -> str:
        """Metadata queries name the enterprise scope with its id (``enterprise_123``)."""
        if self.scope != "enterprise":
            return self.scope
        if not self.enterprise_id:
            me = _as_dict(self._client.users.get_user_me(fields=["enterprise"]))
            self.enterprise_id = str((me.get("enterprise") or {}).get("id") or "")
        return f"enterprise_{self.enterprise_id}"


_UUID_SUFFIX = re.compile(r"-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _template_of(entry: Mapping[str, Any]) -> str | None:
    """Admin-log metadata events name the template as ``<templateKey>-<uuid>``."""
    md = (entry.get("additional_details") or {}).get("metadata") or {}
    kind = md.get("type") if isinstance(md, dict) else None
    return _UUID_SUFFIX.sub("", kind) if isinstance(kind, str) else None
