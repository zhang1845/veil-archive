-- Atomic runtime dependency checks for formal publication authority.
-- Leaves 001's RPC signature and cas_append receipt operation unchanged.
BEGIN;

CREATE OR REPLACE FUNCTION public.veil_archive_cas_append_guarded(
    p_kind text, p_key text, p_owner text, p_expected_version bigint, p_payload jsonb,
    p_digest text, p_event jsonb, p_idempotency_key text, p_request_digest text,
    p_guards jsonb
) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    v_guard jsonb;
    v_lock record;
    v_row public.veil_archive_records%ROWTYPE;
    v_target public.veil_archive_records%ROWTYPE;
    v_receipt public.veil_archive_receipts%ROWTYPE;
    v_found boolean;
    v_target_found boolean := false;
    v_dependencies_ok boolean := true;
BEGIN
    PERFORM public.veil_archive_assert_service_role();
    PERFORM public.veil_archive_validate_identity(p_kind,p_key,p_owner);
    PERFORM public.veil_archive_validate_write(p_payload,p_digest,p_idempotency_key,p_request_digest);
    IF p_expected_version IS NULL OR p_expected_version<1 OR p_expected_version>=9007199254740991
       OR p_event IS NULL OR jsonb_typeof(p_event)<>'object' OR octet_length(p_event::text)>131072
       OR p_guards IS NULL OR jsonb_typeof(p_guards)<>'array' THEN
        RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='VEIL_INVALID_GUARDED_APPEND';
    END IF;
    IF jsonb_array_length(p_guards)>8 THEN
        RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='VEIL_INVALID_GUARDS';
    END IF;
    FOR v_guard IN SELECT value FROM jsonb_array_elements(p_guards) LOOP
        IF jsonb_typeof(v_guard) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='VEIL_INVALID_GUARD';
        END IF;
        IF (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(v_guard) AS keys(k))
              IS DISTINCT FROM ARRAY['digest','expected_version','key','kind','owner']::text[]
           OR jsonb_typeof(v_guard->'kind') IS DISTINCT FROM 'string'
           OR jsonb_typeof(v_guard->'key') IS DISTINCT FROM 'string'
           OR jsonb_typeof(v_guard->'owner') IS DISTINCT FROM 'string'
           OR jsonb_typeof(v_guard->'digest') IS DISTINCT FROM 'string'
           OR (v_guard->>'digest') !~ '^[0-9a-f]{64}$'
           OR jsonb_typeof(v_guard->'expected_version') IS DISTINCT FROM 'number'
           OR (v_guard->>'expected_version') !~ '^[1-9][0-9]{0,15}$' THEN
            RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='VEIL_INVALID_GUARD';
        END IF;
        IF (v_guard->>'expected_version')::numeric>9007199254740991 THEN
            RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='VEIL_INVALID_GUARD';
        END IF;
        PERFORM public.veil_archive_validate_identity(v_guard->>'kind',v_guard->>'key',v_guard->>'owner');
    END LOOP;
    IF (SELECT count(*) FROM jsonb_array_elements(p_guards)) <>
       (SELECT count(*) FROM (SELECT DISTINCT value->>'kind',value->>'key' FROM jsonb_array_elements(p_guards)) AS identities) THEN
        RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='VEIL_DUPLICATE_GUARD';
    END IF;

    -- Acquire EVERY existing row in one global bytewise order. A call never
    -- takes its target lock first and then a lower-sorted dependency. Missing
    -- rows are remembered as failed guards, never re-read as unlocked rows.
    -- All normal writers in 001 lock only their target; the nested original CAS
    -- below re-enters a target lock already held by this ordered lock set.
    FOR v_lock IN
        SELECT identities.kind,identities.record_key FROM (
            SELECT p_kind AS kind,p_key AS record_key
            UNION
            SELECT value->>'kind',value->>'key' FROM jsonb_array_elements(p_guards)
        ) AS identities
        ORDER BY identities.kind COLLATE "C",identities.record_key COLLATE "C"
    LOOP
        SELECT * INTO v_row FROM public.veil_archive_records
            WHERE kind=v_lock.kind AND record_key=v_lock.record_key FOR UPDATE;
        v_found := FOUND;
        IF v_lock.kind=p_kind AND v_lock.record_key=p_key THEN
            v_target_found := v_found;
            v_target := v_row;
        END IF;
        FOR v_guard IN SELECT value FROM jsonb_array_elements(p_guards)
            WHERE value->>'kind'=v_lock.kind AND value->>'key'=v_lock.record_key
        LOOP
            IF NOT v_found OR v_row.owner IS DISTINCT FROM v_guard->>'owner'
               OR v_row.version IS DISTINCT FROM (v_guard->>'expected_version')::bigint
               OR v_row.digest IS DISTINCT FROM v_guard->>'digest' THEN
                v_dependencies_ok := false;
            END IF;
        END LOOP;
    END LOOP;
    IF NOT v_target_found OR v_target.owner IS DISTINCT FROM p_owner THEN
        RETURN jsonb_build_object('ok',false,'status',404,'code','RECORD_NOT_FOUND');
    END IF;

    -- Successful prior commands are returned verbatim even after revocation or
    -- later writes. A changed request digest cannot borrow an old receipt.
    SELECT * INTO v_receipt FROM public.veil_archive_receipts
        WHERE kind=p_kind AND record_key=p_key AND idempotency_key=p_idempotency_key;
    IF FOUND THEN
        IF v_receipt.owner IS DISTINCT FROM p_owner OR v_receipt.operation<>'cas_append'
           OR v_receipt.request_digest IS DISTINCT FROM p_request_digest THEN
            RETURN jsonb_build_object('ok',false,'status',409,'code','IDEMPOTENCY_CONFLICT');
        END IF;
        RETURN jsonb_build_object('ok',true,'record',v_receipt.record_json,'replayed',true,'event',v_receipt.event);
    END IF;
    IF NOT v_dependencies_ok THEN
        RETURN jsonb_build_object('ok',false,'status',409,'code','DEPENDENCY_CONFLICT');
    END IF;
    RETURN public.veil_archive_cas_append(p_kind,p_key,p_owner,p_expected_version,p_payload,
        p_digest,p_event,p_idempotency_key,p_request_digest);
END $$;

REVOKE ALL ON FUNCTION public.veil_archive_cas_append_guarded(text,text,text,bigint,jsonb,text,jsonb,text,text,jsonb)
    FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.veil_archive_cas_append_guarded(text,text,text,bigint,jsonb,text,jsonb,text,text,jsonb)
    TO service_role;
COMMIT;
