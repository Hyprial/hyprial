# Decision record (GUI session host, PR 2b):
# - The host never becomes a business request router. It verifies the selected
#   upstream's ready/catalog projection once per session mint or refresh, then
#   relays the canonical method path and its envelope unchanged.
# - The upstream bearer, principal and endpoint stay host-only. Browser cookies
#   and identity headers are never forwarded; only accept, content-type, the
#   pinned catalog digest, the request id and the bearer cross.
# - httpx honours HTTP(S)_PROXY by default and the Node fetch did not, so the
#   one client is built with trust_env=False and follow_redirects=False: the
#   upstream is the configured loopback endpoint and nothing else.
# - A failure before a response is not-dispatched for a read and unknown for a
#   write; an unreadable or invalid response to a write is unknown. The host
#   never retries.
# - The client keeps no cookie: upstream Set-Cookie is refused by a reject-all
#   policy, so nothing an upstream sets can ever return on a later call, across
#   sessions and authority rotations. The request headers are built from one
#   exact allowlist (relay_request_headers) and nowhere else.
# - After a write relay has started the host never reports not-dispatched: a
#   response that cannot be parsed, validated or turned into headers (deep
#   nesting, an id that is not printable ASCII) is UPSTREAM_INVALID_RESPONSE
#   with dispatch unknown for a write. A browser request id that cannot be sent
#   is refused before anything leaves the host.
# - Mock `mutates` (contract rule, PR #2838): the Contract Mock's wire boolean,
#   otherwise true. The host never guesses false and never consults a local
#   method declaration of its own build.
# - Upstream requests carry `accept-encoding: identity` so the relayed bytes are
#   the bytes the upstream produced. A compressed answer that arrives anyway is
#   still decoded under the response bound, since httpx decodes before the host
#   sees the body; nothing relies on it.
# - A status outside 100..599 cannot be written by the HTTP stack, so it is
#   UPSTREAM_INVALID_RESPONSE (dispatch unknown for a write) rather than an
#   unhandled error that drops the connection.
# - PR 4b-H: the daemon driver's /openapi.json is read with the bearer, like the
#   probe, for the developer schema route. The browser gets a projection of it
#   (project_schema), never the upstream bytes: only the paths the session host
#   itself serves, and no statement about a credential the browser never holds.
"""Upstream probe for the daemon and Contract Mock drivers, and the byte relay."""

from __future__ import annotations

from dataclasses import dataclass
from http.cookiejar import DefaultCookiePolicy
from typing import Any

import httpx

from hyprial.kernel import ipc_errors

from .config import HostConfig, Principal
from .envelope import MAX_RESPONSE_BYTES, HostFailure, header_safe, loads_strict
from .session import SessionAuthority, SessionMethod

Json = dict[str, Any]
DISPATCH_VALUES = ("not-dispatched", "dispatched", "unknown")


@dataclass(frozen=True)
class RelayResult:
    status: int
    headers: dict[str, str]
    body: bytes


def _record(value: Any) -> Json | None:
    return value if isinstance(value, dict) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _mismatch(message: str, code: str = "UPSTREAM_CONTRACT_MISMATCH") -> HostFailure:
    return HostFailure(502, code, message)


def _not_ready(message: str) -> HostFailure:
    return HostFailure(503, "HTTP_NOT_READY", message)


def _nullable_text(source: Json, key: str, what: str) -> str | None:
    """A wire field that may be an explicit ``null`` but may never be absent."""
    if key not in source:
        raise _mismatch(f"{what} must be present on the wire")
    value = source[key]
    if value is None:
        return None
    parsed = _text(value)
    if parsed is None:
        raise _mismatch(f"{what} must be a string or an explicit null")
    return parsed


def _status_failure(status: int, body: Json) -> HostFailure:
    if status in (401, 403):
        return HostFailure(502, "UPSTREAM_AUTH_REJECTED", "the upstream refused the host credential")
    code = _text(body.get("code")) or "UPSTREAM_NOT_READY"
    message = _text(body.get("message")) or "the selected upstream is not ready"
    return HostFailure(
        503 if status == 503 else 502,
        "HTTP_NOT_READY" if status == 503 else code,
        message,
        data={"upstreamCode": code},
    )


def _json_object(raw: bytes) -> Json:
    try:
        value = loads_strict(raw)
    except ValueError:
        raise HostFailure(502, "UPSTREAM_INVALID_RESPONSE", "the upstream response is not JSON") from None
    if not isinstance(value, dict):
        raise HostFailure(502, "UPSTREAM_INVALID_RESPONSE", "the upstream response is not a JSON object")
    return value


