-- Apply only after 001 creates veil_archive_assert_service_role().
-- New CloudBase-only tables: never modify legacy publication/game tables.
BEGIN;

CREATE TABLE IF NOT EXISTS public.veil_archive_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_id bigint NOT NULL CHECK (actor_id > 0),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 128),
    kind text NOT NULL CHECK (kind IN ('creative', 'authoring', 'speech')),
    request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    request_payload jsonb NOT NULL CHECK (jsonb_typeof(request_payload) = 'object'),
    payload jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(payload) = 'object'),
    state text NOT NULL DEFAULT 'QUEUED' CHECK (state IN ('QUEUED', 'RUNNING', 'REVIEW_REQUIRED',
        'COMPLETED', 'FAILED', 'NEEDS_RECONCILIATION')),
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    fence bigint NOT NULL DEFAULT 0 CHECK (fence >= 0),
    lease_token uuid,
    lease_until timestamptz,
    budget_cny numeric(14,8) NOT NULL CHECK (budget_cny BETWEEN 0 AND 1),
    charged_cny numeric(14,8) NOT NULL DEFAULT 0 CHECK (charged_cny >= 0 AND charged_cny <= budget_cny),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (actor_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS public.veil_archive_usage (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL REFERENCES public.veil_archive_jobs(id),
    step_key text NOT NULL CHECK (step_key ~ '^[a-zA-Z0-9_.:-]{1,128}$'),
    prepared_hash text NOT NULL CHECK (prepared_hash ~ '^[0-9a-f]{64}$'),
    prepared jsonb NOT NULL CHECK (jsonb_typeof(prepared) = 'object'),
    state text NOT NULL DEFAULT 'RESERVED' CHECK (state IN ('RESERVED', 'IN_FLIGHT', 'SUCCEEDED', 'FAILED', 'UNKNOWN')),
    reservation_cny numeric(14,8) NOT NULL CHECK (reservation_cny > 0 AND reservation_cny <= 1),
    charged_cny numeric(14,8) NOT NULL CHECK (charged_cny >= 0 AND charged_cny <= reservation_cny),
    dispatch_fence bigint,
    result_hash text CHECK (result_hash ~ '^[0-9a-f]{64}$'),
    result jsonb,
    receipt jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    dispatched_at timestamptz,
    completed_at timestamptz,
    UNIQUE (job_id, step_key)
);

-- Database switch AND the backend's explicit per-call confirmation are required.
-- No public RPC enables this switch. Default migration never authorizes payment.
CREATE TABLE IF NOT EXISTS public.veil_archive_job_settings (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    paid_execution_enabled boolean NOT NULL DEFAULT false
);
INSERT INTO public.veil_archive_job_settings(singleton) VALUES (true) ON CONFLICT DO NOTHING;

ALTER TABLE public.veil_archive_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.veil_archive_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.veil_archive_job_settings ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.veil_archive_jobs, public.veil_archive_usage, public.veil_archive_job_settings FROM PUBLIC, anon, authenticated, service_role;

CREATE OR REPLACE FUNCTION public.veil_archive_job_create(
    p_actor_id bigint, p_idempotency_key text, p_kind text, p_request_hash text,
    p_request_payload jsonb, p_budget_cny numeric DEFAULT 0
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE v_job public.veil_archive_jobs%ROWTYPE;
BEGIN
    PERFORM public.veil_archive_assert_service_role();
    IF p_actor_id IS NULL OR p_actor_id <= 0 OR p_idempotency_key IS NULL OR length(p_idempotency_key) NOT BETWEEN 1 AND 128
       OR p_kind IS NULL OR p_kind NOT IN ('creative','authoring','speech')
       OR p_request_hash IS NULL OR p_request_hash !~ '^[0-9a-f]{64}$'
       OR jsonb_typeof(p_request_payload) IS DISTINCT FROM 'object'
       OR octet_length(p_request_payload::text) > 1048576
       OR p_budget_cny IS NULL OR p_budget_cny < 0 OR p_budget_cny > 1
       OR p_budget_cny != round(p_budget_cny,8) THEN
        RETURN jsonb_build_object('ok',false,'status',400,'code','JOB_INPUT_INVALID');
    END IF;
    INSERT INTO public.veil_archive_jobs(actor_id,idempotency_key,kind,request_hash,request_payload,budget_cny)
        VALUES(p_actor_id,p_idempotency_key,p_kind,p_request_hash,p_request_payload,p_budget_cny)
        ON CONFLICT (actor_id,idempotency_key) DO NOTHING;
    SELECT * INTO v_job FROM public.veil_archive_jobs WHERE actor_id=p_actor_id AND idempotency_key=p_idempotency_key FOR UPDATE;
    IF v_job.request_hash != p_request_hash OR v_job.request_payload != p_request_payload OR v_job.kind != p_kind OR v_job.budget_cny != p_budget_cny THEN
        RETURN jsonb_build_object('ok',false,'status',409,'code','JOB_IDEMPOTENCY_CONFLICT');
    END IF;
    RETURN jsonb_build_object('ok',true,'job',to_jsonb(v_job));
END $$;

CREATE OR REPLACE FUNCTION public.veil_archive_job_get(p_job_id uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE v_job public.veil_archive_jobs%ROWTYPE;
BEGIN
    PERFORM public.veil_archive_assert_service_role();
    SELECT * INTO v_job FROM public.veil_archive_jobs WHERE id=p_job_id;
    IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'status',404,'code','JOB_NOT_FOUND'); END IF;
    RETURN jsonb_build_object('ok',true,'job',to_jsonb(v_job));
END $$;

CREATE OR REPLACE FUNCTION public.veil_archive_usage_get(p_job_id uuid,p_step_key text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE v_usage public.veil_archive_usage%ROWTYPE;
BEGIN
    PERFORM public.veil_archive_assert_service_role();
    SELECT * INTO v_usage FROM public.veil_archive_usage WHERE job_id=p_job_id AND step_key=p_step_key;
    IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'status',404,'code','USAGE_NOT_FOUND'); END IF;
    RETURN jsonb_build_object('ok',true,'usage',to_jsonb(v_usage));
END $$;

CREATE OR REPLACE FUNCTION public.veil_archive_job_list(p_actor_id bigint DEFAULT NULL,p_kind text DEFAULT NULL,
    p_limit integer DEFAULT 50,p_offset integer DEFAULT 0) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE v_jobs jsonb;
BEGIN
    PERFORM public.veil_archive_assert_service_role();
    IF (p_actor_id IS NOT NULL AND p_actor_id<=0) OR (p_kind IS NOT NULL AND p_kind NOT IN ('creative','authoring','speech'))
       OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 OR p_offset IS NULL OR p_offset NOT BETWEEN 0 AND 10000 THEN
        RETURN jsonb_build_object('ok',false,'status',400,'code','JOB_LIST_INPUT_INVALID'); END IF;
    SELECT coalesce(jsonb_agg(item.summary ORDER BY item.created_at DESC,item.id),'[]'::jsonb) INTO v_jobs FROM (
        SELECT id,created_at,jsonb_build_object('id',id,'actor_id',actor_id,'kind',kind,'state',state,'revision',revision,
            'budget_cny',budget_cny,'charged_cny',charged_cny,'created_at',created_at,'updated_at',updated_at) AS summary
        FROM public.veil_archive_jobs WHERE (p_actor_id IS NULL OR actor_id=p_actor_id) AND (p_kind IS NULL OR kind=p_kind)
        ORDER BY created_at DESC,id LIMIT p_limit OFFSET p_offset
    ) AS item;
    RETURN jsonb_build_object('ok',true,'jobs',v_jobs);
END $$;

CREATE OR REPLACE FUNCTION public.veil_archive_job_claim(p_job_id uuid, p_expected_revision bigint, p_lease_seconds integer DEFAULT 120)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE v_job public.veil_archive_jobs%ROWTYPE; v_now timestamptz := clock_timestamp();
BEGIN
    PERFORM public.veil_archive_assert_service_role();
    IF p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 5 AND 300 THEN
        RETURN jsonb_build_object('ok',false,'status',400,'code','JOB_LEASE_INVALID'); END IF;
    SELECT * INTO v_job FROM public.veil_archive_jobs WHERE id=p_job_id FOR UPDATE;
    IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'status',404,'code','JOB_NOT_FOUND'); END IF;
    IF p_expected_revision IS NULL OR v_job.revision != p_expected_revision OR v_job.state NOT IN ('QUEUED','RUNNING')
       OR (v_job.lease_until IS NOT NULL AND v_job.lease_until > v_now) THEN
        RETURN jsonb_build_object('ok',false,'status',409,'code','JOB_CLAIM_CONFLICT'); END IF;
    IF EXISTS (SELECT 1 FROM public.veil_archive_usage WHERE job_id=p_job_id AND state IN ('IN_FLIGHT','UNKNOWN')) THEN
        UPDATE public.veil_archive_usage SET state='UNKNOWN' WHERE job_id=p_job_id AND state='IN_FLIGHT';
        UPDATE public.veil_archive_jobs SET state='NEEDS_RECONCILIATION', lease_token=NULL,lease_until=NULL,
            revision=revision+1,updated_at=v_now WHERE id=p_job_id;
        RETURN jsonb_build_object('ok',false,'status',409,'code','JOB_RESULT_UNKNOWN'); END IF;
    UPDATE public.veil_archive_jobs SET state='RUNNING',revision=revision+1,fence=fence+1,
        lease_token=gen_random_uuid(),lease_until=v_now+make_interval(secs=>p_lease_seconds),updated_at=v_now
        WHERE id=p_job_id RETURNING * INTO v_job;
    RETURN jsonb_build_object('ok',true,'job',to_jsonb(v_job));
END $$;

CREATE OR REPLACE FUNCTION public.veil_archive_job_save(p_job_id uuid,p_token uuid,p_fence bigint,
    p_expected_revision bigint,p_payload jsonb,p_state text DEFAULT 'QUEUED') RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE v_job public.veil_archive_jobs%ROWTYPE;
BEGIN
    PERFORM public.veil_archive_assert_service_role();
    IF jsonb_typeof(p_payload) IS DISTINCT FROM 'object' OR octet_length(p_payload::text)>1048576
        OR p_state IS NULL OR p_state NOT IN ('QUEUED','REVIEW_REQUIRED','COMPLETED','FAILED','NEEDS_RECONCILIATION') THEN
        RETURN jsonb_build_object('ok',false,'status',400,'code','JOB_INPUT_INVALID'); END IF;
    SELECT * INTO v_job FROM public.veil_archive_jobs WHERE id=p_job_id FOR UPDATE;
    IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'status',404,'code','JOB_NOT_FOUND'); END IF;
    IF v_job.state != 'RUNNING' OR v_job.lease_token IS DISTINCT FROM p_token OR p_token IS NULL
        OR v_job.fence IS DISTINCT FROM p_fence OR v_job.revision IS DISTINCT FROM p_expected_revision
        OR v_job.lease_until IS NULL OR v_job.lease_until<=clock_timestamp() THEN
        RETURN jsonb_build_object('ok',false,'status',409,'code','JOB_LEASE_CONFLICT'); END IF;
    IF EXISTS (SELECT 1 FROM public.veil_archive_usage WHERE job_id=p_job_id AND state IN ('IN_FLIGHT','UNKNOWN'))
        AND p_state != 'NEEDS_RECONCILIATION' THEN
        RETURN jsonb_build_object('ok',false,'status',409,'code','JOB_RESULT_UNKNOWN'); END IF;
    IF p_state IN ('REVIEW_REQUIRED','COMPLETED') AND EXISTS (
        SELECT 1 FROM public.veil_archive_usage WHERE job_id=p_job_id AND state='RESERVED') THEN
        RETURN jsonb_build_object('ok',false,'status',409,'code','JOB_USAGE_PENDING'); END IF;
    UPDATE public.veil_archive_jobs SET payload=p_payload,state=p_state,revision=revision+1,
        lease_token=NULL,lease_until=NULL,updated_at=clock_timestamp() WHERE id=p_job_id RETURNING * INTO v_job;
    RETURN jsonb_build_object('ok',true,'job',to_jsonb(v_job));
