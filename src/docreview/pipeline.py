"""The pipeline: classify -> extract -> find references -> review -> write findings beside the document.

Everything content-system-specific is behind ``ECMAdapter``; everything model-specific behind
``AIService``; all wording lives in your prompt files. This module holds the rules that should
not depend on any of those: response schemas, how references are chosen, and how an outcome
is derived from findings.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from docreview.config import Config, DocTypeConfig, FieldSpec, ReferenceRule
from docreview.ecm.base import NS_CLASSIFICATION, NS_FIELDS, NS_REVIEW, ECMAdapter
from docreview.llm import AIService, Attachment, ModelRequest
from docreview.models import (
    DocumentContent,
    DocumentRef,
    Event,
    Finding,
    Outcome,
    ProcessResult,
    ResolvedReference,
    ReviewResult,
    Severity,
)
from docreview.prompts import Prompt, PromptLibrary
from docreview.windows import page_windows

log = logging.getLogger("docreview")

UNKNOWN = "unknown"
_FAILURE_KEYS = ("failed_version", "failure_count", "last_error")
_JSON_TYPES = {"string": "string", "number": "number", "integer": "integer", "boolean": "boolean", "date": "string"}


# --- response schemas (the engine owns the format; prompts own the judgement) ---------------------
def classify_schema(doc_types: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "doc_type": {"type": "string", "enum": [*doc_types, UNKNOWN]},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "reason": {"type": "string"},
        },
        "required": ["doc_type", "confidence", "reason"],
        "additionalProperties": False,
    }


def extract_schema(fields: tuple[FieldSpec, ...]) -> dict[str, Any]:
    props = {}
    for f in fields:
        desc = f.description + (" Format: YYYY-MM-DD." if f.type == "date" else "")
        props[f.name] = {"type": [_JSON_TYPES[f.type], "null"], "description": desc.strip()}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def review_schema() -> dict[str, Any]:
    finding = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "severity": {"type": "string", "enum": [s.value for s in Severity]},
            "body": {"type": "string"},
            "page": {"type": ["integer", "null"]},
            "reference": {"type": ["string", "null"]},
        },
        "required": ["title", "severity", "body", "page", "reference"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"summary": {"type": "string"}, "findings": {"type": "array", "items": finding}},
        "required": ["summary", "findings"],
        "additionalProperties": False,
    }


# --- deterministic rules ----------------------------------------------------------------------
def derive_outcome(findings: list[Finding], references: list[ResolvedReference]) -> tuple[Outcome, list[str]]:
    """The outcome follows from the findings and the evidence, never from the model's say-so."""
    notes = []
    missing = [r.name for r in references if r.required and not r.found]
    if missing:
        notes.append(f"Required reference not found: {', '.join(missing)}. The review could not be completed.")
        return Outcome.INSUFFICIENT_DATA, notes
    top = max((f.severity.rank for f in findings), default=-1)
    outcome = {3: Outcome.BLOCKING_FINDINGS, 2: Outcome.MAJOR_FINDINGS}.get(
        top, Outcome.MINOR_FINDINGS if findings else Outcome.NO_FINDINGS
    )
    return outcome, notes


def fill_criteria(rule: ReferenceRule, fields: Mapping[str, Any]) -> dict[str, str] | None:
    """Criteria for ``find``; ``None`` when a value the rule needs was not extracted."""
    out = {}
    for target, template in rule.match.items():
        value = template
        for ph in rule.placeholders():
            v = fields.get(ph)
            if v is None or str(v).strip() == "":
                return None
            value = value.replace("{" + ph + "}", str(v).strip())
        out[target] = value
    return out


def format_finding(f: Finding) -> str:
    where = f" (page {f.page})" if f.page else ""
    against = f" · compared with {f.reference}" if f.reference else ""
    return f"[{f.severity.value.upper()}] {f.title}\n{f.body}{where}{against}"


