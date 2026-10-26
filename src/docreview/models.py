"""Value objects shared by the pipeline and every ECM adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


@dataclass(frozen=True)
class DocumentRef:
    """A pointer to one document in the content system.

    ``id`` is whatever the adapter needs to find the document again (a Box file id, a path, a
    SharePoint drive-item id). ``version`` identifies the exact content that was read, when the
    content system has versions; findings are tied to it.
    """

    id: str
    name: str
    version: str | None = None
    modified_at: datetime | None = None
    parent_id: str | None = None  # the containing folder, in the adapter's own id format (used as a search scope)
    location: str | None = None  # human-readable folder path, for logs


@dataclass(frozen=True)
class DocumentContent:
    """The bytes of one document version plus its media type."""

    ref: DocumentRef
    data: bytes
    media_type: str  # e.g. "application/pdf", "text/plain", "image/png"


@dataclass(frozen=True)
class Event:
    """Something happened to a document that the pipeline may need to act on."""

    kind: str  # "new_document" | "review_requested"
    ref: DocumentRef


class Severity(StrEnum):
    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    INFO = "info"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK = {Severity.BLOCKER: 3, Severity.MAJOR: 2, Severity.MINOR: 1, Severity.INFO: 0}


class Outcome(StrEnum):
    """Advisory outcome of one review. A person always makes the actual decision."""

    NO_FINDINGS = "no_findings"
    MINOR_FINDINGS = "minor_findings"
    MAJOR_FINDINGS = "major_findings"
    BLOCKING_FINDINGS = "blocking_findings"
    INSUFFICIENT_DATA = "insufficient_data"


@dataclass(frozen=True)
class Finding:
    title: str
    severity: Severity
    body: str
    page: int | None = None
    reference: str | None = None  # name of the reference document this finding compares against


@dataclass
class ResolvedReference:
    name: str  # the rule's name, e.g. "Purchase order"
    required: bool
    ref: DocumentRef | None
    note: str = ""  # why it was picked, or why nothing was found

    @property
    def found(self) -> bool:
        return self.ref is not None


@dataclass
class ReviewResult:
    document: DocumentRef
    doc_type: str
    findings: list[Finding]
    references: list[ResolvedReference]
    outcome: Outcome
    summary: str = ""
    notes: list[str] = field(default_factory=list)  # deterministic notes the engine adds (caps, gaps)
    run_id: str = ""


@dataclass
class ProcessResult:
    """What one pass of the pipeline did to one document."""

    document: DocumentRef
    doc_type: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    review: ReviewResult | None = None
    skipped_reason: str | None = None
    # Reviewed documents that lacked a reference this document now provides; they should be re-reviewed.
    waiting: list[DocumentRef] = field(default_factory=list)
