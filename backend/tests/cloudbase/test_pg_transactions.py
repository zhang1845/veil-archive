"""Opt-in, guarded real PostgreSQL tests for an isolated CloudBase test cluster.

Never imports application/conftest or discovers credentials. This runner refuses
any DSN except the explicitly prepared Unix socket and checks data_directory
before creating its own synthetic database/roles or applying 001-003.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from threading import Barrier
import unittest
from uuid import uuid4

import psycopg

BASE = Path("/Users/zhanglifu/Documents/Codex/veil-upload-prep/cloudbase-free-prep/pg-test")
EXPECTED_DSN = f"host={BASE}/socket port=55482 user=veil_test dbname=postgres"
REPO = Path(__file__).resolve().parents[3]
ENABLED = os.environ.get("VEIL_CLOUDBASE_TEST_DSN") == EXPECTED_DSN


@unittest.skipUnless(ENABLED, "explicit isolated CloudBase test DSN required")
class CloudBasePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with psycopg.connect(EXPECTED_DSN, autocommit=True) as connection:
            if (connection.execute("SHOW data_directory").fetchone()[0] != str(BASE/"data")
                    or connection.execute("SHOW listen_addresses").fetchone()[0] != ""):
                raise RuntimeError("Refusing a non-isolated cluster")
            for role in ("anon", "authenticated", "service_role"):
                if not connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                    connection.execute(f"CREATE ROLE {role} NOLOGIN")
            if not connection.execute("SELECT 1 FROM pg_database WHERE datname='veil_cloudbase_test'").fetchone():
                connection.execute("CREATE DATABASE veil_cloudbase_test")
        cls.dsn = EXPECTED_DSN.replace("dbname=postgres", "dbname=veil_cloudbase_test")
        with psycopg.connect(cls.dsn, autocommit=True) as connection:
            for name in ("001_aggregate_store.sql", "002_auth.sql", "003_jobs.sql", "004_guarded_cas.sql", "005_job_cancel.sql"):
                connection.execute((REPO/"deploy/cloudbase"/name).read_text())

    def setUp(self):
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            connection.execute("UPDATE public.veil_archive_job_settings SET paid_execution_enabled=false WHERE singleton")

    def tearDown(self):
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            connection.execute("UPDATE public.veil_archive_job_settings SET paid_execution_enabled=false WHERE singleton")

    def connection(self, role="service_role", claim="service_role"):
        connection = psycopg.connect(self.dsn, autocommit=True)
        connection.execute(f"SET ROLE {role}")
        if claim is not None:
            connection.execute("SELECT set_config('request.jwt.claims',%s,false)", (json.dumps({"role":claim}),))
        return connection

    def rpc(self, name, parameters, connection=None):
        own = connection is None
        connection = connection or self.connection()
        try:
            fields = ",".join(f"{name} => %s" for name in parameters)
            values = tuple(psycopg.types.json.Jsonb(v) if isinstance(v,(dict,list)) else v for v in parameters.values())
            return connection.execute(f"SELECT public.{name}({fields})", values).fetchone()[0]
        finally:
            if own: connection.close()

    def job(self, budget="0.5"):
        return self.rpc("veil_archive_job_create", dict(p_actor_id=1,p_idempotency_key=str(uuid4()),p_kind="creative",
            p_request_hash="a"*64,p_request_payload={"synthetic":True},p_budget_cny=budget))["job"]

    def claim(self, job):
        return self.rpc("veil_archive_job_claim",dict(p_job_id=job["id"],p_expected_revision=job["revision"]))["job"]

    def enable_synthetic_paid_reservations(self):
        # Only test-ledger transitions. This runner has no external model client.
        with psycopg.connect(self.dsn,autocommit=True) as connection:
            connection.execute("UPDATE public.veil_archive_job_settings SET paid_execution_enabled=true WHERE singleton")

    def reserve(self, job, step="draft", amount="0.3", allow=True):
        return dict(p_job_id=job["id"],p_token=job["lease_token"],p_fence=job["fence"],p_step_key=step,
            p_prepared_hash="b"*64,p_prepared={"synthetic":True},p_reservation_cny=amount,p_allow_paid=allow)

    def race(self, action, count=2):
        barrier = Barrier(count)
        def execute(index):
            with self.connection() as connection:
                barrier.wait(timeout=10)
                return action(connection,index)
        with ThreadPoolExecutor(max_workers=count) as pool:
            return list(pool.map(execute,range(count)))

    def test_every_job_rpc_rejects_missing_or_wrong_gateway_role(self):
        # Grant EXECUTE on harmless get to prove the in-function guard, not only
        # PostgreSQL's normal EXECUTE grant check, enforces the role.
        with psycopg.connect(self.dsn,autocommit=True) as connection:
            connection.execute("GRANT EXECUTE ON FUNCTION public.veil_archive_job_get(uuid) TO anon,authenticated")
        try:
            for role, claim in (("anon","anon"),("authenticated","authenticated"),("service_role",None),("service_role","")):
                with self.connection(role,claim) as connection:
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        self.rpc("veil_archive_job_get",{"p_job_id":uuid4()},connection)
        finally:
            with psycopg.connect(self.dsn,autocommit=True) as connection:
                connection.execute("REVOKE EXECUTE ON FUNCTION public.veil_archive_job_get(uuid) FROM anon,authenticated")

    def test_direct_job_ledger_access_is_denied_even_to_service_role(self):
        for role in ("anon","authenticated","service_role"):
            for table in ("veil_archive_jobs","veil_archive_usage","veil_archive_job_settings"):
                with self.connection(role,role) as connection:
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        connection.execute(f"SELECT * FROM public.{table}")

    def test_concurrent_create_reuses_one_id_and_rejects_changed_request(self):
        params=dict(p_actor_id=1,p_idempotency_key=str(uuid4()),p_kind="creative",p_request_hash="a"*64,p_request_payload={},p_budget_cny="0.5")
        results=self.race(lambda connection,index:self.rpc("veil_archive_job_create",params,connection))
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(results[0]["job"]["id"],results[1]["job"]["id"])
        conflict=self.rpc("veil_archive_job_create",params|{"p_request_payload":{"changed":True}})
        self.assertEqual(conflict["code"],"JOB_IDEMPOTENCY_CONFLICT")

    def test_competing_claims_have_one_lease_and_fence(self):
        job=self.job()
        params=dict(p_job_id=job["id"],p_expected_revision=0)
        results=self.race(lambda connection,index:self.rpc("veil_archive_job_claim",params,connection))
        self.assertEqual(sum(result["ok"] for result in results),1)
        claimed=next(result["job"] for result in results if result["ok"])
        self.assertEqual(claimed["fence"],1)
        self.assertEqual(claimed["revision"],1)

    def test_payload_cas_with_same_lease_has_one_winner(self):
        job=self.claim(self.job())
        params=dict(p_job_id=job["id"],p_token=job["lease_token"],p_fence=job["fence"],p_expected_revision=job["revision"],p_state="QUEUED")
        results=self.race(lambda connection,index:self.rpc("veil_archive_job_save",params|{"p_payload":{"winner":index}},connection))
        self.assertEqual(sum(result["ok"] for result in results),1)
        final=self.rpc("veil_archive_job_get",{"p_job_id":job["id"]})["job"]
        self.assertEqual(final["revision"],2)
        self.assertIsNone(final["lease_token"])

    def test_paid_execution_requires_database_and_per_call_flags(self):
        job=self.claim(self.job())
        disabled=self.rpc("veil_archive_usage_reserve",self.reserve(job))
        self.assertEqual(disabled["code"],"PAID_EXECUTION_DISABLED")
        self.enable_synthetic_paid_reservations()
        disabled=self.rpc("veil_archive_usage_reserve",self.reserve(job,allow=False))
        self.assertEqual(disabled["code"],"PAID_EXECUTION_DISABLED")

    def test_competing_cost_reservations_never_overrun_budget(self):
        self.enable_synthetic_paid_reservations()
        job=self.claim(self.job())
        results=self.race(lambda connection,index:self.rpc("veil_archive_usage_reserve",self.reserve(job,step=f"draft-{index}"),connection))
        self.assertEqual(sum(result["ok"] for result in results),1)
        current=self.rpc("veil_archive_job_get",{"p_job_id":job["id"]})["job"]
        self.assertEqual(current["charged_cny"],0.3)

    def test_reservation_replay_and_competing_dispatch_grant_once(self):
        self.enable_synthetic_paid_reservations()
        job=self.claim(self.job())
        first=self.rpc("veil_archive_usage_reserve",self.reserve(job))
        again=self.rpc("veil_archive_usage_reserve",self.reserve(job))
        self.assertTrue(first["created"])
        self.assertFalse(again["created"])
        self.assertEqual(first["usage"]["id"],again["usage"]["id"])
        changed=self.rpc("veil_archive_usage_reserve",self.reserve(job)|{"p_prepared_hash":"c"*64})
        self.assertEqual(changed["code"],"USAGE_IDEMPOTENCY_CONFLICT")
        params=dict(p_job_id=job["id"],p_token=job["lease_token"],p_fence=job["fence"],p_usage_id=first["usage"]["id"],p_allow_paid=True)
        results=self.race(lambda connection,index:self.rpc("veil_archive_usage_dispatch",params,connection))
        self.assertEqual(sum(result.get("dispatch_permitted",False) for result in results),1)

    def test_completed_usage_is_immutable_and_refund_is_applied_once(self):
        self.enable_synthetic_paid_reservations()
        job=self.claim(self.job())
        usage=self.rpc("veil_archive_usage_reserve",self.reserve(job))["usage"]
        params=dict(p_job_id=job["id"],p_token=job["lease_token"],p_fence=job["fence"],p_usage_id=usage["id"])
        self.rpc("veil_archive_usage_dispatch",params|{"p_allow_paid":True})
        finish=params|dict(p_state="SUCCEEDED",p_result_hash="c"*64,p_result={"draft":"synthetic"},p_receipt={"tokens":2},p_actual_cost_cny="0.1")
        results=self.race(lambda connection,index:self.rpc("veil_archive_usage_finish",finish,connection))
        self.assertEqual(sum(result.get("completed",False) for result in results),1)
        self.assertTrue(all(result["ok"] for result in results))
        changed=self.rpc("veil_archive_usage_finish",finish|{"p_result":{"different":True}})
        self.assertEqual(changed["code"],"USAGE_RESULT_CONFLICT")
        self.assertEqual(self.rpc("veil_archive_job_get",{"p_job_id":job["id"]})["job"]["charged_cny"],0.1)
        saved=self.rpc("veil_archive_job_save",dict(p_job_id=job["id"],p_token=job["lease_token"],p_fence=job["fence"],
            p_expected_revision=job["revision"],p_payload={},p_state="QUEUED"))
        self.assertTrue(saved["ok"])
        self.assertFalse(self.rpc("veil_archive_usage_finish",finish)["completed"])
        fetched=self.rpc("veil_archive_usage_get",dict(p_job_id=job["id"],p_step_key="draft"))
        self.assertEqual(fetched["usage"]["result"],{"draft":"synthetic"})

    def test_unknown_usage_keeps_full_cost_and_is_never_retried(self):
        self.enable_synthetic_paid_reservations()
        job=self.claim(self.job())
        usage=self.rpc("veil_archive_usage_reserve",self.reserve(job))["usage"]
        params=dict(p_job_id=job["id"],p_token=job["lease_token"],p_fence=job["fence"],p_usage_id=usage["id"])
        self.rpc("veil_archive_usage_dispatch",params|{"p_allow_paid":True})
        finish=params|dict(p_state="UNKNOWN",p_result_hash="d"*64,p_result={},p_receipt={"unknown":True},p_actual_cost_cny="0")
        self.assertTrue(self.rpc("veil_archive_usage_finish",finish)["ok"])
        self.assertFalse(self.rpc("veil_archive_usage_finish",finish)["completed"])
        self.assertEqual(self.rpc("veil_archive_job_get",{"p_job_id":job["id"]})["job"]["charged_cny"],0.3)
        self.assertEqual(self.rpc("veil_archive_usage_dispatch",params|{"p_allow_paid":True})["code"],"USAGE_ALREADY_DISPATCHED")

    def test_expired_inflight_lease_requires_reconciliation_and_old_worker_cannot_finish(self):
        self.enable_synthetic_paid_reservations()
        job=self.claim(self.job())
        usage=self.rpc("veil_archive_usage_reserve",self.reserve(job))["usage"]
        params=dict(p_job_id=job["id"],p_token=job["lease_token"],p_fence=job["fence"],p_usage_id=usage["id"])
        self.rpc("veil_archive_usage_dispatch",params|{"p_allow_paid":True})
        with psycopg.connect(self.dsn,autocommit=True) as connection:
            connection.execute("UPDATE public.veil_archive_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(job["id"],))
        renewed=self.rpc("veil_archive_job_claim",dict(p_job_id=job["id"],p_expected_revision=job["revision"]))
        self.assertEqual(renewed["code"],"JOB_RESULT_UNKNOWN")
        late=self.rpc("veil_archive_usage_finish",params|dict(p_state="SUCCEEDED",p_result_hash="c"*64,p_result={},p_receipt={}))
        self.assertEqual(late["code"],"USAGE_RESULT_CONFLICT")
        final=self.rpc("veil_archive_job_get",{"p_job_id":job["id"]})["job"]
        self.assertEqual(final["state"],"NEEDS_RECONCILIATION")
        self.assertEqual(final["charged_cny"],0.3)

    def test_stale_fence_cannot_overwrite_after_expired_lease_is_reclaimed(self):
        old=self.claim(self.job())
        with psycopg.connect(self.dsn,autocommit=True) as connection:
            connection.execute("UPDATE public.veil_archive_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(old["id"],))
        new=self.claim(old)
        self.assertGreater(new["fence"],old["fence"])
        late=self.rpc("veil_archive_job_save",dict(p_job_id=old["id"],p_token=old["lease_token"],p_fence=old["fence"],
            p_expected_revision=new["revision"],p_payload={"stale":True}))
        self.assertEqual(late["code"],"JOB_LEASE_CONFLICT")

    def test_job_list_reads_persisted_rows_and_omits_private_payload_or_lease(self):
        job=self.claim(self.job())
        listing=self.rpc("veil_archive_job_list",dict(p_actor_id=1,p_kind="creative",p_limit=100))
        listed=next(item for item in listing["jobs"] if item["id"]==job["id"])
        self.assertNotIn("payload",listed)
        self.assertNotIn("request_payload",listed)
        self.assertNotIn("lease_token",listed)
        self.assertEqual(listed["state"],"RUNNING")
        self.assertEqual(self.rpc("veil_archive_job_list",{"p_kind":"arbitrary"})["status"],400)

    def test_aggregate_concurrent_create_cas_and_idempotent_replay(self):
        create=dict(p_kind="play",p_key=str(uuid4()),p_owner="synthetic-owner",p_payload={"step":0},
            p_digest="a"*64,p_idempotency_key="create",p_request_digest="b"*64)
        created=self.race(lambda connection,index:self.rpc("veil_archive_create",create,connection))
        self.assertTrue(all(result["ok"] for result in created))
        self.assertEqual(sum(result["replayed"] for result in created),1)
        common={key:create[key] for key in ("p_kind","p_key","p_owner")}
        parameters=[]
        def mutate(connection,index):
            params=common|dict(p_expected_version=1,p_payload={"winner":index},p_digest="c"*64,p_event={"index":index},
                p_idempotency_key=f"cas-{index}",p_request_digest="d"*64)
            parameters.append(params)
            return self.rpc("veil_archive_cas_append",params,connection)
        results=self.race(mutate)
        self.assertEqual(sum(result["ok"] for result in results),1)
        winner=next(result for result in results if result["ok"])
        index=winner["event"]["index"]
        original=next(params for params in parameters if params["p_event"]["index"]==index)
        replay=self.rpc("veil_archive_cas_append",original|dict(p_expected_version=999,p_payload={"new":True},p_event={"changed":True}))
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["record"],winner["record"])
        self.assertEqual(replay["event"],winner["event"])
        changed=self.rpc("veil_archive_cas_append",original|{"p_request_digest":"e"*64})
        self.assertEqual(changed["code"],"IDEMPOTENCY_CONFLICT")
        hidden=self.rpc("veil_archive_get",common|{"p_owner":"other-owner"})
        self.assertEqual(hidden["status"],404)

    def test_aggregate_direct_access_and_in_function_roles_are_denied(self):
        with psycopg.connect(self.dsn,autocommit=True) as connection:
            connection.execute("GRANT EXECUTE ON FUNCTION public.veil_archive_get(text,text,text) TO anon,authenticated")
        try:
            for role in ("anon","authenticated","service_role"):
                for table in ("veil_archive_records","veil_archive_receipts"):
                    with self.connection(role,role) as connection:
                        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                            connection.execute(f"SELECT * FROM public.{table}")
            for role in ("anon","authenticated"):
                with self.connection(role,role) as connection:
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        self.rpc("veil_archive_get",dict(p_kind="play",p_key="missing",p_owner="synthetic"),connection)
        finally:
            with psycopg.connect(self.dsn,autocommit=True) as connection:
                connection.execute("REVOKE EXECUTE ON FUNCTION public.veil_archive_get(text,text,text) FROM anon,authenticated")

    @staticmethod
    def synthetic_hash():
        return uuid4().hex+uuid4().hex

    def guest(self):
        params=dict(p_username="guest_"+uuid4().hex,p_access_hash=self.synthetic_hash(),
                    p_refresh_hash=self.synthetic_hash(),p_client_hash=self.synthetic_hash())
        return self.rpc("veil_archive_auth_guest",params),params

    def test_auth_guests_are_distinct_unprivileged_and_expire(self):
        first,first_params=self.guest()
        second,_=self.guest()
        self.assertNotEqual(first["user"]["id"],second["user"]["id"])
        self.assertFalse(first["user"]["is_admin"])
        self.assertTrue(first["user"]["is_guest"])
        with psycopg.connect(self.dsn,autocommit=True) as connection:
            seconds=connection.execute("SELECT extract(epoch FROM expires_at-created_at) FROM public.veil_archive_auth_users WHERE id=%s",(first["user"]["id"],)).fetchone()[0]
            self.assertLessEqual(abs(seconds-86400),1)
            connection.execute("UPDATE public.veil_archive_auth_users SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",(first["user"]["id"],))
        expired=self.rpc("veil_archive_auth_me",{"p_access_hash":first_params["p_access_hash"]})
        self.assertEqual(expired["status"],401)

    def test_auth_refresh_has_one_winner_and_logout_invalidates_rotated_session(self):
        _,guest=self.guest()
        pairs=[(self.synthetic_hash(),self.synthetic_hash()) for _ in range(2)]
        results=self.race(lambda connection,index:self.rpc("veil_archive_auth_refresh",dict(p_old_refresh_hash=guest["p_refresh_hash"],
            p_access_hash=pairs[index][0],p_refresh_hash=pairs[index][1],p_client_hash=self.synthetic_hash()),connection))
        self.assertEqual(sum(result["ok"] for result in results),1)
        winner=next(index for index,result in enumerate(results) if result["ok"])
        self.assertEqual(self.rpc("veil_archive_auth_me",{"p_access_hash":guest["p_access_hash"]})["status"],401)
        self.assertTrue(self.rpc("veil_archive_auth_me",{"p_access_hash":pairs[winner][0]})["ok"])
        self.rpc("veil_archive_auth_logout",{"p_access_hash":pairs[winner][0]})
        after=self.rpc("veil_archive_auth_refresh",dict(p_old_refresh_hash=pairs[winner][1],p_access_hash=self.synthetic_hash(),
            p_refresh_hash=self.synthetic_hash(),p_client_hash=self.synthetic_hash()))
        self.assertEqual(after["status"],401)

    def test_auth_bootstrap_does_not_reset_or_promote_existing_account(self):
        params=dict(p_username="user_"+uuid4().hex,p_email=uuid4().hex+"@example.invalid",p_password_hash="$argon2id$synthetic-placeholder",p_is_admin=False)
        first=self.rpc("veil_archive_auth_bootstrap",params)
        conflict=self.rpc("veil_archive_auth_bootstrap",params|dict(p_password_hash="$argon2id$changed-placeholder",p_is_admin=True))
        self.assertEqual(conflict["status"],409)
        with psycopg.connect(self.dsn,autocommit=True) as connection:
            row=connection.execute("SELECT password_hash,is_admin FROM public.veil_archive_auth_users WHERE id=%s",(first["user"]["id"],)).fetchone()
        self.assertEqual(row,(params["p_password_hash"],False))

    def test_auth_rate_counter_limits_concurrent_requests_and_retains_failures(self):
        identity=self.synthetic_hash()
        params=dict(p_namespace="login-account",p_identity_hash=identity,p_limit=3,p_window_seconds=60)
        results=self.race(lambda connection,index:self.rpc("veil_archive_auth_take_rate",params,connection),count=6)
        self.assertEqual(sum(results),3)
        with psycopg.connect(self.dsn,autocommit=True) as connection:
            self.assertEqual(connection.execute("SELECT attempts FROM public.veil_archive_auth_rate_windows WHERE namespace='login-account' AND identity_hash=%s",(identity,)).fetchone()[0],6)

    def test_auth_tables_and_in_function_role_guard_are_private(self):
        with psycopg.connect(self.dsn,autocommit=True) as connection:
            connection.execute("GRANT EXECUTE ON FUNCTION public.veil_archive_auth_me(text) TO anon,authenticated")
        try:
            for role in ("anon","authenticated","service_role"):
                for table in ("veil_archive_auth_users","veil_archive_auth_sessions","veil_archive_auth_rate_windows"):
                    with self.connection(role,role) as connection:
                        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                            connection.execute(f"SELECT * FROM public.{table}")
            for role in ("anon","authenticated"):
                with self.connection(role,role) as connection:
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        self.rpc("veil_archive_auth_me",{"p_access_hash":"a"*64},connection)
        finally:
            with psycopg.connect(self.dsn,autocommit=True) as connection:
                connection.execute("REVOKE EXECUTE ON FUNCTION public.veil_archive_auth_me(text) FROM anon,authenticated")

    def guarded_records(self):
        admin=dict(p_kind="admin_workspace",p_key="authority-"+uuid4().hex,p_owner="system",p_payload={"approved":True},
            p_digest="a"*64,p_idempotency_key="create",p_request_digest="a"*64)
        play=dict(p_kind="play_workspace",p_key="play-"+uuid4().hex,p_owner="user-1",p_payload={"step":0},
            p_digest="b"*64,p_idempotency_key="create",p_request_digest="b"*64)
        self.rpc("veil_archive_create",admin)
        self.rpc("veil_archive_create",play)
        guard={"kind":admin["p_kind"],"key":admin["p_key"],"owner":admin["p_owner"],"expected_version":1,"digest":"a"*64}
        write=play|dict(p_expected_version=1,p_payload={"step":1},p_digest="c"*64,p_event={"step":1},
            p_idempotency_key="action-1",p_request_digest="c"*64,p_guards=[guard])
        revoke=admin|dict(p_expected_version=1,p_payload={"approved":False},p_digest="d"*64,p_event={"revoked":True},
            p_idempotency_key="revoke",p_request_digest="d"*64)
        return admin,play,guard,write,revoke

    def test_guarded_write_rejects_stale_missing_owner_and_digest_dependencies(self):
        _,play,guard,write,_=self.guarded_records()
        for altered in (guard|{"expected_version":2},guard|{"digest":"f"*64},guard|{"owner":"another-owner"},guard|{"key":"missing"}):
            result=self.rpc("veil_archive_cas_append_guarded",write|{"p_guards":[altered]})
            self.assertEqual(result["code"],"DEPENDENCY_CONFLICT")
        record=self.rpc("veil_archive_get",{k:play[k] for k in ("p_kind","p_key","p_owner")})["record"]
        self.assertEqual(record["version"],1)
        with psycopg.connect(self.dsn,autocommit=True) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM public.veil_archive_receipts WHERE kind=%s AND record_key=%s",(play["p_kind"],play["p_key"])).fetchone()[0],1)

    def test_guarded_receipt_replays_before_changed_dependencies_or_target_version(self):
        _,_,_,write,revoke=self.guarded_records()
        first=self.rpc("veil_archive_cas_append_guarded",write)
        self.assertTrue(first["ok"])
        self.rpc("veil_archive_cas_append",revoke)
        replay=self.rpc("veil_archive_cas_append_guarded",write|dict(p_expected_version=999,p_payload={"changed":True},p_event={"changed":True}))
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["record"],first["record"])
        self.assertEqual(replay["event"],first["event"])
        changed=self.rpc("veil_archive_cas_append_guarded",write|{"p_request_digest":"e"*64})
        self.assertEqual(changed["code"],"IDEMPOTENCY_CONFLICT")
        fresh=self.rpc("veil_archive_cas_append_guarded",write|dict(p_expected_version=2,p_idempotency_key="action-2",p_request_digest="e"*64))
        self.assertEqual(fresh["code"],"DEPENDENCY_CONFLICT")

    def test_guarded_global_lock_order_avoids_opposite_target_dependency_deadlock(self):
        _,play,_,write,_=self.guarded_records()
        other=play|dict(p_key="other-"+uuid4().hex)
        self.rpc("veil_archive_create",other)
        targets=[play,other]
        def action(connection,index):
            target,dependency=targets[index],targets[1-index]
            guard=dict(kind=dependency["p_kind"],key=dependency["p_key"],owner=dependency["p_owner"],expected_version=1,digest=dependency["p_digest"])
            params=write|{key:target[key] for key in ("p_kind","p_key","p_owner")}|{"p_guards":[guard]}
            return self.rpc("veil_archive_cas_append_guarded",params,connection)
        results=self.race(action)
        self.assertEqual(sum(result["ok"] for result in results),1)
        self.assertEqual(next(result for result in results if not result["ok"])["code"],"DEPENDENCY_CONFLICT")

    def test_guarded_revocation_commit_before_write_prevents_new_action(self):
        from concurrent.futures import TimeoutError
        _,play,_,write,revoke=self.guarded_records()
        with self.connection() as connection:
            connection.execute("BEGIN")
            self.rpc("veil_archive_cas_append",revoke,connection)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(self.rpc,"veil_archive_cas_append_guarded",write)
                try:
                    with self.assertRaises(TimeoutError): future.result(timeout=0.05)
                finally:
                    connection.execute("COMMIT")
                self.assertEqual(future.result(timeout=5)["code"],"DEPENDENCY_CONFLICT")
        self.assertEqual(self.rpc("veil_archive_get",{key:play[key] for key in ("p_kind","p_key","p_owner")})["record"]["version"],1)

    def test_guarded_write_holds_authority_until_action_is_committed(self):
        from concurrent.futures import TimeoutError
        _,_,_,write,revoke=self.guarded_records()
        with self.connection() as connection:
            connection.execute("BEGIN")
            saved=self.rpc("veil_archive_cas_append_guarded",write,connection)
            self.assertTrue(saved["ok"])
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(self.rpc,"veil_archive_cas_append",revoke)
                try:
                    with self.assertRaises(TimeoutError): future.result(timeout=0.05)
                finally:
                    connection.execute("COMMIT")
                self.assertTrue(future.result(timeout=5)["ok"])

    def test_guarded_function_rejects_invalid_and_untrusted_inputs(self):
        _,_,guard,write,_=self.guarded_records()
        for guards in (None,{},[guard]*9,[guard,guard],[guard|{"sql":"SELECT 1"}],[guard|{"expected_version":True}],
                       [guard|{"kind":"arbitrary"}],[guard|{"digest":"bad"}]):
            with self.assertRaises(psycopg.errors.InvalidParameterValue):
                self.rpc("veil_archive_cas_append_guarded",write|{"p_guards":guards})
        signature="public.veil_archive_cas_append_guarded(text,text,text,bigint,jsonb,text,jsonb,text,text,jsonb)"
        with psycopg.connect(self.dsn,autocommit=True) as connection:
            connection.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO anon,authenticated")
        try:
            for role,claim in (("anon","anon"),("authenticated","authenticated"),("service_role",None)):
                with self.connection(role,claim) as connection:
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        self.rpc("veil_archive_cas_append_guarded",write,connection)
        finally:
            with psycopg.connect(self.dsn,autocommit=True) as connection:
                connection.execute(f"REVOKE EXECUTE ON FUNCTION {signature} FROM anon,authenticated")

    def synthetic_admin(self):
        params=dict(p_username="admin_"+uuid4().hex,p_email=uuid4().hex+"@example.invalid",p_password_hash="$argon2id$synthetic-placeholder",p_is_admin=True)
        return self.rpc("veil_archive_auth_bootstrap",params)["user"]["id"]

    def test_cancel_concurrent_receipt_replay_and_immutable_terminal_state(self):
        admin=self.synthetic_admin()
        job=self.job()
        params=dict(p_job_id=job["id"],p_expected_revision=job["revision"],p_admin_actor_id=admin,p_owner_actor_id=job["actor_id"],
            p_idempotency_key="cancel",p_request_hash="e"*64)
        results=self.race(lambda connection,index:self.rpc("veil_archive_job_cancel",params,connection))
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(sum(result["replayed"] for result in results),1)
        self.assertEqual(results[0]["job"]["state"],"CANCELLED")
        replay=self.rpc("veil_archive_job_cancel",params|{"p_expected_revision":999})
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["job"],results[0]["job"])
        changed=self.rpc("veil_archive_job_cancel",params|{"p_request_hash":"f"*64})
        self.assertEqual(changed["code"],"JOB_CANCEL_IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.rpc("veil_archive_job_claim",dict(p_job_id=job["id"],p_expected_revision=replay["job"]["revision"]))["code"],"JOB_CLAIM_CONFLICT")

    def test_cancel_checks_revision_admin_active_lease_and_preserves_unknown_cost(self):
        admin=self.synthetic_admin()
        job=self.job()
        params=dict(p_job_id=job["id"],p_expected_revision=999,p_admin_actor_id=admin,p_owner_actor_id=job["actor_id"],p_idempotency_key="cancel",p_request_hash="e"*64)
        self.assertEqual(self.rpc("veil_archive_job_cancel",params)["code"],"JOB_CANCEL_REVISION_CONFLICT")
        guest,_=self.guest()
        self.assertEqual(self.rpc("veil_archive_job_cancel",params|{"p_admin_actor_id":guest["user"]["id"]})["code"],"JOB_CANCEL_ADMIN_REQUIRED")
        job=self.claim(job)
        params|={"p_expected_revision":job["revision"]}
        self.assertEqual(self.rpc("veil_archive_job_cancel",params)["code"],"JOB_CANCEL_LEASE_ACTIVE")
        self.enable_synthetic_paid_reservations()
        usage=self.rpc("veil_archive_usage_reserve",self.reserve(job))["usage"]
        dispatch=dict(p_job_id=job["id"],p_token=job["lease_token"],p_fence=job["fence"],p_usage_id=usage["id"])
        self.rpc("veil_archive_usage_dispatch",dispatch|{"p_allow_paid":True})
        self.rpc("veil_archive_usage_finish",dispatch|dict(p_state="UNKNOWN",p_result_hash="c"*64,p_result={},p_receipt={},p_actual_cost_cny="0"))
        with psycopg.connect(self.dsn,autocommit=True) as connection:
            connection.execute("UPDATE public.veil_archive_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(job["id"],))
        self.assertEqual(self.rpc("veil_archive_job_cancel",params)["code"],"JOB_CANCEL_RESULT_UNKNOWN")
        self.assertEqual(self.rpc("veil_archive_job_get",{"p_job_id":job["id"]})["job"]["charged_cny"],0.3)

    def test_cancel_receipt_table_and_function_role_guard_are_private(self):
        for role in ("anon","authenticated","service_role"):
            with self.connection(role,role) as connection:
                with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                    connection.execute("SELECT * FROM public.veil_archive_job_cancel_receipts")
        with self.connection("service_role",None) as connection:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                self.rpc("veil_archive_job_cancel",dict(p_job_id=uuid4(),p_expected_revision=0,p_admin_actor_id=1,p_owner_actor_id=1,p_idempotency_key="cancel",p_request_hash="e"*64),connection)


if __name__ == "__main__":
    unittest.main()
