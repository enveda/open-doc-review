"""Example adapter, not tested against a live system: M-Files over the M-Files Web Service (MFWS REST).

Written from the MFWS REST reference and tested against a fake transport only. See docs/MFILES.md,
including Known limits, before pointing it at a vault.

The engine's metadata is stored as properties of the document object. A property change in
M-Files is a check-out, a property update and a check-in, which makes a new object version; the
file is never changed. The adapter has no delete, destroy, undo-check-out, file-upload or
file-replace call.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any

from docreview.ecm.base import NS_CLASSIFICATION, NS_FIELDS, NS_REVIEW
from docreview.models import DocumentContent, DocumentRef, Event

log = logging.getLogger(__name__)

PROP_CLASS = 100  # built-in "Class"
PROP_COMMENT = 33  # built-in "Comment": each value set becomes a version comment
TEXT, INTEGER, FLOAT, DATE, BOOLEAN, MULTILINE = 1, 2, 3, 5, 8, 13  # MFDataType values used here
_CHECKED_OUT_TO_ME, _CHECKED_IN = 2, 0  # MFCheckOutStatus
_VERIFY_CAP = 50  # search hits re-read to confirm a match


def _httpx():
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise ImportError("the M-Files adapter needs httpx: pip install 'open-doc-review[mfiles]'") from exc
    return httpx


def _json(text: Any) -> dict[str, Any]:
    try:
        value = json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _fold(value: Any) -> str:
    return str(value if value is not None else "").strip().casefold()


class MFilesAdapter:
    """Document objects of the configured classes in one M-Files vault.

    ``properties`` maps the engine's metadata to property definition ids::

        {"classification": 1101, "review": 1102, "doc_type": 1103, "requested": 1104,
         "fields": {"po_number": 1110, "invoice_date": 1111}}
    """

    name = "mfiles"

    def __init__(
        self,
        *,
        server_url: str,
        vault_guid: str,
        classes: list[int],
        properties: Mapping[str, Any],
        username: str | None = None,
        password: str | None = None,
        object_type: int = 0,
        search_limit: int = 500,
        transport: Any = None,
        _base_dir: Any = None,
    ) -> None:
        if not server_url.startswith("https://"):
            raise ValueError("mfiles: server_url must be https://<server>/REST")
        self.username = username or os.environ.get("MFILES_USERNAME")
        self.password = password or os.environ.get("MFILES_PASSWORD")
        if not (self.username and self.password):
            raise ValueError("mfiles: set username and password (or MFILES_USERNAME / MFILES_PASSWORD)")
        if not classes:
            raise ValueError("mfiles: classes (the document classes in scope) is required")
        try:
            self.classes = [int(c) for c in classes]
            self.props = {k: int(properties[k]) for k in ("classification", "review", "doc_type", "requested")}
            self.field_props = {str(k): int(v) for k, v in (properties.get("fields") or {}).items()}
            self.object_type = int(object_type)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"mfiles: classes, object_type and every property id must be integers ({exc})") from None
        self.base = server_url.rstrip("/")
        self.vault_guid = vault_guid
        self.search_limit = search_limit
        self._http = _httpx().Client(transport=transport, timeout=60)
        self._token: str | None = None
        self._types: dict[int, int] = {}  # property id -> MFDataType
        self._logged_checked_out: set[tuple[str, int]] = set()  # (object id, object version) already logged

    # --- HTTP -----------------------------------------------------------------------------------
    def _login(self) -> str:
        r = self._http.post(
            f"{self.base}/server/authenticationtokens",
            json={"Username": self.username, "Password": self.password, "VaultGuid": self.vault_guid},
        )
        r.raise_for_status()
        self._token = r.json()["Value"]
        return self._token

    def _call(self, method: str, path: str, *, raw: bool = False, **kw: Any):
        for attempt in (0, 1):
            headers = {"X-Authentication": self._token or self._login()}
            r = self._http.request(method, self.base + path, headers=headers, **kw)
            if r.status_code == 401 and attempt == 0:  # token expired: sign in again once
                self._token = None
                continue
            r.raise_for_status()
            return r.content if raw else r.json()
        raise AssertionError("unreachable")  # pragma: no cover

    def _obj(self, object_id: str | int) -> str:
        return f"/objects/{self.object_type}/{int(object_id)}"

    # --- refs and scope -----------------------------------------------------------------------------
    @staticmethod
    def _file(ov: Mapping[str, Any]) -> dict[str, Any]:
        files = ov.get("Files") or []
        return files[0] if files else {}

    def _ref(self, ov: Mapping[str, Any]) -> DocumentRef:
        f = self._file(ov)
        name = f"{f.get('Name') or ov.get('Title') or ''}{'.' + f['Extension'] if f.get('Extension') else ''}"
        modified = ov.get("LastModifiedUtc")
        return DocumentRef(
            id=str(ov["ObjVer"]["ID"]),
            name=name,
            version=f"{f['ID']}.{f['Version']}" if f else None,  # the FILE's version: property edits don't change it
            modified_at=datetime.fromisoformat(modified.replace("Z", "+00:00")) if modified else None,
        )

    def _latest(self, object_id: str) -> dict[str, Any]:
        if not str(object_id).isdigit():
            raise LookupError(f"mfiles: {object_id!r} is not an object id")
        try:
            ov = self._call("GET", f"{self._obj(object_id)}/latest")
        except _httpx().HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise LookupError(f"mfiles: no object {object_id}") from None
            raise
        if ov.get("Class") not in self.classes or not self._file(ov):
            raise LookupError(f"mfiles: object {object_id} is not a document of the configured classes")
        return ov

    # --- properties -----------------------------------------------------------------------------
    def _type(self, prop: int) -> int:
        if prop not in self._types:
            self._types[prop] = int(self._call("GET", f"/structure/properties/{prop}")["DataType"])
        return self._types[prop]

    def _props(self, object_id: str, version: int | str = "latest") -> dict[int, Any]:
        out = {}
        for pv in self._call("GET", f"{self._obj(object_id)}/{version}/properties"):
            tv = pv.get("TypedValue") or {}
            out[int(pv["PropertyDef"])] = tv.get("Value") if tv.get("HasValue", True) else None
        return out

    def _namespace(self, props: Mapping[int, Any], namespace: str) -> dict[str, Any]:
        if namespace == NS_CLASSIFICATION:
            out = _json(props.get(self.props["classification"]))
            if props.get(self.props["doc_type"]):
                out["doc_type"] = props[self.props["doc_type"]]  # the property people edit is the source of truth
            return out
        if namespace == NS_REVIEW:
            values, requested = _json(props.get(self.props["review"])), props.get(self.props["requested"]) is True
            return {**values, "requested": requested} if values or requested else {}
        if namespace == NS_FIELDS:
            out = {}
            for key, prop in self.field_props.items():
                value = props.get(prop)
                if value is not None:
                    out[key] = value[:10] if self._type(prop) == DATE and isinstance(value, str) else value
            return out
        raise ValueError(f"mfiles: unknown namespace {namespace!r}")

    def _value(self, prop: int, value: Any) -> dict[str, Any]:
        """One MFWS PropertyValue; ``None`` empties the property."""
        data_type = self._type(prop)
        if isinstance(value, (dict, list)):
            value = json.dumps(value, sort_keys=True)
        typed = {"DataType": data_type, "HasValue": value is not None, "Value": value}
        return {"PropertyDef": prop, "TypedValue": typed}

    def _set(self, object_id: str, changes: Callable[[dict[int, Any]], Mapping[int, Any]]) -> None:
        """Check out, read the properties, set ``changes(properties)``, check in. Reading after the
        check-out means nobody else can change them between the read and the write. The file is never
        touched."""
        obj = self._obj(object_id)
        ov = self._call("PUT", f"{obj}/latest/checkedout", json={"Value": _CHECKED_OUT_TO_ME})
        version = int(ov["ObjVer"]["Version"])
        try:
            body = [self._value(p, v) for p, v in changes(self._props(object_id, version)).items()]
            ov = self._call("POST", f"{obj}/{version}/properties", json=body)
            version = int(ov["ObjVer"]["Version"])
        finally:  # check in even when the update fails, so a person is never left locked out
            self._call("PUT", f"{obj}/{version}/checkedout", json={"Value": _CHECKED_IN})

    # --- events ---------------------------------------------------------------------------------
    def _search(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        query = {"o": self.object_type, "limit": self.search_limit, **params}
        body = self._call("GET", "/objects", params=query)
        if body.get("MoreResults"):  # MFWS search has no paging: refuse to drop documents silently
            raise RuntimeError(
                f"mfiles: a search returned more than search_limit={self.search_limit} objects; "
                "raise search_limit or split the scope into more classes"
            )
        return list(body.get("Items") or [])

    def poll(self, cursor: str | None) -> tuple[list[Event], str | None]:
        events = []
        for cls in self.classes:
            for ov in self._search({f"p{PROP_CLASS}": cls}):
                if not self._file(ov):
                    continue
                if ov.get("ObjectCheckedOut") or ov.get("CheckedOutTo"):  # a write would fail; skip it this cycle
                    key = (str(ov["ObjVer"]["ID"]), int(ov["ObjVer"].get("Version") or 0))
                    if key not in self._logged_checked_out:
                        self._logged_checked_out.add(key)
                        log.info("mfiles: skipping object %s while it is checked out", key[0])
                    continue
                ref, props = self._ref(ov), self._props(str(ov["ObjVer"]["ID"]))
                if props.get(self.props["requested"]) is True:
                    events.append(Event("review_requested", ref))
                elif _json(props.get(self.props["classification"])).get("processed_version") != ref.version:
                    events.append(Event("new_document", ref))  # new file version, or not yet processed
        return events, None  # stateless: the properties are the state

    # --- reads ----------------------------------------------------------------------------------
    def get(self, document_id: str) -> DocumentRef:
        return self._ref(self._latest(document_id))

    def download(self, ref: DocumentRef) -> DocumentContent:
        ov = self._latest(ref.id)
        current, f = self._ref(ov), self._file(ov)
        if ref.version and current.version != ref.version:
            raise RuntimeError(f"mfiles: {ref.name} changed since it was read (file version {ref.version})")
        data = self._call("GET", f"{self._obj(ref.id)}/latest/files/{int(f['ID'])}/content", raw=True)
        return DocumentContent(
            ref=current, data=data, media_type=mimetypes.guess_type(current.name)[0] or "application/pdf"
        )

    def read_metadata(self, ref: DocumentRef, namespace: str) -> dict[str, Any]:
        self._latest(ref.id)  # scope check
        return self._namespace(self._props(ref.id), namespace)

    def find(
        self, doc_type: str, criteria: Mapping[str, str], *, scope: str | None = None, limit: int = 20
    ) -> list[DocumentRef]:
        # M-Files has no folders, so refs carry no parent_id and ``scope`` (same_folder rules) has nothing to narrow
        missing = [k for k in criteria if k not in self.field_props]
        if missing:
            raise ValueError(f"mfiles: add {missing} to properties.fields to use them in a reference rule")
        params = {f"p{self.props['doc_type']}": doc_type.strip()}  # httpx encodes every value: nothing is spliced
        params.update({f"p{self.field_props[k]}": str(v).strip() for k, v in criteria.items()})
        hits = []
        for ov in self._search(params)[:_VERIFY_CAP]:
            if ov.get("Class") not in self.classes or not self._file(ov):
                continue
            props = self._props(str(ov["ObjVer"]["ID"]))
            same = _fold(props.get(self.props["doc_type"])) == _fold(doc_type)
            if same and all(_fold(props.get(self.field_props[k])) == _fold(v) for k, v in criteria.items()):
                hits.append(self._ref(ov))
        hits.sort(key=lambda r: str(r.modified_at or ""), reverse=True)
        return hits[:limit]

    # --- writes (properties only, never the file) ------------------------------------------------
    def write_metadata(self, ref: DocumentRef, namespace: str, values: Mapping[str, Any]) -> None:
        self._latest(ref.id)  # scope check
        if namespace == NS_FIELDS:
            unknown = [k for k in values if k not in self.field_props]
            if unknown:
                raise ValueError(f"mfiles: properties.fields has no property for {unknown}")
        elif namespace not in (NS_CLASSIFICATION, NS_REVIEW):
            raise ValueError(f"mfiles: unknown namespace {namespace!r}")
        self._set(ref.id, lambda props: self._changes(props, namespace, values))

    def _changes(self, props: Mapping[int, Any], namespace: str, values: Mapping[str, Any]) -> dict[int, Any]:
        if namespace == NS_FIELDS:
            return {self.field_props[k]: v for k, v in values.items()}  # None empties the property
        changes: dict[int, Any] = {}
        merged = self._namespace(props, namespace)  # a write merges into what is there
        merged.update(values)
        if namespace == NS_REVIEW:
            merged.pop("requested", None)  # a review result answers the request
            changes[self.props["requested"]] = values.get("requested") is True
        merged = {k: v for k, v in merged.items() if v is not None}  # None removes a key
        changes[self.props[namespace]] = json.dumps(merged, sort_keys=True, default=str)
        if namespace == NS_CLASSIFICATION:
            changes[self.props["doc_type"]] = str(merged.get("doc_type") or "").strip() or None
        return changes

    def post_comment(self, ref: DocumentRef, text: str) -> None:
        self._latest(ref.id)
        self._set(ref.id, lambda props: {PROP_COMMENT: text})

    def list_comments(self, ref: DocumentRef) -> Iterable[str]:
        self._latest(ref.id)
        texts = []
        for c in self._call("GET", f"{self._obj(ref.id)}/latest/comments"):
            tv = (c.get("VersionComment") or {}).get("TypedValue") or {}
            if tv.get("Value"):
                texts.append(str(tv["Value"]))
        return texts

    # --- setup check (called by ``docreview check``) -----------------------------------------------
    def check(self, doc_types: list[str]) -> list[str]:
        try:
            self._login()
        except Exception as exc:
            return [f"mfiles: cannot sign in ({exc}); check server_url, vault_guid, username and password"]
        problems = []
        for cls in self.classes:
            try:
                self._call("GET", f"/structure/classes/{cls}")
            except Exception as exc:
                problems.append(f"mfiles: cannot read class {cls} ({exc})")
        wanted = {"classification": MULTILINE, "review": MULTILINE, "doc_type": TEXT, "requested": BOOLEAN}
        checks = [(k, self.props[k], t) for k, t in wanted.items()]
        checks += [(f"fields.{k}", p, None) for k, p in self.field_props.items()]
        for label, prop, data_type in checks:
            try:
                found = self._type(prop)
            except Exception as exc:
                problems.append(f"mfiles: cannot read property {prop} ({label}) ({exc})")
                continue
            if data_type is not None and found != data_type:
                problems.append(f"mfiles: property {prop} ({label}) has data type {found}, expected {data_type}")
        return problems