END $$;

CREATE OR REPLACE FUNCTION public.veil_archive_usage_reserve(p_job_id uuid,p_token uuid,p_fence bigint,
    p_step_key text,p_prepared_hash text,p_prepared jsonb,p_reservation_cny numeric,p_allow_paid boolean DEFAULT false)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE v_job public.veil_archive_jobs%ROWTYPE; v_usage public.veil_archive_usage%ROWTYPE;
BEGIN
    PERFORM public.veil_archive_assert_service_role();
    IF p_allow_paid IS DISTINCT FROM true OR NOT EXISTS (SELECT 1 FROM public.veil_archive_job_settings WHERE singleton AND paid_execution_enabled) THEN
        RETURN jsonb_build_object('ok',false,'status',403,'code','PAID_EXECUTION_DISABLED'); END IF;
    IF p_step_key IS NULL OR p_step_key !~ '^[a-zA-Z0-9_.:-]{1,128}$' OR p_prepared_hash IS NULL OR p_prepared_hash !~ '^[0-9a-f]{64}$'
       OR jsonb_typeof(p_prepared) IS DISTINCT FROM 'object' OR octet_length(p_prepared::text)>1048576
       OR p_reservation_cny IS NULL OR p_reservation_cny<=0 OR p_reservation_cny>1 OR p_reservation_cny!=round(p_reservation_cny,8) THEN
        RETURN jsonb_build_object('ok',false,'status',400,'code','USAGE_INPUT_INVALID'); END IF;
    SELECT * INTO v_job FROM public.veil_archive_jobs WHERE id=p_job_id FOR UPDATE;
    IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'status',404,'code','JOB_NOT_FOUND'); END IF;
    IF v_job.state != 'RUNNING' OR p_token IS NULL OR v_job.lease_token IS DISTINCT FROM p_token
       OR v_job.fence IS DISTINCT FROM p_fence OR v_job.lease_until IS NULL OR v_job.lease_until<=clock_timestamp() THEN
        RETURN jsonb_build_object('ok',false,'status',409,'code','JOB_LEASE_CONFLICT'); END IF;
    SELECT * INTO v_usage FROM public.veil_archive_usage WHERE job_id=p_job_id AND step_key=p_step_key;
    IF FOUND THEN
        IF v_usage.prepared_hash!=p_prepared_hash OR v_usage.prepared!=p_prepared OR v_usage.reservation_cny!=p_reservation_cny THEN
            RETURN jsonb_build_object('ok',false,'status',409,'code','USAGE_IDEMPOTENCY_CONFLICT'); END IF;
        RETURN jsonb_build_object('ok',true,'usage',to_jsonb(v_usage),'created',false);
    END IF;
    IF EXISTS (SELECT 1 FROM public.veil_archive_usage WHERE job_id=p_job_id AND state IN ('RESERVED','IN_FLIGHT','UNKNOWN'))
       OR v_job.charged_cny+p_reservation_cny>v_job.budget_cny
       OR (SELECT count(*) FROM public.veil_archive_usage WHERE job_id=p_job_id)>=32 THEN
        RETURN jsonb_build_object('ok',false,'status',409,'code','USAGE_BUDGET_OR_PENDING'); END IF;
    INSERT INTO public.veil_archive_usage(job_id,step_key,prepared_hash,prepared,reservation_cny,charged_cny)
        VALUES(p_job_id,p_step_key,p_prepared_hash,p_prepared,p_reservation_cny,p_reservation_cny) RETURNING * INTO v_usage;
    UPDATE public.veil_archive_jobs SET charged_cny=charged_cny+p_reservation_cny,updated_at=clock_timestamp() WHERE id=p_job_id;
    RETURN jsonb_build_object('ok',true,'usage',to_jsonb(v_usage),'created',true);
