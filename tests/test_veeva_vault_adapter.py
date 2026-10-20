"""VeevaVaultAdapter against a fake Vault REST API (httpx.MockTransport, no network)."""

from __future__ import annotations

import inspect
import json
import re
from urllib.parse import parse_qs

import pytest

httpx = pytest.importorskip("httpx")

from docreview.ecm import ECMAdapter  # noqa: E402
from docreview.ecm.base import NS_CLASSIFICATION, NS_FIELDS, NS_REVIEW  # noqa: E402
from docreview.ecm.veeva_vault import RECORD_FIELDS, VeevaVaultAdapter  # noqa: E402
from docreview.models import DocumentRef  # noqa: E402

URL = "https://example.veevavault.com"
OBJ = "docreview_result__c"
_LIT = r"'((?:[^'\\]|\\.)*)'"


def _unlit(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


def _doc(doc_id, minor=0, doc_type="Specification", name=None):
    return {
        "id": doc_id,
        "name__v": f"Doc {doc_id}",
        "filename__v": name or f"doc{doc_id}.pdf",
        "major_version_number__v": 1,
        "minor_version_number__v": minor,
        "version_modified_date__v": f"2026-0{1 + int(doc_id) % 8}-01T00:00:00Z",
        "type__v": doc_type,
    }


class FakeVault:
    """The handful of Vault endpoints the adapter uses, with just enough VQL; requests are recorded."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.docs = {1: _doc(1), 2: _doc(2, minor=1), 3: _doc(3, doc_type="SOP")}
        self.records: list[dict] = []  # oldest first
        self.sessions = 0
        self.expire_next = False
        self.fields = [*RECORD_FIELDS, "po_number__c"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v24.1")
        if path == "/auth":
            form = parse_qs(request.content.decode())
            if form.get("password") != ["pw"]:
                return httpx.Response(
                    200, json={"responseStatus": "FAILURE", "errors": [{"type": "USERNAME_OR_PASSWORD_INCORRECT"}]}
                )
            self.sessions += 1
            return httpx.Response(200, json={"responseStatus": "SUCCESS", "sessionId": f"S{self.sessions}"})
        if self.expire_next:
            self.expire_next = False
            return httpx.Response(200, json={"responseStatus": "FAILURE", "errors": [{"type": "INVALID_SESSION_ID"}]})
        assert request.headers["Authorization"].startswith("S")
        if path == "/query":
            return self._query(parse_qs(request.content.decode())["q"][0])
        if path == f"/vobjects/{OBJ}" and request.method in ("POST", "PUT"):
            for row in json.loads(request.content):
                if request.method == "POST":
                    self.records.append({"id": f"R{len(self.records) + 1}", **row})
                else:
                    next(r for r in self.records if r["id"] == row["id"]).update(row)
            return httpx.Response(200, json={"responseStatus": "SUCCESS", "data": [{"responseStatus": "SUCCESS"}]})
        if path == f"/metadata/vobjects/{OBJ}":
            return httpx.Response(
                200, json={"responseStatus": "SUCCESS", "object": {"fields": [{"name": f} for f in self.fields]}}
            )
        m = re.fullmatch(r"/objects/documents/(\d+)/versions/(\d+)/(\d+)/file", path)
        if m:
            return httpx.Response(200, content=b"%PDF", headers={"content-type": "application/octet-stream"})
        return httpx.Response(404, json={"responseStatus": "FAILURE", "errors": [{"type": "INVALID_DATA"}]})

    def _query(self, q: str) -> httpx.Response:
        if " FROM documents " in q:
            rows = [d for d in self.docs.values() if d["type__v"] in re.findall(_LIT, q.split("type__v", 1)[1])]
            m = re.search(r"WHERE id CONTAINS \(([\d, ]+)\)", q)
            if m:
                rows = [d for d in rows if str(d["id"]) in m.group(1).replace(" ", "").split(",")]
            return httpx.Response(200, json={"responseStatus": "SUCCESS", "data": rows})
        rows = list(reversed(self.records))  # ORDER BY created_date__v DESC
        m = re.search(r"document_id__c CONTAINS \(([^)]*)\)", q)
        if m:
            ids = [_unlit(v) for v in re.findall(_LIT, m.group(1))]
            rows = [r for r in rows if r["document_id__c"] in ids]
        for col, value in re.findall(r"(\w+__c) = " + _LIT, q):
            rows = [r for r in rows if r.get(col) == _unlit(value)]
        return httpx.Response(200, json={"responseStatus": "SUCCESS", "data": rows})


def _adapter(vault, **opts):
    opts = {"vault_url": URL, "username": "u", "password": "pw", "scope": {"type__v": ["Specification"]}, **opts}
    return VeevaVaultAdapter(transport=httpx.MockTransport(vault), _base_dir=".", **opts)


def _ref(doc_id="1", version="1_1_0"):
    return DocumentRef(id=doc_id, name="doc.pdf", version=version)


def test_implements_the_port_and_validates_settings():
    assert isinstance(_adapter(FakeVault()), ECMAdapter)
    for bad in ({"scope": {}}, {"scope": {"type__v; DELETE": ["x"]}}, {"match_fields": {"po": "po'__c"}}):
        with pytest.raises(ValueError):
            _adapter(FakeVault(), **bad)


def test_get_enforces_scope_and_builds_the_version():
    adapter = _adapter(FakeVault())
    ref = adapter.get("2")
    assert (ref.version, ref.name) == ("2_1_1", "doc2.pdf")
    for outside in ("3", "99", "1 OR 1=1"):
        with pytest.raises(LookupError):
            adapter.get(outside)


def test_poll_offers_unprocessed_versions_and_review_requests():
    vault = FakeVault()
    adapter = _adapter(vault)
    assert [(e.kind, e.ref.id) for e in adapter.poll(None)[0]] == [("new_document", "1"), ("new_document", "2")]
    adapter.write_metadata(_ref(), NS_CLASSIFICATION, {"doc_type": "specification", "processed_version": "1_1_0"})
    adapter.write_metadata(
        _ref("2", "2_1_1"), NS_CLASSIFICATION, {"doc_type": "specification", "processed_version": "2_1_1"}
    )
    assert adapter.poll(None)[0] == []
    vault.records[0]["review_requested__c"] = True
    vault.docs[2]["minor_version_number__v"] = 2  # a new version of document 2
    events, cursor = adapter.poll(None)
    assert [(e.kind, e.ref.version) for e in events] == [("review_requested", "1_1_0"), ("new_document", "2_1_2")]
    assert cursor is None


def test_writes_go_to_one_record_per_version_and_carry_forward():
    vault = FakeVault()
    adapter = _adapter(vault, match_fields={"po_number": "po_number__c"})
    adapter.write_metadata(_ref(), NS_CLASSIFICATION, {"doc_type": "Specification", "run_id": "r1"})
    adapter.write_metadata(_ref(), NS_FIELDS, {"po_number": " PO-1 ", "lot": "L1"})
    adapter.write_metadata(_ref(), NS_CLASSIFICATION, {"run_id": None, "processed_version": "1_1_0"})
    assert len(vault.records) == 1
    record = vault.records[0]
    assert (record["doc_type__c"], record["po_number__c"], record["processed_version__c"]) == (
        "Specification",
        "po-1",
        "1_1_0",
    )
    assert adapter.read_metadata(_ref(), NS_CLASSIFICATION) == {
        "doc_type": "Specification",
        "processed_version": "1_1_0",
    }
    newer = _ref(version="1_1_1")  # a new version reads the old values until it has its own record
    assert adapter.read_metadata(newer, NS_FIELDS) == {"po_number": " PO-1 ", "lot": "L1"}
    adapter.write_metadata(newer, NS_FIELDS, {"lot": None})
    assert len(vault.records) == 2 and vault.records[1]["document_version__c"] == "1_1_1"
    assert adapter.read_metadata(newer, NS_FIELDS) == {"po_number": " PO-1 "}
    assert adapter.read_metadata(_ref(), NS_FIELDS)["lot"] == "L1"  # the earlier version's record is untouched


def test_review_write_clears_the_request():
    vault = FakeVault()
    adapter = _adapter(vault)
    assert adapter.read_metadata(_ref(), NS_REVIEW) == {}
    adapter.write_metadata(_ref(), NS_REVIEW, {"outcome": "x", "requested": True})
    assert adapter.read_metadata(_ref(), NS_REVIEW) == {"outcome": "x", "requested": True}
    adapter.write_metadata(_ref(), NS_REVIEW, {"outcome": "no_findings", "finding_counts": {"major": 0}})
    assert adapter.read_metadata(_ref(), NS_REVIEW) == {
        "outcome": "no_findings",
        "finding_counts": {"major": 0},
        "requested": False,
    }


def test_find_escapes_values_and_keeps_current_in_scope_documents():
    vault = FakeVault()
    adapter = _adapter(vault, match_fields={"po_number": "po_number__c"})
    for doc_id in ("1", "2", "3"):
        version = adapter.get(doc_id).version if doc_id != "3" else "3_1_0"
        adapter.write_metadata(_ref(doc_id, version), NS_CLASSIFICATION, {"doc_type": "invoice"})
        adapter.write_metadata(_ref(doc_id, version), NS_FIELDS, {"po_number": "O'Brien-1"})
    refs = adapter.find(" invoice", {"po_number": " o'brien-1"})
    assert [r.id for r in refs] == ["2", "1"]  # document 3 is outside the scope; newest first
    q = parse_qs(vault.requests[-3].content.decode())["q"][0]
    assert "doc_type__c = 'invoice' AND po_number__c = 'o\\'brien-1'" in q
    adapter.write_metadata(_ref("1"), NS_FIELDS, {"po_number": "PO-9"})
    assert [r.id for r in adapter.find("invoice", {"po_number": "o'brien-1"})] == ["2"]  # 1 no longer matches
    with pytest.raises(ValueError, match="match_fields"):
        adapter.find("invoice", {"lot": "x"})


def test_find_matches_when_several_versions_carry_the_value():
    vault = FakeVault()
    adapter = _adapter(vault, match_fields={"po_number": "po_number__c"})
    for minor in (0, 1, 2):  # one record per version of document 2, all with the same po_number
        vault.docs[2]["minor_version_number__v"] = minor
        ref = _ref("2", f"2_1_{minor}")
        adapter.write_metadata(ref, NS_CLASSIFICATION, {"doc_type": "invoice"})
        adapter.write_metadata(ref, NS_FIELDS, {"po_number": "X"})
    vault.records.reverse()  # the record query returns rows in no particular order
    assert [r.version for r in adapter.find("invoice", {"po_number": "x"})] == ["2_1_2"]


def test_a_person_edits_doc_type_in_its_own_field():
    vault = FakeVault()
    adapter = _adapter(vault)
    adapter.write_metadata(_ref(), NS_CLASSIFICATION, {"doc_type": "invoice", "run_id": "r1"})
    vault.records[0]["doc_type__c"] = "purchase_order"  # corrected in the Vault UI
    assert adapter.read_metadata(_ref(), NS_CLASSIFICATION)["doc_type"] == "purchase_order"
    adapter.write_metadata(_ref(), NS_CLASSIFICATION, {"processed_version": "1_1_0"})
    adapter.write_metadata(_ref(), NS_REVIEW, {"outcome": "no_findings"})
    assert vault.records[0]["doc_type__c"] == "purchase_order"
    assert json.loads(vault.records[0]["classification__c"])["doc_type"] == "purchase_order"
    assert [r.id for r in adapter.find("purchase_order", {})] == ["1"]


def test_download_is_pinned_and_scoped():
    vault = FakeVault()
    adapter = _adapter(vault)
    content = adapter.download(_ref(version="1_1_0"))
    assert content.data == b"%PDF" and vault.requests[-1].url.path.endswith("/objects/documents/1/versions/1/0/file")
    with pytest.raises(ValueError):
        adapter.download(_ref(version="2_1_0"))  # a version of another document
    with pytest.raises(LookupError):
        adapter.download(_ref("3", "3_1_0"))


def test_comments_live_on_the_record_newest_first():
    vault = FakeVault()
    adapter = _adapter(vault, comments_max_chars=200)
    adapter.post_comment(_ref(), "First.")
    adapter.write_metadata(_ref(), NS_CLASSIFICATION, {"doc_type": "x"})
    adapter.post_comment(_ref(), "Second.\nTwo lines.")
    assert list(adapter.list_comments(_ref())) == ["Second.\nTwo lines.", "First."]
    assert len(vault.records) == 1 and adapter.read_metadata(_ref(), NS_CLASSIFICATION) == {"doc_type": "x"}
    vault.records[0]["review_requested__c"] = True
    adapter.post_comment(_ref(version="1_1_1"), "On the next version.")  # creates that version's record
    assert vault.records[1]["review_requested__c"] is False  # the request was for 1_1_0
    adapter.post_comment(_ref(), "y" * 170)
    assert list(adapter.list_comments(_ref())) == ["y" * 170]


def test_expired_session_is_renewed_once():
    vault = FakeVault()
    adapter = _adapter(vault)
    adapter.get("1")
    vault.expire_next = True
    assert adapter.get("1").id == "1" and vault.sessions == 2


def test_check_reports_sign_in_fields_and_scope():
    assert _adapter(FakeVault()).check(["invoice"]) == []
    assert "sign-in failed" in _adapter(FakeVault(), password="bad").check([])[0]
    vault = FakeVault()
    vault.fields.remove("review__c")
    problems = _adapter(vault, match_fields={"lot": "lot_number__c"}).check([])
    assert problems == [
        f"veeva_vault: object {OBJ} has no field review__c",
        f"veeva_vault: object {OBJ} has no field lot_number__c",
    ]


def test_no_destructive_calls_exist():
    """The adapter sends GET, POST to /auth, /query and the result object, and PUT to the result object.
    It never deletes, and never writes a document or its fields."""
    import docreview.ecm.veeva_vault as vv

    source = inspect.getsource(vv)
    assert set(re.findall(r'_call\(\s*"(\w+)"', source)) == {"GET", "POST"}
    assert re.findall(r'_call\(\s*"POST",\s*"([^"]+)"', source) == ["/query"]
    assert re.findall(r'_save\(\s*"(\w+)"', source) == ["PUT", "POST", "PUT", "POST"]
    save = source.split("def _save", 1)[1].split("def ", 1)[0]
    assert 'f"/vobjects/{self.object}"' in save
    for word in ("DELETE", ".delete(", "/actions", "lifecycle_actions", '/objects/documents/{doc_id}"', "PATCH"):
        assert word not in source
