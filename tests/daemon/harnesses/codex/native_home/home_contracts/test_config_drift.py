from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.codex import (
    CodexAgentHomeError,
    CodexAppServerClient,
    prepare_codex_runtime_roots,
    verify_codex_native_projection,
)
from hyprial.identity.impl.agents.home.config import (
    AgentConfig,
    build_native_projection,
    materialize_native_projection,
)
from hyprial.identity.impl.agents.home.environment import (
    ChildEnvironmentLaunch,
    CompleteChildEnvironment,
)
from hyprial.kernel import HarnessLaunchSpec

from ..support import _runtime_context


def _projected_codex_home(tmp_path: Path):
    source = tmp_path / "source"
    config = source / "native" / "codex" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('model = "gpt-test"\n', encoding="utf-8")
    agent_config = AgentConfig(str(source))
    manifest = agent_config.freeze_manifest()
    projection = build_native_projection(manifest, "codex")
    projection_root = tmp_path / "projection"
    receipt = materialize_native_projection(
        agent_config,
        manifest,
        projection,
        actor="agent:owner:node:codex",
        entity_token="entity-token",
        projection_root=projection_root,
    )
    native_root = tmp_path / "native"
    shutil.copytree(projection_root, native_root)
    session_root = tmp_path / "sessions"
    session_root.mkdir(mode=0o700)
    return manifest, projection, receipt, projection_root, native_root, session_root


def test_codex_tui_state_and_subtables_do_not_drift_headless_projection(
    tmp_path: Path,
) -> None:
    manifest, projection, receipt, projection_root, native_root, session_root = (
        _projected_codex_home(tmp_path)
    )
    native_config = native_root / "config.toml"
    native_config.write_text(
        native_config.read_text(encoding="utf-8")
        + "\n[tui]\nanimations = false\n[tui.notices]\nwelcome = true\n",
        encoding="utf-8",
    )

    verify_codex_native_projection(receipt, native_root)
    prepare_codex_runtime_roots(
        projection_root=projection_root,
        native_root=native_root,
        session_root=session_root,
        codex_executable=tmp_path / "unused-codex",
        receipt=receipt,
    )

    environment = {
        "CODEX_HOME": str(native_root),
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "tmp"),
    }
    context = _runtime_context(
        actor="agent:owner:node:codex",
        source=tmp_path / "source",
        projection_root=projection_root,
        native_root=native_root,
        session_root=session_root,
        manifest=manifest,
        projection=projection,
        receipt=receipt,
        environment=environment,
    )
    CodexAppServerClient(
        HarnessLaunchSpec(
            harness="codex",
            name="codex-headless",
            headless=True,
            cwd=str(tmp_path),
        ),
        command=(str(tmp_path / "unused-codex"),),
        complete_launch=ChildEnvironmentLaunch(
            CompleteChildEnvironment(tuple(sorted(environment.items()))),
            actor=context.actor,
            runtime_context=context,
        ),
    )


@pytest.mark.parametrize("table", ("tuix", "tui_extra"))
def test_codex_tui_prefix_tables_remain_strict(tmp_path: Path, table: str) -> None:
    _, _, receipt, _, native_root, _ = _projected_codex_home(tmp_path)
    native_config = native_root / "config.toml"
    native_config.write_text(
        native_config.read_text(encoding="utf-8")
        + f"\n[{table}]\nchanged = true\n",
        encoding="utf-8",
    )

    with pytest.raises(CodexAgentHomeError, match="config.toml.*drifted"):
        verify_codex_native_projection(receipt, native_root)


def test_invalid_current_codex_config_is_drift(tmp_path: Path) -> None:
    _, _, receipt, _, native_root, _ = _projected_codex_home(tmp_path)
    (native_root / "config.toml").write_text("[tui\n", encoding="utf-8")

    with pytest.raises(CodexAgentHomeError, match="config.toml.*drifted"):
        verify_codex_native_projection(receipt, native_root)


def test_codex_projection_still_refuses_wrong_file_mode(tmp_path: Path) -> None:
    _, _, receipt, projection_root, native_root, session_root = (
        _projected_codex_home(tmp_path)
    )
    (native_root / "config.toml").chmod(0o644)

    with pytest.raises(CodexAgentHomeError, match="config.toml.*drifted"):
        prepare_codex_runtime_roots(
            projection_root=projection_root,
            native_root=native_root,
            session_root=session_root,
            codex_executable=tmp_path / "unused-codex",
            receipt=receipt,
        )
