"""BoxAdapter against an in-memory fake client (no network)."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("box_sdk_gen")

from docreview.ecm import ECMAdapter  # noqa: E402
from docreview.ecm.base import NS_CLASSIFICATION, NS_FIELDS, NS_REVIEW  # noqa: E402
from docreview.ecm.box import CLASSIFICATION_KEYS, REVIEW_KEYS, BoxAdapter  # noqa: E402
from docreview.models import DocumentRef  # noqa: E402

ROOT = "100"
UUID = "c261e3e5-e998-45e6-8792-f89298efd516"
TEMPLATES = {"classification": "docClass", "review": "docReview", "fields": {"invoice": "docInvoice"}}


class ApiError(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.response_info = SimpleNamespace(status_code=status)


def _path(*ids: str) -> dict:
    return {"entries": [{"id": i, "name": f"F{i}"} for i in ids]}


class FakeBox:
    """Just enough of box_sdk_gen.BoxClient; every call is recorded."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.files_by_id = {
            "1": {
                "id": "1",
                "name": "a.pdf",
                "file_version": {"id": "v1"},
                "modified_at": "2026-01-02T00:00:00+00:00",
                "parent": {"id": "200"},
                "path_collection": _path("0", ROOT, "200"),
            },
            "9": {
                "id": "9",
                "name": "out.pdf",
                "file_version": {"id": "v9"},
                "parent": {"id": "900"},
                "path_collection": _path("0", "900"),
            },
        }
        self.metadata: dict[tuple[str, str], dict] = {}
        self.event_pages: list[dict] = []
        self.folder: list[dict] = []
        self.search_pages: list[dict] = []
        self.comments_on: dict[str, list[str]] = {}
        self.files = SimpleNamespace(get_file_by_id=self._get_file)
        self.downloads = SimpleNamespace(download_file=self._download)
        self.events = SimpleNamespace(get_events=self._events)
        self.folders_by_id = {ROOT: _path("0"), "300": _path("0", ROOT), "800": _path("0")}
        self.schemas = {
            "docClass": {k: "float" if k == "failure_count" else "string" for k in CLASSIFICATION_KEYS},
            "docReview": {k: "enum" if k == "requested" else "string" for k in REVIEW_KEYS},
            "docInvoice": {"po_number": "string", "total": "float", "invoice_date": "date"},
        }
        self.signed_in = True
        self.folders = SimpleNamespace(get_folder_items=self._items, get_folder_by_id=self._get_folder)
        self.metadata_templates = SimpleNamespace(get_metadata_template=self._schema)
        self.search = SimpleNamespace(search_by_metadata_query=self._search)
        self.users = SimpleNamespace(get_user_me=self._me)
        self.comments = SimpleNamespace(create_comment=self._comment, get_file_comments=self._comments)
        self.file_metadata = SimpleNamespace(
            get_file_metadata_by_id=self._get_md,
            create_file_metadata_by_id=self._create_md,
            update_file_metadata_by_id=self._update_md,
        )

    def _me(self, **kw):
        if not self.signed_in:
            raise ApiError(401)
        return {"enterprise": {"id": "777"}}

    def _get_folder(self, folder_id, fields=None):
        if folder_id not in self.folders_by_id:
            raise ApiError(404)
        return {"id": folder_id, "path_collection": self.folders_by_id[folder_id]}

    def _schema(self, scope, template):
        if template not in self.schemas:
            raise ApiError(404)
        return {"fields": [{"key": k, "type": t} for k, t in self.schemas[template].items()]}

    def _get_file(self, file_id, fields=None):
        if file_id not in self.files_by_id:
            raise ApiError(404)
        return self.files_by_id[file_id]

    def _download(self, file_id, version=None):
        self.calls.append(("download", file_id, version))
        return io.BytesIO(b"%PDF")

    def _events(self, **kw):
        self.calls.append(("events", kw["stream_position"], [t.value for t in kw["event_type"]]))
        if kw["stream_position"] == "now":
            return {"entries": [], "next_stream_position": "p0"}
        return self.event_pages.pop(0) if self.event_pages else {"entries": [], "next_stream_position": "p9"}

    def _items(self, folder_id, **kw):
        return {"entries": self.folder, "next_marker": None}

    def _search(self, from_, ancestor, **kw):
        self.calls.append(("search", from_, ancestor, kw["query"], kw["query_params"]))
        return self.search_pages.pop(0) if self.search_pages else {"entries": []}

    def _get_md(self, file_id, scope, template):
        if (file_id, template) not in self.metadata:
            raise ApiError(404)
        return {"$id": "x", "$parent": f"file_{file_id}", **self.metadata[(file_id, template)]}

    def _create_md(self, file_id, scope, template, data):
        self.calls.append(("create_md", file_id, template, dict(data)))
        if (file_id, template) in self.metadata:
            raise ApiError(409)
        self.metadata[(file_id, template)] = dict(data)

    def _update_md(self, file_id, scope, template, ops):
        self.calls.append(("update_md", file_id, template, [(o.op.value, o.path, o.value) for o in ops]))
        md = self.metadata[(file_id, template)]
        for o in ops:
            if o.op.value == "remove":
                md.pop(o.path[1:])
            else:
                md[o.path[1:]] = o.value

    def _comment(self, message, item):
        self.comments_on.setdefault(item.id, []).append(message)

    def _comments(self, file_id, **kw):
        texts = self.comments_on.get(file_id, [])
        return {"entries": [{"message": t} for t in texts], "total_count": len(texts)}


