"""Example adapter, not tested against a live system: Amazon S3 through boto3.

``pip install open-doc-review[s3]`` (boto3). Written from the S3 API reference and tested against a fake
client only. Setup, the IAM policy and every assumption: docs/S3.md.

S3 objects are immutable and changing an object's metadata means copying it over itself, which this
adapter never does. Its namespaces and comments live in a sidecar object ``<key>.docreview.json`` (under
``sidecar_prefix``), as the local adapter keeps them in a sidecar file. The only write is a ``PutObject``
of a sidecar; it never puts, copies or deletes a document. Everything it acts on is under ``prefix``.
"""

from __future__ import annotations

import json
import logging
import mimetypes
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from docreview.ecm.base import NS_CLASSIFICATION, NS_FIELDS, NS_REVIEW
from docreview.models import DocumentContent, DocumentRef, Event

log = logging.getLogger("docreview")
SUFFIX = ".docreview.json"
_NAMESPACES = (NS_CLASSIFICATION, NS_FIELDS, NS_REVIEW)
_PUT_ATTEMPTS = 3


def _code(exc: Exception) -> str:
    """The S3 error code of a botocore ``ClientError`` (or anything shaped like one)."""
    return str((getattr(exc, "response", None) or {}).get("Error", {}).get("Code", ""))


def _norm(value: Any) -> str:
    return str(value).strip().casefold()


def _same(found: Any, wanted: str) -> bool:
    if isinstance(found, (int, float)) and not isinstance(found, bool):
        try:
            return float(found) == float(wanted)
        except ValueError:
            return False
    return found is not None and _norm(found) == _norm(wanted)


def _folder(key: str) -> str:
    return key.rsplit("/", 1)[0] + "/" if "/" in key else ""


