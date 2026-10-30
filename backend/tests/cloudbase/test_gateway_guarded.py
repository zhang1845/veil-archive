"""Offline HTTP contract for atomic authority-dependent writes."""
import json
import unittest

from src.cloudbase.gateway import CloudBaseGateway, GatewayConflict, GatewayHTTPResponse, payload_digest


class Transport:
    def __init__(self,response): self.response,self.calls=response,[]
    async def request(self,method,url,**kwargs):
        self.calls.append((method,url,kwargs))
        return self.response


class GuardedGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setup_client(self,result=None):
        payload={"step":1}
        record={"kind":"play_workspace","key":"play-1","owner":"user-1","version":2,"payload":payload,"digest":payload_digest(payload)}
        response=GatewayHTTPResponse(200,json.dumps(result or {"ok":True,"record":record,"event":{"step":1},"replayed":False}).encode())
        transport=Transport(response)
        return CloudBaseGateway("offline-test-123","synthetic",transport=transport),transport

    def guard(self,**updates):
        return {"kind":"admin_workspace","key":"global","owner":"system","expected_version":1,"digest":"a"*64}|updates

    async def write(self,client,guards,**updates):
        return await client.cas_append_guarded("play_workspace","play-1","user-1",1,{"step":1},{"step":1},
            idempotency_key="action-1",guards=guards,**updates)

    async def test_guarded_rpc_has_exact_identity_and_binds_guards_in_request_digest(self):
        client,transport=self.setup_client()
        await self.write(client,[self.guard()])
        sent=transport.calls[0]
        self.assertTrue(sent[1].endswith("/rpc/veil_archive_cas_append_guarded"))
        params=json.loads(sent[2]["body"])
        self.assertEqual(params["p_guards"],[self.guard()])
        original=params["p_request_digest"]
        client,transport=self.setup_client()
        await self.write(client,[self.guard(expected_version=2)])
        self.assertNotEqual(json.loads(transport.calls[0][2]["body"])["p_request_digest"],original)

    async def test_guard_order_is_canonical_and_explicit_original_request_digest_is_retained(self):
        first=self.guard(key="a")
        second=self.guard(key="b")
        digests=[]
        for guards in ([second,first],[first,second]):
            client,transport=self.setup_client()
            await self.write(client,guards)
            sent=json.loads(transport.calls[0][2]["body"])
            self.assertEqual(sent["p_guards"],[first,second])
            digests.append(sent["p_request_digest"])
        self.assertEqual(*digests)
        client,transport=self.setup_client()
        await self.write(client,[first],request_digest="f"*64)
        self.assertEqual(json.loads(transport.calls[0][2]["body"])["p_request_digest"],"f"*64)

    async def test_guard_validation_has_no_network_side_effect(self):
        invalid=[None,{},[self.guard()]*9,[self.guard(),self.guard()],
            [self.guard(kind="unknown")],[self.guard(key="../global")],[self.guard(owner="")],
            [self.guard(expected_version=True)],[self.guard(expected_version=0)],
            [self.guard(digest="bad")],[self.guard()|{"sql":"select secret"}],[{"kind":"admin_workspace"}]]
        client,transport=self.setup_client()
        for guards in invalid:
            with self.subTest(guards=guards),self.assertRaises(ValueError):
                await self.write(client,guards)
        self.assertEqual(transport.calls,[])

    async def test_empty_guards_are_explicitly_valid_for_independent_preview(self):
        client,transport=self.setup_client()
        await self.write(client,[])
        self.assertEqual(json.loads(transport.calls[0][2]["body"])["p_guards"],[])

    async def test_dependency_conflict_is_typed_and_never_retried(self):
        client,transport=self.setup_client({"ok":False,"status":409,"code":"DEPENDENCY_CONFLICT"})
        with self.assertRaises(GatewayConflict) as error:
            await self.write(client,[self.guard()])
        self.assertEqual(error.exception.code,"DEPENDENCY_CONFLICT")
        self.assertEqual(len(transport.calls),1)


if __name__=="__main__": unittest.main()
