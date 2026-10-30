-- CloudBase shared PostgreSQL: install explicitly with its SQL editor/migration API.
-- Only new public.veil_archive_* objects are changed. No extension or DB password.
-- Source: https://docs.cloudbase.net/database/postgresql/rpc
-- REST gateway currently exposes public only and does not enforce EXECUTE grants:
-- EVERY SECURITY DEFINER RPC must check the gateway-injected service_role claim.
-- Never expose the service_role API key to clients, or provide arbitrary SQL RPCs.
-- Existing formal review/publication checks remain Python domain responsibilities.
-- This store supplies durability/CAS, not approval of a release/preview manifest.
BEGIN;

CREATE TABLE IF NOT EXISTS public.veil_archive_records (
    kind text NOT NULL CHECK (kind IN (
        'user', 'auth_session', 'catalog', 'source', 'candidate', 'review', 'release',
        'opening', 'play', 'authoring_job', 'model_receipt', 'artifact', 'cache',
        'rate_limit', 'play_workspace', 'admin_workspace', 'preview_manifest'
    )),
    record_key text NOT NULL CHECK (record_key ~ '^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,191}$'),
    owner text NOT NULL CHECK (owner ~ '^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,191}$'),
    version bigint NOT NULL CHECK (version BETWEEN 1 AND 9007199254740991),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'
                                AND octet_length(payload::text) <= 2097152),
    digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (kind, record_key)
);
CREATE INDEX IF NOT EXISTS veil_archive_records_owner_idx
    ON public.veil_archive_records (kind, owner, record_key COLLATE "C");

-- Each successful write saves its exact original record and event in one TX.
-- This intentionally favors unambiguous replay over compaction; no receipt TTL.
CREATE TABLE IF NOT EXISTS public.veil_archive_receipts (
    kind text NOT NULL,
    record_key text NOT NULL,
    owner text NOT NULL,
    idempotency_key text NOT NULL CHECK (idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,191}$'),
    operation text NOT NULL CHECK (operation IN ('create', 'cas_append')),
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    version bigint NOT NULL,
    record_json jsonb NOT NULL,
    event jsonb CHECK (event IS NULL OR (jsonb_typeof(event) = 'object'
                                        AND octet_length(event::text) <= 131072)),
    created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
    PRIMARY KEY (kind, record_key, idempotency_key),
    UNIQUE (kind, record_key, version),
    FOREIGN KEY (kind, record_key) REFERENCES public.veil_archive_records (kind, record_key)
);

ALTER TABLE public.veil_archive_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.veil_archive_receipts ENABLE ROW LEVEL SECURITY;
-- No anon/authenticated policies. Also deny direct table access to service_role:
-- all normal application access must go through the bounded RPCs below.
REVOKE ALL ON TABLE public.veil_archive_records, public.veil_archive_receipts
    FROM PUBLIC, anon, authenticated, service_role;

CREATE OR REPLACE FUNCTION public.veil_archive_assert_service_role()
RETURNS void LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    -- IS DISTINCT FROM rejects missing/null claims as well as an incorrect role.
    IF (NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role')
        IS DISTINCT FROM 'service_role' THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'VEIL_SERVICE_ROLE_REQUIRED';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.veil_archive_validate_identity(p_kind text, p_key text, p_owner text)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    PERFORM public.veil_archive_assert_service_role();
    IF p_kind IS NULL OR p_kind NOT IN (
        'user', 'auth_session', 'catalog', 'source', 'candidate', 'review', 'release',
        'opening', 'play', 'authoring_job', 'model_receipt', 'artifact', 'cache',
        'rate_limit', 'play_workspace', 'admin_workspace', 'preview_manifest'
    ) OR p_key IS NULL OR p_key !~ '^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,191}$'
      OR p_owner IS NULL OR p_owner !~ '^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,191}$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'VEIL_INVALID_IDENTITY';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.veil_archive_validate_write(
    p_payload jsonb, p_digest text, p_idempotency_key text, p_request_digest text
)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    PERFORM public.veil_archive_assert_service_role();
    -- Python limits compact payloads to 1 MiB. jsonb::text can add whitespace;
    -- its independent 2 MiB DB bound remains enforced even for a raw RPC call.
    IF p_payload IS NULL OR jsonb_typeof(p_payload) <> 'object'
      OR octet_length(p_payload::text) > 2097152
      OR p_digest IS NULL OR p_digest !~ '^[0-9a-f]{64}$'
      OR p_idempotency_key IS NULL OR p_idempotency_key !~ '^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,191}$'
      OR p_request_digest IS NULL OR p_request_digest !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'VEIL_INVALID_WRITE';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION public.veil_archive_get(p_kind text, p_key text, p_owner text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE v_row public.veil_archive_records%ROWTYPE;
BEGIN
    PERFORM public.veil_archive_assert_service_role();
    PERFORM public.veil_archive_validate_identity(p_kind, p_key, p_owner);
    SELECT * INTO v_row FROM public.veil_archive_records
        WHERE kind = p_kind AND record_key = p_key AND owner = p_owner;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'status', 404, 'code', 'RECORD_NOT_FOUND');
    END IF;
    RETURN jsonb_build_object('ok', true, 'record', jsonb_build_object(
        'kind', v_row.kind, 'key', v_row.record_key, 'owner', v_row.owner,
        'version', v_row.version, 'payload', v_row.payload, 'digest', v_row.digest));
