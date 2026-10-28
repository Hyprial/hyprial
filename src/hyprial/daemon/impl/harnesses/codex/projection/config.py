"""Narrow mutable-state tolerance for Codex's projected config."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from .errors import CodexAgentHomeError


# Allen's 2026-10-08 ruling permits only Codex's exact top-level UI-state table.
CODEX_MUTABLE_CONFIG_TABLES = frozenset({"tui"})
_CODEX_AUTH_STORE_LINE = re.compile(
    rb"(?m)^[ \t]*cli_auth_credentials_store[ \t]*=[^\r\n]*(?:\r?\n|$)"
)


def _comparable_config(body: bytes) -> dict[str, object] | None:
    try:
        parsed = tomllib.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    for table in CODEX_MUTABLE_CONFIG_TABLES:
        parsed.pop(table, None)
    return parsed


def codex_projection_body_for_model_vendor(
    native_path: str, projected: bytes, model_provider: str | None
) -> bytes:
    """Return the native bytes required by one model-vendor auth policy."""

    if native_path != "config.toml" or model_provider in {None, "openai"}:
        return projected
    parsed = _comparable_config(projected)
    if parsed is None:
        raise CodexAgentHomeError(
            "Codex projected config.toml is invalid and cannot select an "
            "ephemeral non-OpenAI credential store"
        )
    replacement = b'cli_auth_credentials_store = "ephemeral"\n'
    if "cli_auth_credentials_store" in parsed:
        matches = tuple(_CODEX_AUTH_STORE_LINE.finditer(projected))
        if len(matches) != 1:
            raise CodexAgentHomeError(
                "Codex projected cli_auth_credentials_store cannot be reconciled"
            )
        start, end = matches[0].span()
        body = projected[:start] + replacement + projected[end:]
    else:
        body = replacement + projected
    try:
        effective = tomllib.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise CodexAgentHomeError(
            "Codex projected config.toml cannot enable ephemeral credentials"
        ) from error
    if effective.get("cli_auth_credentials_store") != "ephemeral":
        raise CodexAgentHomeError(
            "Codex projected config.toml did not enable ephemeral credentials"
        )
    return body


def codex_projection_item_reconcilable(
    native_path: str, desired: bytes, current: bytes
) -> bool:
    """Allow repair only when config differs by auth policy and mutable TUI."""

    if native_path != "config.toml":
        return False
    desired_config = _comparable_config(desired)
    current_config = _comparable_config(current)
    if desired_config is None or current_config is None:
        return False
    desired_config.pop("cli_auth_credentials_store", None)
    current_config.pop("cli_auth_credentials_store", None)
    return current_config == desired_config


def codex_projection_item_matches(
    native_path: str, projected: bytes, current: bytes
) -> bool:
    """Compare one projected item, tolerating only exact ``config.toml.tui``."""

    if native_path != "config.toml":
        return current == projected
    projected_config = _comparable_config(projected)
    current_config = _comparable_config(current)
    return (
        projected_config is not None
        and current_config is not None
        and current_config == projected_config
    )


def _codex_config_default_model(config_path: Path) -> str | None:
    """The target config's declared default model, or None when it declares none."""

    try:
        body = Path(config_path).read_bytes()
    except OSError:
        return None
    try:
        parsed = tomllib.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    model = parsed.get("model")
    return model if isinstance(model, str) and model.strip() else None


def _codex_argument_model(args: tuple[str, ...]) -> str | None:
    """The model an explicit ``--model``/``-m`` in runtime args names, if any."""

    for index, value in enumerate(args):
        if value in {"--model", "-m"}:
            return args[index + 1] if index + 1 < len(args) else None
        if value.startswith("--model="):
            return value.split("=", 1)[1] or None
    return None


def resolve_codex_p2_resume_model(
    native_root: Path,
    *,
    explicit_model: str | None,
    model_provider: str | None,
    runtime_args: tuple[str, ...] = (),
) -> str | None:
    """Resolve the model a P2 Codex resume must send to ``thread/resume``.

    On resume Codex inherits the migrated thread's persisted model from its own
    rollout when no ``model`` is supplied, and hyprial has no target-side support
    signal it can trust for it: the 2026-10-16 T3 run showed the target's own
    ``models_cache.json`` advertising a model the account then rejected on the
    first turn.  The target's ``config.toml`` default is the one target-side
    declaration that means "this machine accepts it", so it is sent explicitly;
    when it is absent the launch is refused before the app-server spawns instead
    of letting an unverified source model reach the first turn.  Non-OpenAI
    providers keep their existing provider-owned model selection.
    """

    if explicit_model is None:
        explicit_model = _codex_argument_model(runtime_args)
    if model_provider not in {None, "openai"}:
        return explicit_model
    if explicit_model is not None:
        return explicit_model
    configured = _codex_config_default_model(Path(native_root) / "config.toml")
    if configured is not None:
        return configured
    raise CodexAgentHomeError(
        "Codex P2 resume has no explicit --model and the resolved CODEX_HOME "
        "config.toml declares none: the migrated thread's persisted model would "
        "be resumed without any target-side compatibility check and may be "
        "rejected only at the first turn. Pass --model <target-supported model> "
        "or declare 'model' in the agent's codex config.toml."
    )
