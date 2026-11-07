"""HYPRIAL_HOME plugin loading: manifest parsing and per-harness injection plans."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hyprial.shell.impl.plugins import (
    RESERVED_MCP_SERVER,
    McpServerSpec,
    PluginManifestError,
    builtin_skill_dirs,
    claude_plan,
    codex_plan,
    load_manifest,
    manifest_path,
    materialize_claude_skill_plugin,
    pi_plan,
)


def _write_manifest(home: Path, payload: dict) -> Path:
    path = manifest_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _payload_dir(home: Path, relative: str) -> str:
    target = manifest_path(home).parent / relative
    target.mkdir(parents=True, exist_ok=True)
    return relative


def test_missing_manifest_is_empty_but_builtin_skill_still_rides(tmp_path: Path) -> None:
    manifest = load_manifest(tmp_path)
    assert manifest.plugins == ()
    claude = claude_plan(manifest)
    assert claude.mcp_servers == {}
    assert claude.plugin_dirs == ()
    assert claude.skill_dirs == builtin_skill_dirs()
    pi = pi_plan(manifest)
    assert pi.skill_dirs == builtin_skill_dirs()
    assert pi.extensions == ()
    codex = codex_plan(manifest)
    assert codex.config_args == ()


def test_builtin_skills_are_packaged_with_frontmatter() -> None:
    packaged = builtin_skill_dirs()
    assert packaged, "at least one builtin skill must ship with the wheel"
    names = set()
    for directory in packaged:
        text = (directory / "SKILL.md").read_text(encoding="utf-8")
        assert text.startswith("---\n"), f"{directory} has no frontmatter"
        declared = next(
            line.split(":", 1)[1].strip()
            for line in text.splitlines()
            if line.startswith("name:")
        )
        assert declared, f"{directory} declares an empty name"
        names.add(declared)
    assert {"hyprial-ops", "stc", "asd-ste100"} <= names
    assert {path.name for path in packaged} >= {"hyprial-ops", "stc", "ste100"}


def test_full_manifest_produces_per_harness_plans(tmp_path: Path) -> None:
    skill_rel = _payload_dir(tmp_path, "skills/team-notes")
    ext_dir = manifest_path(tmp_path).parent / "ext"
    ext_dir.mkdir(parents=True)
    (ext_dir / "bridge.ts").write_text("// extension", encoding="utf-8")
    plugin_rel = _payload_dir(tmp_path, "claude/team-plugin")
    _write_manifest(
        tmp_path,
        {
            "version": 1,
            "plugins": [
                {
                    "name": "team-mcp",
                    "kind": "mcp",
                    "server": {
                        "command": "/usr/bin/python3",
                        "args": ["-m", "team.mcp"],
                        "env": {"TEAM_TOKEN_FILE": "/secrets/token"},
                    },
                },
                {"name": "team-notes", "kind": "skill", "path": skill_rel},
                {"name": "bridge-ext", "kind": "pi-extension", "path": "ext/bridge.ts"},
                {"name": "team-plugin", "kind": "claude-plugin", "path": plugin_rel},
            ],
        },
    )
    manifest = load_manifest(tmp_path)

    claude = claude_plan(manifest)
    assert claude.mcp_servers == {
        "team-mcp": {
            "type": "stdio",
            "command": "/usr/bin/python3",
            "args": ["-m", "team.mcp"],
            "env": {"TEAM_TOKEN_FILE": "/secrets/token"},
        }
    }
    assert [path.name for path in claude.plugin_dirs] == ["team-plugin"]
    assert [path.name for path in claude.skill_dirs] == [
        *[path.name for path in builtin_skill_dirs()],
        "team-notes",
    ]

    codex = codex_plan(manifest)
    assert codex.config_args == (
        "-c",
        'mcp_servers.team-mcp.command="/usr/bin/python3"',
        "-c",
        'mcp_servers.team-mcp.args=["-m", "team.mcp"]',
        "-c",
        'mcp_servers.team-mcp.env={"TEAM_TOKEN_FILE"="/secrets/token"}',
    )

    pi = pi_plan(manifest)
    assert [path.name for path in pi.skill_dirs] == [
        *[path.name for path in builtin_skill_dirs()],
        "team-notes",
    ]
    assert [path.name for path in pi.extensions] == ["bridge.ts"]


def test_every_incapable_plugin_is_reported_as_skipped(tmp_path: Path) -> None:
    """Wildcard declarations are still explicit inputs, never silent absence."""

    _write_manifest(
        tmp_path,
        {
            "version": 1,
            "plugins": [
                {
                    "name": "everywhere-mcp",
                    "kind": "mcp",
                    "server": {"command": "srv"},
                }
            ],
        },
    )
    manifest = load_manifest(tmp_path)
    skipped = pi_plan(manifest).skipped
    assert [item.name for item in skipped] == ["everywhere-mcp"]
    assert "cannot load" in skipped[0].reason
    _write_manifest(
        tmp_path,
        {
            "version": 1,
            "plugins": [
                {
                    "name": "pi-only-mcp",
                    "kind": "mcp",
                    "harnesses": ["pi"],
                    "server": {"command": "srv"},
                }
            ],
        },
    )
    skipped = pi_plan(load_manifest(tmp_path)).skipped
    assert [item.name for item in skipped] == ["pi-only-mcp"]
    assert "cannot load" in skipped[0].reason


def test_plan_support_is_derived_from_capability_declaration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mutating the one capability row changes planning without planner edits."""

    from hyprial.daemon.impl.harnesses import capabilities
    from hyprial.daemon.impl.harnesses.capabilities import CapabilitySupport
    from hyprial.kernel import SupportLevel

    skill_rel = _payload_dir(tmp_path, "skills/team-notes")
    _write_manifest(
        tmp_path,
        {
            "version": 1,
            "plugins": [
                {"name": "team-notes", "kind": "skill", "path": skill_rel}
            ],
        },
    )
    manifest = load_manifest(tmp_path)
    assert [path.name for path in pi_plan(manifest).skill_dirs] == [
        *[path.name for path in builtin_skill_dirs()],
        "team-notes",
    ]

    monkeypatch.setitem(
        capabilities._DECLARATIONS[("pi", False)],
        capabilities.Capability.PLUGIN_INJECTION,
        CapabilitySupport(SupportLevel.UNSUPPORTED),
    )

    mutated = pi_plan(manifest)
    assert mutated.skill_dirs == builtin_skill_dirs()
    assert [item.name for item in mutated.skipped] == ["team-notes"]