END;
$$;

CREATE OR REPLACE FUNCTION public.veil_archive_list(
    p_kind text, p_owner text, p_after text DEFAULT NULL, p_limit integer DEFAULT 20
)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE v_rows jsonb;
BEGIN
    PERFORM public.veil_archive_assert_service_role();
    PERFORM public.veil_archive_validate_identity(p_kind, COALESCE(p_after, 'list'), p_owner);
    IF p_limit IS NULL OR p_limit < 1 OR p_limit > 25 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'VEIL_INVALID_LIMIT';
    END IF;
    SELECT COALESCE(jsonb_agg(row_json ORDER BY record_key COLLATE "C"), '[]'::jsonb)
    INTO v_rows FROM (
        SELECT record_key, jsonb_build_object(
            'kind', kind, 'key', record_key, 'owner', owner, 'version', version,
            'payload', payload, 'digest', digest) AS row_json
        FROM public.veil_archive_records
        WHERE kind = p_kind AND owner = p_owner
            AND (p_after IS NULL OR record_key COLLATE "C" > p_after COLLATE "C")
        ORDER BY record_key COLLATE "C" LIMIT p_limit
    ) AS page;
    RETURN jsonb_build_object('ok', true, 'records', v_rows);
END;
$$;

CREATE OR REPLACE FUNCTION public.veil_archive_create(
    p_kind text, p_key text, p_owner text, p_payload jsonb, p_digest text,
    p_idempotency_key text, p_request_digest text
)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_row public.veil_archive_records%ROWTYPE;
    v_receipt public.veil_archive_receipts%ROWTYPE;
    v_inserted boolean;
    v_record jsonb;
BEGIN
    PERFORM public.veil_archive_assert_service_role();
    PERFORM public.veil_archive_validate_identity(p_kind, p_key, p_owner);
    PERFORM public.veil_archive_validate_write(p_payload, p_digest, p_idempotency_key, p_request_digest);
    INSERT INTO public.veil_archive_records (kind, record_key, owner, version, payload, digest)
        VALUES (p_kind, p_key, p_owner, 1, p_payload, p_digest)
        ON CONFLICT (kind, record_key) DO NOTHING RETURNING * INTO v_row;
    v_inserted := FOUND;
    IF NOT v_inserted THEN
        -- Serializes competing creates and waits for a committed receipt.
        SELECT * INTO v_row FROM public.veil_archive_records
            WHERE kind = p_kind AND record_key = p_key FOR UPDATE;
        IF v_row.owner IS DISTINCT FROM p_owner THEN
            RETURN jsonb_build_object('ok', false, 'status', 404, 'code', 'RECORD_NOT_FOUND');
        END IF;
        SELECT * INTO v_receipt FROM public.veil_archive_receipts
            WHERE kind = p_kind AND record_key = p_key AND idempotency_key = p_idempotency_key;
        IF FOUND THEN
            IF v_receipt.owner IS DISTINCT FROM p_owner OR v_receipt.operation <> 'create'
                OR v_receipt.request_digest IS DISTINCT FROM p_request_digest THEN
                RETURN jsonb_build_object('ok', false, 'status', 409, 'code', 'IDEMPOTENCY_CONFLICT');
            END IF;
            RETURN jsonb_build_object('ok', true, 'record', v_receipt.record_json,
                                      'replayed', true, 'event', v_receipt.event);
        END IF;
        RETURN jsonb_build_object('ok', false, 'status', 409, 'code', 'RECORD_EXISTS');
    END IF;
    v_record := jsonb_build_object('kind', v_row.kind, 'key', v_row.record_key,
        'owner', v_row.owner, 'version', v_row.version, 'payload', v_row.payload, 'digest', v_row.digest);
    INSERT INTO public.veil_archive_receipts
        (kind, record_key, owner, idempotency_key, operation, request_digest, version, record_json, event)
        VALUES (p_kind, p_key, p_owner, p_idempotency_key, 'create', p_request_digest, 1, v_record, NULL);
    RETURN jsonb_build_object('ok', true, 'record', v_record, 'replayed', false, 'event', NULL);
