"""Private Pi credential projection from the existing resolved Secret grants.

The projection is ephemeral; only reference/revision metadata enters launch
records. An extra routed credential is ambiguous, never a fallback candidate.
"""

from dataclasses import dataclass, field, fields

from hyprial.error import codes
from hyprial.identity import ChildEnvironmentLaunch, ModelConnection
from hyprial.daemon.impl.harnesses.pi.bindings import KEY_ENV, PiModelBinding
from hyprial.daemon.impl.harnesses.pi.bindings.errors import PiModelBindingError


@dataclass(frozen=True, slots=True)
class BoundChildEnvironmentLaunch(ChildEnvironmentLaunch):
    model_binding: PiModelBinding | None = field(
        default=None, repr=False, compare=False
    )


def select_secret(config, secrets):
    routed = tuple(secret for secret in secrets if secret.route is not None)
    selected = tuple(
        secret
        for secret in routed
        if isinstance(secret.route, ModelConnection)
        and secret.route.connection_ref == config.connection_ref
    )
    if not selected:
        raise PiModelBindingError(codes.MODEL_AUTH_UNAVAILABLE, "authorize", "missing")
    if len(selected) != 1 or len(routed) != 1:
        raise PiModelBindingError(
            codes.MODEL_AUTH_UNAVAILABLE, "authorize", "ambiguous"
        )
    secret = selected[0]
    if secret.route.config_revision != config.config_revision:
        raise PiModelBindingError(
            codes.MODEL_CONFIGURATION_CONFLICT, "bind", "revision-mismatch"
        )
    if any(
        KEY_ENV in item.grant.environment_names
        for item in secrets
        if item is not secret
    ):
        raise PiModelBindingError(
            codes.MODEL_AUTH_UNAVAILABLE, "authorize", "ambiguous"
        )
    return secret


def resolve(binding, secrets):
    config = binding.configuration
    secret = select_secret(config, secrets)
    try:
        return PiModelBinding(
            binding.binding_ref,
            binding.binding_revision,
            secret.route,
            config.model,
            secret.value,
        )
    except ValueError:
        raise PiModelBindingError(
            codes.MODEL_CONFIGURATION_INVALID, "validate", "invalid-connection"
        ) from None


def attach(launch, binding):
    return BoundChildEnvironmentLaunch(
        **{
            item.name: getattr(launch, item.name)
            for item in fields(ChildEnvironmentLaunch)
        },
        model_binding=binding,
    )
