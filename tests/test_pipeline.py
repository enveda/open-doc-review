from __future__ import annotations

import json
from dataclasses import replace

from docreview.cli import run_once
from docreview.ecm.base import NS_CLASSIFICATION, NS_FIELDS, NS_REVIEW
from docreview.models import Finding, Outcome, ResolvedReference, Severity
from docreview.pipeline import derive_outcome, extract_schema, fill_criteria


def _sidecar(workspace, rel):
    return json.loads((workspace / "library" / f"{rel}.docreview.json").read_text())


def test_batch_reviews_invoice_against_po_in_one_poll(workspace, pipeline, fake_model):
    # The invoice sorts before the PO, so this only works because extraction runs before review.
    assert run_once(pipeline) == 0
    inv = _sidecar(workspace, "inbox/INV-2041.txt")
    assert inv[NS_CLASSIFICATION]["doc_type"] == "invoice"
    assert inv[NS_FIELDS]["po_number"] == "PO-7730"
    review = inv[NS_REVIEW]
    assert review["outcome"] == Outcome.BLOCKING_FINDINGS.value
    assert review["references"] == {"Purchase order": "purchase_orders/PO-7730.txt"}
    assert review["finding_counts"] == {"blocker": 1, "major": 2, "minor": 0, "info": 1}
    texts = [c["text"] for c in inv["comments"]]
    assert texts[0].startswith("[BLOCKER] Total mis-added")  # most severe first
    assert len(texts) == 4  # 3 findings at/above "minor" + the summary; the info one is folded in
    assert "1 lower-severity finding" in texts[-1]
    # The reference document was attached to the review call.
    review_req = [r for r, _, _ in fake_model.requests if r.task == "review"][0]
    assert [a.label for a in review_req.attachments] == ["Document under review", "Purchase order"]
    # PO has no review configured: classified and extracted only.
    po = _sidecar(workspace, "purchase_orders/PO-7730.txt")
    assert NS_REVIEW not in po and "comments" not in po


def test_every_model_call_is_audited(workspace, pipeline, fake_model):
    run_once(pipeline)
    rows = [json.loads(line) for line in (workspace / "logs" / "ai_calls.jsonl").read_text().splitlines()]
    assert len(rows) == len(fake_model.requests) == 5  # 2 classify, 2 extract, 1 review
    review = [r for r in rows if r["task"] == "review"][0]
    assert review["status"] == "ok"
    assert set(review["prompts"]) == {"system", "review/invoice"}
    assert all("sha256" in a and "data" not in a for a in review["attachments"])
    assert review["effort"] == "high"


def test_second_poll_does_nothing(workspace, pipeline, fake_model):
    run_once(pipeline)
    n = len(fake_model.requests)
    run_once(pipeline)
    assert len(fake_model.requests) == n


def test_review_request_reruns_without_duplicate_comments(workspace, pipeline, fake_model):
    run_once(pipeline)
    path = workspace / "library" / "inbox" / "INV-2041.txt.docreview.json"
    data = json.loads(path.read_text())
    data[NS_REVIEW]["requested"] = True
    path.write_text(json.dumps(data))
    run_once(pipeline)
    inv = json.loads(path.read_text())
    finding_comments = [c for c in inv["comments"] if c["text"].startswith("[")]
    assert len(finding_comments) == 3  # same findings, not posted twice
    assert inv[NS_REVIEW]["requested"] is False
    assert sum(1 for r, _, _ in fake_model.requests if r.task == "extract") == 2  # unchanged version: no re-extract


def test_human_classification_is_respected(workspace, pipeline, fake_model):
    path = workspace / "library" / "inbox" / "INV-2041.txt.docreview.json"
    path.write_text(json.dumps({NS_CLASSIFICATION: {"doc_type": "purchase_order"}}))
    ref = pipeline.ecm.get("inbox/INV-2041.txt")
    result = pipeline.process(ref)
    assert result.doc_type == "purchase_order"
    assert not any(r.task == "classify" for r, _, _ in fake_model.requests)


def test_missing_required_reference_gives_insufficient_data(workspace, pipeline):
    (workspace / "library" / "purchase_orders" / "PO-7730.txt").unlink()
    run_once(pipeline)
    inv = _sidecar(workspace, "inbox/INV-2041.txt")
    assert inv[NS_REVIEW]["outcome"] == Outcome.INSUFFICIENT_DATA.value
    assert "Required reference not found: Purchase order" in inv["comments"][-1]["text"]


