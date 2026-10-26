"""SharePoint Online adapter over Microsoft Graph (``pip install open-doc-review[sharepoint]``).

EXPERIMENTAL: written against the Graph v1.0 documentation and tested against a fake transport
only; it has not been run against a live tenant. Setup, columns and limits: docs/SHAREPOINT.md.

It reads files and writes library columns beside them. It has no delete, move, rename or upload
call anywhere: grant the app ``Sites.Selected`` with ``write`` on the one site it serves.
Everything it returns or acts on is under ``root_path`` of the configured library.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from docreview.ecm.base import NS_CLASSIFICATION, NS_FIELDS, NS_REVIEW
from docreview.models import DocumentContent, DocumentRef, Event

GRAPH = "https://graph.microsoft.com/v1.0"
_LOGIN = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_ITEM_SELECT = "id,name,cTag,file,folder,parentReference,lastModifiedDateTime"
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FIND_CAP = 500
_TO_PDF = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".rtf", ".odt", ".ods", ".odp"}  # Graph renders these
_RETRIES = 3
# SharePoint field XML per schema type
_SP_TYPES = {
    "string": 'Type="Text"',
    "number": 'Type="Number"',
    "integer": 'Type="Number" Decimals="0"',
    "date": 'Type="DateTime" Format="DateOnly"',
    "boolean": 'Type="Boolean"',
    "json": 'Type="Note" RichText="FALSE" NumLines="6" AppendOnly="FALSE"',
}
_SEPARATOR = "\n\n" + "─" * 24 + "\n\n"  # between comments in the comments column
_STAMP = re.compile(r"^\[[^\]\n]*\]\n")  # "[2026-10-20T12:00:00Z]\n" before each comment


def _httpx():
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError("the SharePoint adapter needs httpx: pip install 'open-doc-review[sharepoint]'") from exc
    return httpx


def _odata_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _parse_time(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _encode(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _json(text: Any) -> dict[str, Any]:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class SharePointAdapter:
    """One document library of one SharePoint Online site, through app-only Microsoft Graph.

    Metadata lives in library columns (``P`` = ``column_prefix``):

    - ``PClassification`` / ``PReview``: multi-line text holding the namespace as JSON;
    - ``PDocType``: single-line text, the document type: people correct it here, reads use it over the
      JSON, and ``find`` filters on it;
    - ``PReviewRequested``: Yes/No, the review namespace's ``requested`` (ticked by people);
    - ``P_<field>``: one column per extracted field;
    - ``PComments``: multi-line text holding the comments (Graph has no file-comment API).
    """

    name = "sharepoint"

    def __init__(
        self,
        *,
        tenant_id: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        site_id: str | None = None,
        hostname: str | None = None,
        site_path: str | None = None,
        drive_id: str | None = None,
        library: str | None = None,
        root_path: str = "/",
        inbox_path: str | None = None,
        recursive: bool = False,
        column_prefix: str = "DocReview",
        comments_max_chars: int = 30000,
        transport: Any = None,
        _base_dir: Any = None,
    ) -> None:
        self.tenant_id = tenant_id or os.environ.get("SHAREPOINT_TENANT_ID")
        self.client_id = client_id or os.environ.get("SHAREPOINT_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("SHAREPOINT_CLIENT_SECRET")
        if not (self.tenant_id and self.client_id and self.client_secret):
            raise ValueError("sharepoint: set tenant_id, client_id and client_secret (or SHAREPOINT_* env vars)")
        if not (site_id or (hostname and site_path)) or not (drive_id or library):
            raise ValueError("sharepoint: set site_id (or hostname + site_path) and drive_id (or library)")
        if not _KEY.match(column_prefix):
            raise ValueError(f"sharepoint: column_prefix {column_prefix!r} must be letters, digits and _")
        self._site_id, self._site_ref = site_id, (hostname, site_path)
        self._drive_id, self._library = drive_id, library
        self.root_path = "/" + root_path.strip("/")
        self.inbox_path = "/" + (inbox_path or root_path).strip("/")
        if not self._inside(self.inbox_path):
            raise ValueError(f"sharepoint: inbox_path {self.inbox_path!r} is not inside root_path {self.root_path!r}")
        self.recursive = recursive
        self.prefix = column_prefix
        self.comments_max_chars = comments_max_chars
        self.col_classification = f"{column_prefix}Classification"
        self.col_doc_type = f"{column_prefix}DocType"
        self.col_review = f"{column_prefix}Review"
        self.col_requested = f"{column_prefix}ReviewRequested"
        self.col_comments = f"{column_prefix}Comments"
        self._http = _httpx().Client(transport=transport, timeout=60, follow_redirects=True)
        self._token: tuple[str, float] | None = None
        self._list_id: str | None = None
        self._columns: dict[str, str] | None = None  # column name -> type ("text", "number", ...)

    # --- HTTP -----------------------------------------------------------------------------------
    def _auth(self) -> str:
        if self._token and self._token[1] > time.time():
            return self._token[0]
        r = self._http.post(
            _LOGIN.format(tenant=self.tenant_id),
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        r.raise_for_status()
        body = r.json()
        self._token = (body["access_token"], time.time() + int(body.get("expires_in", 3600)) - 120)
        return self._token[0]

    def _call(self, method: str, url: str, *, headers: Mapping[str, str] | None = None, **kw: Any):
        """One Graph call with the bearer token; 429 / 503 are retried after ``Retry-After``."""
        url = url if url.startswith("https://") else GRAPH + url
        for attempt in range(_RETRIES + 1):
            hdrs = {"Authorization": f"Bearer {self._auth()}", **(headers or {})}
            r = self._http.request(method, url, headers=hdrs, **kw)
            if r.status_code not in (429, 503) or attempt == _RETRIES:
                r.raise_for_status()
                return r
            wait = r.headers.get("Retry-After")
            time.sleep(min(float(wait) if wait and wait.isdigit() else 2 ** (attempt + 1), 30))
        raise AssertionError("unreachable")  # pragma: no cover

    def _get(self, url: str, **kw: Any) -> dict[str, Any]:
        return self._call("GET", url, **kw).json()

    def _pages(self, url: str, **kw: Any) -> Iterable[dict[str, Any]]:
        while url:
            body = self._get(url, **kw)
            yield from body.get("value") or []
            url = body.get("@odata.nextLink")
            kw.pop("params", None)  # the next link carries the query

    # --- site / drive / list ------------------------------------------------------------------------
    @property
    def site_id(self) -> str:
        if not self._site_id:
            host, path = self._site_ref
            self._site_id = self._get(f"/sites/{host}:/{quote(str(path).strip('/'))}", params={"$select": "id"})["id"]
        return self._site_id

    @property
    def drive_id(self) -> str:
        if not self._drive_id:
            drives = list(self._pages(f"/sites/{self.site_id}/drives", params={"$select": "id,name"}))
            match = [d["id"] for d in drives if d.get("name") == self._library]
            if not match:
                raise ValueError(f"sharepoint: no document library named {self._library!r} on the site")
            self._drive_id = match[0]
        return self._drive_id

    @property
    def list_id(self) -> str:
        if not self._list_id:
            self._list_id = self._get(f"/drives/{self.drive_id}/list", params={"$select": "id"})["id"]
        return self._list_id

    def columns(self) -> dict[str, str]:
        """Library columns by internal name, with their type; read once per process."""
        if self._columns is None:
            kinds = ("text", "number", "dateTime", "boolean", "choice", "currency", "personOrGroup", "lookup")
            self._columns = {}
            for c in self._pages(f"/sites/{self.site_id}/lists/{self.list_id}/columns"):
                self._columns[c["name"]] = next((k for k in kinds if k in c), "other")
        return self._columns

    # --- items and scope ------------------------------------------------------------------------
    def _path_of(self, item: Mapping[str, Any]) -> str:
        """``/Folder/Sub/name.pdf`` relative to the drive root."""
        parent = (item.get("parentReference") or {}).get("path") or ""
        folder = parent.split("root:", 1)[1] if "root:" in parent else ""
        return "/" + "/".join(p for p in (folder.strip("/"), item.get("name") or "") if p)

    def _inside(self, path: str) -> bool:
        root = self.root_path.rstrip("/")
        return not root or path == root or path.startswith(root + "/")

    def _ref(self, item: Mapping[str, Any]) -> DocumentRef:
        path = self._path_of(item)
        return DocumentRef(
            id=str(item["id"]),
            name=item.get("name") or "",
            version=item.get("cTag"),
            modified_at=_parse_time(item.get("lastModifiedDateTime")),
            parent_id=(item.get("parentReference") or {}).get("id"),
            location=path.rsplit("/", 1)[0] or "/",
        )

    def _item(self, item_id: str) -> dict[str, Any]:
        url = f"/drives/{self.drive_id}/items/{quote(item_id, safe='')}"
        return self._get(url, params={"$select": _ITEM_SELECT, "$expand": "listItem($expand=fields)"})

    def _scoped(self, item_id: str) -> dict[str, Any]:
        try:
            item = self._item(item_id)
        except _httpx().HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise LookupError(f"sharepoint: no item {item_id}") from None
            raise
        if "file" not in item or not self._inside(self._path_of(item)):
            raise LookupError(f"sharepoint: item {item_id} is not a file under {self.root_path!r}")
        return item

    # --- events ---------------------------------------------------------------------------------
    def poll(self, cursor: str | None) -> tuple[list[Event], str | None]:
        events = []
        for item in self._files(self.inbox_path):
            fields = (item.get("listItem") or {}).get("fields") or {}
            ref = self._ref(item)
            if fields.get(self.col_requested) is True:
                events.append(Event("review_requested", ref))
            elif _json(fields.get(self.col_classification)).get("processed_version") != ref.version:
                events.append(Event("new_document", ref))  # new, changed, or not yet processed successfully
        return events, None  # stateless: the columns are the state

    def _files(self, path: str) -> Iterable[dict[str, Any]]:
        base = f"/drives/{self.drive_id}/root" if path == "/" else f"/drives/{self.drive_id}/root:{quote(path)}:"
        params = {"$select": _ITEM_SELECT, "$expand": "listItem($expand=fields)", "$top": "200"}
        for item in self._pages(f"{base}/children", params=params):
            if "file" in item:
                yield item
            elif "folder" in item and self.recursive:
                yield from self._files(self._path_of(item))

    # --- reads ----------------------------------------------------------------------------------
    def get(self, document_id: str) -> DocumentRef:
        return self._ref(self._scoped(document_id))

    def download(self, ref: DocumentRef) -> DocumentContent:
        item = self._scoped(ref.id)
        if ref.version and item.get("cTag") != ref.version:
            raise RuntimeError(f"sharepoint: {ref.name} changed since it was read (cTag {ref.version})")
        as_pdf = os.path.splitext(ref.name)[1].lower() in _TO_PDF
        url = f"/drives/{self.drive_id}/items/{quote(ref.id, safe='')}/content"
        data = self._call("GET", url, params={"format": "pdf"} if as_pdf else None).content
        media_type = "application/pdf" if as_pdf else (item.get("file") or {}).get("mimeType")
        media_type = media_type or mimetypes.guess_type(ref.name)[0]
        return DocumentContent(ref=ref, data=data, media_type=media_type or "application/octet-stream")

    def _fields(self, ref: DocumentRef) -> dict[str, Any]:
        url = f"/drives/{self.drive_id}/items/{quote(ref.id, safe='')}/listItem/fields"
        return self._get(url)

    def read_metadata(self, ref: DocumentRef, namespace: str) -> dict[str, Any]:
        return self._namespace(self._fields(ref), namespace)

    def _namespace(self, fields: Mapping[str, Any], namespace: str) -> dict[str, Any]:
        if namespace == NS_CLASSIFICATION:
            out = _json(fields.get(self.col_classification))
            if str(fields.get(self.col_doc_type) or "").strip():
                out["doc_type"] = str(fields[self.col_doc_type]).strip()  # the column people edit wins over the JSON
            return out
        if namespace == NS_REVIEW:
            values, requested = _json(fields.get(self.col_review)), fields.get(self.col_requested) is True
            return {**values, "requested": requested} if values or requested else {}
        if namespace == NS_FIELDS:
            types, lead = self.columns(), f"{self.prefix}_"
            out = {}
            for col, value in fields.items():
                if col.startswith(lead) and value is not None:
                    is_day = types.get(col) == "dateTime" and isinstance(value, str)
                    out[col[len(lead) :]] = value[:10] if is_day else value
            return out
        raise ValueError(f"sharepoint: unknown namespace {namespace!r}")

    def find(
        self, doc_type: str, criteria: Mapping[str, str], *, scope: str | None = None, limit: int = 20
    ) -> list[DocumentRef]:
        want = {k: str(v).strip() for k, v in criteria.items()}
        bad = [k for k in want if not _KEY.match(k)]
        if bad:
            raise ValueError(f"sharepoint: invalid field keys {bad}")
        types = self.columns()
        clauses = [f"fields/{self.col_doc_type} eq {_odata_literal(doc_type)}"]
        for k, v in want.items():
            col = f"{self.prefix}_{k}"
            if types.get(col) == "number":
                clauses.append(f"fields/{col} eq {float(v)!r}")  # float() refuses anything but a number
            else:
                clauses.append(f"fields/{col} eq {_odata_literal(v)}")
        select = ",".join([self.col_doc_type, *(f"{self.prefix}_{k}" for k in want)])
        params = {
            "$filter": " and ".join(clauses),
            "$expand": f"fields($select={select}),driveItem($select={_ITEM_SELECT})",
            "$top": "200",
        }
        headers = {"Prefer": "HonorNonIndexedQueriesWarningMayFailRandomly"}
        matches = []
        for row in self._pages(f"/sites/{self.site_id}/lists/{self.list_id}/items", params=params, headers=headers):
            item, fields = row.get("driveItem") or {}, row.get("fields") or {}
            if not item or "file" not in item or not self._inside(self._path_of(item)):
                continue
            if scope and (item.get("parentReference") or {}).get("id") != scope:
                continue
            same = str(fields.get(self.col_doc_type) or "").strip().casefold() == doc_type.strip().casefold()
            # SharePoint's eq is the recall; equality after case/whitespace folding is the match
            if same and all(_same(fields.get(f"{self.prefix}_{k}"), v) for k, v in want.items()):
                matches.append(item)
            if len(matches) >= _FIND_CAP:
                break
        matches.sort(key=lambda i: str(i.get("lastModifiedDateTime") or ""), reverse=True)
        return [self._ref(i) for i in matches[:limit]]

    # --- writes (library columns of the file's list item, never the file) ----------------------
    def write_metadata(self, ref: DocumentRef, namespace: str, values: Mapping[str, Any]) -> None:
        patch: dict[str, Any] = {}
        if namespace in (NS_CLASSIFICATION, NS_REVIEW):
            merged = self._namespace(self._fields(ref), namespace)  # a write merges into what is there
            merged.update(values)
            if namespace == NS_REVIEW:
                merged.pop("requested", None)  # a review result answers the request: cleared unless set again
                patch[self.col_requested] = values.get("requested") is True
            merged = {k: v for k, v in merged.items() if v is not None}  # None removes a key
            col = self.col_classification if namespace == NS_CLASSIFICATION else self.col_review
            patch[col] = json.dumps(merged, sort_keys=True, default=str)
            if namespace == NS_CLASSIFICATION:
                patch[self.col_doc_type] = merged.get("doc_type")  # merged already took a person's edit
        elif namespace == NS_FIELDS:
            types = self.columns()
            for k, v in values.items():
                if not _KEY.match(k):
                    raise ValueError(f"sharepoint: invalid field key {k!r}")
                col = f"{self.prefix}_{k}"
                if col not in types:
                    raise ValueError(f"sharepoint: the library has no column {col!r} (see docs/SHAREPOINT.md)")
                if types[col] == "dateTime" and isinstance(v, str) and _DAY.match(v):
                    v = f"{v}T00:00:00Z"
                patch[col] = _encode(v)  # None clears the column
        else:
            raise ValueError(f"sharepoint: unknown namespace {namespace!r}")
        self._patch_fields(ref, patch)

    def _patch_fields(self, ref: DocumentRef, patch: Mapping[str, Any]) -> None:
        url = f"/drives/{self.drive_id}/items/{quote(ref.id, safe='')}/listItem/fields"
        self._call("PATCH", url, json=dict(patch))

    def post_comment(self, ref: DocumentRef, text: str) -> None:
        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        entries = [f"[{stamp}]\n{text.replace(_SEPARATOR, chr(10))}", *self._comment_entries(ref)]
        body = _SEPARATOR.join(entries)
        while len(body) > self.comments_max_chars and len(entries) > 1:
            entries.pop()  # the oldest comments drop off first
            body = _SEPARATOR.join(entries)
        self._patch_fields(ref, {self.col_comments: body[: self.comments_max_chars]})

    def list_comments(self, ref: DocumentRef) -> Iterable[str]:
        return [_STAMP.sub("", e, count=1) for e in self._comment_entries(ref)]

    def _comment_entries(self, ref: DocumentRef) -> list[str]:
        raw = self._fields(ref).get(self.col_comments) or ""
        return [e for e in str(raw).split(_SEPARATOR) if e.strip()]

    # --- setup generator (called by ``docreview templates``; no instance, no network) -------------
    @classmethod
    def render_schema(cls, schema, options, match_fields) -> str:  # noqa: ANN001 - see docreview.schema
        """A PnP PowerShell script adding every column; DocType and searched fields are indexed."""
        from xml.sax.saxutils import quoteattr

        from docreview.ecm._render import header, key_lines

        options = options or {}
        p = options.get("column_prefix") or "DocReview"
        library = str(options.get("library") or "Documents")
        searched = set().union(*match_fields.values()) if match_fields else set()
        lines = [header("SharePoint", "Run it in PnP PowerShell after Connect-PnPOnline to the site.")]
        columns: dict[str, tuple[str, str, bool]] = {}  # internal name -> (type xml, description, indexed)
        for ns in schema:
            label = f"{ns.name}" + (f" of {ns.doc_type}" if ns.doc_type else "")
            lines.append(f"# {label}: {ns.description}")
            lines += key_lines(ns)
            if ns.name == NS_CLASSIFICATION:
                columns[f"{p}Classification"] = (
                    _SP_TYPES["json"],
                    "Classification (JSON; written by the engine).",
                    False,
                )
                lines.append(f"#   doc_type lives in {p}DocType: people correct it there, and find filters on it")
                columns[f"{p}DocType"] = (_SP_TYPES["string"], ns.keys[0].description, True)
            elif ns.name == NS_REVIEW:
                columns[f"{p}Review"] = (_SP_TYPES["json"], "Review result (JSON; written by the engine).", False)
                requested = next(k for k in ns.keys if k.name == "requested")
                columns[f"{p}ReviewRequested"] = (_SP_TYPES["boolean"], requested.description, False)
            else:
                for k in ns.keys:
                    col = f"{p}_{k.name}"
                    if col in columns and columns[col][0] != _SP_TYPES[k.type]:
                        lines.append(f"# WARNING: {k.name} has different types across document types; one column")
                    note = " Searched by reference rules (find)." if k.name in searched else ""
                    columns.setdefault(col, (_SP_TYPES[k.type], k.description + note, k.name in searched))
        columns[f"{p}Comments"] = (_SP_TYPES["json"], "Comments from the engine, newest first.", False)
        lines += [f"#   {c}: indexed - find filters on it" for c, (_, _, ix) in columns.items() if ix]
        lines += ["", f"$list = {_ps(library)}", ""]
        for col, (kind, description, indexed) in columns.items():
            attrs = (
                f'{kind} Name="{col}" StaticName="{col}" DisplayName="{col}" Description={quoteattr(description[:255])}'
            )
            xml = f"<Field {attrs}{' Indexed=' + quoteattr('TRUE') if indexed else ''} />"
            lines.append(f"Add-PnPFieldFromXml -List $list -FieldXml {_ps(xml)}")
        lines += ["", "# docreview.yaml, under ecm.options:", f"#   library: {library}", f"#   column_prefix: {p}"]
        return "\n".join(lines)

    # --- setup check (called by ``docreview check``) -----------------------------------------------
    def check(self, doc_types: list[str]) -> list[str]:
        """Problems with credentials, the site, the library, the folders and the columns."""
        try:
            self._auth()
        except Exception as exc:
            return [f"sharepoint: cannot get a token ({exc}); check tenant_id, client_id and client_secret"]
        try:
            columns = self.columns()
        except Exception as exc:
            return [f"sharepoint: cannot read the site or library ({exc}); was Sites.Selected granted on it?"]
        problems = []
        for path in dict.fromkeys((self.root_path, self.inbox_path)):
            try:
                url = f"/drives/{self.drive_id}/root" if path == "/" else f"/drives/{self.drive_id}/root:{quote(path)}"
                if "folder" not in self._get(url, params={"$select": "id,folder"}):
                    problems.append(f"sharepoint: {path!r} is not a folder")
            except Exception as exc:
                problems.append(f"sharepoint: cannot read folder {path!r} ({exc})")
        wanted = {
            self.col_classification: "text",
            self.col_doc_type: "text",
            self.col_review: "text",
            self.col_requested: "boolean",
            self.col_comments: "text",
        }
        for col, kind in wanted.items():
            if col not in columns:
                problems.append(f"sharepoint: the library has no column {col!r} ({kind})")
            elif columns[col] != kind:
                problems.append(f"sharepoint: column {col!r} should be {kind}, not {columns[col]}")
        if not any(c.startswith(f"{self.prefix}_") for c in columns):
            problems.append(f"sharepoint: no field columns ({self.prefix}_<field>) exist yet")
        return problems


def _ps(text: str) -> str:
    """A PowerShell single-quoted string."""
    return "'" + text.replace("'", "''") + "'"


def _same(found: Any, wanted: str) -> bool:
    if isinstance(found, (int, float)) and not isinstance(found, bool):
        try:
            return float(found) == float(wanted)
        except ValueError:
            return False
    return str(found if found is not None else "").strip().casefold() == wanted.casefold()
