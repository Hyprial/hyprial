"""Atomic, incarnation-fenced configuration writes owned by identity."""

from dataclasses import replace
import hashlib
import json
import re

from hyprial.kernel import ipc_errors
from ...home.config import normalize_agent_config
from .._base import Agent, AgentError


class AgentIncarnationMismatch(AgentError):
    code = ipc_errors.AGENT_INCARNATION_MISMATCH


class AgentConfigCasMismatch(AgentError):
    code = ipc_errors.AGENT_CONFIG_CAS_MISMATCH


def agent_config_digest(config: object) -> str:
    """Digest the normalized config value, not the files it references.

    Absence has its own digest (canonical JSON null); callers must not use an
    empty string as a wildcard. File payload verification is a separate port.
    """
    normalized = normalize_agent_config(config)
    payload = None if normalized is None else normalized.to_json()
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(b"hyprial-agent-config-v1\0" + canonical.encode()).hexdigest()


class _RegistryConfigBindingMixin:
    def bind_agent_config(
        self, actor: str, *, expected_entity_token: str,
        config: object, expected_current_config_digest: str,
    ) -> Agent:
        if not isinstance(expected_entity_token, str) or not expected_entity_token.strip():
            raise ValueError("expected_entity_token must not be blank")
        if not isinstance(expected_current_config_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", expected_current_config_digest
        ):
            raise ValueError("expected_current_config_digest must be a SHA-256 digest")
        normalized = normalize_agent_config(config)
        # Serialize even separate registry connections/processes. Do not leave
        # a read/merge/save gap or replay a caller's copy of unrelated fields.
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            current = self.require(actor)
            if current.entity_token != expected_entity_token:
                raise AgentIncarnationMismatch("agent incarnation changed before config binding")
            if agent_config_digest(current.config) != expected_current_config_digest:
                raise AgentConfigCasMismatch("agent config changed before config binding")
            updated = replace(current, config=normalized)
            self._validate_config_location(updated)
            self._db.execute(
                "UPDATE agents SET config = ? WHERE actor = ?",
                (None if normalized is None else json.dumps(normalized.to_json()), current.actor),
            )
            return updated
