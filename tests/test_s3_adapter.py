"""S3Adapter against a fake S3 client (no boto3, no network)."""

from __future__ import annotations

import hashlib
import inspect
import io
import json
import re
from datetime import UTC, datetime

import pytest

from docreview.ecm import ECMAdapter
from docreview.ecm.base import NS_CLASSIFICATION, NS_FIELDS, NS_REVIEW
from docreview.ecm.s3 import SUFFIX, S3Adapter
from docreview.models import DocumentRef


class ClientError(Exception):
    """Shaped like botocore's ClientError: the code is in ``response["Error"]["Code"]``."""

    def __init__(self, code: str):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """One bucket, in memory. Every call is recorded as (operation, kwargs)."""

    def __init__(self, versioned: bool = False):
        self.objects: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.versioned = versioned
        self.page_size = 1000
        self.race_once = False

    def add(self, key, data=b"%PDF", content_type="application/pdf", day=1):
        etag = '"' + hashlib.md5(data).hexdigest() + '"'
        version = f"v{len([k for k in self.calls if k[0] == 'put'])}-{key}-{day}"
        self.objects[key] = {
            "Body": data,
            "ETag": etag,
            "VersionId": version,
            "ContentType": content_type,
            "LastModified": datetime(2026, 1, day, tzinfo=UTC),
        }

    def _keys(self, Prefix="", Delimiter=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return [k for k in keys if not (Delimiter and Delimiter in k[len(Prefix) :])]

    def list_objects_v2(self, Bucket, Prefix="", Delimiter=None, ContinuationToken=None, MaxKeys=None):
        self.calls.append(("list_objects_v2", {"Prefix": Prefix, "Delimiter": Delimiter}))
        keys = self._keys(Prefix, Delimiter)
        start = int(ContinuationToken or 0)
        page = keys[start : start + self.page_size]
        more = start + self.page_size < len(keys)
        contents = [{"Key": k, **{f: self.objects[k][f] for f in ("ETag", "LastModified")}} for k in page]
        return {"Contents": contents, "IsTruncated": more, "NextContinuationToken": str(start + self.page_size)}

    def list_object_versions(self, Bucket, Prefix="", Delimiter=None, KeyMarker=None, VersionIdMarker=None):
        self.calls.append(("list_object_versions", {"Prefix": Prefix}))
        rows = []
        for k in self._keys(Prefix, Delimiter):
            o = self.objects[k]
            rows.append({"Key": k, "IsLatest": True, **{f: o[f] for f in ("ETag", "VersionId", "LastModified")}})
            rows.append({"Key": k, "IsLatest": False, "ETag": '"old"', "VersionId": "old", "LastModified": None})
        return {"Versions": rows, "IsTruncated": False}

    def head_object(self, Bucket, Key):
        self.calls.append(("head", {"Key": Key}))
        if Key not in self.objects:
            raise ClientError("404")
        return {k: v for k, v in self.objects[Key].items() if k != "Body"}

    def get_object(self, Bucket, Key, IfMatch=None, VersionId=None):
        self.calls.append(("get", {"Key": Key, "IfMatch": IfMatch, "VersionId": VersionId}))
        o = self.objects.get(Key)
        if o is None:
            raise ClientError("NoSuchKey")
        if IfMatch and IfMatch.strip('"') != o["ETag"].strip('"'):
            raise ClientError("PreconditionFailed")
        if VersionId and VersionId != o["VersionId"]:
            raise ClientError("NoSuchVersion")
        return {**o, "Body": io.BytesIO(o["Body"])}

    def put_object(self, Bucket, Key, Body, ContentType=None, IfMatch=None, IfNoneMatch=None):
        self.calls.append(("put", {"Key": Key, "IfMatch": IfMatch, "IfNoneMatch": IfNoneMatch}))
        if self.race_once:  # someone else wrote the sidecar between our read and our put
            self.race_once = False
            raise ClientError("PreconditionFailed")
        current = self.objects.get(Key)
        if (IfNoneMatch == "*" and current) or (IfMatch and (not current or current["ETag"] != IfMatch)):
            raise ClientError("PreconditionFailed")
        self.add(Key, Body, ContentType)
        return {"ETag": self.objects[Key]["ETag"]}

    def head_bucket(self, Bucket):
        if Bucket != "docs":
            raise ClientError("404")
        return {}

    def get_bucket_versioning(self, Bucket):
        return {"Status": "Enabled"} if self.versioned else {}


@pytest.fixture
def s3():
    fake = FakeS3()
    fake.add("lib/inbox/a.pdf", b"%PDF-a", day=1)
    fake.add("lib/inbox/b.txt", b"hello", content_type="binary/octet-stream", day=2)
    fake.add("lib/inbox/sub/deep.pdf", b"%PDF-deep", day=3)
    fake.add("lib/po/po-1.pdf", b"%PDF-po", day=4)
    fake.add("other/out.pdf", b"%PDF-out", day=5)
    return fake


def _adapter(fake, **opts):
    return S3Adapter(bucket="docs", prefix="lib/", inbox_prefix="lib/inbox/", client=fake, _base_dir=".", **opts)


def _sidecar(fake, key):
    return json.loads(fake.objects[key + SUFFIX]["Body"])


def test_implements_the_port_and_scopes_settings(s3):
    assert isinstance(_adapter(s3), ECMAdapter)
    with pytest.raises(ValueError, match="not inside"):
        S3Adapter(bucket="docs", prefix="lib/", inbox_prefix="other/", client=s3)


def test_get_is_scoped_and_uses_the_etag(s3):
    adapter = _adapter(s3)
    ref = adapter.get("lib/inbox/a.pdf")
    assert ref.version == hashlib.md5(b"%PDF-a").hexdigest() and ref.parent_id == "lib/inbox/"
    for key in ("other/out.pdf", "lib/inbox/missing.pdf", "lib/inbox/a.pdf" + SUFFIX):
        with pytest.raises(LookupError):
            adapter.get(key)


def test_poll_offers_unprocessed_versions_and_review_requests(s3):
    adapter = _adapter(s3)
    a, b = adapter.get("lib/inbox/a.pdf"), adapter.get("lib/inbox/b.txt")
    adapter.write_metadata(a, NS_CLASSIFICATION, {"processed_version": a.version})
    adapter.write_metadata(b, NS_CLASSIFICATION, {"processed_version": "old"})
    events, cursor = adapter.poll(None)
    assert [(e.kind, e.ref.id) for e in events] == [("new_document", "lib/inbox/b.txt")] and cursor is None
    adapter.write_metadata(a, NS_REVIEW, {"requested": True})
    kinds = [(e.kind, e.ref.id) for e in adapter.poll(None)[0]]
    assert kinds == [("review_requested", "lib/inbox/a.pdf"), ("new_document", "lib/inbox/b.txt")]
    assert "lib/inbox/sub/deep.pdf" in [e.ref.id for e in _adapter(s3, recursive=True).poll(None)[0]]


def test_poll_follows_pages(s3):
    s3.page_size = 1
    assert len(_adapter(s3).poll(None)[0]) == 2


def test_download_is_pinned_to_the_version(s3):
    adapter = _adapter(s3)
    ref = adapter.get("lib/inbox/b.txt")
    content = adapter.download(ref)
    assert (content.data, content.media_type) == (b"hello", "text/plain")
    assert s3.calls[-1] == ("get", {"Key": "lib/inbox/b.txt", "IfMatch": ref.version, "VersionId": None})
    s3.add("lib/inbox/b.txt", b"changed")
    with pytest.raises(RuntimeError, match="changed"):
        adapter.download(ref)
    with pytest.raises(LookupError):
        adapter.download(DocumentRef(id="other/out.pdf", name="out.pdf"))


def test_version_id_mode_lists_versions_and_pins_them():
    fake = FakeS3(versioned=True)
    fake.add("lib/inbox/a.pdf")
    adapter = _adapter(fake, use_version_id=True)
    (event,) = adapter.poll(None)[0]
    assert event.ref.version == fake.objects["lib/inbox/a.pdf"]["VersionId"]
    adapter.download(event.ref)
    assert fake.calls[-1][1]["VersionId"] == event.ref.version and fake.calls[-1][1]["IfMatch"] is None
    assert adapter.check([]) == []
    assert "versioned bucket" in _adapter(FakeS3(), use_version_id=True).check([])[0]


def test_writes_merge_none_removes_and_only_the_sidecar_is_put(s3):
    adapter = _adapter(s3)
    ref = adapter.get("lib/inbox/a.pdf")
    adapter.write_metadata(ref, NS_CLASSIFICATION, {"doc_type": "invoice", "run_id": "r1"})
    adapter.write_metadata(ref, NS_CLASSIFICATION, {"failed_version": "x", "failure_count": 1, "run_id": None})
    assert adapter.read_metadata(ref, NS_CLASSIFICATION) == {
        "doc_type": "invoice",
        "failed_version": "x",
        "failure_count": 1,
    }
    assert s3.objects["lib/inbox/a.pdf"]["Body"] == b"%PDF-a"  # the document is untouched
    puts = [c for c in s3.calls if c[0] == "put"]
    assert {c[1]["Key"] for c in puts} == {"lib/inbox/a.pdf" + SUFFIX}
    assert puts[0][1]["IfNoneMatch"] == "*" and puts[1][1]["IfMatch"]  # conditional: create, then replace


def test_a_concurrent_sidecar_edit_is_retried_not_lost(s3):
    adapter = _adapter(s3)
    ref = adapter.get("lib/inbox/a.pdf")
    adapter.post_comment(ref, "First finding.")
    s3.race_once = True
    adapter.post_comment(ref, "Second finding.")
    assert list(adapter.list_comments(ref)) == ["First finding.", "Second finding."]


def test_sidecars_can_live_under_their_own_prefix(s3):
    adapter = _adapter(s3, sidecar_prefix="_review/")
    ref = adapter.get("lib/inbox/a.pdf")
    adapter.write_metadata(ref, NS_REVIEW, {"outcome": "no_findings"})
    assert _sidecar(s3, "_review/inbox/a.pdf") == {"review": {"outcome": "no_findings"}}


def test_find_reads_sidecars_normalises_and_scopes(s3):
    adapter = _adapter(s3)
    po, a = adapter.get("lib/po/po-1.pdf"), adapter.get("lib/inbox/a.pdf")
    for ref in (po, a):
        adapter.write_metadata(ref, NS_CLASSIFICATION, {"doc_type": "purchase_order"})
        adapter.write_metadata(ref, NS_FIELDS, {"po_number": " po-7730 ", "total": 12.5})
    hits = adapter.find("purchase_order", {"po_number": "PO-7730", "total": "12.50"})
    assert [r.id for r in hits] == ["lib/po/po-1.pdf", "lib/inbox/a.pdf"]  # newest first
    assert [r.id for r in adapter.find("purchase_order", {"po_number": "PO-7730"}, scope="lib/po/")] == [po.id]
    assert adapter.find("invoice", {"po_number": "PO-7730"}) == []
    del s3.objects["lib/po/po-1.pdf"]  # a sidecar left behind by a removed document is skipped
    assert [r.id for r in adapter.find("purchase_order", {"po_number": "PO-7730"})] == [a.id]


def test_prefixes_must_end_at_a_folder_boundary(s3):
    for opts in ({"prefix": "lib"}, {"prefix": "lib/", "inbox_prefix": "lib/in"}, {"sidecar_prefix": "_r"}):
        with pytest.raises(ValueError, match="end in '/'"):
            S3Adapter(bucket="docs", client=s3, **opts)
    assert S3Adapter(bucket="docs", prefix="", client=s3).prefix == ""


def test_a_corrupt_sidecar_is_skipped_by_poll_and_find(s3):
    adapter = _adapter(s3)
    b = adapter.get("lib/inbox/b.txt")
    adapter.write_metadata(b, NS_CLASSIFICATION, {"doc_type": "invoice"})
    adapter.write_metadata(b, NS_FIELDS, {"po_number": "PO-1"})
    s3.add("lib/inbox/a.pdf" + SUFFIX, b"{not json")
    assert [e.ref.id for e in adapter.poll(None)[0]] == ["lib/inbox/b.txt"]
    assert [r.id for r in adapter.find(" INVOICE ", {"po_number": "po-1"})] == [b.id]  # doc_type folded too


def test_metadata_and_comments_refuse_keys_outside_the_prefix(s3):
    adapter = _adapter(s3)
    outside = DocumentRef(id="other/out.pdf", name="out.pdf")
    for call in (
        lambda: adapter.read_metadata(outside, NS_CLASSIFICATION),
        lambda: adapter.write_metadata(outside, NS_CLASSIFICATION, {"doc_type": "x"}),
        lambda: adapter.post_comment(outside, "x"),
        lambda: adapter.list_comments(outside),
    ):
        with pytest.raises(LookupError):
            call()
    assert not [c for c in s3.calls if c[0] in ("get", "put")]


def test_check_reports_bucket_problems(s3):
    assert _adapter(s3).check(["invoice"]) == []
    bad = S3Adapter(bucket="nope", prefix="lib/", client=s3)
    assert "cannot reach bucket 'nope'" in bad.check([])[0]


def test_no_destructive_calls_exist():
    """S3 changes or removes a document through PutObject / CopyObject / DeleteObject(s), multipart
    uploads, restores and ACL or tagging writes. The adapter's only write is a PutObject of a sidecar."""
    import docreview.ecm.s3 as s3

    source = inspect.getsource(s3)
    calls = set(re.findall(r"self\._s3\.(\w+)\(", source))
    assert calls == {
        "head_object",
        "list_objects_v2",
        "list_object_versions",
        "get_object",
        "put_object",
        "head_bucket",
        "get_bucket_versioning",
    }
    assert source.count("self._s3.put_object(") == 1
    put = source.split("self._s3.put_object(", 1)[1].split(")", 1)[0]
    assert "Key=self._sidecar_key(key" in put
    code = source.split('"""', 2)[2].lower()  # after the module docstring, which names what it never does
    for word in ("delete", "copy", "upload", "multipart", "restore", "put_object_acl", "tagging"):
        assert word not in code


def test_templates_render_the_sidecar_shape_and_a_sidecar_only_policy():
    from pathlib import Path

    from docreview.config import load_config
    from docreview.ecm._render import header
    from docreview.schema import build_schema, match_fields

    cfg = load_config(Path(__file__).parents[1] / "examples" / "generic" / "docreview.yaml")
    schema = build_schema(cfg)
    out = S3Adapter.render_schema(schema, {"bucket": "docs", "prefix": "lib/"}, match_fields(cfg))
    assert out.startswith(header("Amazon S3", "x").splitlines()[0])
    for ns in schema:
        for key in ns.keys:
            assert key.name in out, key.name
    policy = json.loads(out[out.index("\n{") : out.rindex("}") + 1])
    put = [st for st in policy["Statement"] if st["Action"] == "s3:PutObject"]
    assert [st["Resource"] for st in put] == ["arn:aws:s3:::docs/lib/*.docreview.json"]
    assert "arn:aws:s3:::<bucket>" in S3Adapter.render_schema(schema, {}, {})  # placeholders without options
