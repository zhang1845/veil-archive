"""Server-only, bounded CloudBase PostgreSQL HTTPS access.

Official protocol: https://docs.cloudbase.net/database/postgresql/rpc and
https://docs.cloudbase.net/database/postgresql/connecting-to-postgresql .
Only a backend service_role API key belongs here. Never send it to a browser.
The SQL functions independently verify the gateway-injected JWT role; decoding
a JWT in this client would not be an authorization check.

Writes are NOT retried automatically: a lost response can mean committed data.
An explicit repeat must use the original idempotency key and request digest.
This module does not replace a SQLAlchemy Session or execute arbitrary SQL.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Mapping, Protocol
from urllib.parse import unquote, urlsplit


ALLOWED_KINDS = frozenset({
    "user", "auth_session", "catalog", "source", "candidate", "review",
    "release", "opening", "play", "authoring_job", "model_receipt", "artifact",
    "cache", "rate_limit", "play_workspace", "admin_workspace", "preview_manifest",
})
MAX_PAYLOAD_BYTES = 1_048_576
MAX_EVENT_BYTES = 65_536
MAX_RESPONSE_BYTES = 8_388_608
_ENV = re.compile(r"[a-z0-9][a-z0-9-]{1,126}[a-z0-9]\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@-]{0,191}\Z")
_RPC = re.compile(r"veil_archive_[a-z][a-z0-9_]{0,95}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ERROR = re.compile(r"[A-Z][A-Z0-9_]{0,79}\Z")


class GatewayError(RuntimeError):
    """A sanitized error. Never includes credentials or a remote response body."""

    def __init__(self, code: str = "GATEWAY_ERROR", status: int = 502) -> None:
        self.code = code if isinstance(code, str) and _ERROR.fullmatch(code) else "GATEWAY_ERROR"
        self.status = status
        super().__init__(self.code)


class GatewayConflict(GatewayError):
    def __init__(self, code: str = "VERSION_CONFLICT", status: int = 409) -> None:
        super().__init__(code, status)


class GatewayNotFound(GatewayError):
    def __init__(self, code: str = "RECORD_NOT_FOUND", status: int = 404) -> None:
        super().__init__(code, status)


class GatewayForbidden(GatewayError):
    def __init__(self, code: str = "GATEWAY_FORBIDDEN", status: int = 403) -> None:
        super().__init__(code, status)


class GatewayUnavailable(GatewayError):
    def __init__(self, code: str = "GATEWAY_UNAVAILABLE", status: int = 503) -> None:
        super().__init__(code, status)


@dataclass(frozen=True)
class GatewayHTTPResponse:
    status: int
    body: bytes
    headers: Mapping[str, str] | None = None


class GatewayTransport(Protocol):
    async def request(self, method: str, url: str, *, headers: Mapping[str, str],
                      body: bytes | None, timeout_seconds: float,
                      max_response_bytes: int) -> GatewayHTTPResponse: ...


class _AiohttpTransport:
    async def request(self, method: str, url: str, *, headers: Mapping[str, str],
                      body: bytes | None, timeout_seconds: float,
                      max_response_bytes: int) -> GatewayHTTPResponse:
        # A session per call deliberately avoids a persistent connection's
        # transparent retry after a disconnect. There is no retry loop here.
        import aiohttp

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout_seconds), trust_env=False,
                raise_for_status=False,
            ) as session:
                async with session.request(method, url, headers=headers, data=body,
                                           allow_redirects=False) as response:
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.content.iter_chunked(16_384):
                        size += len(chunk)
                        if size > max_response_bytes:
                            raise GatewayError("GATEWAY_RESPONSE_TOO_LARGE")
                        chunks.append(chunk)
                    return GatewayHTTPResponse(response.status, b"".join(chunks), dict(response.headers))
        except GatewayError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            raise GatewayUnavailable() from None


def canonical_json(value: Any) -> str:
    """Portable JSON for payload/request hashes; reject non-JSON values/NaN."""
    def normalize(item: Any, depth: int = 0) -> Any:
        if depth > 64:
            raise ValueError("JSON nesting exceeds 64")
        if item is None or type(item) in (str, bool, int):
            return item
        if type(item) is float and math.isfinite(item):
            # jsonb renders integral floats without their original exponent.
            return int(item) if item.is_integer() else item
        if type(item) is list:
            return [normalize(part, depth + 1) for part in item]
        if type(item) is dict and all(type(key) is str for key in item):
            return {key: normalize(part, depth + 1) for key, part in item.items()}
        raise ValueError("Value is not finite JSON")

    return json.dumps(normalize(value), ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))


def payload_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _identity(kind: str, key: str, owner: str) -> None:
    if kind not in ALLOWED_KINDS:
        raise ValueError("Unsupported aggregate kind")
    for value in (key, owner):
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise ValueError("Invalid aggregate identity")


def _object(value: Any, maximum: int) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError("Expected a JSON object")
    encoded = canonical_json(value).encode("utf-8")
    if len(encoded) > maximum:
        raise ValueError("JSON object exceeds size limit")
    return json.loads(encoded)


class CloudBaseGateway:
    def __init__(self, env_id: str, api_key: str, *, transport: GatewayTransport | None = None,
                 timeout_seconds: float = 15) -> None:
        if not isinstance(env_id, str) or not _ENV.fullmatch(env_id):
            raise ValueError("Invalid CloudBase environment ID")
        if (not isinstance(api_key, str) or not api_key or len(api_key) > 16_384
                or any(not 33 <= ord(char) <= 126 for char in api_key)):
            raise ValueError("Invalid server API key")
        if not 0 < timeout_seconds <= 60:
            raise ValueError("Invalid gateway timeout")
        self.origin = f"https://{env_id}.api.tcloudbasegateway.com"
        self._api_key = api_key
        self._transport = transport or _AiohttpTransport()
        self._timeout = timeout_seconds

    async def request(self, method: str, path: str, *, headers: Mapping[str, str] | None = None,
                      json_body: Any = None, body: bytes | None = None,
                      max_response_bytes: int = MAX_RESPONSE_BYTES) -> GatewayHTTPResponse:
        """Fixed-origin HTTP for server-only RPC/storage adapters, without redirects."""
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
            raise ValueError("Unsupported HTTP method")
        if not isinstance(path, str):
            raise ValueError("Expected a relative gateway path")
        decoded = unquote(path)
        parts = urlsplit(path)
        if (not path.startswith("/") or path.startswith("//") or parts.scheme or parts.netloc
                or parts.fragment or "\\" in decoded or "//" in decoded.split("?", 1)[0]
                or any(segment in (".", "..") for segment in decoded.split("?", 1)[0].split("/"))
                or any(ord(char) < 32 or ord(char) == 127 for char in decoded)):
            raise ValueError("Invalid relative gateway path")
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 33_554_432:
            raise ValueError("Invalid response size bound")
        request_headers = {"Accept": "application/json", "Authorization": f"Bearer {self._api_key}"}
        for name, value in (headers or {}).items():
            if (not isinstance(name, str) or not isinstance(value, str)
                    or name.lower() in {"authorization", "host", "cookie", "content-length", "transfer-encoding"}
                    or re.fullmatch(r"[A-Za-z0-9-]+", name) is None
                    or any(char in value for char in "\r\n")):
                raise ValueError("Invalid gateway header")
            request_headers[name] = value
        if json_body is not None:
            if body is not None:
                raise ValueError("Use JSON or bytes, not both")
            body = canonical_json(json_body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if body is not None and not isinstance(body, bytes):
            raise ValueError("Expected bytes body")
        try:
            response = await self._transport.request(
                method, self.origin + path, headers=request_headers, body=body,
                timeout_seconds=self._timeout, max_response_bytes=max_response_bytes,
            )
        except GatewayError:
            raise
        except (TimeoutError, OSError):
            raise GatewayUnavailable() from None
        if len(response.body) > max_response_bytes:
            raise GatewayError("GATEWAY_RESPONSE_TOO_LARGE")
        if 300 <= response.status < 400:
            raise GatewayError("GATEWAY_REDIRECT_REJECTED")
        return response

    async def rpc(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(name, str) or not _RPC.fullmatch(name):
            raise ValueError("RPC must be in the fixed veil_archive namespace")
        if type(params) is not dict:
            raise ValueError("RPC parameters must be a JSON object")
        response = await self.request("POST", f"/v1/rdb/rest/rpc/{name}", json_body=params)
        if response.status in (401, 403):
            raise GatewayForbidden()
        if response.status == 404:
            raise GatewayNotFound("GATEWAY_RPC_NOT_FOUND")
        if response.status >= 500 or response.status == 429:
            raise GatewayUnavailable()
        if response.status == 409:
            raise GatewayConflict()
        if not 200 <= response.status < 300:
            raise GatewayError("GATEWAY_HTTP_ERROR", response.status)
        try:
            result = json.loads(response.body)
        except (ValueError, UnicodeDecodeError):
            raise GatewayError("GATEWAY_INVALID_RESPONSE") from None
        if type(result) is not dict:
            raise GatewayError("GATEWAY_INVALID_RESPONSE")
        if result.get("ok") is False:
            status, code = result.get("status", 502), result.get("code", "GATEWAY_ERROR")
            if type(status) is not int or not 400 <= status <= 599:
                status = 502
            error_type = {403: GatewayForbidden, 404: GatewayNotFound, 409: GatewayConflict,
                          503: GatewayUnavailable}.get(status, GatewayError)
            raise error_type(code, status)
        return result

    @staticmethod
    def _record(result: dict[str, Any], kind: str, key: str | None, owner: str) -> dict[str, Any]:
        record = result.get("record")
        if (type(record) is not dict or record.get("kind") != kind or record.get("owner") != owner
                or (key is not None and record.get("key") != key)
                or type(record.get("version")) is not int or record["version"] < 1
                or type(record.get("payload")) is not dict):
            raise GatewayError("GATEWAY_INVALID_RECORD")
        try:
            actual_digest = payload_digest(record["payload"])
        except (ValueError, UnicodeError):
            raise GatewayError("GATEWAY_INVALID_RECORD") from None
        if actual_digest != record.get("digest"):
            raise GatewayError("GATEWAY_RECORD_DIGEST_MISMATCH")
        return record

    async def get(self, kind: str, key: str, owner: str) -> dict[str, Any] | None:
        _identity(kind, key, owner)
        try:
            result = await self.rpc("veil_archive_get", {"p_kind": kind, "p_key": key, "p_owner": owner})
        except GatewayNotFound as exc:
            if exc.code == "RECORD_NOT_FOUND":
                return None
            raise
        return self._record(result, kind, key, owner)

    async def list_records(self, kind: str, owner: str, *, after: str | None = None,
                           limit: int = 20) -> list[dict[str, Any]]:
        _identity(kind, "list" if after is None else after, owner)
        if type(limit) is not int or not 1 <= limit <= 25:
            raise ValueError("List limit must be between 1 and 25")
        result = await self.rpc("veil_archive_list", {
            "p_kind": kind, "p_owner": owner, "p_after": after, "p_limit": limit,
        })
        if type(result.get("records")) is not list or len(result["records"]) > limit:
            raise GatewayError("GATEWAY_INVALID_RESPONSE")
        return [self._record({"record": row}, kind, None, owner) for row in result["records"]]

    async def create(self, kind: str, key: str, owner: str, payload: dict[str, Any], *,
                     idempotency_key: str, request_digest: str | None = None) -> dict[str, Any]:
        return await self._write("create", kind, key, owner, payload,
                                 idempotency_key=idempotency_key, request_digest=request_digest)

    async def cas_append(self, kind: str, key: str, owner: str, expected_version: int,
                         payload: dict[str, Any], event: dict[str, Any], *,
                         idempotency_key: str, request_digest: str | None = None) -> dict[str, Any]:
        if type(expected_version) is not int or not 1 <= expected_version < 9_007_199_254_740_991:
            raise ValueError("Invalid expected version")
        return await self._write("cas_append", kind, key, owner, payload,
                                 expected_version=expected_version, event=event,
                                 idempotency_key=idempotency_key, request_digest=request_digest)

    async def cas_append_guarded(self, kind: str, key: str, owner: str, expected_version: int,
                                 payload: dict[str, Any], event: dict[str, Any], *,
                                 idempotency_key: str, guards: list[dict[str, Any]],
                                 request_digest: str | None = None) -> dict[str, Any]:
        """CAS with up to eight record dependencies locked in the same DB TX.

        Successful receipt replay precedes dependency checks on the server.
        This method never retries a write or substitutes a later authority read.
        """
        _identity(kind, key, owner)
        if type(expected_version) is not int or not 1 <= expected_version < 9_007_199_254_740_991:
            raise ValueError("Invalid expected version")
        if not isinstance(idempotency_key, str) or not _IDENTIFIER.fullmatch(idempotency_key):
            raise ValueError("Invalid idempotency key")
        if type(guards) is not list or len(guards) > 8:
            raise ValueError("Expected at most eight record guards")
        normalized = []
        identities = set()
        for guard in guards:
            if (type(guard) is not dict or set(guard) != {"kind", "key", "owner", "expected_version", "digest"}
                    or any(type(guard[field]) is not str for field in ("kind", "key", "owner", "digest"))
                    or type(guard["expected_version"]) is not int
                    or not 1 <= guard["expected_version"] <= 9_007_199_254_740_991
                    or not _HASH.fullmatch(guard["digest"])):
                raise ValueError("Invalid record guard")
            _identity(guard["kind"], guard["key"], guard["owner"])
            identity = (guard["kind"], guard["key"])
            if identity in identities:
                raise ValueError("Duplicate record guard")
            identities.add(identity)
            normalized.append(dict(guard))
        normalized.sort(key=lambda guard: (guard["kind"], guard["key"]))
        payload = _object(payload, MAX_PAYLOAD_BYTES)
        params = {"p_kind": kind, "p_key": key, "p_owner": owner, "p_expected_version": expected_version,
                  "p_payload": payload, "p_digest": payload_digest(payload),
                  "p_event": _object(event, MAX_EVENT_BYTES), "p_idempotency_key": idempotency_key,
                  "p_guards": normalized}
        if request_digest is None:
            request_digest = payload_digest({"operation": "cas_append_guarded", **params})
        if not isinstance(request_digest, str) or not _HASH.fullmatch(request_digest):
            raise ValueError("Invalid request digest")
        params["p_request_digest"] = request_digest
        result = await self.rpc("veil_archive_cas_append_guarded", params)
        self._record(result, kind, key, owner)
        if type(result.get("replayed")) is not bool:
            raise GatewayError("GATEWAY_INVALID_RESPONSE")
        return result

    async def _write(self, operation: str, kind: str, key: str, owner: str, payload: dict[str, Any], *,
                     idempotency_key: str, request_digest: str | None,
                     expected_version: int | None = None, event: dict[str, Any] | None = None) -> dict[str, Any]:
        _identity(kind, key, owner)
        if not isinstance(idempotency_key, str) or not _IDENTIFIER.fullmatch(idempotency_key):
            raise ValueError("Invalid idempotency key")
        payload = _object(payload, MAX_PAYLOAD_BYTES)
        params: dict[str, Any] = {"p_kind": kind, "p_key": key, "p_owner": owner,
                                  "p_payload": payload, "p_digest": payload_digest(payload),
                                  "p_idempotency_key": idempotency_key}
        if operation == "cas_append":
            params.update(p_expected_version=expected_version, p_event=_object(event, MAX_EVENT_BYTES))
        if request_digest is None:
            request_digest = payload_digest({"operation": operation, **params})
        if not isinstance(request_digest, str) or not _HASH.fullmatch(request_digest):
            raise ValueError("Invalid request digest")
        params["p_request_digest"] = request_digest
        result = await self.rpc(f"veil_archive_{operation}", params)
        self._record(result, kind, key, owner)
        if type(result.get("replayed")) is not bool:
            raise GatewayError("GATEWAY_INVALID_RESPONSE")
        return result