# --- the pipeline -----------------------------------------------------------------------------
class Pipeline:
    def __init__(self, cfg: Config, adapter: ECMAdapter, ai: AIService, prompts: PromptLibrary) -> None:
        self.cfg = cfg
        self.ecm = adapter
        self.ai = ai
        self.prompts = prompts
        # Dry run: what would have been written, kept in memory so that repeated polls in one process
        # neither re-bill model calls nor re-post anything.
        self._dry: dict[tuple[str, str], dict[str, Any]] = {}

    # entry points
    def handle(self, event: Event) -> ProcessResult:
        return self.process(event.ref, review_requested=event.kind == "review_requested")

    def process(
        self, ref: DocumentRef, *, review_requested: bool = False, review: bool = True, manual: bool = False
    ) -> ProcessResult:
        """One pass over one document.

        ``review=False`` stops after classification and extraction, which lets a batch make every
        document findable before any of them is reviewed. A pass that completes records
        ``processed_version``; adapters offer a document again only when its version differs (or
        a review is requested). A pass that fails records the failure; after ``max_attempts``
        failures on one version, polling skips it until a new version, a review request, or a
        ``manual`` run (the ``process`` / ``review`` commands).
        """
        try:
            return self._process(ref, review_requested=review_requested, review=review, manual=manual)
        except Exception as exc:
            self._record_failure(ref, exc)
            raise

    def _process(self, ref: DocumentRef, *, review_requested: bool, review: bool, manual: bool) -> ProcessResult:
        run_id = uuid.uuid4().hex[:12]
        dry_review = self._dry.get((ref.id, NS_REVIEW), {})
        if review_requested and dry_review.get("reviewed_version") == ref.version and not manual:
            review_requested = False  # dry run: the request was already answered in memory
        cls = self._read(ref, NS_CLASSIFICATION)
        failures = int(cls.get("failure_count") or 0) if cls.get("failed_version") == ref.version else 0
        if failures >= self.cfg.max_attempts and not manual:
            return ProcessResult(
                document=ref,
                doc_type=cls.get("doc_type"),
                skipped_reason=f"failed {failures} times on this version (last error: {cls.get('last_error')}); "
                "upload a new version, or run `docreview process` / `docreview review`",
            )
        content = self.ecm.download(ref)  # one download per pass; every step of the pass uses these bytes
        ref = content.ref
        result = ProcessResult(document=ref)

        doc_type = cls.get("doc_type")
        if doc_type == UNKNOWN and cls.get("processed_version") != ref.version:
            doc_type = None  # "unknown" is the model's answer for one version: try again on a new one
        if not doc_type:  # a value already there (set by a person, or an earlier pass) is respected
            doc_type = self.classify(content, run_id)
            cls = {**cls, "doc_type": doc_type, "classified_at": _now(), "run_id": run_id}
            self._write(ref, NS_CLASSIFICATION, cls)
        result.doc_type = doc_type
        dt = self.cfg.doc_types.get(doc_type)

        if dt is not None:
            # Fields are extracted once per version; values a person corrected since are kept.
            fields = self._read(ref, NS_FIELDS)
            if dt.fields and (not fields or cls.get("fields_version") != ref.version):
                fields = self.extract(content, dt, run_id)
                self._write(ref, NS_FIELDS, fields)
                cls = {**cls, "fields_version": ref.version}
                self._write(ref, NS_CLASSIFICATION, cls)
                result.waiting = self.find_waiting(ref, dt.name, fields)
            result.fields = fields

        if not review:
            return result
        if dt is None:
            result.skipped_reason = f"document type {doc_type!r} is not configured"
        elif not dt.review_enabled:
            result.skipped_reason = "review not enabled for this document type"
        elif self._read(ref, NS_REVIEW).get("reviewed_version") == ref.version and not review_requested:
            result.skipped_reason = "this version was already reviewed"
        else:
            result.review = self.review(content, dt, result.fields, run_id)
        # None removes a key: adapters that merge (rather than replace) must still drop the failure record.
        self._write(ref, NS_CLASSIFICATION, {**cls, **dict.fromkeys(_FAILURE_KEYS), "processed_version": ref.version})
        return result

    def _record_failure(self, ref: DocumentRef, exc: Exception) -> None:
        try:
            cls = self._read(ref, NS_CLASSIFICATION)
            same = cls.get("failed_version") == ref.version
            count = (int(cls.get("failure_count") or 0) if same else 0) + 1
            error = f"{type(exc).__name__}: {exc}"[:500]
            self._write(
                ref,
                NS_CLASSIFICATION,
                {**cls, "failed_version": ref.version, "failure_count": count, "last_error": error},
            )
        except Exception:  # recording the failure must not hide the original error
            log.exception("could not record the failure on %s", ref.id)

    def find_waiting(self, ref: DocumentRef, doc_type: str, fields: Mapping[str, Any]) -> list[DocumentRef]:
        """Reviewed documents whose review could not find a reference that ``ref`` now satisfies.

        Only rules whose ``match`` values are bare ``{field}`` placeholders can be inverted.
        """
        waiting: dict[str, DocumentRef] = {}
        for dt in self.cfg.doc_types.values():
            if not dt.review_enabled:
                continue
            for rule in dt.references:
                inverse = rule.inverse() if rule.doc_type == doc_type else None
                if not inverse:
                    continue
                values = {mine: fields.get(theirs) for mine, theirs in inverse.items()}
                if any(v is None or str(v).strip() == "" for v in values.values()):
                    continue
                criteria = {k: str(v).strip() for k, v in values.items()}
                scope = ref.parent_id if rule.same_folder else None
                for hit in self.ecm.find(dt.name, criteria, scope=scope):
                    review = self._read(hit, NS_REVIEW) if hit.id != ref.id else {}
                    if review.get("reviewed_version") and not (review.get("references") or {}).get(rule.name):
                        waiting[hit.id] = hit
        return list(waiting.values())

    # steps
    def classify(self, content: DocumentContent, run_id: str) -> str:
        types = self.cfg.doc_types
        listing = "\n".join(f"- {n}: {d.description}" for n, d in types.items())
        prompt = self.prompts.get("classify")
        instruction = prompt.render(doc_types=listing, document_name=content.ref.name)
        data = self._call(
            "classify",
            [prompt],
            instruction,
            classify_schema(list(types)),
            [Attachment("Document", content)],
            run_id,
            content.ref,
        )
        log.info("classified %s as %s (%s)", content.ref.name, data["doc_type"], data["confidence"])
        return data["doc_type"]

    def extract(self, content: DocumentContent, dt: DocTypeConfig, run_id: str) -> dict[str, Any]:
        prompt = (
            self.prompts.get(f"extract/{dt.name}")
            if self.prompts.has(f"extract/{dt.name}")
            else self.prompts.get("extract")
        )
        listing = "\n".join(f"- {f.name} ({f.type}): {f.description}" for f in dt.fields)
        instruction = prompt.render(
            doc_type=dt.name, doc_type_description=dt.description, fields=listing, document_name=content.ref.name
        )
        data = self._call(
            "extract",
            [prompt],
            instruction,
            extract_schema(dt.fields),
            [Attachment("Document", content)],
            run_id,
            content.ref,
        )
        return {f.name: data.get(f.name) for f in dt.fields}

    def resolve_references(
        self, ref: DocumentRef, dt: DocTypeConfig, fields: Mapping[str, Any]
    ) -> list[ResolvedReference]:
        out = []
        for rule in dt.references:
            criteria = fill_criteria(rule, fields)
            if criteria is None:
                out.append(
                    ResolvedReference(
                        rule.name, rule.required, None, f"no value extracted for {sorted(rule.placeholders())}"
                    )
                )
                continue
            scope = ref.parent_id if rule.same_folder else None
            hits = [h for h in self.ecm.find(rule.doc_type, criteria, scope=scope) if h.id != ref.id]
            if not hits:
                out.append(ResolvedReference(rule.name, rule.required, None, f"nothing matched {criteria}"))
            elif len(hits) == 1:
                out.append(ResolvedReference(rule.name, rule.required, hits[0], f"matched {criteria}"))
            else:
                out.append(
                    ResolvedReference(
                        rule.name,
                        rule.required,
                        hits[0],
                        f"{len(hits)} documents matched {criteria}; used the newest, "
                        f"{hits[0].name}. Check that it is the right one.",
                    )
                )
        return out

    def review(
        self, content: DocumentContent, dt: DocTypeConfig, fields: Mapping[str, Any], run_id: str
    ) -> ReviewResult:
        ref = content.ref
        refs = self.resolve_references(ref, dt, fields)
        ref_attachments = [Attachment(r.name, self.ecm.download(r.ref)) for r in refs if r.ref is not None]
        ref_lines = (
            "\n".join(
                f"- {r.name}: {'attached as ' + repr(r.ref.name) if r.ref else 'NOT FOUND (' + r.note + ')'}"
                for r in refs
            )
            or "- none"
        )
        prompt = self.prompts.get(dt.review_prompt or f"review/{dt.name}")
        windows = page_windows(content, self.cfg.review.window_pages)
        findings: list[Finding] = []
        summaries: list[str] = []
        for w in windows:
            instruction = prompt.render(
                doc_type=dt.name,
                doc_type_description=dt.description,
                document_name=ref.name,
                fields=json.dumps(dict(fields), indent=2, default=str),
                references=ref_lines,
                severities=", ".join(s.value for s in Severity),
                window=w.describe(),
            )
            if not w.is_whole:  # mechanical note, so page numbers can be mapped back to the whole document
                instruction += (
                    f"\n\nThe document under review is attached as an excerpt: {w.describe()}. Review only this "
                    "excerpt. Give page numbers counted from the first page of the excerpt."
                )
            attachments = [Attachment("Document under review", w.content), *ref_attachments]
            data = self._call("review", [prompt], instruction, review_schema(), attachments, run_id, ref)
            offset = w.first_page - 1 if not w.is_whole else 0
            span = w.last_page - w.first_page + 1 if w.total_pages else None
            for f in data["findings"]:
                if f.get("page") is not None and (f["page"] < 1 or (span and f["page"] > span)):
                    f["page"] = None  # a page outside what was shown is a guess: drop it, keep the finding
                findings.append(
                    Finding(
                        title=f["title"].strip(),
                        severity=Severity(f["severity"]),
                        body=f["body"].strip(),
                        page=f["page"] + offset if f.get("page") else None,
                        reference=f.get("reference"),
                    )
                )
            summary = data["summary"].strip()
            summaries.append(summary if len(windows) == 1 else f"{w.describe().capitalize()}: {summary}")
        findings.sort(key=lambda f: (-f.severity.rank, f.page or 0))
        outcome, notes = derive_outcome(findings, refs)
        notes += [r.note for r in refs if r.found and "documents matched" in r.note]
        result = ReviewResult(
            document=ref,
            doc_type=dt.name,
            findings=findings,
            references=refs,
            outcome=outcome,
            summary="\n".join(summaries),
            notes=notes,
            run_id=run_id,
        )
        self._publish(result)
        return result

    # writing back
    def _publish(self, result: ReviewResult) -> None:
        ref = result.document
        settings = self.cfg.review
        shown = [f for f in result.findings if f.severity.rank >= settings.comment_min_severity.rank]
        if settings.post_comments:
            existing = set(self.ecm.list_comments(ref)) if not settings.dry_run else set()
            for f in shown:
                text = format_finding(f)
                if text not in existing:
                    self._comment(ref, text)
            self._comment(ref, self._summary_comment(result, len(result.findings) - len(shown)))
        counts = {s.value: sum(1 for f in result.findings if f.severity is s) for s in Severity}
        self._write(
            ref,
            NS_REVIEW,
            {
                "outcome": result.outcome.value,
                "finding_counts": counts,
                "reviewed_version": ref.version,
                "reviewed_at": _now(),
                "run_id": result.run_id,
                "references": {r.name: (r.ref.id if r.ref else None) for r in result.references},
                "requested": False,
            },
        )

    @staticmethod
    def _summary_comment(result: ReviewResult, folded: int) -> str:
        lines = [f"Review summary — advisory outcome: {result.outcome.value.replace('_', ' ')}", result.summary]
        for r in result.references:
            lines.append(f"• {r.name}: {r.ref.name if r.ref else 'not found'}")
        if folded:
            lines.append(f"{folded} lower-severity finding(s) not posted as separate comments.")
        lines += result.notes
        lines.append(f"Run {result.run_id}. A person makes every decision; these findings are advisory.")
        return "\n".join(line for line in lines if line)

    def _read(self, ref: DocumentRef, namespace: str) -> dict[str, Any]:
        if (ref.id, namespace) in self._dry:
            return dict(self._dry[(ref.id, namespace)])
        return self.ecm.read_metadata(ref, namespace)

    def _write(self, ref: DocumentRef, namespace: str, values: Mapping[str, Any]) -> None:
        if self.cfg.review.dry_run:
            log.info("[dry run] would write %s on %s: %s", namespace, ref.name, dict(values))
            self._dry[(ref.id, namespace)] = {k: v for k, v in values.items() if v is not None}
            return
        self.ecm.write_metadata(ref, namespace, values)

    def _comment(self, ref: DocumentRef, text: str) -> None:
        if self.cfg.review.dry_run:
            log.info("[dry run] would comment on %s:\n%s", ref.name, text)
            return
        self.ecm.post_comment(ref, text)

    def _call(
        self,
        task: str,
        prompts: list[Prompt],
        instruction: str,
        schema: dict[str, Any],
        attachments: list[Attachment],
        run_id: str,
        ref: DocumentRef,
    ) -> dict[str, Any]:
        system_prompt = self.prompts.get("system") if self.prompts.has("system") else None
        system = system_prompt.render() if system_prompt else ""
        used = ([system_prompt] if system_prompt else []) + prompts
        meta = {p.name: {"sha256": p.sha256, **p.meta} for p in used}
        req = ModelRequest(task=task, system=system, instruction=instruction, schema=schema, attachments=attachments)
        return self.ai.call(req, run_id=run_id, document_id=ref.id, prompt_meta=meta).data


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
