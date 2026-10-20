"""A folder on disk as a tiny content system — for trying the engine, tests and demos.

Every document ``x.pdf`` gets a sidecar ``x.pdf.docreview.json`` holding its metadata
namespaces and comments. The document file itself is never written.

A document is offered by ``poll`` until a pass over its current content completes (the sidecar's
``processed_version`` equals the content hash), so edited files are picked up again. To request a
re-review, set ``"review": {"requested": true}`` in its sidecar (or run ``docreview review <path>``).
Options: ``root`` (the library folder) and ``watch`` (a sub-folder to poll; default: all of ``root``).
Hidden files and folders are ignored.
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from docreview.ecm.base import NS_CLASSIFICATION, NS_FIELDS, NS_REVIEW
from docreview.models import DocumentContent, DocumentRef, Event

log = logging.getLogger("docreview")
SIDECAR = ".docreview.json"
_TYPES = {".md": "text/plain", ".txt": "text/plain", ".csv": "text/plain", ".pdf": "application/pdf"}


def _norm(value: Any) -> str:
    return " ".join(str(value).split()).casefold()


class LocalFolderAdapter:
    name = "local"

    def __init__(self, root: str = "library", watch: str | None = None, _base_dir: str = ".") -> None:
        base = Path(_base_dir)
        self.root = (base / root).resolve()
        self.watch = (self.root / watch).resolve() if watch else self.root
        if not self.root.is_dir():
            raise FileNotFoundError(f"local adapter root {self.root} does not exist")

    @classmethod
    def render_schema(cls, schema, options, match_fields) -> str:  # noqa: ANN001 - see docreview.schema
        """Nothing to create: every namespace is a key of the sidecar JSON file."""
        from docreview.ecm._render import header, key_lines

        lines = [header("local folder", "Nothing to create: results go in <document>.docreview.json sidecars.")]
        for ns in schema:
            label = f'"{ns.name}"' + (f" (when doc_type is {ns.doc_type!r})" if ns.doc_type else "")
            lines.append(f"# {label}: {ns.description}")
            lines += key_lines(ns)
        lines.append('# "comments": a list of {"at": ..., "text": ...} entries.')
        return "\n".join(lines)

    # --- helpers --------------------------------------------------------------------------------
    def _path(self, document_id: str) -> Path:
        p = (self.root / document_id).resolve()
        if self.root not in p.parents or not p.is_file():
            raise FileNotFoundError(f"no document {document_id!r} under {self.root}")
        return p

    def _documents(self, under: Path) -> Iterable[Path]:
        for p in sorted(under.rglob("*")):
            rel = p.relative_to(self.root).parts
            if p.is_file() and SIDECAR not in p.name and not any(s.startswith(".") for s in rel):
                yield p

    def _sidecar(self, p: Path) -> dict[str, Any]:
        s = p.with_name(p.name + SIDECAR)
        return json.loads(s.read_text(encoding="utf-8")) if s.exists() else {}

    def _save_sidecar(self, p: Path, data: dict[str, Any]) -> None:
        s = p.with_name(p.name + SIDECAR)
        tmp = s.with_name(s.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True, default=str), encoding="utf-8")
        tmp.replace(s)

    def _ref(self, p: Path) -> DocumentRef:
        return DocumentRef(
            id=p.relative_to(self.root).as_posix(),
            name=p.name,
            version=hashlib.sha1(p.read_bytes()).hexdigest()[:12],
            modified_at=datetime.fromtimestamp(p.stat().st_mtime, UTC),
            parent_id=p.parent.relative_to(self.root).as_posix() or ".",
            location=p.parent.relative_to(self.root).as_posix() or ".",
        )

    # --- port -----------------------------------------------------------------------------------
    def poll(self, cursor: str | None) -> tuple[list[Event], str | None]:
        events = []
        for p in self._documents(self.watch):
            try:
                meta = self._sidecar(p)
            except (OSError, ValueError) as exc:
                log.warning("skipping %s: unreadable sidecar (%s)", p, exc)
                continue
            ref = self._ref(p)
            if (meta.get(NS_REVIEW) or {}).get("requested"):
                events.append(Event("review_requested", ref))
            elif (meta.get(NS_CLASSIFICATION) or {}).get("processed_version") != ref.version:
                events.append(Event("new_document", ref))  # new, changed, or not yet processed successfully
        return events, None  # stateless: the sidecars are the state

    def get(self, document_id: str) -> DocumentRef:
        return self._ref(self._path(document_id))

    def download(self, ref: DocumentRef) -> DocumentContent:
        p = self._path(ref.id)
        data = p.read_bytes()
        if ref.version and hashlib.sha1(data).hexdigest()[:12] != ref.version:
            raise RuntimeError(f"{ref.id} changed since it was listed (version {ref.version})")
        media = _TYPES.get(p.suffix.lower()) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        return DocumentContent(ref=self._ref(p), data=data, media_type=media)

    def read_metadata(self, ref: DocumentRef, namespace: str) -> dict[str, Any]:
        return dict(self._sidecar(self._path(ref.id)).get(namespace) or {})

    def find(
        self, doc_type: str, criteria: Mapping[str, str], *, scope: str | None = None, limit: int = 20
    ) -> list[DocumentRef]:
        under = (self.root / scope).resolve() if scope else self.root
        wanted = {k: _norm(v) for k, v in criteria.items()}
        hits = []
        for p in self._documents(under):
            meta = self._sidecar(p)
            if (meta.get(NS_CLASSIFICATION) or {}).get("doc_type") != doc_type:
                continue
            fields = meta.get(NS_FIELDS) or {}
            if all(k in fields and fields[k] is not None and _norm(fields[k]) == v for k, v in wanted.items()):
                hits.append(self._ref(p))
        hits.sort(key=lambda r: r.modified_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return hits[:limit]

    def write_metadata(self, ref: DocumentRef, namespace: str, values: Mapping[str, Any]) -> None:
        p = self._path(ref.id)
        meta = self._sidecar(p)
        meta[namespace] = {k: v for k, v in values.items() if v is not None}  # None = remove the key
        self._save_sidecar(p, meta)

    def post_comment(self, ref: DocumentRef, text: str) -> None:
        p = self._path(ref.id)
        meta = self._sidecar(p)
        meta.setdefault("comments", []).append({"at": datetime.now(UTC).isoformat(), "text": text})
        self._save_sidecar(p, meta)

    def list_comments(self, ref: DocumentRef) -> Iterable[str]:
        return [c["text"] for c in self._sidecar(self._path(ref.id)).get("comments", [])]