END $$;

CREATE OR REPLACE FUNCTION public.veil_archive_usage_dispatch(p_job_id uuid,p_token uuid,p_fence bigint,p_usage_id uuid,p_allow_paid boolean DEFAULT false)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE v_job public.veil_archive_jobs%ROWTYPE; v_usage public.veil_archive_usage%ROWTYPE;
BEGIN
    PERFORM public.veil_archive_assert_service_role();
    IF p_allow_paid IS DISTINCT FROM true OR NOT EXISTS (SELECT 1 FROM public.veil_archive_job_settings WHERE singleton AND paid_execution_enabled) THEN
        RETURN jsonb_build_object('ok',false,'status',403,'code','PAID_EXECUTION_DISABLED'); END IF;
    SELECT * INTO v_job FROM public.veil_archive_jobs WHERE id=p_job_id FOR UPDATE;
    IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'status',404,'code','JOB_NOT_FOUND'); END IF;
    IF v_job.state != 'RUNNING' OR p_token IS NULL OR v_job.lease_token IS DISTINCT FROM p_token OR v_job.fence IS DISTINCT FROM p_fence
       OR v_job.lease_until IS NULL OR v_job.lease_until<=clock_timestamp() THEN
        RETURN jsonb_build_object('ok',false,'status',409,'code','JOB_LEASE_CONFLICT'); END IF;
    UPDATE public.veil_archive_usage SET state='IN_FLIGHT',dispatch_fence=p_fence,dispatched_at=clock_timestamp()
        WHERE id=p_usage_id AND job_id=p_job_id AND state='RESERVED' RETURNING * INTO v_usage;
    IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'status',409,'code','USAGE_ALREADY_DISPATCHED'); END IF;
    -- Only this successful transition grants permission for ONE external call.
    -- If the HTTP acknowledgment is lost, reconcile; never repeat dispatch/call.
    RETURN jsonb_build_object('ok',true,'usage',to_jsonb(v_usage),'dispatch_permitted',true);
