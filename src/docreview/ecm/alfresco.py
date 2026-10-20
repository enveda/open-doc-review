"""Example adapter, not tested against a live system: Alfresco Content Services over REST API v1.

``pip install open-doc-review[alfresco]``. Written from the REST API v1 documentation, tested against a fake
server only; setup, content model and assumptions in docs/ALFRESCO.md. It writes one aspect's properties
(``PUT /nodes/{id}`` with ``properties``, which never touches content) and native comments, and has no
delete, move, copy, lock or upload call. Everything it returns or acts on is under ``root_node_id``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any
from urllib.parse import quote

from docreview.ecm.base import NS_CLASSIFICATION, NS_FIELDS, NS_REVIEW
from docreview.models import DocumentContent, DocumentRef, Event

log = logging.getLogger("docreview")

API = "/alfresco/api/-default-/public/alfresco/versions/1"
SEARCH = "/alfresco/api/-default-/public/search/versions/1/search"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")  # a field or prefix name, safe inside a property QName
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FIND_CAP = 500
_RETRIES = 3
_TO_PDF = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".rtf", ".odt", ".ods", ".odp"}
_INCLUDE = "properties,path,aspectNames"


def _httpx():
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError("the Alfresco adapter needs httpx: pip install 'open-doc-review[alfresco]'") from exc
    return httpx


def _json(text: Any) -> dict[str, Any]:
    try:
        value = json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _afts(value: str) -> str:
    """An AFTS quoted phrase: backslash and double quote escaped."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _same(found: Any, wanted: str) -> bool:
    if isinstance(found, (int, float)) and not isinstance(found, bool):
        try:
            return float(found) == float(wanted)
        except ValueError:
            return False
    return str(found if found is not None else "").strip().casefold() == wanted.strip().casefold()


