"""Select one native login slot without copying secrets or adding defaults."""

from dataclasses import dataclass
from pathlib import Path

from hyprial.error import codes
from hyprial.identity import NativeAuthTarget, NativeCredentialError, native_auth_targets
from ..pi.bindings.errors import PiModelBindingError

MODEL_AUTH_NAMES = frozenset({
    "OPENAI_API_KEY", "OPENAI_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN", "DEEPSEEK_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "AZURE_OPENAI_API_KEY", "MISTRAL_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY",
    "XAI_API_KEY", "ZAI_API_KEY", "KIMI_API_KEY", "HYPRIAL_PI_CONNECTION_KEY",
})


def target_for(config):
    try:
        target = NativeAuthTarget.from_reference(config.native_auth_ref)
        if target.harness != config.harness:
            raise ValueError
        return target
    except ValueError:
        raise PiModelBindingError(codes.MODEL_COMBINATION_UNSUPPORTED, "admission", "runtime-option") from None


def select_native(config, secrets, registry, agent):
    target = target_for(config)
    if any(secret.route is not None or MODEL_AUTH_NAMES.intersection(secret.grant.environment_names)
           for secret in secrets):
        raise PiModelBindingError(codes.MODEL_AUTH_UNAVAILABLE, "authorize", "ambiguous")
    try:
        home = Path(registry.home_receipt(agent.actor).path)
        targets = native_auth_targets(home, config.harness)
    except NativeCredentialError as error:
        reason = "missing" if error.code == codes.PROVIDER_SOURCE_MISSING else "unreadable"
        raise PiModelBindingError(codes.MODEL_AUTH_UNAVAILABLE, "authorize", reason) from None
    if target not in targets:
        raise PiModelBindingError(codes.MODEL_AUTH_UNAVAILABLE, "authorize", "missing")
    return target


@dataclass(frozen=True, slots=True)
class NativeModelBinding:
    binding_ref: str
    binding_revision: int
    target: NativeAuthTarget
    model: str

    @property
    def provider_id(self):
        return self.target.target

    def snapshot(self):
        return {"schemaVersion": 1, "authKind": "native", "bindingRef": self.binding_ref,
                "bindingRevision": self.binding_revision, "nativeAuthRef": self.target.reference,
                "harness": self.target.harness, "model": self.model, "target": self.target.target}

    def definitions(self):
        return {}


def resolve(binding, secrets, registry, agent):
    return NativeModelBinding(binding.binding_ref, binding.binding_revision,
                              select_native(binding.configuration, secrets, registry, agent),
                              binding.configuration.model)
