"""Explicit native target slots in an owned agent's existing credential store.

References select a harness, credential kind and target within one incarnation.
They contain no token fingerprint: OAuth refresh preserves a selection; logout
makes it unavailable, and explicit login can repopulate the same target slot.
This inventory neither proves upstream validity nor interprets command/env keys.
"""

from dataclasses import dataclass
import json
import math
import re
from pathlib import Path

from hyprial.kernel import ipc_errors
from ..native_credential import NativeCredentialError, read_native_credential_set


@dataclass(frozen=True, slots=True)
class NativeAuthTarget:
    harness: str
    target: str
    kind: str

    def __post_init__(self):
        if (self.harness not in {"pi", "claude", "codex"}
                or self.kind not in {"oauth", "api-key"}
                or not isinstance(self.target, str)
                or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", self.target) is None
                or self.harness == "claude" and (self.target, self.kind) != ("anthropic", "oauth")
                or self.harness == "codex" and self.target != "openai"):
            raise ValueError("unsupported native target")

    @property
    def reference(self):
        return f"native:v1:{self.harness}:{self.kind}:{self.target}"

    @classmethod
    def from_reference(cls, value):
        if not isinstance(value, str):
            raise ValueError("invalid native target reference")
        match = re.fullmatch(r"native:v1:([^:]*):([^:]*):([^:]*)", value)
        if match is None:
            raise ValueError("invalid native target reference")
        return cls(match[1], match[3], match[2])

    def to_json(self):
        return {"nativeAuthRef": self.reference, "target": self.target, "kind": self.kind}


def _secret(value):
    return (isinstance(value, str) and 0 < len(value) <= 16384
            and not value.startswith("!") and all(33 <= ord(c) <= 126 for c in value))


def _pairs(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("duplicate native credential field")
        data[key] = value
    return data


def native_auth_targets(agent_home: Path, harness: str) -> tuple[NativeAuthTarget, ...]:
    """Return metadata only; unreadable, ambiguous and unsupported stores refuse."""
    filename = ".credentials.json" if harness == "claude" else "auth.json"
    documents = dict(read_native_credential_set(agent_home, harness, filenames=(filename,), max_bytes=65536))
    try:
        raw = documents[".credentials.json" if harness == "claude" else "auth.json"]
        if len(raw) > 1024 * 1024:
            raise ValueError
        data = json.loads(raw, object_pairs_hook=_pairs)
        if not isinstance(data, dict) or not data:
            raise ValueError
        if harness == "pi":
            targets = []
            for target, credential in sorted(data.items()):
                if not isinstance(credential, dict):
                    raise ValueError
                kind = credential.get("type")
                if "env" in credential:
                    raise ValueError
                if kind == "api_key" and _secret(credential.get("key")):
                    targets.append(NativeAuthTarget(harness, target, "api-key"))
                elif (kind == "oauth" and _secret(credential.get("access"))
                      and _secret(credential.get("refresh"))
                      and type(credential.get("expires")) in {int, float}
                      and math.isfinite(credential["expires"])):
                    targets.append(NativeAuthTarget(harness, target, "oauth"))
                else:
                    raise ValueError
            return tuple(targets)
        if harness == "claude":
            credential = data.get("claudeAiOauth")
            if not isinstance(credential, dict) or not _secret(credential.get("accessToken")):
                raise ValueError
            return (NativeAuthTarget(harness, "anthropic", "oauth"),)
        if harness == "codex":
            token = data.get("tokens")
            oauth = isinstance(token, dict) and _secret(token.get("access_token"))
            key = _secret(data.get("OPENAI_API_KEY"))
            if oauth == key:
                raise ValueError
            return (NativeAuthTarget(harness, "openai", "oauth" if oauth else "api-key"),)
        raise ValueError
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise NativeCredentialError(ipc_errors.MODEL_AUTH_UNAVAILABLE,
                                    "Native authentication target is unreadable or unsupported.") from None
