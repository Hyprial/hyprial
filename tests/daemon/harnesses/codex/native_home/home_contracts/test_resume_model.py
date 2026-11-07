"""T3 D4: a P2 Codex resume must name a target-side model.

On resume Codex inherits the migrated thread's persisted model from its own
rollout when ``model`` is absent, and the 2026-10-16 dual-machine run showed
that model can be rejected only at the first turn (``... is not supported when
using Codex with a ChatGPT account``).  ``resolve_codex_p2_resume_model`` sends
the target's own ``config.toml`` default explicitly, or refuses before the
app-server spawns when even that is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.codex import (
    CodexAgentHomeError,
    resolve_codex_p2_resume_model,
)


def _native(tmp_path: Path, body: str | None) -> Path:
    native_root = tmp_path / "native" / "codex"
    native_root.mkdir(parents=True)
    if body is not None:
        (native_root / "config.toml").write_text(body, encoding="utf-8")
    return native_root


def test_target_config_default_is_sent_explicitly(tmp_path: Path) -> None:
    native_root = _native(tmp_path, 'model = "gpt-6-sol"\n')

    assert (
        resolve_codex_p2_resume_model(
            native_root, explicit_model=None, model_provider=None
        )
        == "gpt-6-sol"
    )


def test_explicit_model_wins_over_the_target_default(tmp_path: Path) -> None:
    native_root = _native(tmp_path, 'model = "target-default"\n')

    assert (
        resolve_codex_p2_resume_model(
            native_root, explicit_model="operator-choice", model_provider="openai"
        )
        == "operator-choice"
    )


def test_runtime_arg_model_counts_as_explicit(tmp_path: Path) -> None:
    native_root = _native(tmp_path, 'model = "target-default"\n')

    assert (
        resolve_codex_p2_resume_model(
            native_root,
            explicit_model=None,
            model_provider="openai",
            runtime_args=("--model=cli-choice",),
        )
        == "cli-choice"
    )


def test_missing_target_default_is_refused_before_spawn(tmp_path: Path) -> None:
    native_root = _native(tmp_path, 'cli_auth_credentials_store = "file"\n')

    with pytest.raises(CodexAgentHomeError, match="explicit --model"):
        resolve_codex_p2_resume_model(
            native_root, explicit_model=None, model_provider="openai"
        )
    # An absent config.toml is "no target-side declaration" too, not a pass.
    with pytest.raises(CodexAgentHomeError, match="explicit --model"):
        resolve_codex_p2_resume_model(
            _native(tmp_path / "empty", None),
            explicit_model=None,
            model_provider=None,
        )


def test_non_openai_provider_keeps_provider_owned_selection(tmp_path: Path) -> None:
    native_root = _native(tmp_path, 'model = "target-default"\n')

    # DeepSeek/Hyprial own the model through their provider configuration; this
    # guard neither substitutes the target default nor refuses.
    assert (
        resolve_codex_p2_resume_model(
            native_root, explicit_model=None, model_provider="deepseek"
        )
        is None
    )
