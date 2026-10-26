"""Example adapter, not tested against a live system: Veeva Vault over the Vault REST API and VQL.

Written from Veeva's published REST API reference and tested against a fake transport only. See
docs/VEEVA_VAULT.md, including Known limits, before pointing it at a Vault.

A Vault document is a controlled record, so this adapter never edits a document or its fields.
Everything the engine writes goes into a custom object (default ``docreview_result__c``): one
record per document version. It downloads document files and creates or updates records of that
one object; it has no other write call.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from docreview.ecm.base import NS_CLASSIFICATION, NS_FIELDS, NS_REVIEW
from docreview.models import DocumentContent, DocumentRef, Event

_NAME = re.compile(r"^[a-z][a-z0-9_]*(__v|__c|__sys)$")  # a Vault field or object name, safe in VQL
_ID = re.compile(r"^\d+$")
_BATCH = 200  # document ids per VQL CONTAINS (...)
_SEPARATOR = "\n\n" + "─" * 24 + "\n\n"
_STAMP = re.compile(r"^\[[^\]\n]*\]\n")
# record field -> what it holds; the admin creates these on the object (docs/VEEVA_VAULT.md)
RECORD_FIELDS = {
    "document_id__c": "text",
    "document_version__c": "text",
    "doc_type__c": "text",
    "processed_version__c": "text",
    "review_requested__c": "boolean",
    "classification__c": "long text",
    "review__c": "long text",
    "fields__c": "long text",
    "comments__c": "long text",
}
_FIELD_SPEC = {  # record field -> (Vault field type, purpose) for ``docreview templates``
    "document_id__c": ("Text (20)", "The document id."),
    "document_version__c": ("Text (40)", "The version this record is for: {id}_{major}_{minor}."),
    "doc_type__c": ("Text (100)", "Document type id. A person corrects the type here; it wins over the JSON."),
    "processed_version__c": ("Text (40)", "Copy of classification.processed_version, for the poll query."),
    "review_requested__c": ("Yes/No", "A person sets it to ask for a re-review; the engine clears it."),
    "classification__c": ("Long Text (32000)", "Classification, as JSON."),
    "review__c": ("Long Text (32000)", "Review result, as JSON."),
    "fields__c": ("Long Text (32000)", "Extracted fields, as JSON."),
    "comments__c": ("Long Text (32000)", "Comments from the engine, newest first."),
}
_JSON = {NS_CLASSIFICATION: "classification__c", NS_REVIEW: "review__c", NS_FIELDS: "fields__c"}


def _httpx():
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError("the Veeva Vault adapter needs httpx: pip install 'open-doc-review[veeva_vault]'") from exc
    return httpx


def _literal(value: Any) -> str:
    """A VQL string literal: backslash and single quote escaped with a backslash."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _key(value: Any) -> str:
    """Match columns hold a folded copy of the value, so VQL's exact ``=`` is a case-insensitive match."""
    return str(value if value is not None else "").strip().casefold()


def _json(text: Any) -> dict[str, Any]:
    try:
        value = json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class VaultError(RuntimeError):
    pass