def _adapter(fake, **opts):
    opts.setdefault("events", "enterprise")
    return BoxAdapter(root_folder_id=ROOT, templates=TEMPLATES, client=fake, _base_dir=".", **opts)


def _ref(file_id="1", version="v1"):
    return DocumentRef(id=file_id, name="a.pdf", version=version)


def _upload(file_id):
    return {"event_type": "UPLOAD", "source": {"item_type": "file", "item_id": file_id}}


def test_implements_the_port():
    assert isinstance(_adapter(FakeBox()), ECMAdapter)


def test_folder_scan_is_the_default():
    assert BoxAdapter(root_folder_id=ROOT, templates=TEMPLATES, client=FakeBox()).events == "folder_scan"


def test_rejects_incomplete_templates():
    with pytest.raises(ValueError):
        BoxAdapter(root_folder_id=ROOT, templates={"classification": "x"}, client=FakeBox())


def test_get_is_scoped_to_root():
    adapter = _adapter(FakeBox())
    ref = adapter.get("1")
    assert (ref.version, ref.parent_id, ref.location) == ("v1", "200", "F100/F200")
    with pytest.raises(LookupError):
        adapter.get("9")


def test_download_pins_the_version():
    fake = FakeBox()
    content = _adapter(fake).download(_ref())
    assert content.data == b"%PDF" and content.media_type == "application/pdf"
    assert ("download", "1", "v1") in fake.calls


def test_first_poll_only_learns_the_position():
    fake = FakeBox()
    events, cursor = _adapter(fake).poll(None)
    assert events == [] and cursor == "p0"


def test_stream_poll_filters_scope_template_and_trigger():
    fake = FakeBox()
    fake.metadata[("1", "docReview")] = {"requested": "true"}
    review_update = {
        "event_type": "METADATA_INSTANCE_UPDATE",
        "source": {"item_type": "file", "item_id": "1"},
        "additional_details": {"metadata": {"type": "docReview-" + UUID}},
    }
    other_update = {**review_update, "additional_details": {"metadata": {"type": "docClass-" + UUID}}}
    fake.event_pages = [
        {
            "entries": [
                _upload("1"),
                _upload("1"),
                _upload("9"),
                _upload("404"),
                review_update,
                other_update,
                {"event_type": "UPLOAD", "source": {"item_type": "folder", "item_id": "5"}},
            ],
            "next_stream_position": "p1",
        },
    ]
    events, cursor = _adapter(fake).poll("p0")
    assert [(e.kind, e.ref.id) for e in events] == [("new_document", "1"), ("review_requested", "1")]
    assert cursor == "p9"  # read on until an empty page
    assert fake.calls[0] == ("events", "p0", ["UPLOAD", "METADATA_INSTANCE_UPDATE"])


def test_review_event_ignored_when_trigger_is_off():
    fake = FakeBox()
    fake.metadata[("1", "docReview")] = {"requested": "false", "outcome": "no_findings"}
    fake.event_pages = [
        {
            "entries": [
                {
                    "event_type": "METADATA_INSTANCE_UPDATE",
                    "source": {"item_type": "file", "item_id": "1"},
                    "additional_details": {"metadata": {"type": "docReview"}},
                }
            ],
            "next_stream_position": "p1",
        }
    ]
    events, _ = _adapter(fake).poll("p0")
    assert events == []