def test_harness_filter_is_respected(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        {
            "version": 1,
            "plugins": [
                {
                    "name": "codex-only",
                    "kind": "mcp",
                    "harnesses": ["codex"],
                    "server": {"command": "srv"},
                }
            ],
        },
    )
    manifest = load_manifest(tmp_path)
    assert claude_plan(manifest).mcp_servers == {}
    assert codex_plan(manifest).config_args[:2] == (
        "-c",
        'mcp_servers.codex-only.command="srv"',
    )


@pytest.mark.parametrize(
    "entry, message",
    (
        ({"name": RESERVED_MCP_SERVER, "kind": "mcp", "server": {"command": "x"}}, "reserved"),
        ({"name": "bad name!", "kind": "mcp", "server": {"command": "x"}}, "name"),
        ({"name": "x", "kind": "nope"}, "kind"),
        ({"name": "x", "kind": "mcp"}, "server"),
        ({"name": "x", "kind": "skill", "path": "does/not/exist"}, "does not exist"),
        ({"name": "x", "kind": "skill", "path": "../outside"}, "escapes"),
        ({"name": "x", "kind": "mcp", "harnesses": ["not-a-harness"], "server": {"command": "x"}}, "subset"),
    ),
)
def test_invalid_entries_are_refused(tmp_path: Path, entry: dict, message: str) -> None:
    _write_manifest(tmp_path, {"version": 1, "plugins": [entry]})
    with pytest.raises(PluginManifestError) as failure:
        load_manifest(tmp_path)
    assert message in str(failure.value)


def test_duplicate_names_and_bad_version_are_refused(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        {
            "version": 1,
            "plugins": [
                {"name": "x", "kind": "mcp", "server": {"command": "a"}},
                {"name": "x", "kind": "mcp", "server": {"command": "b"}},
            ],
        },
    )
    with pytest.raises(PluginManifestError, match="duplicate"):
        load_manifest(tmp_path)
    _write_manifest(tmp_path, {"version": 2, "plugins": []})
    with pytest.raises(PluginManifestError, match="version"):
        load_manifest(tmp_path)


def test_materialized_claude_skill_plugin_shape(tmp_path: Path) -> None:
    source = tmp_path / "my-skill"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: my-skill\n---\nbody\n", encoding="utf-8")
    target = tmp_path / "session-plugin"

    produced = materialize_claude_skill_plugin((*builtin_skill_dirs(), source), target)

    assert produced == target
    meta = json.loads((target / ".claude-plugin" / "plugin.json").read_text())
    assert meta["name"] == "hyprial"
    assert (target / "skills" / "hyprial-ops" / "SKILL.md").exists()
    assert (target / "skills" / "my-skill" / "SKILL.md").read_text().endswith("body\n")

    # Re-materializing over the same target (a crashed earlier launch's
    # leftovers) must replace, not fail or merge stale payloads.
    (source / "SKILL.md").write_text("---\nname: my-skill\n---\nnewer\n", encoding="utf-8")
    materialize_claude_skill_plugin((source,), target)
    assert (target / "skills" / "my-skill" / "SKILL.md").read_text().endswith("newer\n")


