"""`agent.provider.*`: install a harness-native credential into an agent home.

Split out of ``registry_ops`` to keep that dispatch file under the file-length
gate. The IPC router sends every ``agent.provider.*`` method to
:meth:`_AgentProviderMixin._handle_agent_provider` inside the same agent error
boundary as ``_handle_agent``, so this module owns the parameter reading and the
error mapping while the application object keeps owning the registry and the
agent home authority.

Claude login stores either an owned SQLite setup token or an uploaded native
login. ``--default`` designates the fenced owner reference in registry SQLite;
identity creation applies it to CLI, PAC and lifecycle agents. The daemon never
reads arbitrary operator paths. Historical provider-name defaults remain in
``provider-default.json`` for CLI compatibility only. Captured Claude defaults
replace that name; clearing removes both kinds but keeps existing grants.

Default mutation requires the local host operator. Provider login's ``actor``
is the recipient; trusted worker origin, session identity and registered
interactive peer checks still gate mutation. This remains L0 local bookkeeping,
not a new authentication boundary. ``agent.create.providerDefault`` reports the
captured source object, historical name, or null, so the CLI does not copy again.

This module also holds the bounded sqlite-to-environment launch helper. The
adjacent ``registry_ops`` dispatch file is already at the structural line gate;
the helper still runs inside its acquire/compensate boundary.
"""

from __future__ import annotations

import base64
import binascii
import json
import threading
from pathlib import Path
from typing import Any

from hyprial.daemon.impl.ipc.params import JsonObject, _required_string
from hyprial.identity import (
    NATIVE_CREDENTIAL_FILES,
    AgentSecretStore,
    FingerprintUnavailable,
    NativeCredentialError,
    install_native_credential_set,
    remove_native_credentials,
    secret_environment_name_is_allowed,
)
from hyprial.kernel import DaemonRequestError, atomic_json_write, ipc_errors

_DEFAULT_FILE = "provider-default.json"
_DEFAULT_LOCK = threading.Lock()


class _AgentProviderMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _handle_agent_provider_request(
        self, method: str, params: JsonObject, *, trusted_message_origin: str | None,
    ) -> Any:
        # Login's actor is the recipient. Keep worker/session/interactive-peer fences.
        attribution = ({key: value for key, value in params.items() if key != "actor"}
                       if method == "agent.provider.login" else params)
        operator, _ = self._identity_binding_operator(
            attribution, trusted_message_origin=trusted_message_origin,
        )
        return self._handle_agent_provider(method, params, operator=operator)

    def _handle_agent_provider(
        self, method: str, params: JsonObject, *, operator: bool = True
    ) -> Any:
        if method == "agent.provider.login":
            if params.get("makeDefault") and not operator:
                raise DaemonRequestError(ipc_errors.CALLER_NOT_AUTHORIZED, "only the host operator may change the provider default")
            return handle_provider_login(self, params)
        if method == "agent.provider.logout":
            return handle_provider_logout(self, params)
        if method == "agent.provider.default":
            reference = self._agent_registry.default_claude_credential()
            result = handle_provider_default(
                Path(self.state_dir) / _DEFAULT_FILE, params, operator=operator
            )
            if params.get("clear") or params.get("provider") is not None:
                change_default_credential(self, {"harness": "claude", "clear": True}, operator=operator)
                if reference:
                    result["previous"] = "claude"
            elif reference:
                result.update(provider="claude", credentialSource=reference["kind"], defaultCredential=reference)
            return result
        raise DaemonRequestError(
            ipc_errors.METHOD_NOT_FOUND, f"unknown daemon method {method}"
        )

    def _handle_agent_create_with_default(self, params: JsonObject) -> Any:
        created = self._handle_agent("agent.create", params)
        if not isinstance(created, dict):
            return created
        try:
            reference = self._agent_registry.default_claude_credential()
            default = ({"provider": "claude", "credentialSource": reference["kind"],
                        "defaultCredential": reference} if reference else
                       read_provider_default(Path(self.state_dir) / _DEFAULT_FILE))
        except DaemonRequestError as error:
            return {**created, "providerDefault": None, "providerDefaultError": str(error)}
        return {**created, "providerDefault": default}