END $$;

CREATE OR REPLACE FUNCTION public.veil_archive_usage_finish(p_job_id uuid,p_token uuid,p_fence bigint,p_usage_id uuid,
    p_state text,p_result_hash text,p_result jsonb,p_receipt jsonb,p_actual_cost_cny numeric DEFAULT NULL) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE v_job public.veil_archive_jobs%ROWTYPE; v_usage public.veil_archive_usage%ROWTYPE; v_charge numeric;
BEGIN
    PERFORM public.veil_archive_assert_service_role();
    IF p_state IS NULL OR p_state NOT IN ('SUCCEEDED','FAILED','UNKNOWN') OR p_result_hash IS NULL OR p_result_hash !~ '^[0-9a-f]{64}$'
       OR jsonb_typeof(p_receipt) IS DISTINCT FROM 'object' OR p_result IS NULL
       OR octet_length(p_result::text)>1048576 OR octet_length(p_receipt::text)>65536
       OR (p_actual_cost_cny IS NOT NULL AND (p_actual_cost_cny<0 OR p_actual_cost_cny!=round(p_actual_cost_cny,8))) THEN
        RETURN jsonb_build_object('ok',false,'status',400,'code','USAGE_INPUT_INVALID'); END IF;
    SELECT * INTO v_job FROM public.veil_archive_jobs WHERE id=p_job_id FOR UPDATE;
    IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'status',404,'code','JOB_NOT_FOUND'); END IF;
    SELECT * INTO v_usage FROM public.veil_archive_usage WHERE id=p_usage_id AND job_id=p_job_id FOR UPDATE;
    IF NOT FOUND THEN RETURN jsonb_build_object('ok',false,'status',404,'code','USAGE_NOT_FOUND'); END IF;
    IF v_usage.state IN ('SUCCEEDED','FAILED','UNKNOWN') THEN
        IF v_usage.dispatch_fence IS NOT DISTINCT FROM p_fence
           AND v_usage.result_hash=p_result_hash AND v_usage.state=p_state AND v_usage.result=p_result AND v_usage.receipt=p_receipt
           AND (p_state='UNKNOWN' OR p_actual_cost_cny IS NULL OR p_actual_cost_cny=v_usage.charged_cny) THEN
            RETURN jsonb_build_object('ok',true,'usage',to_jsonb(v_usage),'completed',false); END IF;
        RETURN jsonb_build_object('ok',false,'status',409,'code','USAGE_RESULT_CONFLICT'); END IF;
    IF v_job.state != 'RUNNING' OR p_token IS NULL OR v_job.lease_token IS DISTINCT FROM p_token OR v_job.fence IS DISTINCT FROM p_fence
       OR v_job.lease_until IS NULL OR v_job.lease_until<=clock_timestamp() THEN
        RETURN jsonb_build_object('ok',false,'status',409,'code','JOB_LEASE_CONFLICT'); END IF;
    IF v_usage.state!='IN_FLIGHT' OR v_usage.dispatch_fence IS DISTINCT FROM p_fence THEN
        RETURN jsonb_build_object('ok',false,'status',409,'code','USAGE_NOT_IN_FLIGHT'); END IF;
    IF p_actual_cost_cny>v_usage.reservation_cny THEN
        RETURN jsonb_build_object('ok',false,'status',409,'code','USAGE_COST_EXCEEDS_RESERVATION'); END IF;
    -- Unknown calls retain their full reservation even if a caller supplies 0.
    v_charge := CASE WHEN p_state='UNKNOWN' THEN v_usage.reservation_cny ELSE coalesce(p_actual_cost_cny,v_usage.reservation_cny) END;
    UPDATE public.veil_archive_usage SET state=p_state,result_hash=p_result_hash,result=p_result,receipt=p_receipt,
        charged_cny=v_charge,completed_at=clock_timestamp() WHERE id=p_usage_id RETURNING * INTO v_usage;
    UPDATE public.veil_archive_jobs SET charged_cny=charged_cny-(v_usage.reservation_cny-v_charge),updated_at=clock_timestamp() WHERE id=p_job_id;
    RETURN jsonb_build_object('ok',true,'usage',to_jsonb(v_usage),'completed',true);