def test_dry_run_writes_nothing(workspace, pipeline):
    pipeline.cfg = replace(pipeline.cfg, review=replace(pipeline.cfg.review, dry_run=True))
    run_once(pipeline)
    assert not list((workspace / "library").rglob("*.docreview.json"))


def test_outcome_rules():
    req_missing = [ResolvedReference("Spec", True, None)]
    assert derive_outcome([], req_missing)[0] is Outcome.INSUFFICIENT_DATA
    assert derive_outcome([], [])[0] is Outcome.NO_FINDINGS
    minor = Finding("t", Severity.MINOR, "b")
    assert derive_outcome([minor], [])[0] is Outcome.MINOR_FINDINGS
    assert derive_outcome([minor, replace(minor, severity=Severity.MAJOR)], [])[0] is Outcome.MAJOR_FINDINGS


def test_fill_criteria_needs_every_value(pipeline):
    rule = pipeline.cfg.doc_type("invoice").references[0]
    assert fill_criteria(rule, {"po_number": " PO-1 "}) == {"po_number": "PO-1"}
    assert fill_criteria(rule, {"po_number": ""}) is None


def test_extract_schema_is_strict_and_nullable(pipeline):
    schema = extract_schema(pipeline.cfg.doc_type("invoice").fields)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["total"]["type"] == ["number", "null"]
    assert "YYYY-MM-DD" in schema["properties"]["invoice_date"]["description"]


class _Boom(RuntimeError):
    pass


def test_failed_review_is_retried_then_given_up(workspace, pipeline, fake_model):
    real = fake_model.complete

    def failing(request, **kw):
        if request.task == "review":
            raise _Boom("overloaded")
        return real(request, **kw)

    fake_model.complete = failing
    for attempt in range(1, 4):
        assert run_once(pipeline) == 1
        cls = _sidecar(workspace, "inbox/INV-2041.txt")[NS_CLASSIFICATION]
        assert cls["failure_count"] == attempt and "overloaded" in cls["last_error"]
    reviews = sum(1 for r, _, _ in fake_model.requests if r.task == "review")
    assert run_once(pipeline) == 0  # max_attempts reached: skipped, no model call
    assert sum(1 for r, _, _ in fake_model.requests if r.task == "review") == reviews
    # A manual run still goes through, and success clears the failure record.
    fake_model.complete = real
    result = pipeline.process(pipeline.ecm.get("inbox/INV-2041.txt"), manual=True)
    assert result.review is not None
    cls = _sidecar(workspace, "inbox/INV-2041.txt")[NS_CLASSIFICATION]
    assert "failure_count" not in cls and cls["processed_version"]


def test_new_version_is_reextracted_and_rereviewed(workspace, pipeline, fake_model):
    run_once(pipeline)
    doc = workspace / "library" / "inbox" / "INV-2041.txt"
    doc.write_text(doc.read_text().replace("1,460.00", "1,450.00"))
    run_once(pipeline)
    tasks = [r.task for r, _, _ in fake_model.requests]
    assert tasks.count("classify") == 2  # the type is kept across versions
    assert tasks.count("extract") == 3 and tasks.count("review") == 2
    inv = _sidecar(workspace, "inbox/INV-2041.txt")
    assert inv[NS_REVIEW]["reviewed_version"] == inv[NS_CLASSIFICATION]["processed_version"]


def test_poll_failure_does_not_raise(pipeline, monkeypatch):
    def broken(cursor):
        raise ConnectionError("ECM down")

    monkeypatch.setattr(pipeline.ecm, "poll", broken)
    assert run_once(pipeline) == 1


def test_corrupt_sidecar_is_skipped(workspace, pipeline):
    (workspace / "library" / "purchase_orders" / "PO-7730.txt.docreview.json").write_text("{not json")
    events, _ = pipeline.ecm.poll(None)
    assert [e.ref.id for e in events] == ["inbox/INV-2041.txt"]


def test_late_reference_triggers_rereview(workspace, pipeline, fake_model):
    po = workspace / "library" / "purchase_orders" / "PO-7730.txt"
    held = po.read_text()
    po.unlink()
    run_once(pipeline)
    assert _sidecar(workspace, "inbox/INV-2041.txt")[NS_REVIEW]["outcome"] == Outcome.INSUFFICIENT_DATA.value
    po.write_text(held)  # the purchase order arrives later
    run_once(pipeline)
    inv = _sidecar(workspace, "inbox/INV-2041.txt")
    assert inv[NS_REVIEW]["outcome"] == Outcome.BLOCKING_FINDINGS.value
    assert inv[NS_REVIEW]["references"] == {"Purchase order": "purchase_orders/PO-7730.txt"}
    run_once(pipeline)  # and nothing more happens after that
    assert sum(1 for r, _, _ in fake_model.requests if r.task == "review") == 2