def test_folder_scan_offers_files_until_their_current_version_is_processed():
    fake = FakeBox()
    fake.folder = [
        {"type": "file", "id": "1", "name": "a.pdf", "file_version": {"id": "v1"}},
        {"type": "file", "id": "2", "name": "b.pdf", "file_version": {"id": "v2"}},
        {"type": "folder", "id": "3", "name": "sub"},
    ]
    fake.metadata[("1", "docClass")] = {"doc_type": "invoice"}  # classified, but the pass did not finish
    fake.metadata[("2", "docClass")] = {"doc_type": "invoice", "processed_version": "v2"}
    adapter = _adapter(fake, events="folder_scan")
    events, cursor = adapter.poll(None)
    assert [(e.kind, e.ref.id, e.ref.parent_id) for e in events] == [("new_document", "1", ROOT)]
    assert json.loads(cursor) == {"seen": {"2": "v2"}}
    events, cursor = adapter.poll(cursor)  # still not processed (e.g. it failed): offered again
    assert [e.ref.id for e in events] == ["1"]
    fake.metadata[("1", "docClass")]["processed_version"] = "v1"
    events, cursor = adapter.poll(cursor)
    assert events == [] and json.loads(cursor) == {"seen": {"1": "v1", "2": "v2"}}
    fake.folder[0]["file_version"] = {"id": "v1b"}  # a new version keeps the old metadata but is offered
    assert [e.ref.version for e in adapter.poll(cursor)[0]] == ["v1b"]


def test_folder_scan_refuses_an_inbox_outside_the_root():
    with pytest.raises(ValueError, match="not inside"):
        _adapter(FakeBox(), events="folder_scan", inbox_folder_id="800").poll(None)
    fake = FakeBox()
    _adapter(fake, events="folder_scan", inbox_folder_id="300").poll(None)  # inside: fine


def test_write_metadata_creates_then_updates_idempotently():
    fake = FakeBox()
    adapter = _adapter(fake)
    adapter.write_metadata(_ref(), NS_CLASSIFICATION, {"doc_type": "invoice", "run_id": "r1"})
    adapter.write_metadata(_ref(), NS_CLASSIFICATION, {"doc_type": "invoice", "run_id": "r2", "note": None})
    assert fake.metadata[("1", "docClass")] == {"doc_type": "invoice", "run_id": "r2"}
    assert fake.calls[-1] == (
        "update_md",
        "1",
        "docClass",
        [("replace", "/doc_type", "invoice"), ("replace", "/run_id", "r2")],
    )


def test_fields_namespace_uses_the_doc_types_template():
    fake = FakeBox()
    fake.metadata[("1", "docClass")] = {"doc_type": "invoice"}
    adapter = _adapter(fake)
    adapter.write_metadata(_ref(), NS_FIELDS, {"po_number": "PO-1", "total": 12.5})
    assert fake.metadata[("1", "docInvoice")] == {"po_number": "PO-1", "total": 12.5}
    assert adapter.read_metadata(_ref(), NS_FIELDS) == {"po_number": "PO-1", "total": 12.5}


def test_none_clears_a_stale_value_and_is_skipped_on_create():
    fake = FakeBox()
    fake.metadata[("1", "docClass")] = {"doc_type": "invoice"}
    adapter = _adapter(fake)
    adapter.write_metadata(_ref(), NS_FIELDS, {"po_number": None, "total": 3})
    assert fake.metadata[("1", "docInvoice")] == {"total": 3}
    adapter.write_metadata(_ref(), NS_FIELDS, {"po_number": "PO-1", "total": None})
    adapter.write_metadata(_ref(), NS_FIELDS, {"po_number": None, "total": None})
    assert fake.metadata[("1", "docInvoice")] == {}
    assert fake.calls[-1][3] == [("remove", "/po_number", None)]


def test_date_fields_are_written_as_timestamps_and_read_as_days():
    fake = FakeBox()
    fake.metadata[("1", "docClass")] = {"doc_type": "invoice"}
    adapter = _adapter(fake)
    adapter.write_metadata(_ref(), NS_FIELDS, {"invoice_date": "2026-03-04", "po_number": "2026-03-04"})
    assert fake.metadata[("1", "docInvoice")] == {"invoice_date": "2026-03-04T00:00:00Z", "po_number": "2026-03-04"}
    fake.metadata[("1", "docInvoice")]["invoice_date"] = "2026-03-04T00:00:00.000Z"  # as Box returns it
    assert adapter.read_metadata(_ref(), NS_FIELDS)["invoice_date"] == "2026-03-04"


def test_check_passes_on_a_complete_setup():
    assert _adapter(FakeBox(), events="folder_scan", inbox_folder_id="300").check(["invoice"]) == []


def test_check_stops_at_bad_credentials():
    fake = FakeBox()
    fake.signed_in = False
    problems = _adapter(fake).check(["invoice"])
    assert len(problems) == 1 and "cannot sign in" in problems[0]


