-- Requires 001-003. Narrow admin cancellation; no model call, refund or re-arm.
-- Only this application's owned table constraint is extended for a terminal
-- CANCELLED state. Existing claim/dispatch/save functions cannot restart it.
BEGIN;

ALTER TABLE public.veil_archive_jobs DROP CONSTRAINT IF EXISTS veil_archive_jobs_state_check;
ALTER TABLE public.veil_archive_jobs ADD CONSTRAINT veil_archive_jobs_state_check
    CHECK (state IN ('QUEUED','RUNNING','REVIEW_REQUIRED','COMPLETED','FAILED','NEEDS_RECONCILIATION','CANCELLED'));

CREATE TABLE IF NOT EXISTS public.veil_archive_job_cancel_receipts (
    job_id uuid NOT NULL REFERENCES public.veil_archive_jobs(id),
    admin_actor_id bigint NOT NULL REFERENCES public.veil_archive_auth_users(id),
    idempotency_key text NOT NULL CHECK (idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$'),
    request_hash text NOT NULL CHECK (request_hash ~ '^[a-f0-9]{64}$'),
    job_snapshot jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (job_id, admin_actor_id, idempotency_key)
);
ALTER TABLE public.veil_archive_job_cancel_receipts ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.veil_archive_job_cancel_receipts FROM PUBLIC, anon, authenticated, service_role;

CREATE OR REPLACE FUNCTION public.veil_archive_job_cancel(
    p_job_id uuid, p_expected_revision bigint, p_admin_actor_id bigint,
    p_owner_actor_id bigint, p_idempotency_key text, p_request_hash text,
    p_kind text DEFAULT 'creative'
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_job public.veil_archive_jobs%ROWTYPE;
    v_receipt public.veil_archive_job_cancel_receipts%ROWTYPE;
    v_snapshot jsonb;
    v_now timestamptz := clock_timestamp();
BEGIN
    PERFORM public.veil_archive_assert_service_role();
    IF p_job_id IS NULL OR p_expected_revision IS NULL OR p_expected_revision < 0
       OR p_admin_actor_id IS NULL OR p_admin_actor_id <= 0
       OR p_owner_actor_id IS NULL OR p_owner_actor_id <= 0
       OR p_kind IS DISTINCT FROM 'creative'
       OR p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$'
       OR p_request_hash IS NULL OR p_request_hash !~ '^[a-f0-9]{64}$' THEN
        RETURN jsonb_build_object('ok',false,'status',422,'code','JOB_CANCEL_INPUT_INVALID');
    END IF;
    -- Recheck current account status in the mutation transaction. FOR SHARE
    -- prevents concurrent privilege revocation until this short transaction ends.
    PERFORM id FROM public.veil_archive_auth_users WHERE id = p_admin_actor_id
        AND is_admin AND is_active AND NOT is_guest FOR SHARE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok',false,'status',403,'code','JOB_CANCEL_ADMIN_REQUIRED');
    END IF;
    SELECT * INTO v_job FROM public.veil_archive_jobs WHERE id = p_job_id
        AND kind = p_kind AND actor_id = p_owner_actor_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok',false,'status',404,'code','JOB_NOT_FOUND');
    END IF;
    SELECT * INTO v_receipt FROM public.veil_archive_job_cancel_receipts
        WHERE job_id = p_job_id AND admin_actor_id = p_admin_actor_id AND idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF v_receipt.request_hash IS DISTINCT FROM p_request_hash THEN
            RETURN jsonb_build_object('ok',false,'status',409,'code','JOB_CANCEL_IDEMPOTENCY_CONFLICT');
        END IF;
        RETURN jsonb_build_object('ok',true,'job',v_receipt.job_snapshot,'replayed',true);
    END IF;
    IF v_job.revision IS DISTINCT FROM p_expected_revision THEN
        RETURN jsonb_build_object('ok',false,'status',409,'code','JOB_CANCEL_REVISION_CONFLICT');
    END IF;
    IF v_job.state NOT IN ('QUEUED','RUNNING','REVIEW_REQUIRED','FAILED') THEN
        RETURN jsonb_build_object('ok',false,'status',409,'code','JOB_CANCEL_STATE_CONFLICT');
    END IF;
    IF (v_job.lease_until IS NOT NULL AND v_job.lease_until > v_now)
       OR (v_job.state = 'RUNNING' AND v_job.lease_until IS NULL) THEN
        RETURN jsonb_build_object('ok',false,'status',409,'code','JOB_CANCEL_LEASE_ACTIVE');
    END IF;
    -- All dispatch/reserve/finish RPCs lock the same job row first. This test
    -- therefore cannot race a new dispatch. Unknown usage is never retried or
    -- refunded, and even a pending undispatched reservation needs reconciliation.
    IF EXISTS (SELECT 1 FROM public.veil_archive_usage WHERE job_id = p_job_id
               AND state IN ('IN_FLIGHT','UNKNOWN')) THEN
        RETURN jsonb_build_object('ok',false,'status',409,'code','JOB_CANCEL_RESULT_UNKNOWN');
    END IF;
    IF EXISTS (SELECT 1 FROM public.veil_archive_usage WHERE job_id = p_job_id AND state = 'RESERVED') THEN
        RETURN jsonb_build_object('ok',false,'status',409,'code','JOB_CANCEL_USAGE_PENDING');
    END IF;
    UPDATE public.veil_archive_jobs SET state = 'CANCELLED', revision = revision + 1,
        fence = fence + 1, lease_token = NULL, lease_until = NULL, updated_at = v_now,
        payload = payload || jsonb_build_object('cancelled_by',p_admin_actor_id,'cancelled_at',v_now)
        WHERE id = p_job_id RETURNING * INTO v_job;
    -- charged_cny and ALL usage records are preserved exactly.
    v_snapshot := to_jsonb(v_job);
    INSERT INTO public.veil_archive_job_cancel_receipts
        (job_id,admin_actor_id,idempotency_key,request_hash,job_snapshot)
        VALUES (p_job_id,p_admin_actor_id,p_idempotency_key,p_request_hash,v_snapshot);
    RETURN jsonb_build_object('ok',true,'job',v_snapshot,'replayed',false);
END $$;

REVOKE ALL ON FUNCTION public.veil_archive_job_cancel(uuid,bigint,bigint,bigint,text,text,text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.veil_archive_job_cancel(uuid,bigint,bigint,bigint,text,text,text) TO service_role;
COMMIT;