def test_inverse_needs_bare_placeholders(pipeline):
    rule = pipeline.cfg.doc_type("invoice").references[0]
    assert rule.inverse() == {"po_number": "po_number"}
    assert replace(rule, match={"po_number": "PO-{po_number}"}).inverse() is None


def test_dry_run_polls_bill_once(workspace, pipeline, fake_model):
    pipeline.cfg = replace(pipeline.cfg, review=replace(pipeline.cfg.review, dry_run=True))
    run_once(pipeline)
    n = len(fake_model.requests)
    assert n == 5  # classify x2, extract x2, review x1
    run_once(pipeline)
    run_once(pipeline)
    assert len(fake_model.requests) == n
    assert not list((workspace / "library").rglob("*.docreview.json"))


def test_review_request_is_not_retried_forever(workspace, pipeline, fake_model):
    run_once(pipeline)
    path = workspace / "library" / "inbox" / "INV-2041.txt.docreview.json"
    data = json.loads(path.read_text())
    data[NS_REVIEW]["requested"] = True
    path.write_text(json.dumps(data))

    def failing(request, **kw):
        raise _Boom("forbidden")

    fake_model.complete = failing
    for _ in range(5):
        run_once(pipeline)
    cls = json.loads(path.read_text())[NS_CLASSIFICATION]
    assert cls["failure_count"] == 3  # stopped at max_attempts despite the open request


def test_unknown_is_reclassified_on_a_new_version(workspace, pipeline, fake_model):
    real = fake_model.complete

    def unknown_first(request, **kw):
        resp = real(request, **kw)
        if request.task == "classify" and request.attachments[0].content.ref.name.startswith("INV"):
            resp.data["doc_type"] = "unknown"
        return resp

    fake_model.complete = unknown_first
    run_once(pipeline)
    assert _sidecar(workspace, "inbox/INV-2041.txt")[NS_CLASSIFICATION]["doc_type"] == "unknown"
    fake_model.complete = real
    run_once(pipeline)  # same version: left alone
    assert _sidecar(workspace, "inbox/INV-2041.txt")[NS_CLASSIFICATION]["doc_type"] == "unknown"
    doc = workspace / "library" / "inbox" / "INV-2041.txt"
    doc.write_text(doc.read_text() + "\n")
    run_once(pipeline)
    assert _sidecar(workspace, "inbox/INV-2041.txt")[NS_CLASSIFICATION]["doc_type"] == "invoice"


def test_success_clears_failure_record_with_none_on_merging_adapters(workspace, pipeline, monkeypatch):
    writes = []
    real = pipeline.ecm.write_metadata
    monkeypatch.setattr(pipeline.ecm, "write_metadata", lambda r, ns, v: (writes.append((ns, dict(v))), real(r, ns, v)))
    run_once(pipeline)
    final = [v for ns, v in writes if ns == NS_CLASSIFICATION and "processed_version" in v][-1]
    assert final["failure_count"] is None and final["last_error"] is None


def test_retry_limit_holds_when_failure_cannot_be_recorded(workspace, pipeline, fake_model, monkeypatch):
    def locked(ref, namespace, values):
        raise PermissionError("document is checked out")

    monkeypatch.setattr(pipeline.ecm, "write_metadata", locked)
    for _ in range(6):
        run_once(pipeline)
    classify_calls = sum(1 for r, _, _ in fake_model.requests if r.task == "classify")
    assert classify_calls == 2 * 3  # two documents, stopped at max_attempts each, in memory


def test_batch_record_example_reviews_against_its_master_record(full_workspace, fake_model):
    from docreview.cli import build
    from docreview.config import load_config

    pipeline = build(load_config(full_workspace / "docreview.yaml"), model_client=fake_model)
    assert run_once(pipeline) == 0
    ebr = _sidecar(full_workspace, "inbox/EBR-B24-0117.txt")
    assert ebr[NS_CLASSIFICATION]["doc_type"] == "executed_batch_record"
    assert ebr[NS_REVIEW]["references"] == {"Master batch record": "master_batch_records/MBR-0042.txt"}
    review = [r for r, _, _ in fake_model.requests if r.task == "review" and "EBR" in r.attachments[0].content.ref.name]
    assert [a.label for a in review[0].attachments] == ["Document under review", "Master batch record"]
    assert "executed batch record" in review[0].instruction
