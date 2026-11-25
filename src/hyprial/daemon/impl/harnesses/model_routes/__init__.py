"""Bind the explicit launch selection to one granted model service route.

Only the existing DeepSeek definition and credential mapping are supported.
This rejects implicit session defaults and other harnesses before any value
is delivered. It does not infer a model service from user-selected entry IDs.
"""

from typing import Any

from hyprial.identity import SecretResolutionError
from hyprial.kernel import HarnessLaunchSpec

from ..model_provider import DEEPSEEK_API_KEY_ENV, DEEPSEEK_MODEL, DEEPSEEK_PROVIDER, OPENAI_BASE_URL_ENV


def route_environment(spec: HarnessLaunchSpec, secrets: tuple[Any, ...]) -> dict[str, str]:
    routed = tuple(secret for secret in secrets if secret.route is not None)
    if not routed:
        return {}
    if (spec.harness != "pi" or spec.model_provider != DEEPSEEK_PROVIDER
            or spec.model != DEEPSEEK_MODEL or len(routed) != 1):
        raise SecretResolutionError("route-unsupported", "model-service", "launch")
    selected = routed[0]
    route = selected.route
    candidates = tuple(secret for secret in secrets if DEEPSEEK_API_KEY_ENV in secret.grant.environment_names)
    if (len(candidates) != 1 or candidates[0] is not selected
            or selected.grant.environment_names != (DEEPSEEK_API_KEY_ENV,)
            or route.vendor_id != spec.model_provider or route.protocol != "openai-completions"):
        raise SecretResolutionError("route-mismatch", selected.grant.entry_id, "launch")
    return {OPENAI_BASE_URL_ENV: route.endpoint}
