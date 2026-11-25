"""Probe input and protocol projection. Explicit destinations never use ambient configuration.

Only HTTPS or literal loopback HTTP is accepted. Localhost is pinned to loopback.
Custom addresses require an explicit protocol; defaults are a small fixed set.
Keys are HTTP header values and must be printable ASCII. No input is persisted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from unicodedata import category
from urllib.parse import urlsplit, urlunsplit


DEFAULTS = {
    "deepseek": ("https://api.deepseek.com", "openai-completions"),
    "openai": ("https://api.openai.com/v1", "openai-completions"),
    "anthropic": ("https://api.anthropic.com", "anthropic-messages"),
    "hyprial": ("https://model.hyprial.ai/v1", "openai-completions"),
}
PROTOCOLS = ("openai-completions", "anthropic-messages")
INVALID = "Invalid credential probe input."


def endpoint_url(value: object) -> str:
    """Validate a service base; never echo a rejected address."""
    try:
        if not isinstance(value, str) or not value or len(value) > 2048:
            raise ValueError
        if any(char.isspace() or ord(char) < 32 or char in '\\"<>?#' for char in value):
            raise ValueError
        url = urlsplit(value)
        if not url.hostname or url.username is not None or url.password is not None:
            raise ValueError
        port = url.port
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError
        if url.hostname == "localhost":
            url = url._replace(netloc="127.0.0.1" + (f":{port}" if port is not None else ""))
        return urlunsplit(url).rstrip("/")
    except (ValueError, UnicodeError):
        raise ValueError(INVALID) from None


@dataclass(frozen=True)
class ProbeRequest:
    url: str
    protocol: str
    headers: Mapping[str, str] = field(repr=False)
    body: Mapping[str, object] = field(repr=False)


def prepare(params: Mapping[str, object]) -> ProbeRequest:
    """Translate validated temporary input without consulting a home or registry."""
    try:
        if set(params) - {"provider", "secret", "model", "endpoint", "protocol", "timeoutSeconds"}:
            raise ValueError
        if "timeoutSeconds" in params and (isinstance(params["timeoutSeconds"], bool)
                or not isinstance(params["timeoutSeconds"], (int, float))
                or not 1 <= params["timeoutSeconds"] <= 60
                or int(params["timeoutSeconds"]) != params["timeoutSeconds"]):
            raise ValueError
        vendor, secret, model = (params.get(name) for name in ("provider", "secret", "model"))
        if not isinstance(vendor, str) or not vendor or len(vendor) > 128:
            raise ValueError
        if not isinstance(secret, str) or not 1 <= len(secret) <= 16384 or any(not 33 <= ord(c) <= 126 for c in secret):
            raise ValueError
        if not isinstance(model, str) or not model.strip() or len(model) > 256 or any(category(c).startswith("C") for c in model):
            raise ValueError
        default = DEFAULTS.get(vendor)
        explicit = "endpoint" in params
        if (explicit and "protocol" not in params) or (not explicit and default is None):
            raise ValueError
        endpoint = endpoint_url(params["endpoint"] if explicit else default[0])
        protocol = params.get("protocol", default[1] if default else None)
        if protocol not in PROTOCOLS or (not explicit and protocol != default[1]):
            raise ValueError
        headers = {"accept": "application/json", "accept-encoding": "identity"}
        body = {"model": model.strip(), "messages": [{"role": "user", "content": "Reply OK."}],
                "stream": False, "max_tokens": 64}
        if protocol == "anthropic-messages":
            headers.update({"x-api-key": secret, "anthropic-version": "2023-06-01"})
            url = endpoint + ("/messages" if endpoint.endswith("/v1") else "/v1/messages")
        else:
            headers["authorization"] = "Bearer " + secret
            url = endpoint + "/chat/completions"
            if vendor == "openai" and not explicit:
                body["max_completion_tokens"] = body.pop("max_tokens")
        return ProbeRequest(url, protocol, headers, body)
    except (TypeError, ValueError):
        raise ValueError(INVALID) from None