END;
$$;

CREATE OR REPLACE FUNCTION public.veil_archive_cas_append(
    p_kind text, p_key text, p_owner text, p_expected_version bigint, p_payload jsonb,
    p_digest text, p_event jsonb, p_idempotency_key text, p_request_digest text
)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_row public.veil_archive_records%ROWTYPE;
    v_receipt public.veil_archive_receipts%ROWTYPE;
    v_record jsonb;
BEGIN
    PERFORM public.veil_archive_assert_service_role();
    PERFORM public.veil_archive_validate_identity(p_kind, p_key, p_owner);
    PERFORM public.veil_archive_validate_write(p_payload, p_digest, p_idempotency_key, p_request_digest);
    IF p_expected_version IS NULL OR p_expected_version < 1 OR p_expected_version >= 9007199254740991
      OR p_event IS NULL OR jsonb_typeof(p_event) <> 'object' OR octet_length(p_event::text) > 131072 THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'VEIL_INVALID_APPEND';
    END IF;
    SELECT * INTO v_row FROM public.veil_archive_records
        WHERE kind = p_kind AND record_key = p_key AND owner = p_owner FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('ok', false, 'status', 404, 'code', 'RECORD_NOT_FOUND');
    END IF;
    -- Receipt BEFORE expected_version: replay is valid after later writes too.
    SELECT * INTO v_receipt FROM public.veil_archive_receipts
        WHERE kind = p_kind AND record_key = p_key AND idempotency_key = p_idempotency_key;
    IF FOUND THEN
        IF v_receipt.owner IS DISTINCT FROM p_owner OR v_receipt.operation <> 'cas_append'
            OR v_receipt.request_digest IS DISTINCT FROM p_request_digest THEN
            RETURN jsonb_build_object('ok', false, 'status', 409, 'code', 'IDEMPOTENCY_CONFLICT');
        END IF;
        RETURN jsonb_build_object('ok', true, 'record', v_receipt.record_json,
                                  'replayed', true, 'event', v_receipt.event);
    END IF;
    IF v_row.version <> p_expected_version THEN
        RETURN jsonb_build_object('ok', false, 'status', 409, 'code', 'VERSION_CONFLICT');
    END IF;
    UPDATE public.veil_archive_records SET payload = p_payload, digest = p_digest,
        version = version + 1, updated_at = statement_timestamp()
        WHERE kind = p_kind AND record_key = p_key RETURNING * INTO v_row;
    v_record := jsonb_build_object('kind', v_row.kind, 'key', v_row.record_key,
        'owner', v_row.owner, 'version', v_row.version, 'payload', v_row.payload, 'digest', v_row.digest);
    INSERT INTO public.veil_archive_receipts
        (kind, record_key, owner, idempotency_key, operation, request_digest, version, record_json, event)
        VALUES (p_kind, p_key, p_owner, p_idempotency_key, 'cas_append', p_request_digest,
                v_row.version, v_record, p_event);
    RETURN jsonb_build_object('ok', true, 'record', v_record, 'replayed', false, 'event', p_event);
END;
$$;

REVOKE ALL ON FUNCTION public.veil_archive_assert_service_role() FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.veil_archive_validate_identity(text, text, text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.veil_archive_validate_write(jsonb, text, text, text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.veil_archive_get(text, text, text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.veil_archive_list(text, text, text, integer) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.veil_archive_create(text, text, text, jsonb, text, text, text) FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION public.veil_archive_cas_append(text, text, text, bigint, jsonb, text, jsonb, text, text) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.veil_archive_assert_service_role() TO service_role;
GRANT EXECUTE ON FUNCTION public.veil_archive_get(text, text, text) TO service_role;
GRANT EXECUTE ON FUNCTION public.veil_archive_list(text, text, text, integer) TO service_role;
GRANT EXECUTE ON FUNCTION public.veil_archive_create(text, text, text, jsonb, text, text, text) TO service_role;
GRANT EXECUTE ON FUNCTION public.veil_archive_cas_append(text, text, text, bigint, jsonb, text, jsonb, text, text) TO service_role;

COMMIT;
