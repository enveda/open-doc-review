"""The port every content-management system plugs into.

An adapter is the ONLY code that talks to the ECM. The pipeline never sees an SDK object; it
sees ``DocumentRef``, ``DocumentContent`` and plain dicts.

Deliberately missing from this interface: delete, move, rename and overwrite-content. The
engine is advisory. It reads documents and writes findings *beside* them (metadata and
comments) and has no way to change or remove the document itself. Keep it that way in your
adapter: give its service account the narrowest permissions that allow the methods below.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol, runtime_checkable

from docreview.models import DocumentContent, DocumentRef, Event

# Metadata namespaces the pipeline writes. Adapters map each to their own storage (a Box
# metadata template, a SharePoint column group, a sidecar JSON key, ...).
NS_CLASSIFICATION = "classification"  # doc_type, classified_at, run_id, fields_version, processed_version,
#                                       and after a failed pass: failed_version, failure_count, last_error
NS_FIELDS = "fields"  # the extracted fields for the document's type
NS_REVIEW = "review"  # {"outcome": "...", "finding_counts": {...}, "reviewed_version": ..., ...}


@runtime_checkable
class ECMAdapter(Protocol):
    """Implement these methods to connect a content system. See docs/ADAPTERS.md."""

    name: str

    # --- events -------------------------------------------------------------------------------
    def poll(self, cursor: str | None) -> tuple[list[Event], str | None]:
        """Return events since ``cursor`` and the new cursor to persist.

        Offer a document as ``new_document`` while its classification ``processed_version`` differs
        from its current version (new, changed, or not yet processed successfully), and as
        ``review_requested`` while its review ``requested`` value is true.

        ``cursor=None`` means "first run": return what is currently waiting (or nothing, if your
        system only offers a live stream). The pipeline stores the returned cursor and passes
        it back next time; its format is yours. Returning the same event twice is allowed
        (processing is idempotent per document version); losing one is not.
        """
        ...

    # --- reads --------------------------------------------------------------------------------
    def get(self, document_id: str) -> DocumentRef:
        """Current name and version of one document."""
        ...

    def download(self, ref: DocumentRef) -> DocumentContent:
        """The content of ``ref.version`` (or the current version if ``ref.version`` is None)."""
        ...

    def read_metadata(self, ref: DocumentRef, namespace: str) -> dict[str, Any]:
        """Previously written values for ``namespace``; ``{}`` when none."""
        ...

    def find(
        self, doc_type: str, criteria: Mapping[str, str], *, scope: str | None = None, limit: int = 20
    ) -> list[DocumentRef]:
        """Documents of ``doc_type`` whose extracted fields equal every value in ``criteria``.

        Matching is exact after the adapter's own normalisation (case, surrounding whitespace).
        ``scope`` optionally narrows the search (a folder id/path); ``None`` means everywhere the
        service account can see. Return newest first when the system can sort; the pipeline
        treats more than one result as ambiguous either way.
        """
        ...

    # --- writes (beside the document, never to it) ----------------------------------------------
    def write_metadata(self, ref: DocumentRef, namespace: str, values: Mapping[str, Any]) -> None:
        """Create or replace ``namespace`` on the document. Idempotent."""
        ...

    def post_comment(self, ref: DocumentRef, text: str) -> None:
        """Attach one human-readable comment to the document."""
        ...

    def list_comments(self, ref: DocumentRef) -> Iterable[str]:
        """Comment texts on the document, used to avoid posting the same finding twice."""
        ...
