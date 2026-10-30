"""No network/cloud credentials: exercise real gateway with a bounded transport."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.cloudbase.gateway import CloudBaseGateway, GatewayHTTPResponse
from src.cloudbase.storage import MAX_BLOB_BYTES, PrivateBlobStore, PrivateStorageError
from src.fusion.source_bundles import SourceBundleStore


BUCKET = "veil-private"
DATA = b"synthetic private source"
DIGEST = sha256(DATA).hexdigest()


class Transport:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def json_response(document, status=200):
    return GatewayHTTPResponse(status, json.dumps(document).encode())


def private_bucket():
    return json_response({"id": BUCKET, "public": False})


def setup_store(responses):
    transport = Transport(responses)
    gateway = CloudBaseGateway("test-env-123", "synthetic-server-only-key", transport=transport)
    return PrivateBlobStore(gateway, BUCKET), transport


class PrivateStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_operator_creates_only_absent_private_bucket_and_reads_configuration_back(self):
        store,transport=setup_store([json_response({},404),json_response({"name":BUCKET},201),private_bucket()])
        self.assertTrue(await store.create_private_bucket())
        self.assertEqual([call[0] for call in transport.calls],["GET","POST","GET"])
        created=transport.calls[1]
        self.assertTrue(created[1].endswith("/v1/storages/bucket/"))
        payload=json.loads(created[2]["body"])
        self.assertEqual(payload["id"],BUCKET)
        self.assertEqual(payload["name"],BUCKET)
        self.assertIs(payload["public"],False)
        self.assertEqual(payload["file_size_limit"],MAX_BLOB_BYTES)
        self.assertIn("application/json",payload["allowed_mime_types"])

    async def test_operator_never_updates_existing_bucket(self):
        store,transport=setup_store([private_bucket()])
        self.assertFalse(await store.create_private_bucket())
        self.assertEqual([call[0] for call in transport.calls],["GET"])
        for document in ({"id":BUCKET,"public":True},{"id":BUCKET},{"id":"another-bucket","public":False}):
            store,transport=setup_store([json_response(document)])
            with self.assertRaises(PrivateStorageError):
                await store.create_private_bucket()
            self.assertEqual([call[0] for call in transport.calls],["GET"])

    async def test_operator_rechecks_concurrent_create_and_refuses_public_configuration(self):
        store,transport=setup_store([json_response({},404),json_response({},409),private_bucket()])
        self.assertFalse(await store.create_private_bucket())
        self.assertEqual([call[0] for call in transport.calls],["GET","POST","GET"])
        store,_=setup_store([json_response({},404),json_response({},201),json_response({"id":BUCKET,"public":True})])
        with self.assertRaisesRegex(PrivateStorageError,"REQUIRES_PRIVATE_BUCKET"):
            await store.create_private_bucket()

    async def test_operator_does_not_create_after_ambiguous_lookup_or_retry_lost_write(self):
        store,transport=setup_store([json_response({},403)])
        with self.assertRaises(PrivateStorageError):
            await store.create_private_bucket()
        self.assertEqual(len(transport.calls),1)
        store,transport=setup_store([json_response({},404),TimeoutError("synthetic remote details")])
        with self.assertRaises(PrivateStorageError):
            await store.create_private_bucket()
        self.assertEqual(len(transport.calls),2)

    async def test_upload_reads_back_digest_and_never_exposes_url_or_overwrites(self):
        ref = PrivateBlobStore(CloudBaseGateway("test-env-123", "synthetic"), BUCKET).reference("source", DIGEST, len(DATA), "text/plain")
        store, transport = setup_store([private_bucket(), json_response({"Id":"object-1", "Key":f"{BUCKET}/{ref.key}"}), private_bucket(), GatewayHTTPResponse(200, DATA)])
        actual = await store.put_immutable(DATA, kind="source", media_type="text/plain", expected_sha256=DIGEST)
        self.assertEqual(actual, ref)
        methods = [call[0] for call in transport.calls]
        self.assertEqual(methods, ["GET", "POST", "GET", "GET"])
        upload, download = transport.calls[1], transport.calls[3]
        self.assertEqual(upload[2]["body"], DATA)
        self.assertEqual(upload[2]["headers"]["x-upsert"], "false")
        self.assertEqual(upload[2]["headers"]["Cache-Control"], "private, no-store")
        self.assertIn("/object/authenticated/", download[1])
        self.assertEqual(download[2]["max_response_bytes"], len(DATA))
        for call in transport.calls:
            self.assertEqual(call[2]["headers"]["Authorization"], "Bearer synthetic-server-only-key")
            self.assertTrue(call[1].startswith("https://test-env-123.api.tcloudbasegateway.com/"))
        self.assertFalse(hasattr(store, "get_public_url"))
        self.assertFalse(hasattr(store, "delete"))

    async def test_existing_object_is_reused_only_after_byte_verification(self):
        store, transport = setup_store([private_bucket(), json_response({"message":"duplicate"},409), private_bucket(), GatewayHTTPResponse(200,DATA)])
        ref = await store.put_immutable(DATA)
        self.assertEqual(ref.sha256, DIGEST)
        self.assertEqual(sum(call[0]=="POST" for call in transport.calls),1)

    async def test_existing_corrupt_object_cannot_be_overwritten(self):
        store, transport = setup_store([private_bucket(), json_response({},409), private_bucket(), GatewayHTTPResponse(200,b"x"*len(DATA))])
        with self.assertRaisesRegex(PrivateStorageError,"INTEGRITY_FAILED"):
            await store.put_immutable(DATA)
        self.assertEqual(sum(call[0]=="POST" for call in transport.calls),1)

    async def test_read_detects_corrupt_or_truncated_content(self):
        for damaged in (b"", b"x"*len(DATA)):
            store, _ = setup_store([private_bucket(),GatewayHTTPResponse(200,damaged)])
            ref = store.reference("source", DIGEST, len(DATA), "text/plain")
            with self.assertRaisesRegex(PrivateStorageError,"INTEGRITY_FAILED"):
                await store.read_verified(ref)

    async def test_oversized_download_rejected_by_gateway(self):
        store, _ = setup_store([private_bucket(),GatewayHTTPResponse(200,DATA+b"x")])
        ref = store.reference("source",DIGEST,len(DATA),"text/plain")
        with self.assertRaisesRegex(PrivateStorageError,"PRIVATE_STORAGE_UNAVAILABLE"):
            await store.read_verified(ref)

    async def test_public_bucket_and_ambiguous_configuration_fail_before_upload(self):
        for document in ({"id":BUCKET,"public":True},{"id":BUCKET},{"id":"another-bucket","public":False},[] ):
            store, transport = setup_store([json_response(document)])
            with self.assertRaises(PrivateStorageError):
                await store.put_immutable(DATA)
            self.assertEqual([call[0] for call in transport.calls],["GET"])

    async def test_bucket_becoming_public_before_readback_is_rejected(self):
        store, _ = setup_store([private_bucket(),json_response({"Id":"x","Key":f"{BUCKET}/veil-archive/private/v1/source/sha256/{DIGEST[:2]}/{DIGEST}"}),json_response({"id":BUCKET,"public":True})])
        with self.assertRaisesRegex(PrivateStorageError,"REQUIRES_PRIVATE_BUCKET"):
            await store.put_immutable(DATA)

    async def test_invalid_reference_cannot_fetch_foreign_or_public_source(self):
        store, transport = setup_store([])
        ref = store.reference("source", DIGEST, len(DATA), "text/plain")
        for bad in (replace(ref,bucket="public-assets"),replace(ref,key="truth/solution.json"),replace(ref,key="../secret"),
                    replace(ref,key="https://evil.test/file"),replace(ref,key=ref.key.replace("/private/","/public/")),
                    replace(ref,sha256="0"*64),replace(ref,size_bytes=-1),replace(ref,media_type="text/html")):
            with self.assertRaises(PrivateStorageError):
                await store.read_verified(bad)
        self.assertEqual(transport.calls,[])

    async def test_invalid_expected_hash_or_upload_size_has_no_network_side_effect(self):
        store, transport = setup_store([])
        for data, expected in ((DATA,"0"*64),(DATA,"invalid"),(b"x"*(MAX_BLOB_BYTES+1),None)):
            with self.assertRaises(PrivateStorageError):
                await store.put_immutable(data,expected_sha256=expected)
        self.assertEqual(transport.calls,[])

    async def test_upload_rejects_ambiguous_ack_and_never_leaks_remote_message(self):
        for response in (GatewayHTTPResponse(200,b"not-json-secret"),json_response({"Id":"x","Key":"https://secret.invalid"}),
                         json_response({"secret":"synthetic-secret-from-server"},500)):
            store, transport = setup_store([private_bucket(),response])
            with self.assertRaises(PrivateStorageError) as error:
                await store.put_immutable(DATA)
            self.assertNotIn("secret",str(error.exception))
            self.assertEqual(len(transport.calls),2)

    async def test_network_disconnect_is_not_retried(self):
        store, transport = setup_store([private_bucket(),TimeoutError("synthetic-secret")])
        with self.assertRaisesRegex(PrivateStorageError,"PRIVATE_STORAGE_UNAVAILABLE"):
            await store.put_immutable(DATA)
        self.assertEqual(len(transport.calls),2)

    async def test_redirect_is_not_followed(self):
        store, transport = setup_store([private_bucket(),GatewayHTTPResponse(302,b"",{"Location":"https://evil.invalid"})])
        with self.assertRaises(PrivateStorageError):
            await store.put_immutable(DATA)
        self.assertEqual(len(transport.calls),2)

    async def test_missing_download_is_sanitized(self):
        store, _ = setup_store([private_bucket(),GatewayHTTPResponse(404,b"private traceback")])
        ref=store.reference("source",DIGEST,len(DATA),"text/plain")
        with self.assertRaises(PrivateStorageError) as error:
            await store.read_verified(ref)
        self.assertEqual(error.exception.status,404)
        self.assertEqual(str(error.exception),"PRIVATE_STORAGE_OBJECT_MISSING")


class MemoryStorageTransport:
    def __init__(self):
        self.objects={}

    async def request(self,method,url,**kwargs):
        bucket_prefix="https://test-env-123.api.tcloudbasegateway.com/v1/storages/bucket/"
        object_prefix="https://test-env-123.api.tcloudbasegateway.com/v1/storages/object/"
        if method=="GET" and url==bucket_prefix+BUCKET:
            return private_bucket()
        if method=="POST" and url.startswith(object_prefix+BUCKET+"/"):
            key=url.removeprefix(object_prefix+BUCKET+"/")
            if key in self.objects: return json_response({},409)
            self.objects[key]=kwargs["body"]
            return json_response({"Id":"synthetic-object","Key":BUCKET+"/"+key})
        if method=="GET" and url.startswith(object_prefix+"authenticated/"+BUCKET+"/"):
            key=url.removeprefix(object_prefix+"authenticated/"+BUCKET+"/")
            return GatewayHTTPResponse(200,self.objects[key]) if key in self.objects else GatewayHTTPResponse(404,b"")
        raise AssertionError("Unexpected storage API route")


class SourceWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    def source_fixture(self,directory):
        root=Path(directory)
        original=root/"input"
        original.mkdir()
        (original/"clue.txt").write_text("Synthetic clue, authored for offline testing.\n")
        store=SourceBundleStore(root/"archive")
        bundle=store.freeze(original,{"schema_version":"source-plan/1.0","script_key":"synthetic-source",
            "edition":"offline-1","sources":[{"relative_path":"clue.txt","kind":"original","material_type":"clue"}]})
        return store,bundle["bundle_hash"]

    async def test_round_trip_uses_real_verifier_and_persists_new_report_before_cache_disposal(self):
        transport=MemoryStorageTransport()
        blobs=PrivateBlobStore(CloudBaseGateway("test-env-123","synthetic",transport=transport),BUCKET)
        with TemporaryDirectory(prefix="veil-test-source-") as directory:
            original,bundle=self.source_fixture(directory)
            archive=await blobs.archive_source_bundle(original,bundle)
        # Original fixture no longer exists; only cloud object references persist.
        async with blobs.source_workspace(bundle,archive) as workspace:
            cached_root=workspace.store.root
            self.assertTrue(workspace.store.verify(bundle,persist=False)["valid"])
            report=workspace.store.verify(bundle)
            self.assertFalse(report["publication_ready"])
            refs=await workspace.persist_reports()
            self.assertIn(report["report_hash"],refs)
        self.assertFalse(cached_root.exists())
        archive["report_refs"].update(refs)
        async with blobs.source_workspace(bundle,archive) as workspace:
            self.assertEqual(workspace.store.get_report(report["report_hash"])["bundle_hash"],bundle)
            self.assertEqual(await workspace.persist_reports(),refs)

    async def test_missing_or_corrupt_source_never_materializes_a_valid_snapshot(self):
        transport=MemoryStorageTransport()
        blobs=PrivateBlobStore(CloudBaseGateway("test-env-123","synthetic",transport=transport),BUCKET)
        with TemporaryDirectory(prefix="veil-test-source-") as directory:
            original,bundle=self.source_fixture(directory)
            archive=await blobs.archive_source_bundle(original,bundle)
        key=next(iter(archive["file_refs"].values()))["key"]
        transport.objects[key]=b"tampered"
        with self.assertRaises(PrivateStorageError):
            async with blobs.source_workspace(bundle,archive):
                self.fail("A corrupt source must not be exposed to the verifier")

    async def test_archive_cannot_inject_foreign_path_or_drop_declared_files(self):
        transport=MemoryStorageTransport()
        blobs=PrivateBlobStore(CloudBaseGateway("test-env-123","synthetic",transport=transport),BUCKET)
        with TemporaryDirectory(prefix="veil-test-source-") as directory:
            original,bundle=self.source_fixture(directory)
            archive=await blobs.archive_source_bundle(original,bundle)
        for bad in (archive|{"file_refs":{}},archive|{"bundle_hash":"0"*64},archive|{"root":"/tmp/forged"}):
            with self.assertRaises(PrivateStorageError):
                async with blobs.source_workspace(bundle,bad):
                    self.fail("Invalid archive was accepted")


if __name__ == "__main__":
    unittest.main()