class UpstreamClient:
    """One ``httpx`` client for every upstream call of one host."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            transport=transport,
            headers={"accept-encoding": "identity"},
        )
        self._client.cookies.jar.set_policy(DefaultCookiePolicy(allowed_domains=[]))

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _bounded(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise HostFailure(502, "UPSTREAM_RESPONSE_TOO_LARGE", "the upstream response exceeded the host limit")
            chunks.append(chunk)
        return b"".join(chunks)

    async def get_json(
        self, config: HostConfig, path: str, *, authenticated: bool
    ) -> tuple[int, httpx.Headers, Json]:
        headers = {"accept": "application/json"}
        if authenticated:
            headers["authorization"] = f"Bearer {config.upstream_token}"
        try:
            async with self._client.stream(
                "GET", f"{config.endpoint}{path}", headers=headers, timeout=config.upstream_request_seconds
            ) as response:
                if 300 <= response.status_code < 400:
                    raise httpx.TooManyRedirects("redirect refused", request=response.request)
                raw = await self._bounded(response)
                status, response_headers = response.status_code, response.headers
        except HostFailure:
            raise
        except Exception:
            raise HostFailure(503, "UPSTREAM_UNAVAILABLE", "the selected upstream is not reachable") from None
        return status, response_headers, _json_object(raw)

    async def schema(self, config: HostConfig) -> Json:
        """The daemon driver's request schema, projected for a browser of this host."""
        status, _, document = await self.get_json(config, "/openapi.json", authenticated=True)
        if status != 200:
            raise _status_failure(status, document)
        return project_schema(document)

    async def describe(self, config: HostConfig) -> SessionAuthority:
        """Probe the selected upstream and return what a new session may pin."""
        if config.driver == "daemon":
            return await self._describe_daemon(config)
        return await self._describe_mock(config)

    async def _describe_daemon(self, config: HostConfig) -> SessionAuthority:
        status, _, ready = await self.get_json(config, "/readyz", authenticated=True)
        if status != 200:
            raise _status_failure(status, ready)
        identity = _check_ready(ready, config.principal)
        status, headers, catalog = await self.get_json(config, "/daemon/request", authenticated=True)
        if status != 200:
            raise _status_failure(status, catalog)
        return _check_catalog(catalog, headers, identity, config.principal)

    async def _describe_mock(self, config: HostConfig) -> SessionAuthority:
        status, _, health = await self.get_json(config, "/healthz", authenticated=False)
        if status != 200 or health.get("ok") is not True:
            raise _not_ready("the Contract Mock is not healthy")
        status, _, contract = await self.get_json(config, "/mock/contract", authenticated=True)
        if status != 200:
            raise _status_failure(status, contract)
        if contract.get("mock") is not True:
            raise _mismatch("the upstream is not the Contract Mock")
        digest, raw_methods = _text(contract.get("contractDigest")), _record(contract.get("methods"))
        if digest is None or raw_methods is None:
            raise _mismatch("the Contract Mock metadata is incomplete")
        methods = []
        for name, raw in raw_methods.items():
            method = _record(raw)
            if method is None or not isinstance(method.get("scenarios"), list):
                raise _mismatch("a Contract Mock method is incomplete")
            methods.append(SessionMethod(name, _mock_mutates(method), True, "available"))
        return SessionAuthority(
            driver="contract-mock",
            phase="ready",
            catalog_digest=digest,
            declaration_digest=digest,
            backend_epoch=f"contract-mock:{digest}",
            http_contract_version=1,
            catalog_version=_text(contract.get("contractVersion")) or "1",
            methods=tuple(methods),
        )

    async def relay(
        self,
        config: HostConfig,
        *,
        path: str,
        method: str,
        incoming_request_id: str | None,
        body: bytes | None,
        write: bool,
        pinned_digest: str,
        expected_method: str | None,
        fallback_request_id: str | None,
    ) -> RelayResult:
        """Send one request upstream once and return its bytes with rebuilt headers."""
        uncertain = "unknown" if write else "not-dispatched"
        request_id = incoming_request_id or fallback_request_id
        if request_id and not header_safe(request_id):
            raise HostFailure(
                400, ipc_errors.INVALID_REQUEST, "x-hyprial-request-id must be printable ASCII", dispatch="not-dispatched"
            )
        headers = relay_request_headers(
            token=config.upstream_token,
            pinned_digest=pinned_digest,
            request_id=request_id,
            has_body=body is not None,
        )
        try:
            async with self._client.stream(
                method,
                f"{config.endpoint}{path}",
                headers=headers,
                content=body,
                timeout=config.upstream_request_seconds,
            ) as response:
                if 300 <= response.status_code < 400:
                    raise httpx.TooManyRedirects("redirect refused", request=response.request)
                try:
                    raw = await self._bounded(response)
                except HostFailure as failure:
                    raise failure.but(dispatch=uncertain) from None
                except Exception:
                    raise HostFailure(
                        502, "UPSTREAM_UNAVAILABLE", "the upstream response could not be read", dispatch=uncertain
                    ) from None
                status, upstream_headers = response.status_code, response.headers
        except HostFailure:
            raise
        except Exception:
            raise HostFailure(
                502,
                "UPSTREAM_UNAVAILABLE",
                "the request may have reached the upstream but no response was received"
                if write
                else "the upstream could not be reached",
                dispatch=uncertain,
            ) from None
        if not 100 <= status <= 599:
            raise HostFailure(
                502,
                "UPSTREAM_INVALID_RESPONSE",
                "the upstream returned a status code that cannot be relayed",
                dispatch=uncertain,
                method=expected_method,
            )
        try:
            parsed = _json_object(raw)
        except HostFailure:
            raise HostFailure(
                502,
                "UPSTREAM_INVALID_RESPONSE",
                "the upstream returned a response that cannot be validated",
                dispatch=uncertain,
            ) from None
        if expected_method is not None and not _is_method_envelope(parsed, expected_method):
            raise HostFailure(
                502,
                "UPSTREAM_INVALID_RESPONSE",
                "the upstream returned an invalid method envelope",
                dispatch=uncertain,
                method=expected_method,
            )
        try:
            relayed = _relay_headers(upstream_headers, parsed, pinned_digest)
        except ValueError:
            raise HostFailure(
                502,
                "UPSTREAM_INVALID_RESPONSE",
                "the upstream returned a response that cannot be relayed",
                dispatch=uncertain,
                method=expected_method,
            ) from None
        return RelayResult(status, relayed, raw)