def change_default_credential(app: Any, params: JsonObject, *, operator: bool) -> dict[str, Any]:
    from uuid import uuid4
    from hyprial.identity import (
        DefaultClaudeCredentialCommand, DefaultClaudeCredentialCompleted, SecretResolutionError,
    )

    reference = params.get("reference")
    clear = params.get("clear", False)
    if params.get("harness") != "claude" or not isinstance(clear, bool):
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "expected harness claude and boolean clear")
    if (reference is not None or clear) and not operator:
        raise DaemonRequestError(ipc_errors.CALLER_NOT_AUTHORIZED, "only the host operator may change the Claude credential default")
    owner = entry = token = None
    kind = str(params.get("kind", "setup-token"))
    if reference is not None:
        if clear or not isinstance(reference, str) or reference.count("/") != 1:
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "reference must be OWNER/ENTRY; cannot combine with clear")
        owner, entry = reference.split("/")
        try:
            agent = _owned_agent(app, owner)
            token = (agent.entity_token if kind == "native" else
                     app._agent_registry.validate_default_claude_credential(owner, entry))
        except (ValueError, SecretResolutionError) as error:
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error)) from error
    generation = app._agent_session_domains.agent.generation
    event = app._agent_session_domains.call_agent(
        DefaultClaudeCredentialCommand(uuid4().hex, owner, entry, token, clear, kind),
        DefaultClaudeCredentialCompleted,
    )
    if event.generation != generation:
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "default credential completion fence mismatch")
    return {"ok": True, "provider": "claude" if event.reference else None,
            "credentialSource": kind if event.reference else None, "defaultCredential": event.reference}


def materialize_agent_secret_environment(*, registry: Any, actor: str) -> dict[str, str]:
    """Read one agent's sqlite values and record the launch decision."""

    agent = registry.require(actor)
    receipt = registry.home_receipt(agent.actor)
    store = AgentSecretStore(Path(receipt.path))
    write_receipts: dict[tuple[str, str], tuple[str, int]] = {}
    for event in store.history():
        if event.field is None or event.result != "ok":
            continue
        key = (event.service, event.field)
        if event.action in {"set", "rotate"} and event.fingerprint is not None:
            write_receipts[key] = (event.fingerprint, event.at_ms)
        elif event.action == "remove":
            write_receipts.pop(key, None)

    environment: dict[str, str] = {}
    at_ms = registry.now_ms()
    for secret in store.list():
        result = None
        recorded = write_receipts.get((secret.service, secret.field))
        if not secret_environment_name_is_allowed(secret.field):
            result = "rejected:reserved-name"
        elif recorded != (secret.fingerprint, secret.updated_at_ms):
            result = "rejected:no-write-event"
        else:
            try:
                matches = store.fingerprint_matches(
                    secret.field, secret.value, secret.fingerprint
                )
            except FingerprintUnavailable:
                result = "rejected:fingerprint-unavailable"
            else:
                if not matches:
                    result = "rejected:no-write-event"
        if result is not None:
            store.record_event(
                action="rejected",
                service=secret.service,
                field=secret.field,
                actor=agent.actor,
                via="daemon",
                at_ms=at_ms,
                result=result,
            )
            continue
        if secret.field in environment:
            store.record_event(
                action="rejected",
                service=secret.service,
                field=secret.field,
                actor=agent.actor,
                via="daemon",
                at_ms=at_ms,
                result="rejected:environment-conflict",
            )
            store.record_event(
                action="materialize",
                service="runtime-launch",
                field=None,
                actor=agent.actor,
                via="daemon",
                at_ms=at_ms,
                result="rejected:environment-conflict",
            )
            raise ValueError(f"agent secret environment conflict for {secret.field}")
        environment[secret.field] = secret.value
    store.record_event(
        action="materialize",
        service="runtime-launch",
        field=",".join(sorted(environment)) or None,
        actor=agent.actor,
        via="daemon",
        at_ms=at_ms,
    )
    return environment


def handle_provider_login(application: Any, params: JsonObject) -> dict[str, object]:
    """Write the uploaded documents into the agent's canonical secrets tree.

    ``files`` is the whole set the harness reads, each document base64-encoded
    because JSON has no byte string. The identity writer checks the set whole
    before writing, and every refusal keeps the code it raised.
    """

    actor = _required_string(params.get("actor"), "actor")
    provider = _required_string(params.get("provider"), "provider")
    force = bool(params.get("force", False))
    make_default = params.get("makeDefault", False)
    if not isinstance(make_default, bool):
        raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "makeDefault must be boolean")
    agent = _owned_agent(application, actor)
    if make_default and agent.hosted_by is not None:
        raise DaemonRequestError(ipc_errors.TARGET_NOT_AUTHORIZED, "a hosted agent cannot own the machine default")
    receipt = application._agent_registry.home_receipt(agent.actor, require_ready=False)
    if "setupToken" in params:
        token = params["setupToken"]
        if (provider != "claude" or "files" in params or not isinstance(token, str)
                or not token.strip() or "\x00" in token
                or any(char.isspace() for char in token) or len(token) > 16384):
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "setupToken requires one non-empty Claude token and no files")
        store = AgentSecretStore(Path(receipt.path))
        if store.get("claude", "CLAUDE_CODE_OAUTH_TOKEN") is not None and not force:
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, "Claude setup token exists; use --force to replace it")
        store.set(service="claude", field="CLAUDE_CODE_OAUTH_TOKEN", value=token,
                  actor=agent.actor, via="daemon", at_ms=application._agent_registry.now_ms())
        # The owner is an explicit consumer too; normal grant resolution injects it.
        application._handle_agent("agent.secret.grant", {
            "actor": agent.actor, "grantId": "provider-claude", "source": "agent-private",
            "entryId": "claude", "fieldName": "CLAUDE_CODE_OAUTH_TOKEN",
            "environmentNames": ["CLAUDE_CODE_OAUTH_TOKEN"],
            "revision": 1 + (prior.revision if (prior := application._agent_registry.secret_grant(agent.actor, "provider-claude")) else 0),
        })
        installed = {"provider": provider, "credentialSource": "setup-token"}
    else:
        documents = _decoded_documents(params.get("files"))
        try:
            installed = install_native_credential_set(Path(receipt.path), provider, documents, force=force)
        except NativeCredentialError as error:
            raise DaemonRequestError(error.code, error.message) from error
    result = {"ok": True, "actor": agent.actor, **installed}
    if make_default:
        previous = application._handle_agent_provider("agent.provider.default", {}).get("provider")
        if provider == "claude":
            result["default"] = change_default_credential(application, {
                "harness": "claude", "reference": f"{agent.actor}/claude",
                "kind": "setup-token" if "setupToken" in params else "native",
            }, operator=True)
            (Path(application.state_dir) / _DEFAULT_FILE).unlink(missing_ok=True)
            result["default"]["previous"] = previous
        else:
            result["default"] = application._handle_agent_provider("agent.provider.default", {"provider": provider})
    return result