class AlfrescoAdapter:
    """One folder tree of an Alfresco repository. Metadata lives in one aspect (``P`` = ``prefix``):

    ``P:classification`` / ``P:review`` (d:text, JSON), ``P:docType`` (d:text), ``P:reviewRequested``
    (d:boolean, ticked by people) and ``P:f_<field>`` (one property per extracted field).
    """

    name = "alfresco"

    def __init__(
        self,
        *,
        base_url: str,
        root_node_id: str,
        inbox_node_id: str | None = None,
        username: str | None = None,
        password: str | None = None,
        ticket: str | None = None,
        prefix: str = "dr",
        aspect: str | None = None,
        transport: Any = None,
        _base_dir: Any = None,
    ) -> None:
        username = username or os.environ.get("ALFRESCO_USERNAME")
        password = password or os.environ.get("ALFRESCO_PASSWORD")
        ticket = ticket or os.environ.get("ALFRESCO_TICKET")
        if not (ticket or (username and password)):
            raise ValueError("alfresco: set username + password, or ticket (or ALFRESCO_* env vars)")
        if not _NAME.match(prefix):
            raise ValueError(f"alfresco: prefix {prefix!r} must be letters, digits and _")
        # A ticket is sent as Basic base64(ticket), as the authentication API documents.
        token = base64.b64encode((ticket or f"{username}:{password}").encode()).decode()
        self._headers = {"Authorization": f"Basic {token}"}
        self.base_url = base_url.rstrip("/")
        self.root_node_id, self.inbox_node_id = root_node_id, inbox_node_id or root_node_id
        self.prefix, self.aspect = prefix, aspect or f"{prefix}:documentReview"
        self.p_classification, self.p_review = f"{prefix}:classification", f"{prefix}:review"
        self.p_doc_type, self.p_requested = f"{prefix}:docType", f"{prefix}:reviewRequested"
        self._http = _httpx().Client(transport=transport, timeout=60)
        self._types: dict[str, str] | None = None
        self._inbox_checked = False

    # --- HTTP -----------------------------------------------------------------------------------
    def _call(self, method: str, path: str, **kw: Any):
        """One call; 429 / 503 are retried after ``Retry-After``."""
        url = self.base_url + (path if path.startswith("/alfresco/") else API + path)
        for attempt in range(_RETRIES + 1):
            r = self._http.request(method, url, headers=self._headers, **kw)
            if r.status_code not in (429, 503) or attempt == _RETRIES:
                r.raise_for_status()
                return r
            wait = r.headers.get("Retry-After")
            time.sleep(min(float(wait) if wait and wait.isdigit() else 2 ** (attempt + 1), 30))
        raise AssertionError("unreachable")  # pragma: no cover

    def _pages(self, path: str, params: dict[str, Any]) -> Iterable[dict[str, Any]]:
        skip = 0
        while True:
            body = self._call("GET", path, params={**params, "skipCount": skip, "maxItems": 100}).json()
            entries = [e["entry"] for e in (body.get("list") or {}).get("entries") or []]
            yield from entries
            skip += len(entries)
            if not entries or not ((body.get("list") or {}).get("pagination") or {}).get("hasMoreItems"):
                return

    def _node(self, node_id: str) -> dict[str, Any]:
        return self._call("GET", f"/nodes/{quote(node_id, safe='')}", params={"include": _INCLUDE}).json()["entry"]

    def _inside(self, node: Mapping[str, Any]) -> bool:
        ancestors = [e.get("id") for e in (node.get("path") or {}).get("elements") or []]
        return self.root_node_id in ancestors

    def _scoped(self, node_id: str) -> dict[str, Any]:
        try:
            node = self._node(node_id)
        except _httpx().HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise LookupError(f"alfresco: no node {node_id}") from None
            raise
        if not node.get("isFile") or not self._inside(node):
            raise LookupError(f"alfresco: node {node_id} is not a file under {self.root_node_id}")
        return node

    @staticmethod
    def _version(node: Mapping[str, Any]) -> str | None:
        props = node.get("properties") or {}
        if props.get("cm:autoVersionOnUpdateProps") is True:
            raise RuntimeError(f"alfresco: {node.get('name')} versions on property updates; see docs/ALFRESCO.md")
        if props.get("cm:versionLabel"):
            return props["cm:versionLabel"]
        # Not cm:versionable: the content size stands in (our property writes change modifiedAt, not this)
        return f"unversioned:{(node.get('content') or {}).get('sizeInBytes', '')}"

    def _ref(self, node: Mapping[str, Any]) -> DocumentRef:
        modified = node.get("modifiedAt")
        return DocumentRef(
            id=str(node["id"]),
            name=node.get("name") or "",
            version=self._version(node),
            modified_at=datetime.fromisoformat(modified.replace("Z", "+00:00")) if modified else None,
            parent_id=node.get("parentId"),
            location=(node.get("path") or {}).get("name"),
        )

    # --- events ---------------------------------------------------------------------------------
    def poll(self, cursor: str | None) -> tuple[list[Event], str | None]:
        if not self._inbox_checked:
            if self.inbox_node_id != self.root_node_id and not self._inside(self._node(self.inbox_node_id)):
                raise ValueError(f"alfresco: inbox {self.inbox_node_id} is not inside {self.root_node_id}")
            self._inbox_checked = True
        events = []
        params = {"include": _INCLUDE, "where": "(isFile=true)"}
        for node in self._pages(f"/nodes/{quote(self.inbox_node_id, safe='')}/children", params):
            try:
                props, ref = node.get("properties") or {}, self._ref(node)
            except RuntimeError as exc:  # one misconfigured node must not stop the whole inbox
                log.warning("skipping %s: %s", node.get("id"), exc)
                continue
            if props.get(self.p_requested) is True:
                events.append(Event("review_requested", ref))
            elif _json(props.get(self.p_classification)).get("processed_version") != ref.version:
                events.append(Event("new_document", ref))  # new, changed, or not yet processed successfully
        return events, None  # stateless: the properties are the state

    # --- reads ----------------------------------------------------------------------------------
    def get(self, document_id: str) -> DocumentRef:
        return self._ref(self._scoped(document_id))

    def download(self, ref: DocumentRef) -> DocumentContent:
        node = self._scoped(ref.id)
        if ref.version and self._version(node) != ref.version:
            raise RuntimeError(f"alfresco: {ref.name} changed since it was read (version {ref.version})")
        base, mime = f"/nodes/{quote(ref.id, safe='')}", (node.get("content") or {}).get("mimeType")
        if os.path.splitext(ref.name)[1].lower() in _TO_PDF and mime != "application/pdf":
            rendition = self._call("GET", f"{base}/renditions/pdf").json()["entry"]
            if rendition.get("status") != "CREATED":
                try:  # ask the repository to create it; a 409 means it was already requested
                    self._call("POST", f"{base}/renditions", json={"id": "pdf"})
                except _httpx().HTTPStatusError as exc:
                    if exc.response.status_code != 409:
                        raise
                raise RuntimeError(f"alfresco: PDF rendition of {ref.name} pending; it is retried on a later poll")
            return DocumentContent(ref, self._call("GET", f"{base}/renditions/pdf/content").content, "application/pdf")
        data = self._call("GET", f"{base}/content").content
        return DocumentContent(ref=ref, data=data, media_type=mime or "application/octet-stream")

    def property_types(self) -> dict[str, str]:
        """The aspect's properties and data types (``d:text``, ...), from the Model API, once per process."""
        if self._types is None:
            entry = self._call("GET", f"/aspects/{quote(self.aspect, safe='')}").json()["entry"]
            self._types = {p["id"]: p.get("dataType", "") for p in entry.get("properties") or []}
        return self._types

    def read_metadata(self, ref: DocumentRef, namespace: str) -> dict[str, Any]:
        return self._namespace(self._scoped(ref.id).get("properties") or {}, namespace)

    def _namespace(self, props: Mapping[str, Any], namespace: str) -> dict[str, Any]:
        if namespace == NS_CLASSIFICATION:
            return _json(props.get(self.p_classification))
        if namespace == NS_REVIEW:
            values, requested = _json(props.get(self.p_review)), props.get(self.p_requested) is True
            return {**values, "requested": requested} if values or requested else {}
        if namespace == NS_FIELDS:
            lead, types = f"{self.prefix}:f_", self.property_types()
            return {
                k[len(lead) :]: v[:10] if types.get(k) == "d:date" and isinstance(v, str) else v
                for k, v in props.items()
                if k.startswith(lead) and v is not None
            }
        raise ValueError(f"alfresco: unknown namespace {namespace!r}")

    def find(
        self, doc_type: str, criteria: Mapping[str, str], *, scope: str | None = None, limit: int = 20
    ) -> list[DocumentRef]:
        want = {k: str(v).strip() for k, v in criteria.items()}
        if bad := [k for k in want if not _NAME.match(k)]:
            raise ValueError(f"alfresco: invalid field keys {bad}")
        anchor = f"workspace://SpacesStore/{scope or self.root_node_id}"
        clauses = [f"{'PARENT' if scope else 'ANCESTOR'}:{_afts(anchor)}", f"ASPECT:{_afts(self.aspect)}"]
        clauses += [f"{self.p_doc_type}:{_afts(doc_type)}"]
        clauses += [f"{self.prefix}:f_{k}:{_afts(v)}" for k, v in want.items()]  # phrase match: the recall
        body = {
            "query": {"language": "afts", "query": " AND ".join(clauses)},
            "include": ["properties", "path"],
            "sort": [{"type": "FIELD", "field": "cm:modified", "ascending": False}],
        }
        matches: list[dict[str, Any]] = []
        while len(matches) < _FIND_CAP:
            body["paging"] = {"maxItems": 100, "skipCount": len(matches)}
            page = (self._call("POST", SEARCH, json=body).json().get("list")) or {}
            nodes = [e["entry"] for e in page.get("entries") or []]
            matches += nodes
            if not nodes or not (page.get("pagination") or {}).get("hasMoreItems"):
                break
        out = []
        for n in matches:  # exact equality after case/whitespace folding is the match; scope re-checked here
            props = n.get("properties") or {}
            if not n.get("isFile") or not self._inside(n) or (scope and n.get("parentId") != scope):
                continue
            if _same(props.get(self.p_doc_type), doc_type) and all(
                _same(props.get(f"{self.prefix}:f_{k}"), v) for k, v in want.items()
            ):
                out.append(n)
        out.sort(key=lambda n: str(n.get("modifiedAt") or ""), reverse=True)
        refs = []
        for n in out[:limit]:
            try:
                refs.append(self._ref(n))
            except RuntimeError as exc:  # skipped, as in poll
                log.warning("skipping %s: %s", n.get("id"), exc)
        return refs

    # --- writes (aspect properties and comments, never content) -----------------------------------
    def write_metadata(self, ref: DocumentRef, namespace: str, values: Mapping[str, Any]) -> None:
        node = self._scoped(ref.id)
        props, patch = node.get("properties") or {}, {}
        if namespace in (NS_CLASSIFICATION, NS_REVIEW):
            merged = {**self._namespace(props, namespace), **values}  # a write merges into what is there
            if namespace == NS_REVIEW:
                merged.pop("requested", None)  # a review result answers the request: cleared unless set again
                patch[self.p_requested] = values.get("requested") is True
            merged = {k: v for k, v in merged.items() if v is not None}  # None removes a key
            patch[self.p_classification if namespace == NS_CLASSIFICATION else self.p_review] = json.dumps(
                merged, sort_keys=True, default=str
            )
            if namespace == NS_CLASSIFICATION:
                patch[self.p_doc_type] = merged.get("doc_type")
        elif namespace == NS_FIELDS:
            types = self.property_types()
            for k, v in values.items():
                prop = f"{self.prefix}:f_{k}"
                if not _NAME.match(k) or prop not in types:
                    raise ValueError(f"alfresco: aspect {self.aspect!r} has no property {prop!r} (docs/ALFRESCO.md)")
                if types[prop] in ("d:date", "d:datetime") and isinstance(v, str) and _DAY.match(v):
                    v = f"{v}T00:00:00.000+0000"
                patch[prop] = v  # None clears the property
        else:
            raise ValueError(f"alfresco: unknown namespace {namespace!r}")
        aspects = sorted(set(node.get("aspectNames") or []) | {self.aspect})  # PUT replaces the aspect list
        self._call("PUT", f"/nodes/{quote(ref.id, safe='')}", json={"aspectNames": aspects, "properties": patch})

    def post_comment(self, ref: DocumentRef, text: str) -> None:
        self._scoped(ref.id)
        self._call("POST", f"/nodes/{quote(ref.id, safe='')}/comments", json={"content": text})

    def list_comments(self, ref: DocumentRef) -> Iterable[str]:
        self._scoped(ref.id)
        return [c.get("content") or "" for c in self._pages(f"/nodes/{quote(ref.id, safe='')}/comments", {})]

    # --- setup check (called by ``docreview check``) -----------------------------------------------
    def check(self, doc_types: list[str]) -> list[str]:
        """Problems with credentials, the folders and the aspect; an empty list means ready."""
        try:
            self._call("GET", "/people/-me-")
        except Exception as exc:
            return [f"alfresco: cannot sign in ({exc}); check base_url and the credentials"]
        problems = []
        for label, node_id in (("root", self.root_node_id), ("inbox", self.inbox_node_id)):
            try:
                node = self._node(node_id)
                if not node.get("isFolder"):
                    problems.append(f"alfresco: {label} node {node_id} is not a folder")
                elif label == "inbox" and node_id != self.root_node_id and not self._inside(node):
                    problems.append(f"alfresco: inbox {node_id} is not inside {self.root_node_id}")
            except Exception as exc:
                problems.append(f"alfresco: cannot read {label} node {node_id} ({exc})")
        try:
            types = self.property_types()
        except Exception as exc:
            return [*problems, f"alfresco: cannot read aspect {self.aspect!r} ({exc}); deploy the content model"]
        wanted = (self.p_classification, "d:text"), (self.p_review, "d:text"), (self.p_doc_type, "d:text")
        for prop, kind in (*wanted, (self.p_requested, "d:boolean")):
            if prop not in types:
                problems.append(f"alfresco: aspect {self.aspect!r} has no property {prop!r} ({kind})")
            elif types[prop] != kind:
                problems.append(f"alfresco: property {prop!r} should be {kind}, not {types[prop]}")
        if not any(p.startswith(f"{self.prefix}:f_") for p in types):
            problems.append(f"alfresco: no field properties ({self.prefix}:f_<field>) exist yet")
        return problems