class VeevaVaultAdapter:
    """Documents in a Vault, selected by ``scope`` (document fields → allowed values)."""

    name = "veeva_vault"

    def __init__(
        self,
        *,
        vault_url: str,
        scope: Mapping[str, Any],
        username: str | None = None,
        password: str | None = None,
        api_version: str = "v24.1",
        result_object: str = "docreview_result__c",
        match_fields: Mapping[str, str] | None = None,
        comments_max_chars: int = 30000,
        transport: Any = None,
        _base_dir: Any = None,
    ) -> None:
        if not vault_url.startswith("https://"):
            raise ValueError("veeva_vault: vault_url must be https://<vault>.veevavault.com")
        self.username = username or os.environ.get("VEEVA_USERNAME")
        self.password = password or os.environ.get("VEEVA_PASSWORD")
        if not (self.username and self.password):
            raise ValueError("veeva_vault: set username and password (or VEEVA_USERNAME / VEEVA_PASSWORD)")
        if not scope:
            raise ValueError("veeva_vault: scope needs at least one document field, e.g. {type__v: [Specification]}")
        self.scope = {k: [v] if isinstance(v, str) else list(v) for k, v in scope.items()}
        self.match_fields = dict(match_fields or {})  # extracted field -> record field holding its folded copy
        names = [*self.scope, result_object, *self.match_fields.values()]
        bad = [n for n in names if not _NAME.match(n)] + [n for n in self.match_fields if not n.isidentifier()]
        if bad or not re.match(r"^v\d+\.\d+$", api_version):
            raise ValueError(f"veeva_vault: invalid names {bad} or api_version {api_version!r}")
        self.base = f"{vault_url.rstrip('/')}/api/{api_version}"
        self.object = result_object
        self.comments_max_chars = comments_max_chars
        self._http = _httpx().Client(transport=transport, timeout=60)
        self._session: str | None = None

    # --- HTTP -----------------------------------------------------------------------------------
    def _login(self) -> str:
        r = self._http.post(f"{self.base}/auth", data={"username": self.username, "password": self.password})
        body = r.json() if r.content else {}
        if r.status_code != 200 or body.get("responseStatus") != "SUCCESS":
            raise VaultError(
                f"veeva_vault: sign-in failed ({(body.get('errors') or [{}])[0].get('type', r.status_code)})"
            )
        self._session = body["sessionId"]
        return self._session

    def _call(self, method: str, path: str, *, raw: bool = False, **kw: Any):
        """One call with the session id; an expired session is renewed once. Vault reports most errors
        as HTTP 200 with ``responseStatus: FAILURE``, so the body is checked too."""
        for attempt in (0, 1):
            headers = {"Authorization": self._session or self._login(), "Accept": "application/json"}
            r = self._http.request(
                method, path if path.startswith("https://") else self.base + path, headers=headers, **kw
            )
            if raw and r.status_code == 200 and not r.headers.get("content-type", "").startswith("application/json"):
                return r.content
            body = r.json() if r.content else {}
            errors = body.get("errors") or []
            if attempt == 0 and any(e.get("type") == "INVALID_SESSION_ID" for e in errors):
                self._session = None
                continue
            if r.status_code >= 400 or body.get("responseStatus") == "FAILURE":
                raise VaultError(f"veeva_vault: {method} {path} failed: {errors or r.status_code}")
            return body
        raise AssertionError("unreachable")  # pragma: no cover

    def _vql(self, query: str) -> Iterable[dict[str, Any]]:
        body = self._call("POST", "/query", data={"q": query})
        while True:
            yield from body.get("data") or []
            nxt = (body.get("responseDetails") or {}).get("next_page")
            if not nxt:
                return
            body = self._call("GET", nxt if nxt.startswith("https://") else self.base.split("/api/")[0] + nxt)

    # --- scope and refs ---------------------------------------------------------------------------
    def _scope_where(self) -> str:
        return " AND ".join(f"{k} CONTAINS ({', '.join(_literal(v) for v in vals)})" for k, vals in self.scope.items())

    def _documents(self, ids: list[str] | None = None) -> list[dict[str, Any]]:
        """Current versions of in-scope documents (all of them, or these ids)."""
        cols = "id, name__v, filename__v, major_version_number__v, minor_version_number__v, version_modified_date__v"
        where = self._scope_where()
        if ids is None:
            return list(self._vql(f"SELECT {cols} FROM documents WHERE {where}"))
        out = []
        for i in range(0, len(ids), _BATCH):
            chunk = ", ".join(str(int(d)) for d in ids[i : i + _BATCH])  # ids are numbers: int() refuses anything else
            out += self._vql(f"SELECT {cols} FROM documents WHERE id CONTAINS ({chunk}) AND {where}")
        return out

    @staticmethod
    def _ref(doc: Mapping[str, Any]) -> DocumentRef:
        doc_id = str(doc["id"])
        version = f"{doc_id}_{doc['major_version_number__v']}_{doc['minor_version_number__v']}"
        modified = doc.get("version_modified_date__v")
        return DocumentRef(
            id=doc_id,
            name=doc.get("filename__v") or doc.get("name__v") or doc_id,
            version=version,
            modified_at=datetime.fromisoformat(modified.replace("Z", "+00:00")) if modified else None,
        )

    def _records(self, doc_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        """Result records per document id, newest first."""
        cols = ", ".join(["id", *RECORD_FIELDS, *dict.fromkeys(self.match_fields.values())])
        by_doc: dict[str, list[dict[str, Any]]] = {}
        for i in range(0, len(doc_ids), _BATCH):
            ids = ", ".join(_literal(d) for d in doc_ids[i : i + _BATCH])
            q = f"SELECT {cols} FROM {self.object} WHERE document_id__c CONTAINS ({ids}) ORDER BY created_date__v DESC"
            for row in self._vql(q):
                by_doc.setdefault(str(row["document_id__c"]), []).append(row)
        return by_doc

    @staticmethod
    def _pick(records: list[dict[str, Any]], version: str | None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """(the record for this version or None, the record whose values apply: this version's, else the newest).
        Values carry forward to a new version until it gets its own record, as metadata does in other systems."""
        own = next((r for r in records if r.get("document_version__c") == version), None)
        return own, own or (records[0] if records else {})

    # --- events ---------------------------------------------------------------------------------
    def poll(self, cursor: str | None) -> tuple[list[Event], str | None]:
        docs = self._documents()
        records = self._records([str(d["id"]) for d in docs])
        events = []
        for doc in docs:
            ref = self._ref(doc)
            own, _ = self._pick(records.get(ref.id, []), ref.version)
            if own and own.get("review_requested__c") is True:
                events.append(Event("review_requested", ref))
            elif not own or own.get("processed_version__c") != ref.version:
                events.append(Event("new_document", ref))  # new version, or not yet processed successfully
        return events, None  # stateless: the result records are the state

    # --- reads ----------------------------------------------------------------------------------
    def get(self, document_id: str) -> DocumentRef:
        if not _ID.match(str(document_id)):
            raise LookupError(f"veeva_vault: {document_id!r} is not a document id")
        docs = self._documents([str(document_id)])
        if not docs:
            raise LookupError(f"veeva_vault: document {document_id} does not exist or is outside the scope")
        return self._ref(docs[0])

    def download(self, ref: DocumentRef) -> DocumentContent:
        ref = ref if ref.version else self.get(ref.id)
        doc_id, major, minor = str(ref.version).split("_")
        if doc_id != ref.id or not (major.isdigit() and minor.isdigit()):
            raise ValueError(f"veeva_vault: {ref.version!r} is not a version of document {ref.id}")
        self.get(ref.id)  # scope check
        data = self._call("GET", f"/objects/documents/{doc_id}/versions/{major}/{minor}/file", raw=True)
        if isinstance(data, dict):
            raise VaultError(f"veeva_vault: no file for {ref.version}")
        return DocumentContent(ref=ref, data=data, media_type=mimetypes.guess_type(ref.name)[0] or "application/pdf")

    def read_metadata(self, ref: DocumentRef, namespace: str) -> dict[str, Any]:
        _, values = self._pick(self._records([ref.id]).get(ref.id, []), ref.version)
        return self._namespace(values, namespace)

    @staticmethod
    def _namespace(record: Mapping[str, Any], namespace: str) -> dict[str, Any]:
        out = _json(record.get(_JSON[namespace]))
        if namespace == NS_CLASSIFICATION and record.get("doc_type__c"):
            out["doc_type"] = record["doc_type__c"]  # the field people edit in Vault is the source of truth
        if namespace == NS_REVIEW and record:
            out["requested"] = record.get("review_requested__c") is True
        return out

    def find(
        self, doc_type: str, criteria: Mapping[str, str], *, scope: str | None = None, limit: int = 20
    ) -> list[DocumentRef]:
        # Vault has no folders, so refs carry no parent_id and ``scope`` (same_folder rules) has nothing to narrow
        missing = [k for k in criteria if k not in self.match_fields]
        if missing:
            raise ValueError(f"veeva_vault: add {missing} to match_fields to use them in a reference rule")
        where = [f"doc_type__c = {_literal(doc_type.strip())}"]
        where += [f"{self.match_fields[k]} = {_literal(_key(v))}" for k, v in criteria.items()]
        q = f"SELECT document_id__c, document_version__c FROM {self.object} WHERE {' AND '.join(where)}"
        hits: dict[str, set[str]] = {}  # document id -> versions whose record matches
        for r in self._vql(q):
            hits.setdefault(str(r["document_id__c"]), set()).add(r["document_version__c"])
        docs = [self._ref(d) for d in self._documents(list(hits))]  # in scope, current version
        # a match must be on the document's current values: its own record, or the newest one carried forward
        records = self._records([d.id for d in docs])
        applies = {d.id: self._pick(records.get(d.id, []), d.version)[1].get("document_version__c") for d in docs}
        current = [d for d in docs if applies[d.id] in hits[d.id]]
        current.sort(key=lambda d: d.modified_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return current[:limit]

    # --- writes (result records only, never the document) -------------------------------------
    def write_metadata(self, ref: DocumentRef, namespace: str, values: Mapping[str, Any]) -> None:
        ref = ref if ref.version else self.get(ref.id)
        own, current = self._pick(self._records([ref.id]).get(ref.id, []), ref.version)
        record = {k: current.get(k) for k in [*_JSON.values(), "doc_type__c", "processed_version__c"]}
        merged = self._namespace(current, namespace)
        merged.update(values)
        if namespace == NS_REVIEW:
            merged.pop("requested", None)  # a review result answers the request
            record["review_requested__c"] = values.get("requested") is True
        merged = {k: v for k, v in merged.items() if v is not None}  # None removes a key
        record[_JSON[namespace]] = json.dumps(merged, sort_keys=True, default=str)
        classification = merged if namespace == NS_CLASSIFICATION else self._namespace(current, NS_CLASSIFICATION)
        fields = merged if namespace == NS_FIELDS else _json(record["fields__c"])
        record["doc_type__c"] = str(classification.get("doc_type") or "").strip() or None
        record["processed_version__c"] = classification.get("processed_version")
        for k, col in self.match_fields.items():
            record[col] = _key(fields.get(k)) or None
        if own:
            self._save("PUT", {"id": own["id"], **record})
        else:
            record.setdefault("review_requested__c", False)  # a request was for the earlier version
            self._save("POST", {"name__v": ref.version, "document_id__c": ref.id, "document_version__c": ref.version,
                                "comments__c": current.get("comments__c"), **record})  # fmt: skip

    def _save(self, method: str, record: Mapping[str, Any]) -> None:
        """Create (POST) or update (PUT) one record of the result object, and nothing else."""
        body = self._call(method, f"/vobjects/{self.object}", json=[dict(record)])
        failed = [row for row in body.get("data") or [] if row.get("responseStatus") != "SUCCESS"]
        if failed:
            raise VaultError(f"veeva_vault: saving the result record failed: {failed}")

    def post_comment(self, ref: DocumentRef, text: str) -> None:
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        entries = [f"[{stamp}]\n{text.replace(_SEPARATOR, chr(10))}", *self._comment_entries(ref)]
        while len(_SEPARATOR.join(entries)) > self.comments_max_chars and len(entries) > 1:
            entries.pop()  # the oldest drop off first
        ref = ref if ref.version else self.get(ref.id)
        own, current = self._pick(self._records([ref.id]).get(ref.id, []), ref.version)
        body = _SEPARATOR.join(entries)[: self.comments_max_chars]
        if own:
            self._save("PUT", {"id": own["id"], "comments__c": body})
        else:
            carried = {k: current.get(k) for k in RECORD_FIELDS if k not in ("document_id__c", "document_version__c")}
            carried.update(comments__c=body, review_requested__c=False)  # a request was for the earlier version
            self._save("POST", {"name__v": ref.version, "document_id__c": ref.id, "document_version__c": ref.version,
                                **carried})  # fmt: skip

    def list_comments(self, ref: DocumentRef) -> Iterable[str]:
        return [_STAMP.sub("", e, count=1) for e in self._comment_entries(ref)]

    def _comment_entries(self, ref: DocumentRef) -> list[str]:
        _, values = self._pick(self._records([ref.id]).get(ref.id, []), ref.version)
        return [e for e in str(values.get("comments__c") or "").split(_SEPARATOR) if e.strip()]

    # --- setup generator (called by ``docreview templates``; no instance, no network) -------------
    @classmethod
    def render_schema(cls, schema, options, match_fields) -> str:  # noqa: ANN001 - see docreview.schema
        """The custom object's field table (create it in Admin > Configuration > Objects), plus the
        ``match_fields`` option. Searched fields get their own text field, which VQL filters on."""
        from docreview.ecm._render import header, key_lines

        options = options or {}
        obj = options.get("result_object") or "docreview_result__c"
        given = dict(options.get("match_fields") or {})
        searched = sorted(set().union(*match_fields.values())) if match_fields else []
        mapping = {f: given.get(f) or f"{f.lower()}__c" for f in searched}
        how = f"Create custom object {obj} with these fields (Admin > Configuration > Objects), then validate it."
        lines = [header("Veeva Vault", how), f"# Object: {obj}  (name__v: Text, editable; set to the version id)", "#"]
        lines.append(f"# {'Field':<28} {'Type':<18} Purpose")
        rows = [(name, *_FIELD_SPEC[name]) for name in RECORD_FIELDS]
        rows += [
            (col, "Text (255)", f"Folded copy of {f}. Searched by reference rules (find).")
            for f, col in mapping.items()
        ]
        lines += [f"# {name:<28} {kind:<18} {purpose}" for name, kind, purpose in rows]
        for ns in schema:
            where = {NS_CLASSIFICATION: "classification__c", NS_REVIEW: "review__c"}.get(ns.name, "fields__c")
            label = f" (doc type {ns.doc_type})" if ns.doc_type else ""
            lines += ["#", f"# JSON keys in {where}{label}: {ns.description}", *key_lines(ns)]
        lines += ["#", "# docreview.yaml, under ecm.options:", f"#   result_object: {obj}", "#   match_fields:"]
        lines += [f"#     {f}: {col}" for f, col in mapping.items()] or ["#     {}"]
        return "\n".join(lines)

    # --- setup check (called by ``docreview check``) -----------------------------------------------
    def check(self, doc_types: list[str]) -> list[str]:
        try:
            self._login()
        except Exception as exc:
            return [str(exc) + "; check vault_url, username and password"]
        problems = []
        try:
            fields = {f["name"] for f in self._call("GET", f"/metadata/vobjects/{self.object}")["object"]["fields"]}
            for name in [*RECORD_FIELDS, *self.match_fields.values()]:
                if name not in fields:
                    problems.append(f"veeva_vault: object {self.object} has no field {name}")
        except Exception as exc:
            problems.append(f"veeva_vault: cannot read object {self.object} ({exc}); create it (docs/VEEVA_VAULT.md)")
        try:
            next(iter(self._vql(f"SELECT id FROM documents WHERE {self._scope_where()} PAGESIZE 1")), None)
        except Exception as exc:
            problems.append(f"veeva_vault: the scope query fails ({exc}); check the scope fields and values")
        return problems