def test_check_reports_folders_templates_and_missing_keys():
    fake = FakeBox()
    del fake.folders_by_id[ROOT]
    del fake.schemas["docInvoice"]
    fake.schemas["docClass"].pop("failure_count")
    fake.schemas["docReview"].pop("requested")
    problems = _adapter(fake).check(["invoice", "receipt"])
    text = "\n".join(problems)
    assert len(problems) == 5, text
    assert "cannot read folder 100" in text and "no template for document type 'receipt'" in text
    assert "'docInvoice' (fields of 'invoice') cannot be read" in text
    assert "'docClass' (classification) has no field failure_count" in text
    assert "'docReview' (review) has no field requested" in text


def test_fields_write_without_classification_is_refused():
    with pytest.raises(ValueError):
        _adapter(FakeBox()).write_metadata(_ref(), NS_FIELDS, {"po_number": "PO-1"})


def test_review_write_encodes_structures_and_clears_the_trigger():
    fake = FakeBox()
    fake.metadata[("1", "docReview")] = {"requested": "true"}
    adapter = _adapter(fake)
    values = {"outcome": "no_findings", "finding_counts": {"major": 0}, "requested": False}
    adapter.write_metadata(_ref(), NS_REVIEW, values)
    stored = {"outcome": "no_findings", "finding_counts": '{"major": 0}', "requested": "false"}
    assert fake.metadata[("1", "docReview")] == stored
    assert adapter.read_metadata(_ref(), NS_REVIEW)["finding_counts"] == {"major": 0}


def test_custom_trigger_field_is_removed_on_review_write():
    fake = FakeBox()
    fake.metadata[("1", "docReview")] = {"askReview": "yes", "requested": "false"}
    _adapter(fake, review_trigger_field="askReview").write_metadata(_ref(), NS_REVIEW, {"requested": False})
    assert fake.metadata[("1", "docReview")] == {"requested": "false"}


def test_read_metadata_missing_is_empty_and_hides_system_keys():
    fake = FakeBox()
    adapter = _adapter(fake)
    assert adapter.read_metadata(_ref(), NS_REVIEW) == {}
    fake.metadata[("1", "docReview")] = {"outcome": "x"}
    assert adapter.read_metadata(_ref(), NS_REVIEW) == {"outcome": "x"}


def test_find_binds_values_filters_exactly_and_sorts_newest_first():
    fake = FakeBox()
    hit = {"enterprise_777": {"docInvoice": {"po_number": "po-1 "}}}
    fake.search_pages = [
        {
            "entries": [
                {"type": "file", "id": "a", "name": "old", "modified_at": "2026-01-01T00:00:00+00:00", "metadata": hit},
                {
                    "type": "file",
                    "id": "b",
                    "name": "new",
                    "modified_at": "2026-03-01T00:00:00+00:00",
                    "metadata": hit,
                    "parent": {"id": "200"},
                },
                {
                    "type": "file",
                    "id": "c",
                    "name": "wild",
                    "metadata": {"enterprise_777": {"docInvoice": {"po_number": "PO-12"}}},
                },
            ],
            "next_marker": None,
        }
    ]
    refs = _adapter(fake).find("invoice", {"po_number": " PO-1"}, scope="200")
    assert [r.id for r in refs] == ["b", "a"] and refs[0].parent_id == "200"
    _, from_, ancestor, query, params = fake.calls[-1]
    assert (from_, ancestor) == ("enterprise_777.docInvoice", "200")
    assert (query, params) == ("po_number ILIKE :v0", {"v0": "PO-1"})


def test_find_escapes_wildcards_and_rejects_unsafe_keys():
    fake = FakeBox()
    adapter = _adapter(fake, enterprise_id="5")
    adapter.find("invoice", {"po_number": "50%_off"})
    assert fake.calls[-1][2:] == (ROOT, "po_number ILIKE :v0", {"v0": r"50\%\_off"})
    with pytest.raises(ValueError):
        adapter.find("invoice", {"po_number = 'x' OR 1": "y"})
    with pytest.raises(ValueError):
        adapter.find("receipt", {"po_number": "x"})


def test_comments_round_trip():
    fake = FakeBox()
    adapter = _adapter(fake)
    adapter.post_comment(_ref(), "Total does not match the PO.")
    assert list(adapter.list_comments(_ref())) == ["Total does not match the PO."]


def test_no_destructive_calls_exist():
    import inspect

    import docreview.ecm.box as box

    source = inspect.getsource(box)
    for call in ("delete_", "update_file_by_id", "uploads.", "copy_file", "create_folder"):
        assert call not in source
