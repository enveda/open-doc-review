"""SharePointAdapter against a fake Microsoft Graph (httpx.MockTransport, no network)."""

from __future__ import annotations

import inspect
import json
from urllib.parse import unquote

import pytest

httpx = pytest.importorskip("httpx")

from docreview.ecm import ECMAdapter  # noqa: E402
from docreview.ecm.base import NS_CLASSIFICATION, NS_FIELDS, NS_REVIEW  # noqa: E402
from docreview.ecm.sharepoint import SharePointAdapter  # noqa: E402
from docreview.models import DocumentRef  # noqa: E402

DRIVE, SITE, LIST = "d1", "s1", "l1"
P = "DocReview"


def _item(item_id, name, folder="/Inbox", ctag="c1", parent="f-inbox", **extra):
    return {
        "id": item_id,
        "name": name,
        "cTag": ctag,
        "file": {"mimeType": "application/pdf"},
        "parentReference": {"id": parent, "path": f"/drives/{DRIVE}/root:{folder}"},
        "lastModifiedDateTime": extra.pop("modified", "2026-01-01T00:00:00Z"),
        **extra,
    }


class FakeGraph:
    """Just enough of Graph v1.0; every request is recorded."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.items = {
            "1": _item("1", "a.pdf"),
            "2": _item("2", "b.pdf", ctag="c2"),
            "9": _item("9", "out.pdf", folder="/Elsewhere", parent="f-else"),
        }
        self.fields: dict[str, dict] = {"1": {}, "2": {}, "9": {}}
        self.columns = {
            f"{P}Classification": "text",
            f"{P}DocType": "text",
            f"{P}Review": "text",
            f"{P}ReviewRequested": "boolean",
            f"{P}Comments": "text",
            f"{P}_po_number": "text",
            f"{P}_total": "number",
            f"{P}_invoice_date": "dateTime",
        }
        self.list_rows: list[dict] = []
        self.token_ok = True
        self.throttle_once = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = unquote(request.url.path), request.method
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200 if self.token_ok else 401, json={"access_token": "T", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer T"
        if self.throttle_once:
            self.throttle_once = False
            return httpx.Response(429, headers={"Retry-After": "0"})
        path = path.removeprefix("/v1.0")
        if path == f"/drives/{DRIVE}/root:/Inbox:/children":
            files = [dict(i, listItem={"fields": self.fields[k]}) for k, i in self.items.items() if k != "9"]
            folder = {"id": "f-sub", "name": "Sub", "folder": {}, "parentReference": {"path": "x"}}
            more = "https://graph.microsoft.com/v1.0/next-children"
            return httpx.Response(200, json={"value": files[:1], "@odata.nextLink": more})
        if path == "/next-children":  # the second page of the inbox listing
            files = [dict(i, listItem={"fields": self.fields[k]}) for k, i in self.items.items() if k == "2"]
            folder = {"id": "f-sub", "name": "Sub", "folder": {}, "parentReference": {"path": "x"}}
            return httpx.Response(200, json={"value": [*files, folder]})
        if path in (f"/drives/{DRIVE}/root:/Inbox", f"/drives/{DRIVE}/root:/Docs"):
            return httpx.Response(200, json={"id": "f", "folder": {}})
        if path == f"/drives/{DRIVE}/list":
            return httpx.Response(200, json={"id": LIST})
        if path == f"/sites/{SITE}/lists/{LIST}/columns":
            kinds = [{"name": n, k: {}} for n, k in self.columns.items()]
            return httpx.Response(200, json={"value": kinds})
        if path == f"/sites/{SITE}/lists/{LIST}/items":
            return httpx.Response(200, json={"value": self.list_rows})
        for item_id, item in self.items.items():
            base = f"/drives/{DRIVE}/items/{item_id}"
            if path == base:
                return httpx.Response(200, json=dict(item, listItem={"fields": self.fields[item_id]}))
            if path == f"{base}/content":
                return httpx.Response(200, content=b"%PDF")
            if path == f"{base}/listItem/fields" and method == "GET":
                return httpx.Response(200, json=dict(self.fields[item_id]))
            if path == f"{base}/listItem/fields" and method == "PATCH":
                for k, v in json.loads(request.content).items():
                    if v is None:
                        self.fields[item_id].pop(k, None)
                    else:
                        self.fields[item_id][k] = v
                return httpx.Response(200, json=self.fields[item_id])
        return httpx.Response(404, json={"error": {"code": "itemNotFound"}})


def _adapter(graph, **opts):
    opts = {
        "tenant_id": "t",
        "client_id": "c",
        "client_secret": "s",
        "site_id": SITE,
        "drive_id": DRIVE,
        "root_path": "/Inbox",
        **opts,
    }
    return SharePointAdapter(transport=httpx.MockTransport(graph), _base_dir=".", **opts)


def _ref(item_id="1", version="c1"):
    return DocumentRef(id=item_id, name="a.pdf", version=version)


def test_implements_the_port_and_needs_settings():
    assert isinstance(_adapter(FakeGraph()), ECMAdapter)
    with pytest.raises(ValueError):
        SharePointAdapter(tenant_id="t", client_id="c", client_secret="s", site_id=SITE)  # no library
    with pytest.raises(ValueError, match="not inside"):
        _adapter(FakeGraph(), root_path="/Inbox", inbox_path="/Other")


def test_get_is_scoped_and_uses_ctag_as_version():
    adapter = _adapter(FakeGraph())
    ref = adapter.get("1")
    assert (ref.version, ref.parent_id, ref.location) == ("c1", "f-inbox", "/Inbox")
    with pytest.raises(LookupError):
        adapter.get("9")
    with pytest.raises(LookupError):
        adapter.get("404")


def test_poll_offers_unprocessed_versions_and_review_requests():
    graph = FakeGraph()
    graph.fields["1"] = {f"{P}Classification": json.dumps({"processed_version": "c1"})}
    graph.fields["2"] = {f"{P}Classification": json.dumps({"processed_version": "old"})}
    adapter = _adapter(graph)
    events, cursor = adapter.poll(None)
    assert [(e.kind, e.ref.id) for e in events] == [("new_document", "2")] and cursor is None
    graph.fields["1"][f"{P}ReviewRequested"] = True
    assert [(e.kind, e.ref.id) for e in adapter.poll(None)[0]] == [("review_requested", "1"), ("new_document", "2")]


def test_download_refuses_a_changed_version():
    graph = FakeGraph()
    adapter = _adapter(graph)
    content = adapter.download(_ref())
    assert content.data == b"%PDF" and content.media_type == "application/pdf"
    with pytest.raises(RuntimeError, match="changed"):
        adapter.download(_ref(version="c0"))
    word = adapter.download(DocumentRef(id="1", name="memo.docx", version="c1"))
    assert word.media_type == "application/pdf" and graph.requests[-1].url.params["format"] == "pdf"


def test_classification_merges_and_mirrors_doc_type():
    graph = FakeGraph()
    adapter = _adapter(graph)
    adapter.write_metadata(_ref(), NS_CLASSIFICATION, {"doc_type": "invoice", "run_id": "r1"})
    adapter.write_metadata(_ref(), NS_CLASSIFICATION, {"failed_version": "c1", "failure_count": 1, "run_id": None})
    assert adapter.read_metadata(_ref(), NS_CLASSIFICATION) == {
        "doc_type": "invoice",
        "failed_version": "c1",
        "failure_count": 1,
    }
    assert graph.fields["1"][f"{P}DocType"] == "invoice"


def test_review_requested_is_its_own_column_and_the_write_clears_it():
    graph = FakeGraph()
    adapter = _adapter(graph)
    assert adapter.read_metadata(_ref(), NS_REVIEW) == {}
    graph.fields["1"][f"{P}ReviewRequested"] = True
    assert adapter.read_metadata(_ref(), NS_REVIEW) == {"requested": True}
    adapter.write_metadata(_ref(), NS_REVIEW, {"outcome": "no_findings", "finding_counts": {"major": 0}})
    assert graph.fields["1"][f"{P}ReviewRequested"] is False
    assert adapter.read_metadata(_ref(), NS_REVIEW) == {
        "outcome": "no_findings",
        "finding_counts": {"major": 0},
        "requested": False,
    }


def test_fields_get_one_column_each_with_dates_and_clearing():
    graph = FakeGraph()
    adapter = _adapter(graph)
    adapter.write_metadata(_ref(), NS_FIELDS, {"po_number": "PO-1", "total": 12.5, "invoice_date": "2026-03-04"})
    assert graph.fields["1"][f"{P}_invoice_date"] == "2026-03-04T00:00:00Z"
    assert adapter.read_metadata(_ref(), NS_FIELDS) == {
        "po_number": "PO-1",
        "total": 12.5,
        "invoice_date": "2026-03-04",
    }
    adapter.write_metadata(_ref(), NS_FIELDS, {"po_number": None})
    assert "po_number" not in adapter.read_metadata(_ref(), NS_FIELDS)
    with pytest.raises(ValueError, match="no column"):
        adapter.write_metadata(_ref(), NS_FIELDS, {"vendor": "x"})


def test_find_escapes_filters_and_rechecks_in_python():
    graph = FakeGraph()
    hit = {f"{P}DocType": "Invoice", f"{P}_po_number": "o'brien-1 "}
    graph.list_rows = [
        {"fields": hit, "driveItem": _item("1", "old.pdf", modified="2026-01-01T00:00:00Z")},
        {"fields": hit, "driveItem": _item("2", "new.pdf", modified="2026-03-01T00:00:00Z")},
        {"fields": hit, "driveItem": _item("9", "out.pdf", folder="/Elsewhere")},  # outside root_path
        {"fields": {**hit, f"{P}_po_number": "O'BRIEN-12"}, "driveItem": _item("3", "near.pdf")},
    ]
    adapter = _adapter(graph)
    refs = adapter.find("invoice", {"po_number": " O'Brien-1"})
    assert [r.id for r in refs] == ["2", "1"]
    request = graph.requests[-1]
    assert request.headers["Prefer"] == "HonorNonIndexedQueriesWarningMayFailRandomly"
    assert request.url.params["$filter"] == (f"fields/{P}DocType eq 'invoice' and fields/{P}_po_number eq 'O''Brien-1'")
    assert adapter.find("invoice", {"po_number": "o'brien-1"}, scope="nowhere") == []
    with pytest.raises(ValueError):
        adapter.find("invoice", {"po_number eq 'x' or true": "y"})
    with pytest.raises(ValueError):
        adapter.find("invoice", {"total": "5 or 1 eq 1"})  # number columns take numbers only


def test_comments_are_kept_newest_first_and_capped():
    graph = FakeGraph()
    adapter = _adapter(graph, comments_max_chars=200)
    adapter.post_comment(_ref(), "First finding.")
    adapter.post_comment(_ref(), "Second finding.\nWith a second line.")
    assert list(adapter.list_comments(_ref())) == ["Second finding.\nWith a second line.", "First finding."]
    adapter.post_comment(_ref(), "x" * 150)
    assert list(adapter.list_comments(_ref())) == ["x" * 150]  # the older ones no longer fit


def test_throttled_calls_are_retried():
    graph = FakeGraph()
    graph.throttle_once = True
    assert _adapter(graph).get("1").id == "1"


def test_check_reports_token_folders_and_columns():
    assert _adapter(FakeGraph(), root_path="/Docs", inbox_path="/Docs").check(["invoice"]) == []
    graph = FakeGraph()
    graph.token_ok = False
    assert "cannot get a token" in _adapter(graph).check([])[0]
    graph = FakeGraph()
    del graph.columns[f"{P}ReviewRequested"]
    graph.columns[f"{P}Review"] = "number"
    problems = _adapter(graph, root_path="/Missing").check([])
    text = "\n".join(problems)
    assert len(problems) == 3, text
    assert "cannot read folder '/Missing'" in text
    assert f"no column '{P}ReviewRequested'" in text and f"'{P}Review' should be text" in text


def test_no_destructive_calls_exist():
    """Graph changes or removes a file only through PUT, DELETE, POST (copy, upload session) or a
    PATCH of the drive item itself. The adapter sends GET, PATCH to listItem/fields, and one POST
    for the token."""
    import re

    import docreview.ecm.sharepoint as sharepoint

    source = inspect.getsource(sharepoint)
    assert set(re.findall(r'_call\(\s*"(\w+)"', source)) == {"GET", "PATCH"}
    assert re.findall(r'_call\(\s*"PATCH",\s*(\w+)', source) == ["url"]
    assert "/listItem/fields" in source.split("def _patch_fields", 1)[1].split("def ", 1)[0]
    assert source.count("self._http.post(") == 1 and "self._http.request(" in source
    for word in ("createUploadSession", "/copy", "DELETE", "PUT", ".delete(", ".put("):
        assert word not in source
