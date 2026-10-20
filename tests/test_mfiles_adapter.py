"""MFilesAdapter against a fake M-Files Web Service (httpx.MockTransport, no network)."""

from __future__ import annotations

import inspect
import json
import re

import pytest

httpx = pytest.importorskip("httpx")

from docreview.ecm import ECMAdapter  # noqa: E402
from docreview.ecm.base import NS_CLASSIFICATION, NS_FIELDS, NS_REVIEW  # noqa: E402
from docreview.ecm.mfiles import BOOLEAN, DATE, MULTILINE, TEXT, MFilesAdapter  # noqa: E402
from docreview.models import DocumentRef  # noqa: E402

BASE = "https://mf.example.com/REST"
CLASS = 5
PROPS = {"classification": 1101, "review": 1102, "doc_type": 1103, "requested": 1104,
         "fields": {"po_number": 1110, "invoice_date": 1111}}  # fmt: skip
TYPES = {1101: MULTILINE, 1102: MULTILINE, 1103: TEXT, 1104: BOOLEAN, 1110: TEXT, 1111: DATE, 33: MULTILINE}


class FakeMFiles:
    """The MFWS resources the adapter uses; check-out state and versions are tracked."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.objects = {1: self._new(1, CLASS), 2: self._new(2, CLASS), 3: self._new(3, 9)}
        self.props: dict[int, dict[int, object]] = {1: {}, 2: {}, 3: {}}
        self.comments: dict[int, list[str]] = {1: [], 2: [], 3: []}
        self.types = dict(TYPES)
        self.tokens = 0
        self.fail_next_properties = False
        self.on_checkout = None  # called at check-out: another writer's last change before we hold the lock
        self.more_results = False

    @staticmethod
    def _new(obj_id, cls):
        return {"ObjVer": {"ID": obj_id, "Type": 0, "Version": 1}, "Class": cls, "Title": f"Doc {obj_id}",
                "Files": [{"ID": 10 + obj_id, "Version": 1, "Name": f"doc{obj_id}", "Extension": "pdf"}],
                "LastModifiedUtc": f"2026-0{obj_id}-01T00:00:00Z", "CheckedOutTo": 0}  # fmt: skip

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path.removeprefix("/REST"), request.method
        if path == "/server/authenticationtokens":
            if json.loads(request.content)["Password"] != "pw":
                return httpx.Response(403, json={"Message": "Login failed"})
            self.tokens += 1
            return httpx.Response(200, json={"Value": f"T{self.tokens}"})
        if request.headers.get("X-Authentication") != f"T{self.tokens}":
            return httpx.Response(401, json={"Message": "expired"})
        if m := re.fullmatch(r"/structure/properties/(\d+)", path):
            prop = int(m.group(1))
            return (
                httpx.Response(200, json={"ID": prop, "DataType": self.types[prop]})
                if prop in self.types
                else httpx.Response(404)
            )
        if m := re.fullmatch(r"/structure/classes/(\d+)", path):
            return httpx.Response(200 if int(m.group(1)) in (CLASS, 9) else 404, json={"ID": int(m.group(1))})
        if path == "/objects":
            rows = list(self.objects.values())
            for key, value in request.url.params.items():
                if key == "p100":
                    rows = [o for o in rows if str(o["Class"]) == value]
                elif key.startswith("p"):  # M-Files text conditions are case-insensitive
                    prop = int(key[1:])
                    rows = [
                        o for o in rows if str(self.props[o["ObjVer"]["ID"]].get(prop) or "").lower() == value.lower()
                    ]
            return httpx.Response(200, json={"Items": rows, "MoreResults": self.more_results})
        m = re.fullmatch(r"/objects/0/(\d+)/(latest|\d+)(/.*)?", path)
        if not m or int(m.group(1)) not in self.objects:
            return httpx.Response(404, json={"Message": "not found"})
        obj_id, rest = int(m.group(1)), m.group(3) or ""
        ov = self.objects[obj_id]
        if rest == "" and method == "GET":
            return httpx.Response(200, json=ov)
        if rest == "/properties" and method == "GET":
            body = [
                {"PropertyDef": p, "TypedValue": {"Value": v, "HasValue": v is not None}}
                for p, v in self.props[obj_id].items()
            ]
            return httpx.Response(200, json=[{"PropertyDef": 100, "TypedValue": {"Value": ov["Class"]}}, *body])
        if rest == "/checkedout" and method == "PUT":
            status = json.loads(request.content)["Value"]
            ov["CheckedOutTo"] = 1 if status == 2 else 0
            ov["ObjVer"]["Version"] += 1 if status == 2 else 0
            if status == 2 and self.on_checkout:
                self.on_checkout(obj_id)
            return httpx.Response(200, json=ov)
        if rest == "/properties" and method == "POST":
            assert ov["CheckedOutTo"] == 1 and m.group(2) == str(ov["ObjVer"]["Version"])
            if self.fail_next_properties:
                self.fail_next_properties = False
                return httpx.Response(400, json={"Message": "invalid value"})
            for pv in json.loads(request.content):
                prop, tv = pv["PropertyDef"], pv["TypedValue"]
                assert tv["DataType"] == self.types[prop]
                if prop == 33:
                    self.comments[obj_id].insert(0, tv["Value"])
                else:
                    self.props[obj_id][prop] = tv["Value"] if tv["HasValue"] else None
            return httpx.Response(200, json=ov)
        if rest == "/comments" and method == "GET":
            return httpx.Response(
                200,
                json=[
                    {"VersionComment": {"PropertyDef": 33, "TypedValue": {"Value": t}}} for t in self.comments[obj_id]
                ],
            )
        if re.fullmatch(r"/files/\d+/content", rest) and method == "GET":
            return httpx.Response(200, content=b"%PDF")
        return httpx.Response(404, json={"Message": "no such resource"})


def _adapter(mf, **opts):
    opts = {"server_url": BASE, "vault_guid": "{G}", "username": "u", "password": "pw", "classes": [CLASS],
            "properties": PROPS, **opts}  # fmt: skip
    return MFilesAdapter(transport=httpx.MockTransport(mf), _base_dir=".", **opts)


def _ref(obj_id="1", version="11.1"):
    return DocumentRef(id=obj_id, name="doc1.pdf", version=version)


def test_implements_the_port_and_validates_settings():
    assert isinstance(_adapter(FakeMFiles()), ECMAdapter)
    for bad in ({"classes": []}, {"properties": {"classification": "x"}}, {"server_url": "http://x"}):
        with pytest.raises(ValueError):
            _adapter(FakeMFiles(), **bad)


def test_get_is_scoped_and_versioned_by_the_file():
    mf = FakeMFiles()
    adapter = _adapter(mf)
    ref = adapter.get("1")
    assert (ref.version, ref.name) == ("11.1", "doc1.pdf")
    for outside in ("3", "99", "1/../3"):
        with pytest.raises(LookupError):
            adapter.get(outside)
    adapter.write_metadata(ref, NS_CLASSIFICATION, {"doc_type": "invoice"})
    assert mf.objects[1]["ObjVer"]["Version"] == 2 and adapter.get("1").version == "11.1"  # property edit ≠ new file


def test_poll_offers_unprocessed_files_and_review_requests():
    mf = FakeMFiles()
    adapter = _adapter(mf)
    assert [(e.kind, e.ref.id) for e in adapter.poll(None)[0]] == [("new_document", "1"), ("new_document", "2")]
    adapter.write_metadata(_ref(), NS_CLASSIFICATION, {"processed_version": "11.1"})
    adapter.write_metadata(_ref("2", "12.1"), NS_CLASSIFICATION, {"processed_version": "12.1"})
    assert adapter.poll(None) == ([], None)
    mf.props[1][1104] = True
    mf.objects[2]["Files"][0]["Version"] = 2  # a new file version
    assert [(e.kind, e.ref.version) for e in adapter.poll(None)[0]] == [
        ("review_requested", "11.1"),
        ("new_document", "12.2"),
    ]


def test_writes_merge_remove_and_check_in():
    mf = FakeMFiles()
    adapter = _adapter(mf)
    adapter.write_metadata(_ref(), NS_CLASSIFICATION, {"doc_type": "Invoice", "run_id": "r1"})
    adapter.write_metadata(_ref(), NS_CLASSIFICATION, {"run_id": None, "failure_count": 1})
    assert adapter.read_metadata(_ref(), NS_CLASSIFICATION) == {"doc_type": "Invoice", "failure_count": 1}
    assert mf.props[1][1103] == "Invoice" and mf.objects[1]["CheckedOutTo"] == 0
    adapter.write_metadata(_ref(), NS_FIELDS, {"po_number": "PO-1", "invoice_date": "2026-03-04"})
    mf.props[1][1111] = "2026-03-04T00:00:00Z"
    assert adapter.read_metadata(_ref(), NS_FIELDS) == {"po_number": "PO-1", "invoice_date": "2026-03-04"}
    adapter.write_metadata(_ref(), NS_FIELDS, {"po_number": None})
    assert adapter.read_metadata(_ref(), NS_FIELDS) == {"invoice_date": "2026-03-04"}
    with pytest.raises(ValueError, match="no property"):
        adapter.write_metadata(_ref(), NS_FIELDS, {"vendor": "x"})


def test_the_merge_reads_after_the_check_out():
    mf = FakeMFiles()
    adapter = _adapter(mf)
    adapter.write_metadata(_ref(), NS_CLASSIFICATION, {"doc_type": "invoice"})
    other = json.dumps({"doc_type": "invoice", "run_id": "other"})
    mf.on_checkout = lambda obj_id: mf.props[obj_id].update({1101: other})  # written just before our check-out
    adapter.write_metadata(_ref(), NS_CLASSIFICATION, {"processed_version": "11.1"})
    assert adapter.read_metadata(_ref(), NS_CLASSIFICATION) == {
        "doc_type": "invoice",
        "run_id": "other",
        "processed_version": "11.1",
    }


def test_a_person_edits_doc_type_in_its_own_property():
    mf = FakeMFiles()
    adapter = _adapter(mf)
    adapter.write_metadata(_ref(), NS_CLASSIFICATION, {"doc_type": "invoice", "run_id": "r1"})
    mf.props[1][1103] = "purchase_order"  # corrected on the metadata card
    adapter.write_metadata(_ref(), NS_CLASSIFICATION, {"processed_version": "11.1"})
    assert adapter.read_metadata(_ref(), NS_CLASSIFICATION)["doc_type"] == "purchase_order"
    assert json.loads(mf.props[1][1101])["doc_type"] == "purchase_order" and mf.props[1][1103] == "purchase_order"


def test_poll_skips_checked_out_documents_and_logs_once(caplog):
    mf = FakeMFiles()
    mf.objects[1]["ObjectCheckedOut"], mf.objects[1]["CheckedOutTo"] = True, 7  # a person is editing it
    adapter = _adapter(mf)
    with caplog.at_level("INFO", logger="docreview.ecm.mfiles"):
        assert [e.ref.id for e in adapter.poll(None)[0]] == ["2"]
        assert [e.ref.id for e in adapter.poll(None)[0]] == ["2"]
    assert sum("checked out" in r.message for r in caplog.records) == 1


def test_poll_refuses_a_truncated_search():
    mf = FakeMFiles()
    mf.more_results = True
    with pytest.raises(RuntimeError, match="search_limit"):
        _adapter(mf, search_limit=2).poll(None)


def test_a_failed_update_still_checks_in():
    mf = FakeMFiles()
    mf.fail_next_properties = True
    with pytest.raises(httpx.HTTPStatusError):
        _adapter(mf).write_metadata(_ref(), NS_FIELDS, {"po_number": "x"})
    assert mf.objects[1]["CheckedOutTo"] == 0


def test_review_write_clears_the_request():
    mf = FakeMFiles()
    adapter = _adapter(mf)
    assert adapter.read_metadata(_ref(), NS_REVIEW) == {}
    mf.props[1][1104] = True
    assert adapter.read_metadata(_ref(), NS_REVIEW) == {"requested": True}
    adapter.write_metadata(_ref(), NS_REVIEW, {"outcome": "no_findings", "references": {"spec": "2"}})
    assert adapter.read_metadata(_ref(), NS_REVIEW) == {
        "outcome": "no_findings",
        "references": {"spec": "2"},
        "requested": False,
    }


def test_find_binds_values_and_rechecks():
    mf = FakeMFiles()
    adapter = _adapter(mf)
    for obj_id in ("1", "2", "3"):
        mf.props[int(obj_id)].update({1103: "invoice", 1110: "PO-1&p1103=x"})
    refs = adapter.find("Invoice", {"po_number": " po-1&p1103=x "})
    assert [r.id for r in refs] == ["2", "1"]  # 3 is another class; newest first
    params = mf.requests[-3].url.params
    assert (params["p1103"], params["p1110"], params["o"]) == ("Invoice", "po-1&p1103=x", "0")
    with pytest.raises(ValueError, match="properties.fields"):
        adapter.find("invoice", {"lot": "x"})


def test_download_checks_the_file_version():
    mf = FakeMFiles()
    adapter = _adapter(mf)
    assert adapter.download(_ref()).data == b"%PDF"
    assert mf.requests[-1].url.path == "/REST/objects/0/1/latest/files/11/content"
    with pytest.raises(RuntimeError, match="changed"):
        adapter.download(_ref(version="11.0"))


def test_comments_use_the_comment_property():
    mf = FakeMFiles()
    adapter = _adapter(mf)
    adapter.post_comment(_ref(), "First.")
    adapter.post_comment(_ref(), "Second.")
    assert list(adapter.list_comments(_ref())) == ["Second.", "First."]


def test_expired_token_is_renewed():
    mf = FakeMFiles()
    adapter = _adapter(mf)
    adapter.get("1")
    mf.tokens += 1  # the server forgot our token
    adapter.get("1")
    assert mf.tokens == 3


def test_check_reports_sign_in_classes_and_property_types():
    assert _adapter(FakeMFiles()).check(["invoice"]) == []
    assert "cannot sign in" in _adapter(FakeMFiles(), password="bad").check([])[0]
    mf = FakeMFiles()
    mf.types[1104] = TEXT
    del mf.types[1111]
    problems = _adapter(mf, classes=[CLASS, 77]).check([])
    text = "\n".join(problems)
    assert len(problems) == 3, text
    assert (
        "class 77" in text
        and "property 1104 (requested) has data type 1" in text
        and "1111 (fields.invoice_date)" in text
    )


def test_no_destructive_calls_exist():
    """Only GETs, the token POST, POST of properties, and PUT of the check-out state. No DELETE (which
    would delete, destroy or undo a check-out), and nothing writes to /files."""
    import docreview.ecm.mfiles as mfiles

    source = inspect.getsource(mfiles).replace(mfiles.__doc__, "")  # the docstring names what is absent
    calls = re.findall(r'_call\(\s*"(\w+)",\s*f?"([^"]+)"', source)
    assert {m for m, _ in calls} == {"GET", "PUT", "POST"}
    assert {p for m, p in calls if m == "PUT"} == {"{obj}/latest/checkedout", "{obj}/{version}/checkedout"}
    assert {p for m, p in calls if m == "POST"} == {"{obj}/{version}/properties"}
    assert all("/files/" not in p or p.endswith("/content") for m, p in calls if m == "GET")
    for word in ("DELETE", ".delete(", '/files"', "undocheckout", "destroy", "/deleted"):
        assert word not in source
