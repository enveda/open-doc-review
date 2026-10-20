"""AlfrescoAdapter against a fake Alfresco REST API v1 (httpx.MockTransport, no network)."""

from __future__ import annotations

import inspect
import json
import re
from urllib.parse import unquote

import pytest

httpx = pytest.importorskip("httpx")

from docreview.ecm import ECMAdapter  # noqa: E402
from docreview.ecm.alfresco import API, SEARCH, AlfrescoAdapter  # noqa: E402
from docreview.ecm.base import NS_CLASSIFICATION, NS_FIELDS, NS_REVIEW  # noqa: E402
from docreview.models import DocumentRef  # noqa: E402

ROOT, INBOX = "root-1", "inbox-1"
ASPECT = "dr:documentReview"


def _node(node_id, name, parent=INBOX, label="1.0", inside=True, **extra):
    chain = [{"id": "company-home"}, *([{"id": ROOT}, {"id": INBOX}] if inside else [{"id": "elsewhere"}])]
    return {
        "id": node_id,
        "name": name,
        "isFile": True,
        "isFolder": False,
        "parentId": parent,
        "modifiedAt": extra.pop("modified", "2026-01-01T00:00:00.000+0000"),
        "content": {"mimeType": extra.pop("mime", "application/pdf")},
        "path": {"name": "/Company Home/Root/Inbox", "elements": chain},
        "aspectNames": ["cm:auditable", "cm:versionable"],
        "properties": {"cm:versionLabel": label} if label else {},
        **extra,
    }


