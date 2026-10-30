"""Server-only immutable objects in a private CloudBase PG Storage bucket.

No public URL, signing, listing, overwrite, delete or local-disk fallback is
provided. Callers must enforce the existing admin/player material permissions
before forwarding bytes. A locator is not an authorization credential.

Paths were checked against @cloudbase/manager-node 5.9.4 uploadObject and
downloadAuthenticatedObject; PG Storage is not the classic COS upload API.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from dataclasses import asdict
from hashlib import sha256
import hmac
import json
import re
from pathlib import Path
from tempfile import TemporaryDirectory

from src.fusion.package_validation import canonical_json, content_hash, parse_package_json
from src.fusion.source_bundles import MAX_BUNDLE_BYTES, SourceBundleStore, read_beneath
from src.schemas.source_bundle import SourceManifest

from .gateway import CloudBaseGateway, GatewayError

PROJECT_PREFIX = "veil-archive/private/v1"
MAX_BLOB_BYTES = 16 * 1024 * 1024
_KINDS = frozenset({"source", "creative", "report", "asset", "speech"})
_MEDIA_TYPES = frozenset({"application/json", "application/octet-stream", "text/plain", "text/markdown",
                          "text/csv", "image/png", "image/jpeg", "image/webp", "audio/mpeg", "audio/wav"})


class PrivateStorageError(ValueError):
    def __init__(self, code: str, status: int = 503):
        self.code, self.status = code, status
        super().__init__(code)


@dataclass(frozen=True)
class BlobRef:
    bucket: str
    key: str
    sha256: str
    size_bytes: int
    media_type: str


class PrivateBlobStore:
    def __init__(self, gateway: CloudBaseGateway, bucket: str):
        if not isinstance(bucket, str) or not re.fullmatch(r"[a-z][a-z0-9-]{2,62}", bucket):
            raise PrivateStorageError("PRIVATE_STORAGE_BUCKET_INVALID", 400)
        self.gateway, self.bucket = gateway, bucket

    @staticmethod
    def _digest(digest: str) -> str:
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise PrivateStorageError("PRIVATE_STORAGE_DIGEST_INVALID", 400)
        return digest

    def reference(self, kind: str,
                  digest: str, size_bytes: int, media_type: str) -> BlobRef:
        self._digest(digest)
        if (not isinstance(kind, str) or kind not in _KINDS or type(size_bytes) is not int
                or not 0 <= size_bytes <= MAX_BLOB_BYTES or not isinstance(media_type, str)
                or media_type not in _MEDIA_TYPES):
            raise PrivateStorageError("PRIVATE_STORAGE_REFERENCE_INVALID", 400)
        return BlobRef(self.bucket, f"{PROJECT_PREFIX}/{kind}/sha256/{digest[:2]}/{digest}",
                       digest, size_bytes, media_type)

    def _validate_reference(self, ref: BlobRef) -> None:
        if not isinstance(ref, BlobRef) or ref.bucket != self.bucket:
            raise PrivateStorageError("PRIVATE_STORAGE_REFERENCE_INVALID", 400)
        kind = ref.key.split("/")[3] if isinstance(ref.key, str) and len(ref.key.split("/")) == 7 else None
        if kind not in _KINDS or ref != self.reference(kind, ref.sha256, ref.size_bytes, ref.media_type):
            raise PrivateStorageError("PRIVATE_STORAGE_REFERENCE_INVALID", 400)

    async def _request(self, method, path, **kwargs):
        try:
            return await self.gateway.request(method, path, **kwargs)
        except GatewayError:
            raise PrivateStorageError("PRIVATE_STORAGE_UNAVAILABLE") from None

    def _check_private_bucket(self, response) -> None:
        if response.status != 200:
            raise PrivateStorageError("PRIVATE_STORAGE_UNAVAILABLE")
        try:
            document = json.loads(response.body)
        except (ValueError, UnicodeError):
            raise PrivateStorageError("PRIVATE_STORAGE_CONFIGURATION_INVALID") from None
        if not isinstance(document, dict) or document.get("id") != self.bucket or document.get("public") is not False:
            raise PrivateStorageError("PRIVATE_STORAGE_REQUIRES_PRIVATE_BUCKET")

    async def _require_private_bucket(self) -> None:
        response = await self._request("GET", f"/v1/storages/bucket/{self.bucket}", max_response_bytes=16384)
        self._check_private_bucket(response)

    async def create_private_bucket(self) -> bool:
        """Explicit operator helper: create if absent, never change an existing bucket.

        Returns whether POST created the bucket. An existing private bucket is
        accepted unchanged. Public/ambiguous buckets and unconfirmed writes fail
        closed; no PATCH, PUT, policy changes, retries or public access are used.
        Official HTTP example: https://docs.cloudbase.net/en/storage/pg/quickstart
        """
        response = await self._request("GET", f"/v1/storages/bucket/{self.bucket}", max_response_bytes=16384)
        if response.status == 200:
            self._check_private_bucket(response)
            return False
        if response.status != 404:
            raise PrivateStorageError("PRIVATE_STORAGE_UNAVAILABLE")
        created = await self._request("POST", "/v1/storages/bucket/", json_body={
            "id": self.bucket, "name": self.bucket, "public": False,
            "file_size_limit": MAX_BLOB_BYTES, "allowed_mime_types": sorted(_MEDIA_TYPES),
        }, max_response_bytes=16384)
        if created.status not in {200, 201, 409}:
            raise PrivateStorageError("PRIVATE_STORAGE_BUCKET_CREATE_UNCONFIRMED")
        await self._require_private_bucket()
        return created.status != 409

    async def put_immutable(self, data: bytes, *, kind="source", media_type="application/octet-stream",
                            expected_sha256: str | None = None) -> BlobRef:
        if not isinstance(data, bytes) or len(data) > MAX_BLOB_BYTES:
            raise PrivateStorageError("PRIVATE_STORAGE_SIZE_INVALID", 400)
        digest = sha256(data).hexdigest()
        if expected_sha256 is not None and not hmac.compare_digest(self._digest(expected_sha256), digest):
            raise PrivateStorageError("PRIVATE_STORAGE_INTEGRITY_FAILED", 409)
        ref = self.reference(kind, digest, len(data), media_type)
        await self._require_private_bucket()
        response = await self._request("POST", f"/v1/storages/object/{self.bucket}/{ref.key}", body=data,
            headers={"Content-Type": media_type, "x-upsert": "false",
                     "Cache-Control": "private, no-store", "X-Robots-Tag": "noindex, nofollow"},
            max_response_bytes=16384)
        if response.status not in {200, 201, 409}:
            raise PrivateStorageError("PRIVATE_STORAGE_WRITE_UNCONFIRMED")
        if response.status != 409:
            try:
                document = json.loads(response.body)
            except (ValueError, UnicodeError):
                raise PrivateStorageError("PRIVATE_STORAGE_WRITE_UNCONFIRMED") from None
            if (not isinstance(document, dict) or not isinstance(document.get("Id"), str) or not document["Id"]
                    or document.get("Key") not in {ref.key, f"{self.bucket}/{ref.key}"}):
                raise PrivateStorageError("PRIVATE_STORAGE_WRITE_UNCONFIRMED")
        # Neither an upload acknowledgment nor an existing key proves bytes.
        await self.read_verified(ref)
        return ref

    async def read_verified(self, ref: BlobRef) -> bytes:
        self._validate_reference(ref)
        await self._require_private_bucket()
        response = await self._request("GET", f"/v1/storages/object/authenticated/{self.bucket}/{ref.key}",
                                       max_response_bytes=max(1, ref.size_bytes))
        if response.status == 404:
            raise PrivateStorageError("PRIVATE_STORAGE_OBJECT_MISSING", 404)
        if response.status != 200:
            raise PrivateStorageError("PRIVATE_STORAGE_UNAVAILABLE")
        if (len(response.body) != ref.size_bytes
                or not hmac.compare_digest(sha256(response.body).hexdigest(), ref.sha256)):
            raise PrivateStorageError("PRIVATE_STORAGE_INTEGRITY_FAILED", 409)
        return response.body

    async def archive_source_bundle(self, source_store: SourceBundleStore, bundle_hash: str,
                                    report_hashes: tuple[str, ...] = ()) -> dict:
        """Explicit internal bootstrap/import; never expose as a public endpoint.

        Returns references for a subsequent PG CAS. Object upload alone never
        registers or publishes a source bundle. Original hashes remain intact.
        """
        manifest = source_store.manifest(self._digest(bundle_hash))
        manifest_ref = await self.put_immutable(canonical_json(manifest.model_dump()).encode(), kind="source",
                                               media_type="application/json", expected_sha256=bundle_hash)
        files = {}
        for entry in manifest.sources:
            data, media_type = source_store.read_source(bundle_hash, entry.id)
            files[entry.id] = asdict(await self.put_immutable(data, kind="source", media_type=media_type,
                                                            expected_sha256=entry.sha256))
        reports = {}
        if len(report_hashes) > 100:
            raise PrivateStorageError("PRIVATE_SOURCE_ARCHIVE_INVALID", 400)
        for digest in report_hashes:
            report = source_store.get_report(self._digest(digest))
            if report["bundle_hash"] != bundle_hash:
                raise PrivateStorageError("PRIVATE_SOURCE_ARCHIVE_INVALID", 400)
            report = {key:value for key,value in report.items() if key != "report_hash"}
            reports[digest] = asdict(await self.put_immutable(canonical_json(report).encode(), kind="report",
                                                            media_type="application/json", expected_sha256=digest))
        return {"bundle_hash":bundle_hash,"manifest_ref":asdict(manifest_ref),"file_refs":files,"report_refs":reports}

    @asynccontextmanager
    async def source_workspace(self, bundle_hash: str, archive: dict):
        """Rehydrate only verified references in a private, disposable cache.

        archive comes from the backend's source/admin aggregate, never a raw
        player/candidate request. Existing SourceBundleStore checks remain the
        authority; this bridge supplies persistence, not a verification verdict.
        """
        self._digest(bundle_hash)
        if (not isinstance(archive, dict) or set(archive) != {"bundle_hash","manifest_ref","file_refs","report_refs"}
                or archive["bundle_hash"] != bundle_hash or not isinstance(archive["file_refs"],dict)
                or not isinstance(archive["report_refs"],dict) or len(archive["report_refs"])>100):
            raise PrivateStorageError("PRIVATE_SOURCE_ARCHIVE_INVALID", 400)
        try:
            manifest_ref = _archive_ref(self, archive["manifest_ref"], "source", maximum=2*1024*1024)
            if manifest_ref.sha256 != bundle_hash or manifest_ref.media_type != "application/json":
                raise PrivateStorageError("PRIVATE_SOURCE_ARCHIVE_INVALID",400)
            manifest_bytes = await self.read_verified(manifest_ref)
            document = parse_package_json(manifest_bytes)
            manifest = SourceManifest.model_validate(document)
            if content_hash(document) != bundle_hash or sum(entry.size_bytes for entry in manifest.sources)>MAX_BUNDLE_BYTES:
                raise PrivateStorageError("PRIVATE_SOURCE_ARCHIVE_INVALID",400)
            if set(archive["file_refs"]) != {entry.id for entry in manifest.sources}:
                raise PrivateStorageError("PRIVATE_SOURCE_ARCHIVE_INVALID",400)
        except (ValueError,TypeError,KeyError) as error:
            if isinstance(error,PrivateStorageError): raise
            raise PrivateStorageError("PRIVATE_SOURCE_ARCHIVE_INVALID",400) from None
        with TemporaryDirectory(prefix="veil-private-source-") as temporary:
            root = Path(temporary)/"sources"
            root.mkdir(mode=0o700)
            store = SourceBundleStore(root)
            _cache_write(root/bundle_hash/"manifest.json",manifest_bytes)
            try:
                for entry in manifest.sources:
                    ref = _archive_ref(self,archive["file_refs"][entry.id],"source")
                    if (ref.sha256,ref.size_bytes,ref.media_type)!=(entry.sha256,entry.size_bytes,entry.media_type):
                        raise PrivateStorageError("PRIVATE_SOURCE_ARCHIVE_INVALID",400)
                    _cache_write(root/bundle_hash/"files"/entry.relative_path,await self.read_verified(ref))
                for digest,raw_ref in archive["report_refs"].items():
                    self._digest(digest)
                    ref = _archive_ref(self,raw_ref,"report",maximum=2*1024*1024)
                    if ref.sha256 != digest or ref.media_type != "application/json":
                        raise PrivateStorageError("PRIVATE_SOURCE_ARCHIVE_INVALID",400)
                    _cache_write(root/"verification-reports"/(digest+".json"),await self.read_verified(ref))
                    if store.get_report(digest)["bundle_hash"] != bundle_hash:
                        raise PrivateStorageError("PRIVATE_SOURCE_ARCHIVE_INVALID",400)
                # Includes canonical IDs, paths, relations and original bytes.
                store.manifest(bundle_hash)
                for entry in manifest.sources:
                    store.read_source(bundle_hash,entry.id)
            except (ValueError,OSError,TypeError,KeyError) as error:
                if isinstance(error,PrivateStorageError): raise
                raise PrivateStorageError("PRIVATE_SOURCE_ARCHIVE_INVALID",400) from None
            yield SourceWorkspace(self,store,bundle_hash,dict(archive["report_refs"]))


def _archive_ref(store: PrivateBlobStore, value: dict, kind: str, maximum: int = MAX_BLOB_BYTES) -> BlobRef:
    try:
        if not isinstance(value,dict) or set(value)!={"bucket","key","sha256","size_bytes","media_type"}:
            raise ValueError
        ref = BlobRef(**value)
        store._validate_reference(ref)
        if ref.size_bytes>maximum or not ref.key.startswith(f"{PROJECT_PREFIX}/{kind}/"):
            raise ValueError
        return ref
    except (ValueError,TypeError):
        raise PrivateStorageError("PRIVATE_SOURCE_ARCHIVE_INVALID",400) from None


def _cache_write(path: Path,data: bytes) -> None:
    """Only inside a fresh 0700 TemporaryDirectory with validated relative paths."""
    import os
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,"wb") as stream:
        stream.write(data)


@dataclass
class SourceWorkspace:
    blobs: PrivateBlobStore
    store: SourceBundleStore
    bundle_hash: str
    report_refs: dict

    async def persist_reports(self) -> dict:
        """Return all report refs; caller must PG-CAS them with review changes."""
        directory = self.store.root/"verification-reports"
        if not directory.exists(): return dict(self.report_refs)
        if directory.is_symlink() or not directory.is_dir():
            raise PrivateStorageError("PRIVATE_SOURCE_ARCHIVE_INVALID",400)
        refs=dict(self.report_refs)
        for path in sorted(directory.iterdir()):
            if not re.fullmatch(r"[a-f0-9]{64}\.json",path.name):
                raise PrivateStorageError("PRIVATE_SOURCE_ARCHIVE_INVALID",400)
            digest=path.stem
            try:
                report=self.store.get_report(digest)
                if report["bundle_hash"] != self.bundle_hash: raise ValueError
                data=read_beneath(self.store.root,"verification-reports/"+path.name,2*1024*1024)
            except (ValueError,OSError):
                raise PrivateStorageError("PRIVATE_SOURCE_ARCHIVE_INVALID",400) from None
            if digest not in refs:
                refs[digest]=asdict(await self.blobs.put_immutable(data,kind="report",media_type="application/json",expected_sha256=digest))
        self.report_refs=refs
        return dict(refs)