def _owned_agent(application: Any, actor: str) -> Any:
    agent = application.agents.require(actor)
    if agent.hosted_by == "host-invite" or agent.owner != application.owner:
        raise DaemonRequestError(
            ipc_errors.TARGET_NOT_AUTHORIZED,
            f"agent {agent.actor} belongs to {agent.owner}; provider login and "
            "logout act only on agents owned by the user this daemon is logged in as",
        )
    return agent


def handle_provider_logout(application: Any, params: JsonObject) -> dict[str, object]:
    """Remove the provider's credential files from the agent's secrets tree."""

    actor = _required_string(params.get("actor"), "actor")
    provider = _required_string(params.get("provider"), "provider")
    agent = _owned_agent(application, actor)
    receipt = application._agent_registry.home_receipt(agent.actor, require_ready=False)
    try:
        removed = remove_native_credentials(Path(receipt.path), provider)
    except NativeCredentialError as error:
        raise DaemonRequestError(error.code, error.message) from error
    return {"ok": True, "actor": agent.actor, **removed}


def handle_provider_default(
    store: Path, params: JsonObject, *, operator: bool = True
) -> dict[str, object]:
    """Read, set (``provider``) or clear (``clear: true``) the machine default."""

    requested = params.get("provider")
    clear = params.get("clear") is True
    if (requested is not None or clear) and not operator:
        raise DaemonRequestError(
            ipc_errors.CALLER_NOT_AUTHORIZED,
            "only the machine operator may change the provider default; "
            "an agent may read it",
        )
    if requested is not None and clear:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "pass provider or clear, not both"
        )
    if requested is not None:
        provider = _required_string(requested, "provider")
        if provider not in NATIVE_CREDENTIAL_FILES:
            raise DaemonRequestError(
                ipc_errors.PROVIDER_UNSUPPORTED,
                f"provider {provider!r} is not supported; supported: "
                + ", ".join(sorted(NATIVE_CREDENTIAL_FILES)),
            )
    with _DEFAULT_LOCK:
        previous = read_provider_default(store)
        if clear:
            store.unlink(missing_ok=True)
            current = None
        elif requested is not None:
            atomic_json_write(store, {"schemaVersion": 1, "provider": requested})
            current = requested
        else:
            return {"ok": True, "provider": previous, "previous": None}
    return {"ok": True, "provider": current, "previous": previous}


def read_provider_default(store: Path) -> str | None:
    try:
        raw = json.loads(store.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"cannot read the provider default {store}: {error}",
        ) from error
    provider = raw.get("provider") if isinstance(raw, dict) else None
    if (
        not isinstance(raw, dict)
        or raw.get("schemaVersion") != 1
        or provider not in NATIVE_CREDENTIAL_FILES
    ):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT,
            f"the provider default {store} is not a schemaVersion 1 document",
        )
    return str(provider)


def _decoded_documents(raw: object) -> list[tuple[str, bytes]]:
    if not isinstance(raw, list) or not raw:
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "files must be a non-empty array"
        )
    documents: list[tuple[str, bytes]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "each file must be an object"
            )
        filename = _required_string(item.get("filename"), "filename")
        encoded = _required_string(item.get("contentBase64"), "contentBase64")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as error:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                f"contentBase64 of {filename} is not valid base64",
            ) from error
        documents.append((filename, content))
    return documents