def test_launch_cleanup_removes_the_materialized_skill_plugin(tmp_path: Path) -> None:
    """The skill payload must not outlive the launch that generated it."""

    from hyprial.daemon.impl.harnesses.runtime._launch_cleanup import cleanup_launch_resources

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config_path = state_dir / "claude-channel-session-abcd1234.json"
    config_path.write_text("{}", encoding="utf-8")
    target = state_dir / f"{config_path.stem}-skills"
    materialize_claude_skill_plugin(builtin_skill_dirs(), target)
    assert (target / "skills" / "hyprial-ops" / "SKILL.md").exists()

    cleanup_launch_resources(config_path)

    assert not target.exists()
    # Idempotent: teardown runs on paths that may already be gone.
    cleanup_launch_resources(config_path)


@pytest.mark.skipif(
    not os.environ.get("HYPRIAL_TEST_LIVE_CLAUDE_SESSION"),
    reason=(
        "starts a real authenticated Claude session (billable); "
        "set HYPRIAL_TEST_LIVE_CLAUDE_SESSION=1 to run it"
    ),
)
@pytest.mark.skipif(
    shutil.which(os.environ.get("HARNESS_CLAUDE_BIN", "claude")) is None,
    reason="the claude binary is not installed on this machine",
)
def test_generated_plugin_is_loadable_by_the_real_claude_binary(tmp_path: Path) -> None:
    """The layout claim is about another program, so ask that program.

    Asserting our own argv proves nothing about whether Claude Code accepts
    ``.claude-plugin/plugin.json`` + ``skills/<name>/``.  A session launched
    with --plugin-dir must actually list the packaged skill.

    Opt-in, unlike the codex checks below: this one starts a real model
    session, so it costs money and depends on live credentials.  Left in the
    default suite it would fail on any machine with claude installed but
    logged out -- for a reason that has nothing to do with the layout.  The
    codex checks stay on by default because they only parse argv locally.
    """

    plugin_dir = materialize_claude_skill_plugin(builtin_skill_dirs(), tmp_path / "p")
    completed = subprocess.run(
        [
            os.environ.get("HARNESS_CLAUDE_BIN", "claude"),
            "--plugin-dir",
            str(plugin_dir),
            "-p",
            "List the names of every skill available to you, one per line.",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=tmp_path,
    )

    assert completed.returncode == 0, completed.stderr
    assert "hyprial-ops" in completed.stdout, completed.stdout


@pytest.mark.skipif(
    shutil.which(os.environ.get("HARNESS_CODEX_BIN", "codex")) is None,
    reason="the codex binary is not installed on this machine",
)
def test_codex_overrides_are_accepted_by_the_real_codex_binary(tmp_path: Path) -> None:
    """Same reasoning for codex's dotted-path TOML override surface."""

    from hyprial.shell.impl.plugins import _codex_overrides

    overrides = _codex_overrides(
        "hyprial-probe",
        McpServerSpec(
            command="/usr/bin/python3", args=["-m", "probe"], env={"K": "v"}
        ),
    )
    completed = subprocess.run(
        [os.environ.get("HARNESS_CODEX_BIN", "codex"), *overrides, "mcp", "list"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )

    assert completed.returncode == 0, completed.stderr
    # Codex materialized the injected server rather than rejecting the syntax.
    assert "hyprial-probe" in completed.stdout, completed.stdout
    assert "/usr/bin/python3" in completed.stdout, completed.stdout


@pytest.mark.skipif(
    shutil.which(os.environ.get("HARNESS_CODEX_BIN", "codex")) is None,
    reason="the codex binary is not installed on this machine",
)
def test_codex_overrides_parse_in_the_app_server_position(tmp_path: Path) -> None:
    """The overrides must parse where we actually put them.

    ``mcp list`` above proves the semantics, but it passes ``-c`` BEFORE the
    subcommand while CodexInteractiveAppServer puts them AFTER ``app-server``.
    A surface accepted in one position is not automatically accepted in the
    other, so exercise the real one.  ``generate-json-schema`` is used because
    it parses the same argv without binding the app-server's socket.
    """

    from hyprial.shell.impl.plugins import _codex_overrides

    overrides = _codex_overrides(
        "hyprial-probe",
        McpServerSpec(
            command="/usr/bin/python3", args=["-m", "probe"], env={"K": "v"}
        ),
    )
    out_dir = tmp_path / "schema"
    out_dir.mkdir()
    completed = subprocess.run(
        [
            os.environ.get("HARNESS_CODEX_BIN", "codex"),
            "app-server",
            *overrides,
            "generate-json-schema",
            "--out",
            str(out_dir),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=tmp_path,
    )

    assert completed.returncode == 0, completed.stderr
    assert "unexpected argument" not in completed.stderr.lower(), completed.stderr
