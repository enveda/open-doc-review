"""Every metadata key the engine writes, in one place.

Adapters, ``docreview check`` and ``docreview templates`` all read this, so the template or column
set an ECM needs is generated from the config instead of written by hand. Adapters render it in
their own terms through an optional ``render_schema`` classmethod (see docs/METADATA.md).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from docreview.config import Config
from docreview.ecm.base import NS_CLASSIFICATION, NS_FIELDS, NS_REVIEW

# Value types: the config's field types plus "json" (an object, stored as text by most ECMs).
TYPES = ("string", "number", "integer", "boolean", "date", "json")


@dataclass(frozen=True)
class MetaKey:
    name: str
    type: str
    description: str
    person_editable: bool = False  # a person may set it in the ECM, and the engine respects it


@dataclass(frozen=True)
class Namespace:
    name: str  # NS_CLASSIFICATION, NS_FIELDS or NS_REVIEW
    keys: tuple[MetaKey, ...]
    doc_type: str | None = None  # set for the fields namespace: one per document type
    description: str = ""


CLASSIFICATION = Namespace(
    NS_CLASSIFICATION,
    (
        MetaKey("doc_type", "string", "Document type id from the config. Set it by hand to override the model.", True),
        MetaKey("classified_at", "string", "When the document was classified (ISO 8601)."),
        MetaKey("run_id", "string", "Id of the run that classified it; matches the audit log."),
        MetaKey("fields_version", "string", "Document version the fields were extracted from."),
        MetaKey("processed_version", "string", "Last document version processed to the end."),
        MetaKey("failed_version", "string", "Document version the last failure happened on."),
        MetaKey("failure_count", "number", "Failures on failed_version; polling stops at max_attempts."),
        MetaKey("last_error", "string", "The last error, up to 500 characters."),
    ),
    description="What the document is, and where processing stands.",
)

REVIEW = Namespace(
    NS_REVIEW,
    (
        MetaKey(
            "outcome",
            "string",
            "no_findings | minor_findings | major_findings | blocking_findings | insufficient_data",
        ),
        MetaKey(
            "finding_counts", "json", 'Findings per severity, e.g. {"blocker": 1, "major": 2, "minor": 0, "info": 0}.'
        ),
        MetaKey("reviewed_version", "string", "Document version the review covers."),
        MetaKey("reviewed_at", "string", "When the review ran (ISO 8601)."),
        MetaKey("run_id", "string", "Id of the review run; matches the audit log."),
        MetaKey("references", "json", 'Reference documents used, by rule name, e.g. {"Purchase order": "<id>"}.'),
        MetaKey("requested", "boolean", "Set to true by a person to ask for a re-review; the engine resets it.", True),
    ),
    description="The latest review's result.",
)

CLASSIFICATION_KEYS = tuple(k.name for k in CLASSIFICATION.keys)
REVIEW_KEYS = tuple(k.name for k in REVIEW.keys)


def fields_namespaces(cfg: Config) -> tuple[Namespace, ...]:
    return tuple(
        Namespace(
            NS_FIELDS,
            tuple(MetaKey(f.name, f.type, f.description, True) for f in dt.fields),
            doc_type=dt.name,
            description=f"Fields extracted from documents of type {dt.name!r}. A person may correct them; "
            "corrections are kept until a new version arrives.",
        )
        for dt in cfg.doc_types.values()
        if dt.fields
    )


def build_schema(cfg: Config) -> tuple[Namespace, ...]:
    return (CLASSIFICATION, REVIEW, *fields_namespaces(cfg))


def match_fields(cfg: Config) -> dict[str, set[str]]:
    """Per document type, the fields that reference rules search on (worth indexing in the ECM)."""
    out: dict[str, set[str]] = {}
    for dt in cfg.doc_types.values():
        for rule in dt.references:
            out.setdefault(rule.doc_type, set()).update(rule.match)
            if inverse := rule.inverse():  # a late reference searches the reviewed type the other way
                out.setdefault(dt.name, set()).update(inverse)
    return out


def render_json(schema: tuple[Namespace, ...]) -> str:
    return json.dumps([asdict(ns) for ns in schema], indent=2)