def project_schema(document: Json) -> Json:
    """Keep the ``/daemon/request`` paths and drop every credential statement.

    The upstream document must be a bare OpenAPI object whose ``paths`` is an
    object; anything else (an envelope, a list) is an invalid upstream response.
    ``servers`` goes too: the browser calls this host, not the upstream endpoint.
    """
    paths = document.get("paths")
    if not isinstance(paths, dict) or _text(document.get("openapi")) is None:
        raise HostFailure(502, "UPSTREAM_INVALID_RESPONSE", "the upstream schema is not an OpenAPI document")
    kept: Json = {}
    for path, item in paths.items():
        if path != "/daemon/request" and not path.startswith("/daemon/request/"):
            continue
        kept[path] = (
            {
                verb: {k: v for k, v in operation.items() if k != "security"} if isinstance(operation, dict) else operation
                for verb, operation in item.items()
            }
            if isinstance(item, dict)
            else item
        )
    projected = {key: value for key, value in document.items() if key not in ("security", "servers")}
    projected["paths"] = kept
    components = document.get("components")
    if isinstance(components, dict):
        projected["components"] = {key: value for key, value in components.items() if key != "securitySchemes"}
    return projected


def _mock_mutates(method: Json) -> bool:
    wire = method.get("mutates")
    return wire if isinstance(wire, bool) else True


def relay_request_headers(*, token: str, pinned_digest: str, request_id: str | None, has_body: bool) -> dict[str, str]:
    """The only request headers the host sends upstream; nothing else is ever added."""
    headers = {
        "accept": "application/json",
        "x-hyprial-catalog-digest": pinned_digest,
        "authorization": f"Bearer {token}",
    }
    if has_body:
        headers["content-type"] = "application/json"
    if request_id:
        headers["x-hyprial-request-id"] = request_id
    return headers


def _is_method_envelope(body: Json, method: str) -> bool:
    return (
        body.get("schemaVersion") == 1
        and isinstance(body.get("ok"), bool)
        and _text(body.get("requestId")) is not None
        and body.get("dispatch") in DISPATCH_VALUES
        and body.get("method") == method
    )


def _relay_headers(upstream: httpx.Headers, body: Json, fallback_digest: str) -> dict[str, str]:
    result = {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
    }
    request_id = _text(body.get("requestId")) or _text(upstream.get("x-hyprial-request-id"))
    digest = _text(upstream.get("x-hyprial-catalog-digest")) or _text(body.get("catalogDigest")) or fallback_digest
    dispatch = _text(upstream.get("x-hyprial-dispatch")) or _text(body.get("dispatch"))
    for name, value in (
        ("x-hyprial-request-id", request_id),
        ("x-hyprial-catalog-digest", digest),
        ("x-hyprial-dispatch", dispatch),
    ):
        if value:
            if not header_safe(value):
                raise ValueError(f"{name} cannot be sent as a header")
            result[name] = value
    return result


@dataclass(frozen=True)
class _Identity:
    owner: str | None
    node: str
    epoch: str
    digest: str
    declaration: str


