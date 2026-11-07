from __future__ import annotations

import json
from pathlib import Path


from hyprial.identity.impl.agents.home.config import (
    ConfigManifest,
    ConfigProjectionReceipt,
    NativeConfigProjection,
)
from hyprial.identity.impl.agents.runtime import (
    AgentRuntimeContext,
    AgentRuntimeRoots,
)


P20_SNAPSHOT = (
    Path(__file__).resolve().parents[5] / 'docs/notes/evidence/agent-home-p2-p20-20260921/probe-snapshot.json'
)


def _runtime_context(
    *,
    actor: str,
    source: Path,
    projection_root: Path,
    native_root: Path,
    session_root: Path,
    manifest: ConfigManifest,
    projection: NativeConfigProjection,
    receipt: ConfigProjectionReceipt,
    environment: dict[str, str],
) -> AgentRuntimeContext:
    agent_home = native_root.parents[2]
    roots = AgentRuntimeRoots(
        agent_home=agent_home,
        config_source=source,
        projection_root=projection_root,
        native_root=native_root,
        session_root=session_root,
        tool_home=agent_home / "state" / "home",
        xdg_config_home=agent_home / "secrets" / "tools" / "xdg",
        xdg_cache_home=agent_home / "state" / "xdg" / "cache",
        xdg_data_home=agent_home / "state" / "xdg" / "data",
        xdg_state_home=agent_home / "state" / "xdg" / "state",
        tool_profile_root=agent_home / "state" / "config" / "tools",
    )
    return AgentRuntimeContext(
        actor=actor,
        entity_token="p23-incarnation",
        harness="codex",
        manifest=manifest,
        projection=projection,
        projection_receipt=receipt,
        roots=roots,
        tool_profile_id="p23-test-profile",
        environment_items=tuple(sorted(environment.items())),
    )


def _native_responses(
    tmp_path: Path,
) -> tuple[Path, Path, dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    """Adapt P20's real app-server response and P23's real account/skill shapes."""

    snapshot = json.loads(P20_SNAPSHOT.read_text(encoding="utf-8"))
    responses = snapshot["codex"]["config_discovery"]["rpc"]["responses"]
    native_root = tmp_path / "agent" / "secrets" / "native" / "codex"
    workspace = tmp_path / "workspace" / "repo"
    native_root.mkdir(parents=True)
    native_root.chmod(0o700)
    (native_root / "config.toml").write_text(
        'cli_auth_credentials_store = "file"\n'
    )
    (native_root / "config.toml").chmod(0o600)
    workspace.mkdir(parents=True)
    (workspace / ".git").mkdir()
    auth = native_root / "auth.json"
    auth.write_text('{"auth_mode":"apikey","OPENAI_API_KEY":"P23_SENTINEL"}')
    auth.chmod(0o600)

    initialize = json.loads(json.dumps(responses["1"]["result"]))
    initialize["codexHome"] = str(native_root)
    config_read = json.loads(json.dumps(responses["2"]["result"]))
    for layer in config_read["layers"]:
        name = layer["name"]
        if name["type"] == "project":
            name["dotCodexFolder"] = str(workspace / ".codex")
        elif name["type"] == "user":
            name["file"] = str(native_root / "config.toml")
    config_read["config"]["projects"] = {
        str(workspace): {"trust_level": "trusted"}
    }
    config_read["origins"][f"projects.{workspace}.trust_level"] = (
        config_read["origins"].pop(
            "projects.<PROBE_ROOT>/workspace.trust_level"
        )
    )
    config_read["origins"][f"projects.{workspace}.trust_level"]["name"][
        "file"
    ] = str(native_root / "config.toml")
    for origin in config_read["origins"].values():
        name = origin["name"]
        if name.get("type") == "user":
            name["file"] = str(native_root / "config.toml")

    account_read = {
        "account": {"type": "apiKey"},
        "requiresOpenaiAuth": True,
    }
    skills_list = {
        "data": [
            {
                "cwd": str(workspace),
                "skills": [
                    {
                        "name": "p23-codex-project",
                        "path": str(
                            workspace
                            / ".agents"
                            / "skills"
                            / "p23-codex-project"
                            / "SKILL.md"
                        ),
                        "scope": "repo",
                        "enabled": True,
                    },
                    {
                        "name": "p23-codex-user",
                        "path": str(
                            native_root
                            / "skills"
                            / "p23-codex-user"
                            / "SKILL.md"
                        ),
                        "scope": "user",
                        "enabled": True,
                    },
                    {
                        "name": "openai-docs",
                        "path": str(
                            native_root
                            / "skills"
                            / ".system"
                            / "openai-docs"
                            / "SKILL.md"
                        ),
                        "scope": "system",
                        "enabled": True,
                    },
                ],
                "errors": [],
            }
        ]
    }
    return (
        native_root,
        workspace,
        initialize,
        config_read,
        account_read,
        skills_list,
    )























# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    'P20_SNAPSHOT',
    '_native_responses',
    '_runtime_context',
]
