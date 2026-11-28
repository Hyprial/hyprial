"""Typed Pi connection projection; callers supply already-authorized credentials.

PR2 has no production caller: PR4 must resolve the durable binding and grant
before passing this value to the P2 launch builder. Tests inject synthetic
bindings. No binding is inferred from legacy provider arguments or environment.
The SDK's zero price is a required placeholder, never known accounting data.
"""

from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
from types import MappingProxyType
from collections.abc import Mapping
from unicodedata import category

from hyprial.identity import ModelConnection

KEY_ENV = "HYPRIAL_PI_CONNECTION_KEY"
TEMPLATE = "openai-chat-text/v1"


@dataclass(frozen=True, slots=True)
class PiModelBinding:
    binding_ref: str
    binding_revision: int
    connection: ModelConnection
    model: str
    key: str = field(repr=False)
    context_window: int = 32768
    max_tokens: int = 4096

    def __post_init__(self) -> None:
        if (not isinstance(self.binding_ref, str) or not self.binding_ref
                or len(self.binding_ref) > 256
                or any(category(c).startswith("C") for c in self.binding_ref)
                or type(self.binding_revision) is not int or not 1 <= self.binding_revision <= 2**53 - 1):
            raise ValueError("invalid binding reference")
        if not isinstance(self.connection, ModelConnection):
            raise ValueError("invalid model connection")
        if self.connection.config_revision > 2**53 - 1:
            raise ValueError("connection revision exceeds the Pi representation")
        if (not isinstance(self.model, str) or not 1 <= len(self.model) <= 256
                or self.model != self.model.strip()
                or any(category(c).startswith("C") for c in self.model)):
            raise ValueError("invalid model identifier")
        if (not isinstance(self.key, str) or not 1 <= len(self.key) <= 16384
                or any(not 33 <= ord(c) <= 126 for c in self.key)):
            raise ValueError("invalid model credential")
        if (type(self.context_window) is not int or type(self.max_tokens) is not int
                or not 0 < self.max_tokens < self.context_window <= 2**31 - 1):
            raise ValueError("invalid model limits")

    @property
    def provider_id(self) -> str:
        material = json.dumps([self.connection.connection_ref, self.connection.config_revision], separators=(",", ":"))
        return "hyprial-connection-" + sha256(material.encode()).hexdigest()

    def snapshot(self) -> dict[str, object]:
        return {
            "schemaVersion": 1, "bindingRef": self.binding_ref,
            "bindingRevision": self.binding_revision, "authKind": "api-key",
            "connectionRef": self.connection.connection_ref,
            "configRevision": self.connection.config_revision, "model": self.model,
            "harness": "pi", "profile": self.connection.profile, "template": TEMPLATE,
            "contextWindow": self.context_window, "maxTokens": self.max_tokens,
            "costStatus": "unknown",
        }

    def definitions(self) -> dict[str, dict[str, object]]:
        return {self.provider_id: {
            "api": self.connection.protocol, "baseUrl": self.connection.endpoint,
            "apiKey": KEY_ENV,
            "models": [{
                "id": self.model, "name": self.model, "reasoning": False, "input": ["text"],
                "cost": dict.fromkeys(("input", "output", "cacheRead", "cacheWrite"), 0),
                "contextWindow": self.context_window, "maxTokens": self.max_tokens,
                "compat": {
                    "supportsStore": False, "supportsDeveloperRole": False,
                    "maxTokensField": "max_tokens", "requiresReasoningContentOnAssistantMessages": False,
                    "supportsStrictMode": False,
                },
            }],
        }}


def validated_snapshot(value: object) -> Mapping[str, object]:
    """Read only the versioned, non-secret provenance emitted by this adapter."""
    fixed = {"schemaVersion": 1, "authKind": "api-key", "harness": "pi",
             "profile": "openai-chat-completions/v1", "template": TEMPLATE, "costStatus": "unknown"}
    fields = set(fixed) | {"bindingRef", "bindingRevision", "connectionRef", "configRevision",
                           "model", "contextWindow", "maxTokens"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("invalid Pi model binding snapshot")
    if any(type(value[k]) is not type(v) or value[k] != v for k, v in fixed.items()):
        raise ValueError("unsupported Pi model binding snapshot")
    for name in ("bindingRef", "model"):
        text = value[name]
        if (not isinstance(text, str) or not 1 <= len(text) <= 256
                or any(category(c).startswith("C") for c in text)
                or name == "model" and text != text.strip()):
            raise ValueError("invalid Pi model binding identity")
    ref = value["connectionRef"]
    if not isinstance(ref, str) or not re.fullmatch(r"[a-f0-9]{32}", ref):
        raise ValueError("invalid Pi connection reference")
    for name in ("bindingRevision", "configRevision", "contextWindow", "maxTokens"):
        if type(value[name]) is not int or not 1 <= value[name] <= 2**53 - 1:
            raise ValueError("invalid Pi model binding number")
    if not value["maxTokens"] < value["contextWindow"] <= 2**31 - 1:
        raise ValueError("invalid Pi model limits")
    return MappingProxyType(dict(value))