def _check_ready(ready: Json, principal: Principal | None) -> _Identity:
    if ready.get("ok") is not True or ready.get("ready") is not True or ready.get("transport") != "ready":
        raise _not_ready("the selected daemon is not ready")
    backend, catalog = _record(ready.get("backend")), _record(ready.get("catalog"))
    if backend is None or catalog is None:
        raise _mismatch("the daemon readiness payload is incomplete")
    if backend.get("available") is not True:
        raise _not_ready("the selected daemon is not available")
    owner = _nullable_text(backend, "owner", "the daemon readiness owner")
    node, epoch = _text(backend.get("nodeId")), _text(backend.get("epoch"))
    digest, declaration = _text(catalog.get("catalogDigest")), _text(catalog.get("declarationDigest"))
    if catalog.get("fresh") is not True:
        raise _not_ready("the daemon ready projection does not carry a fresh catalog")
    if not node or not epoch or not digest or not declaration:
        raise _mismatch("the daemon identity or catalog digest is missing")
    if principal is None:
        if owner is not None:
            raise _mismatch("a bootstrap view requires an ownerless daemon", "PRINCIPAL_MISMATCH")
    elif owner is None or owner != principal.owner or node != principal.node:
        raise _mismatch("the configured principal does not belong to the selected daemon", "PRINCIPAL_MISMATCH")
    if isinstance(ready.get("notReady"), list) and ready["notReady"]:
        raise _not_ready("the selected daemon has methods that are not ready")
    return _Identity(owner, node, epoch, digest, declaration)


def _check_catalog(
    catalog: Json, headers: httpx.Headers, identity: _Identity, principal: Principal | None
) -> SessionAuthority:
    if catalog.get("ok") is not True:
        raise _mismatch("the daemon catalog is not ok")
    if catalog.get("schemaVersion") != 1 or catalog.get("generatedFrom") != "daemon.catalog":
        raise _mismatch("the daemon catalog envelope is not the served contract")
    backend, claimed = _record(catalog.get("backend")), _record(catalog.get("principal"))
    if backend is None or claimed is None:
        raise _mismatch("the daemon catalog identity is incomplete")
    if (
        _nullable_text(backend, "owner", "the daemon catalog owner") != identity.owner
        or _text(backend.get("nodeId")) != identity.node
        or _text(backend.get("epoch")) != identity.epoch
    ):
        raise _mismatch("the daemon readiness and catalog epochs differ")
    owner = _nullable_text(claimed, "owner", "the daemon catalog principal owner")
    actor = _nullable_text(claimed, "actor", "the daemon catalog principal actor")
    if principal is None:
        if owner is not None or actor is not None:
            raise _mismatch("the ownerless daemon catalog claims an identity", "PRINCIPAL_MISMATCH")
    elif owner != principal.owner or actor != principal.actor:
        raise _mismatch("the daemon catalog principal differs from the trusted principal", "PRINCIPAL_MISMATCH")
    digest = _text(catalog.get("catalogDigest"))
    if digest is None:
        raise _mismatch("the upstream did not report a catalog digest", "CATALOG_MISMATCH")
    header = _text(headers.get("x-hyprial-catalog-digest"))
    if header and header != digest:
        raise _mismatch("the upstream catalog digest header differs from its body", "CATALOG_MISMATCH")
    if digest != identity.digest:
        raise _mismatch("the daemon readiness and catalog digests differ", "CATALOG_MISMATCH")
    declaration = _text(catalog.get("declarationDigest"))
    if declaration is None or declaration != identity.declaration:
        raise _mismatch("the daemon readiness and catalog declarations differ", "CATALOG_MISMATCH")
    methods = _catalog_methods(catalog.get("methods"))
    version = catalog.get("httpContractVersion")
    return SessionAuthority(
        driver="daemon",
        phase="bootstrap" if principal is None else "ready",
        catalog_digest=digest,
        declaration_digest=declaration,
        backend_epoch=identity.epoch,
        http_contract_version=version if isinstance(version, int) and not isinstance(version, bool) and version > 0 else 1,
        catalog_version=_text(catalog.get("catalogVersion")) or "1",
        methods=methods,
    )


def _catalog_methods(raw: Any) -> tuple[SessionMethod, ...]:
    if not isinstance(raw, list):
        raise _mismatch("the daemon catalog has no method list")
    methods = []
    for entry in raw:
        method = _record(entry)
        if (
            method is None
            or _text(method.get("name")) is None
            or not isinstance(method.get("mutates"), bool)
            or not isinstance(method.get("callable"), bool)
            or _text(method.get("state")) is None
        ):
            raise _mismatch("a daemon catalog method is incomplete")
        methods.append(SessionMethod(method["name"], method["mutates"], method["callable"], method["state"]))
    return tuple(methods)