END $$;

-- EXECUTE alone is insufficient in some CloudBase gateways; every function also
-- checks the gateway-verified service_role claims inside its body above.
REVOKE ALL ON FUNCTION public.veil_archive_job_create(bigint,text,text,text,jsonb,numeric),
 public.veil_archive_job_get(uuid), public.veil_archive_job_list(bigint,text,integer,integer),
 public.veil_archive_usage_get(uuid,text), public.veil_archive_job_claim(uuid,bigint,integer),
 public.veil_archive_job_save(uuid,uuid,bigint,bigint,jsonb,text),
 public.veil_archive_usage_reserve(uuid,uuid,bigint,text,text,jsonb,numeric,boolean),
 public.veil_archive_usage_dispatch(uuid,uuid,bigint,uuid,boolean),
 public.veil_archive_usage_finish(uuid,uuid,bigint,uuid,text,text,jsonb,jsonb,numeric) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.veil_archive_job_create(bigint,text,text,text,jsonb,numeric),
 public.veil_archive_job_get(uuid), public.veil_archive_job_list(bigint,text,integer,integer),
 public.veil_archive_usage_get(uuid,text), public.veil_archive_job_claim(uuid,bigint,integer),
 public.veil_archive_job_save(uuid,uuid,bigint,bigint,jsonb,text),
 public.veil_archive_usage_reserve(uuid,uuid,bigint,text,text,jsonb,numeric,boolean),
 public.veil_archive_usage_dispatch(uuid,uuid,bigint,uuid,boolean),
 public.veil_archive_usage_finish(uuid,uuid,bigint,uuid,text,text,jsonb,jsonb,numeric) TO service_role;
COMMIT;