class FakeAlfresco:
    """Just enough of the v1 API; every request is recorded."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.nodes = {
            "1": _node("1", "a.pdf"),
            "2": _node("2", "b.pdf", label="2.0"),
            "9": _node("9", "out.pdf", inside=False),
            ROOT: {"id": ROOT, "isFolder": True, "path": {"elements": [{"id": "company-home"}]}},
            INBOX: {"id": INBOX, "isFolder": True, "path": {"elements": [{"id": "company-home"}, {"id": ROOT}]}},
        }
        self.comments: dict[str, list[str]] = {}
        self.search_hits: list[dict] = []
        self.rendition = "CREATED"
        self.rendition_requests = 0
        self.auth_ok = True
        self.properties = {
            "dr:classification": "d:text",
            "dr:review": "d:text",
            "dr:docType": "d:text",
            "dr:reviewRequested": "d:boolean",
            "dr:f_po_number": "d:text",
            "dr:f_total": "d:double",
            "dr:f_invoice_date": "d:date",
        }

    @staticmethod
    def _list(entries, more=False):
        return {"list": {"entries": [{"entry": e} for e in entries], "pagination": {"hasMoreItems": more}}}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.auth_ok:
            return httpx.Response(401)
        path, method = unquote(request.url.path), request.method
        if path == SEARCH and method == "POST":
            return httpx.Response(200, json=self._list(self.search_hits))
        path = path.removeprefix(API)
        if path == "/people/-me-":
            return httpx.Response(200, json={"entry": {"id": "svc"}})
        if path == f"/aspects/{ASPECT}":
            props = [{"id": k, "dataType": v} for k, v in self.properties.items()]
            return httpx.Response(200, json={"entry": {"id": ASPECT, "properties": props}})
        if path == f"/nodes/{INBOX}/children":
            files = [n for n in self.nodes.values() if n.get("isFile") and n["id"] != "9"]
            skip = int(request.url.params["skipCount"])
            return httpx.Response(200, json=self._list(files[skip : skip + 1], more=skip + 1 < len(files)))
        m = re.fullmatch(r"/nodes/([^/]+)(/.*)?", path)
        if not m or m.group(1) not in self.nodes:
            return httpx.Response(404, json={"error": {"statusCode": 404}})
        node, rest = self.nodes[m.group(1)], m.group(2) or ""
        if rest == "" and method == "GET":
            return httpx.Response(200, json={"entry": node})
        if rest == "" and method == "PUT":
            body = json.loads(request.content)
            node["aspectNames"] = body["aspectNames"]
            for k, v in body["properties"].items():
                if v is None:
                    node["properties"].pop(k, None)
                else:
                    node["properties"][k] = v
            return httpx.Response(200, json={"entry": node})
        if rest == "/content":
            return httpx.Response(200, content=b"%PDF-original")
        if rest == "/renditions/pdf":
            return httpx.Response(200, json={"entry": {"id": "pdf", "status": self.rendition}})
        if rest == "/renditions" and method == "POST":
            self.rendition_requests += 1
            return httpx.Response(409 if self.rendition_requests > 1 else 202, json={})
        if rest == "/renditions/pdf/content":
            return httpx.Response(200, content=b"%PDF-rendition")
        if rest == "/comments" and method == "POST":
            self.comments.setdefault(node["id"], []).insert(0, json.loads(request.content)["content"])
            return httpx.Response(201, json={"entry": {}})
        if rest == "/comments":
            return httpx.Response(200, json=self._list([{"content": c} for c in self.comments.get(node["id"], [])]))
        return httpx.Response(404)


def _adapter(fake, **opts):
    opts = {"base_url": "https://acs.example.com", "root_node_id": ROOT, "inbox_node_id": INBOX, **opts}
    return AlfrescoAdapter(username="svc", password="pw", transport=httpx.MockTransport(fake), **opts)


def _ref(node_id="1", version="1.0"):
    return DocumentRef(id=node_id, name="a.pdf", version=version)


def test_implements_the_port_and_needs_credentials(monkeypatch):
    assert isinstance(_adapter(FakeAlfresco()), ECMAdapter)
    for var in ("ALFRESCO_USERNAME", "ALFRESCO_PASSWORD", "ALFRESCO_TICKET"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValueError, match="username"):
        AlfrescoAdapter(base_url="https://x", root_node_id=ROOT)
    fake = FakeAlfresco()
    AlfrescoAdapter(base_url="https://x", root_node_id=ROOT, ticket="TICKET_1", transport=httpx.MockTransport(fake))
    _adapter(fake).get("1")
    assert fake.requests[-1].headers["Authorization"] == "Basic c3ZjOnB3"  # svc:pw


def test_get_is_scoped_and_uses_the_version_label():
    adapter = _adapter(FakeAlfresco())
    ref = adapter.get("1")
    assert (ref.version, ref.parent_id, ref.location) == ("1.0", INBOX, "/Company Home/Root/Inbox")
    with pytest.raises(LookupError):
        adapter.get("9")  # outside root_node_id
    with pytest.raises(LookupError):
        adapter.get("404")


def test_unversioned_nodes_are_offered_once_and_again_when_their_size_changes():
    fake = FakeAlfresco()
    fake.nodes["2"]["properties"]["dr:classification"] = json.dumps({"processed_version": "2.0"})
    fake.nodes["1"]["properties"] = {}  # not cm:versionable
    fake.nodes["1"]["content"]["sizeInBytes"] = 120
    adapter = _adapter(fake)
    (event,) = adapter.poll(None)[0]
    assert (event.ref.id, event.ref.version) == ("1", "unversioned:120")
    adapter.write_metadata(event.ref, NS_CLASSIFICATION, {"processed_version": event.ref.version})
    assert adapter.poll(None)[0] == []  # our own property write does not make it look new
    fake.nodes["1"]["content"]["sizeInBytes"] = 135
    assert [e.ref.version for e in adapter.poll(None)[0]] == ["unversioned:135"]


def test_a_node_that_versions_on_property_updates_is_refused_but_does_not_stop_the_poll():
    fake = FakeAlfresco()
    fake.nodes["1"]["properties"] = {"cm:versionLabel": "1.0", "cm:autoVersionOnUpdateProps": True}
    adapter = _adapter(fake)
    with pytest.raises(RuntimeError, match="versions on property updates"):
        adapter.get("1")
    assert [e.ref.id for e in adapter.poll(None)[0]] == ["2"]  # node 1 skipped and logged


def test_poll_pages_and_offers_unprocessed_versions_and_review_requests():
    fake = FakeAlfresco()
    fake.nodes["1"]["properties"]["dr:classification"] = json.dumps({"processed_version": "1.0"})
    fake.nodes["2"]["properties"]["dr:classification"] = json.dumps({"processed_version": "1.0"})
    adapter = _adapter(fake)
    events, cursor = adapter.poll(None)
    assert [(e.kind, e.ref.id) for e in events] == [("new_document", "2")] and cursor is None
    fake.nodes["1"]["properties"]["dr:reviewRequested"] = True
    assert [(e.kind, e.ref.id) for e in adapter.poll(None)[0]] == [("review_requested", "1"), ("new_document", "2")]


def test_download_refuses_a_changed_version_and_uses_the_pdf_rendition():
    fake = FakeAlfresco()
    adapter = _adapter(fake)
    assert adapter.download(_ref()).data == b"%PDF-original"
    with pytest.raises(RuntimeError, match="changed"):
        adapter.download(_ref(version="0.9"))
    fake.nodes["1"]["content"]["mimeType"] = "application/msword"
    word = adapter.download(DocumentRef(id="1", name="memo.docx", version="1.0"))
    assert (word.data, word.media_type) == (b"%PDF-rendition", "application/pdf")
    fake.rendition = "NOT_CREATED"
    for _ in range(2):  # the first call requests the rendition, the second finds it already requested (409)
        with pytest.raises(RuntimeError, match="rendition of memo.docx pending"):
            adapter.download(DocumentRef(id="1", name="memo.docx", version="1.0"))
    posts = [json.loads(r.content) for r in fake.requests if r.method == "POST" and r.url.path.endswith("/renditions")]
    assert posts == [{"id": "pdf"}, {"id": "pdf"}]


def test_classification_merges_adds_the_aspect_and_mirrors_doc_type():
    fake = FakeAlfresco()
    adapter = _adapter(fake)
    adapter.write_metadata(_ref(), NS_CLASSIFICATION, {"doc_type": "invoice", "run_id": "r1"})
    adapter.write_metadata(_ref(), NS_CLASSIFICATION, {"failed_version": "1.0", "failure_count": 1, "run_id": None})
    assert adapter.read_metadata(_ref(), NS_CLASSIFICATION) == {
        "doc_type": "invoice",
        "failed_version": "1.0",
        "failure_count": 1,
    }
    node = fake.nodes["1"]
    assert node["properties"]["dr:docType"] == "invoice"
    assert node["aspectNames"] == ["cm:auditable", "cm:versionable", ASPECT]  # existing aspects kept
    put = next(r for r in fake.requests if r.method == "PUT")
    assert set(json.loads(put.content)) == {"aspectNames", "properties"}  # never content


def test_review_write_clears_the_request():
    fake = FakeAlfresco()
    adapter = _adapter(fake)
    assert adapter.read_metadata(_ref(), NS_REVIEW) == {}
    fake.nodes["1"]["properties"]["dr:reviewRequested"] = True
    assert adapter.read_metadata(_ref(), NS_REVIEW) == {"requested": True}
    adapter.write_metadata(_ref(), NS_REVIEW, {"outcome": "no_findings", "finding_counts": {"major": 0}})
    assert adapter.read_metadata(_ref(), NS_REVIEW) == {
        "outcome": "no_findings",
        "finding_counts": {"major": 0},
        "requested": False,
    }


def test_fields_get_one_property_each_with_dates_and_clearing():
    fake = FakeAlfresco()
    adapter = _adapter(fake)
    adapter.write_metadata(_ref(), NS_FIELDS, {"po_number": "PO-1", "total": 12.5, "invoice_date": "2026-03-04"})
    assert fake.nodes["1"]["properties"]["dr:f_invoice_date"] == "2026-03-04T00:00:00.000+0000"
    assert adapter.read_metadata(_ref(), NS_FIELDS) == {
        "po_number": "PO-1",
        "total": 12.5,
        "invoice_date": "2026-03-04",
    }
    adapter.write_metadata(_ref(), NS_FIELDS, {"po_number": None})
    assert "po_number" not in adapter.read_metadata(_ref(), NS_FIELDS)
    with pytest.raises(ValueError, match="no property"):
        adapter.write_metadata(_ref(), NS_FIELDS, {"vendor": "x"})


def test_find_escapes_the_query_and_rechecks_in_python():
    fake = FakeAlfresco()
    props = {"dr:docType": "Invoice", "dr:f_po_number": 'PO "7" \\ A '}
    fake.search_hits = [
        _node("1", "old.pdf", modified="2026-01-01T00:00:00.000+0000", properties=dict(props)),
        _node("2", "new.pdf", modified="2026-03-01T00:00:00.000+0000", properties=dict(props)),
        _node("9", "out.pdf", inside=False, properties=dict(props)),  # outside root_node_id
        _node("3", "near.pdf", properties={**props, "dr:f_po_number": 'PO "7" \\ AB'}),
    ]
    adapter = _adapter(fake)
    assert [r.id for r in adapter.find("invoice", {"po_number": ' po "7" \\ a'})] == ["2", "1"]
    query = json.loads(fake.requests[-1].content)["query"]
    assert query["language"] == "afts"
    assert query["query"] == (
        f'ANCESTOR:"workspace://SpacesStore/{ROOT}" AND ASPECT:"{ASPECT}" AND dr:docType:"invoice" '
        'AND dr:f_po_number:"po \\"7\\" \\\\ a"'
    )
    assert adapter.find("invoice", {"po_number": 'PO "7" \\ A'}, scope="other-folder") == []
    assert json.loads(fake.requests[-1].content)["query"]["query"].startswith('PARENT:"workspace://SpacesStore/other')
    with pytest.raises(ValueError, match="invalid field keys"):
        adapter.find("invoice", {"po_number:x OR TYPE": "y"})


def test_metadata_and_comments_refuse_nodes_outside_the_root():
    fake = FakeAlfresco()
    adapter = _adapter(fake)
    outside = DocumentRef(id="9", name="out.pdf", version="1.0")
    for call in (
        lambda: adapter.read_metadata(outside, NS_CLASSIFICATION),
        lambda: adapter.write_metadata(outside, NS_CLASSIFICATION, {"doc_type": "invoice"}),
        lambda: adapter.post_comment(outside, "x"),
        lambda: adapter.list_comments(outside),
    ):
        with pytest.raises(LookupError):
            call()
    assert not [r for r in fake.requests if r.method in ("PUT", "POST")]


def test_comments_use_the_native_api():
    fake = FakeAlfresco()
    adapter = _adapter(fake)
    adapter.post_comment(_ref(), "First finding.")
    adapter.post_comment(_ref(), "Second finding.")
    assert list(adapter.list_comments(_ref())) == ["Second finding.", "First finding."]


def test_check_reports_sign_in_folders_and_the_aspect():
    assert _adapter(FakeAlfresco()).check(["invoice"]) == []
    fake = FakeAlfresco()
    fake.auth_ok = False
    assert "cannot sign in" in _adapter(fake).check([])[0]
    fake = FakeAlfresco()
    del fake.properties["dr:reviewRequested"]
    fake.properties["dr:review"] = "d:int"
    problems = _adapter(fake, inbox_node_id="1").check([])
    text = "\n".join(problems)
    assert len(problems) == 3, text
    assert "node 1 is not a folder" in text
    assert "no property 'dr:reviewRequested'" in text and "'dr:review' should be d:text" in text


def test_no_destructive_calls_exist():
    """Alfresco changes or removes content through DELETE, PUT .../content, POST .../children (upload),
    .../copy, .../move, .../lock and version reverts. The adapter sends GET, one PUT of /nodes/{id} with
    aspectNames + properties, and POST only to /comments, /renditions (a PDF rendition request) and the
    search API."""
    import docreview.ecm.alfresco as alfresco

    source = inspect.getsource(alfresco)
    assert set(re.findall(r'_call\(\s*"(\w+)"', source)) == {"GET", "PUT", "POST"}
    assert re.findall(r'_call\(\s*"PUT",\s*(f?"[^"]*"|\w+)', source) == ["f\"/nodes/{quote(ref.id, safe='')}\""]
    posts = re.findall(r'_call\(\s*"POST",\s*(f?"[^"]*"|\w+)', source)
    assert posts == ['f"{base}/renditions"', "SEARCH", "f\"/nodes/{quote(ref.id, safe='')}/comments\""]
    for word in ("DELETE", "/copy", "/move", "/lock", "/revert", '/children", json', '/content", json'):
        assert word not in source
    assert source.count('"PUT"') == 1 and "self._http.request(" in source