class S3Adapter:
    """Documents under ``prefix`` of one bucket. A document's id is its object key.

    ``version``: the object's ETag, or its VersionId with ``use_version_id: true`` on a versioned bucket.
    """

    name = "s3"

    @classmethod
    def render_schema(cls, schema, options, match_fields) -> str:  # noqa: ANN001 - see docreview.schema
        """Nothing to create in S3: the sidecar shape, and an IAM policy that allows only sidecar writes."""
        from docreview.ecm._render import header, key_lines

        o = options or {}
        bucket, prefix = o.get("bucket") or "<bucket>", o.get("prefix") or ""
        sidecars = prefix if o.get("sidecar_prefix") is None else o["sidecar_prefix"]
        lines = [header("Amazon S3", f"Nothing to create in S3: results go in <key>{SUFFIX} sidecar objects.")]
        for ns in schema:
            label = f'"{ns.name}"' + (f" (when doc_type is {ns.doc_type!r})" if ns.doc_type else "")
            lines.append(f"# {label}: {ns.description}")
            lines += key_lines(ns)
        lines.append('# "comments": a list of {"at": ..., "text": ...} entries.')
        lines.append("#\n# IAM policy for the adapter's role (reads under prefix; writes only sidecars):")
        listed = sorted({f"{p}*" for p in (prefix, sidecars)})
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:ListBucket", "s3:ListBucketVersions"],
                    "Resource": f"arn:aws:s3:::{bucket}",
                    "Condition": {"StringLike": {"s3:prefix": listed}},
                },
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:GetObjectVersion"],
                    "Resource": sorted({f"arn:aws:s3:::{bucket}/{p}*" for p in (prefix, sidecars)}),
                },
                {"Effect": "Allow", "Action": "s3:PutObject", "Resource": f"arn:aws:s3:::{bucket}/{sidecars}*{SUFFIX}"},
            ],
        }
        lines.append(json.dumps(policy, indent=2))
        lines.append("# Add kms:Decrypt and kms:GenerateDataKey when the bucket uses SSE-KMS.")
        return "\n".join(lines)

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        inbox_prefix: str | None = None,
        sidecar_prefix: str | None = None,
        recursive: bool = False,
        use_version_id: bool = False,
        region: str | None = None,
        client: Any = None,
        _base_dir: Any = None,
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix
        self.inbox_prefix = prefix if inbox_prefix is None else inbox_prefix
        self.sidecar_prefix = prefix if sidecar_prefix is None else sidecar_prefix
        for name, value in (
            ("prefix", prefix),
            ("inbox_prefix", self.inbox_prefix),
            ("sidecar_prefix", self.sidecar_prefix),
        ):
            if value and not value.endswith("/"):  # "lib" would also cover "library/..."
                raise ValueError(f"s3: {name} {value!r} must be empty or end in '/'")
        if not self.inbox_prefix.startswith(self.prefix):
            raise ValueError(f"s3: inbox_prefix {self.inbox_prefix!r} is not inside prefix {self.prefix!r}")
        self.recursive, self.use_version_id = recursive, use_version_id
        if client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - depends on the environment
                raise ImportError("the S3 adapter needs boto3: pip install 'open-doc-review[s3]'") from exc
            client = boto3.client("s3", region_name=region)
        self._s3 = client

    # --- keys and scope -------------------------------------------------------------------------
    def _is_document(self, key: str) -> bool:
        return key.startswith(self.prefix) and not key.endswith("/") and not key.endswith(SUFFIX)

    def _scoped(self, key: str) -> None:
        if not self._is_document(key):
            raise LookupError(f"s3: {key!r} is not a document under {self.prefix!r}")

    def _sidecar_key(self, key: str) -> str:
        return self.sidecar_prefix + key[len(self.prefix) :] + SUFFIX

    def _document_key(self, sidecar_key: str) -> str:
        return self.prefix + sidecar_key[len(self.sidecar_prefix) : -len(SUFFIX)]

    def _ref(self, key: str, etag: Any, version_id: Any, modified: Any) -> DocumentRef:
        version = version_id if self.use_version_id else (str(etag).strip('"') if etag else None)
        return DocumentRef(
            id=key,
            name=key.rsplit("/", 1)[-1],
            version=version,
            modified_at=modified if isinstance(modified, datetime) else None,
            parent_id=_folder(key),
            location=_folder(key) or "/",
        )

    def _head(self, key: str) -> DocumentRef:
        self._scoped(key)
        try:
            h = self._s3.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if _code(exc) in ("404", "NoSuchKey", "NotFound"):
                raise LookupError(f"s3: no object {key!r}") from None
            raise
        return self._ref(key, h.get("ETag"), h.get("VersionId"), h.get("LastModified"))

    def _list(self, prefix: str, *, recursive: bool) -> Iterable[dict[str, Any]]:
        args: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
        if not recursive:
            args["Delimiter"] = "/"
        while True:
            page = self._s3.list_objects_v2(**args)
            yield from page.get("Contents") or []
            if not page.get("IsTruncated"):
                return
            args["ContinuationToken"] = page["NextContinuationToken"]

    def _documents(self, prefix: str, *, recursive: bool) -> Iterable[DocumentRef]:
        """Current documents; a versioned listing is needed for VersionIds (``list_objects_v2`` has none)."""
        if not self.use_version_id:
            for o in self._list(prefix, recursive=recursive):
                if self._is_document(o["Key"]):
                    yield self._ref(o["Key"], o.get("ETag"), None, o.get("LastModified"))
            return
        args: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix, **({} if recursive else {"Delimiter": "/"})}
        while True:
            page = self._s3.list_object_versions(**args)
            for v in page.get("Versions") or []:
                if v.get("IsLatest") and self._is_document(v["Key"]):
                    yield self._ref(v["Key"], v.get("ETag"), v.get("VersionId"), v.get("LastModified"))
            if not page.get("IsTruncated"):
                return
            args.update(KeyMarker=page.get("NextKeyMarker"), VersionIdMarker=page.get("NextVersionIdMarker"))

    # --- the sidecar ----------------------------------------------------------------------------
    def _load(self, key: str) -> tuple[dict[str, Any], str | None]:
        """The sidecar's content and ETag; ``({}, None)`` when there is none."""
        try:
            obj = self._s3.get_object(Bucket=self.bucket, Key=self._sidecar_key(key))
        except Exception as exc:
            if _code(exc) in ("404", "NoSuchKey", "NotFound"):
                return {}, None
            raise
        data = json.loads(obj["Body"].read() or b"{}")
        return (data if isinstance(data, dict) else {}), obj.get("ETag")

    def _save(self, key: str, change: Callable[[dict[str, Any]], None]) -> None:
        """Read-change-write of the sidecar with a conditional put, so a concurrent edit is never lost."""
        for attempt in range(_PUT_ATTEMPTS):
            data, etag = self._load(key)
            change(data)
            cond = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
            body = json.dumps(data, indent=2, sort_keys=True, default=str).encode()
            try:
                self._s3.put_object(
                    Bucket=self.bucket, Key=self._sidecar_key(key), Body=body, ContentType="application/json", **cond
                )
                return
            except Exception as exc:
                if _code(exc) not in ("PreconditionFailed", "412", "ConditionalRequestConflict") or (
                    attempt == _PUT_ATTEMPTS - 1
                ):
                    raise

    # --- port -----------------------------------------------------------------------------------
    def poll(self, cursor: str | None) -> tuple[list[Event], str | None]:
        events = []
        for ref in self._documents(self.inbox_prefix, recursive=self.recursive):
            try:
                data, _ = self._load(ref.id)
            except ValueError as exc:  # a hand-edited sidecar that is not JSON: skip it, as the local adapter does
                log.warning("skipping %s: unreadable sidecar (%s)", ref.id, exc)
                continue
            if (data.get(NS_REVIEW) or {}).get("requested") is True:
                events.append(Event("review_requested", ref))
            elif (data.get(NS_CLASSIFICATION) or {}).get("processed_version") != ref.version:
                events.append(Event("new_document", ref))  # new, changed, or not yet processed successfully
        return events, None  # stateless: the sidecars are the state

    def get(self, document_id: str) -> DocumentRef:
        return self._head(document_id)

    def download(self, ref: DocumentRef) -> DocumentContent:
        self._scoped(ref.id)
        args: dict[str, Any] = {"Bucket": self.bucket, "Key": ref.id}
        if ref.version:  # pinned: the exact version, or the request fails rather than read newer bytes
            args["VersionId" if self.use_version_id else "IfMatch"] = ref.version
        try:
            obj = self._s3.get_object(**args)
        except Exception as exc:
            if _code(exc) in ("PreconditionFailed", "412"):
                raise RuntimeError(f"s3: {ref.name} changed since it was listed (ETag {ref.version})") from None
            raise
        media = obj.get("ContentType")
        if not media or media in ("binary/octet-stream", "application/octet-stream"):
            media = mimetypes.guess_type(ref.name)[0] or "application/octet-stream"
        return DocumentContent(ref=ref, data=obj["Body"].read(), media_type=media)

    def read_metadata(self, ref: DocumentRef, namespace: str) -> dict[str, Any]:
        if namespace not in _NAMESPACES:
            raise ValueError(f"s3: unknown namespace {namespace!r}")
        self._scoped(ref.id)
        return dict(self._load(ref.id)[0].get(namespace) or {})

    def find(
        self, doc_type: str, criteria: Mapping[str, str], *, scope: str | None = None, limit: int = 20
    ) -> list[DocumentRef]:
        """Reads every sidecar under ``sidecar_prefix``: fine for small libraries (see docs/S3.md)."""
        hits = []
        for o in self._list(self.sidecar_prefix, recursive=True):
            if not o["Key"].endswith(SUFFIX):
                continue
            key = self._document_key(o["Key"])
            if not self._is_document(key) or (scope is not None and _folder(key) != scope):
                continue
            try:
                data, _ = self._load(key)
            except ValueError as exc:
                log.warning("skipping %s: unreadable sidecar (%s)", key, exc)
                continue
            fields = data.get(NS_FIELDS) or {}
            if _same((data.get(NS_CLASSIFICATION) or {}).get("doc_type"), doc_type) and all(
                _same(fields.get(k), v) for k, v in criteria.items()
            ):
                try:
                    hits.append(self._head(key))
                except LookupError:  # the document was removed and its sidecar left behind
                    continue
        hits.sort(key=lambda r: r.modified_at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return hits[:limit]

    def write_metadata(self, ref: DocumentRef, namespace: str, values: Mapping[str, Any]) -> None:
        self._scoped(ref.id)
        if namespace not in _NAMESPACES:
            raise ValueError(f"s3: unknown namespace {namespace!r}")

        def change(data: dict[str, Any]) -> None:
            merged = {**(data.get(namespace) or {}), **values}  # a write merges into what is there
            data[namespace] = {k: v for k, v in merged.items() if v is not None}  # None removes a key

        self._save(ref.id, change)

    def post_comment(self, ref: DocumentRef, text: str) -> None:
        self._scoped(ref.id)
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        self._save(ref.id, lambda data: data.setdefault("comments", []).append({"at": stamp, "text": text}))

    def list_comments(self, ref: DocumentRef) -> Iterable[str]:
        self._scoped(ref.id)
        return [c.get("text", "") for c in self._load(ref.id)[0].get("comments") or []]

    # --- setup check (called by ``docreview check``) -----------------------------------------------
    def check(self, doc_types: list[str]) -> list[str]:
        """Problems with credentials, the bucket and its versioning; an empty list means ready."""
        try:
            self._s3.head_bucket(Bucket=self.bucket)
        except Exception as exc:
            return [f"s3: cannot reach bucket {self.bucket!r} ({exc}); check credentials, region and policy"]
        problems = []
        try:
            self._s3.list_objects_v2(Bucket=self.bucket, Prefix=self.inbox_prefix, MaxKeys=1)
        except Exception as exc:
            problems.append(f"s3: cannot list {self.inbox_prefix!r} ({exc}); the policy needs s3:ListBucket")
        if self.use_version_id:
            status = self._s3.get_bucket_versioning(Bucket=self.bucket).get("Status")
            if status != "Enabled":
                problems.append(f"s3: use_version_id needs a versioned bucket (versioning is {status or 'off'})")
        return problems
