"""P21 T03/T16: frozen C projection, drift fences, and receipt privacy."""

from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path

import pytest

import hyprial.identity.impl.agents.home.config as config_module
from hyprial.identity.impl.agents import (
    NATIVE_CONFIG_MAPPING_VERSION,
    AgentConfig,
    AgentConfigError,
    ConfigManifest,
    ConfigProjectionReceipt,
    NativeConfigProjection,
    build_native_projection,
    materialize_native_projection,
    verify_native_projection,
)
from hyprial.identity.impl.agents.actor.ports import AgentProjection


def _config(source: Path, *, required: bool = True) -> AgentConfig:
    return AgentConfig.from_json(
        {
            "sources": [{"path": str(source), "required": required}],
            "discovery": "explicit-only",
        }
    )


def _write(source: Path, relative: str, body: str) -> Path:
    path = source.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _full_c(tmp_path: Path) -> Path:
    source = tmp_path / "C"
    _write(source, "native/claude/settings.json", '{"model":"claude"}\n')
    _write(source, "native/claude/CLAUDE.md", "claude-personality\n")
    _write(source, "native/codex/config.toml", 'model = "codex"\n')
    _write(source, "native/codex/AGENTS.md", "codex-personality\n")
    _write(source, "native/pi/settings.json", '{"model":"pi"}\n')
    _write(source, "native/pi/AGENTS.md", "pi-personality\n")
    _write(source, "shared/skills/review/SKILL.md", "shared-review\n")
    return source


@pytest.mark.parametrize(
    ("harness", "expected"),
    (
        ("claude", {"CLAUDE.md", "settings.json", "skills/review/SKILL.md"}),
        ("codex", {"AGENTS.md", "config.toml", "skills/review/SKILL.md"}),
        ("pi", {"AGENTS.md", "settings.json", "skills/review/SKILL.md"}),
    ),
)
def test_t03_projection_uses_frozen_mapping_and_atomic_private_publish(
    tmp_path: Path, harness: str, expected: set[str]
) -> None:
    source = _full_c(tmp_path)
    config = _config(source)
    manifest = config.freeze_manifest()
    projection = build_native_projection(manifest, harness)
    parent = tmp_path / "projections"
    parent.mkdir(mode=0o700)
    target = parent / harness

    receipt = materialize_native_projection(
        config,
        manifest,
        projection,
        actor="worker",
        entity_token="incarnation-1",
        projection_root=target,
    )

    assert projection.mapping_version == NATIVE_CONFIG_MAPPING_VERSION
    assert {item.native_path for item in projection.items} == expected
    assert {path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()} == expected
    assert stat_mode(target) == 0o700
    assert {stat_mode(path) for path in target.rglob("*") if path.is_file()} == {0o600}
    verify_native_projection(projection, target)
    assert NativeConfigProjection.from_json(projection.to_json()) == projection
    assert ConfigProjectionReceipt.from_json(receipt.to_json()) == receipt

    # Publishing the same content-addressed projection is idempotent, not an
    # overwrite of a mutable native home.
    assert materialize_native_projection(
        config,
        manifest,
        projection,
        actor="worker",
        entity_token="incarnation-1",
        projection_root=target,
        incumbent=receipt,
    ) == receipt

    with pytest.raises(AgentConfigError, match="incarnation-bound receipt"):
        materialize_native_projection(
            config,
            manifest,
            projection,
            actor="another-worker",
            entity_token="incarnation-2",
            projection_root=target,
        )


def test_t03_rejects_mapping_collisions_instead_of_inventing_precedence(
    tmp_path: Path,
) -> None:
    source = tmp_path / "C"
    _write(source, "shared/skills/review/SKILL.md", "shared\n")
    _write(source, "native/claude/skills/review/SKILL.md", "claude\n")

    with pytest.raises(AgentConfigError, match="no implicit precedence"):
        build_native_projection(_config(source).freeze_manifest(), "claude")


@pytest.mark.parametrize(
    ("relative", "message"),
    (
        ("unclassified.txt", "outside the frozen native/shared mapping"),
        ("native/claude/.credentials.json", "authentication must remain outside C"),
        ("native/codex/auth.json", "authentication must remain outside C"),
        ("native/pi/auth.json", "authentication must remain outside C"),
        ("native/pi/models.json", "authentication must remain outside C"),
    ),
)
def test_t03_rejects_unmapped_or_credential_bearing_c_items(
    tmp_path: Path, relative: str, message: str
) -> None:
    source = tmp_path / "C"
    _write(source, relative, "P20_SENTINEL_NOT_A_SECRET\n")
    with pytest.raises(AgentConfigError, match=message):
        manifest = _config(source).freeze_manifest()
        build_native_projection(manifest, "pi")


@pytest.mark.parametrize(
    ("harness", "relative", "target"),
    (
        ("pi", "AGENTS.md", "config/native/pi/AGENTS.md"),
        ("claude", "CLAUDE.md", "config/native/claude/CLAUDE.md"),
        ("pi", "settings.json", "config/native/pi/settings.json"),
        ("pi", "skills/review/SKILL.md", "config/shared/skills/review/SKILL.md"),
        ("codex", "notes/todo.md", "config/native/codex/notes/todo.md"),
    ),
)
def test_t03_unmapped_c_item_names_the_item_and_its_exact_move_target(
    tmp_path: Path, harness: str, relative: str, target: str
) -> None:
    """A legacy root-level item must say where it belongs, not only that it failed."""

    source = tmp_path / "config"
    _write(source, relative, "legacy-root-item\n")

    with pytest.raises(AgentConfigError) as caught:
        build_native_projection(_config(source).freeze_manifest(), harness)

    message = str(caught.value)
    assert caught.value.code == "INVALID_ARGUMENT"
    assert f"config item {relative}" in message
    assert "outside the frozen native/shared mapping" in message
    assert f"move it to {target}" in message


def test_t03_known_auth_path_is_rejected_before_any_content_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "C"
    _write(source, "native/codex/auth.json", "P20_SENTINEL_NOT_A_SECRET\n")
    hashed: list[bytes] = []
    original_sha256 = config_module._sha256

    def observed_sha256(body: bytes) -> str:
        hashed.append(body)
        return original_sha256(body)

    monkeypatch.setattr(config_module, "_sha256", observed_sha256)
    with pytest.raises(AgentConfigError, match="authentication must remain outside C"):
        _config(source).freeze_manifest()
    assert hashed == []


def test_t03_optional_missing_is_distinct_from_required_or_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "missing"
    optional = _config(missing, required=False).freeze_manifest()
    assert optional.missing is True
    assert optional.items == ()
    assert ConfigManifest.from_json(optional.to_json()) == optional

    with pytest.raises(AgentConfigError, match="does not exist"):
        _config(missing).freeze_manifest()

    source = tmp_path / "unreadable"
    source.mkdir()
    original_lstat = Path.lstat

    def denied(path: Path) -> os.stat_result:
        if path == source:
            raise PermissionError("fixture-denied")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", denied)
    with pytest.raises(AgentConfigError, match="cannot inspect.*fixture-denied"):
        _config(source, required=False).freeze_manifest()


def test_t03_walk_errors_links_and_fifo_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "C"
    source.mkdir()
    linked_target = tmp_path / "outside"
    linked_target.mkdir()
    (source / "linked").symlink_to(linked_target, target_is_directory=True)
    with pytest.raises(AgentConfigError, match="symbolic link"):
        _config(source).freeze_manifest()
    (source / "linked").unlink()

    fifo = source / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(AgentConfigError, match="regular file"):
        _config(source).freeze_manifest()
    fifo.unlink()

    real_scandir = os.scandir

    def failed_scandir(path: os.PathLike[str] | str | int):  # type: ignore[no-untyped-def]
        if not isinstance(path, int) and Path(path) == source:
            raise OSError("walk-failed")
        return real_scandir(path)

    monkeypatch.setattr(config_module.os, "scandir", failed_scandir)
    with pytest.raises(AgentConfigError, match="walk-failed"):
        _config(source).freeze_manifest()


def test_t03_source_drift_before_publish_and_native_drift_before_consumption(
    tmp_path: Path,
) -> None:
    source = tmp_path / "C"
    settings = _write(source, "native/codex/config.toml", 'model = "v1"\n')
    skill = _write(source, "shared/skills/review/SKILL.md", "review-v1\n")
    config = _config(source)
    manifest = config.freeze_manifest()
    projection = build_native_projection(manifest, "codex")
    parent = tmp_path / "projections"
    parent.mkdir()

    settings.write_text('model = "v2"\n')
    with pytest.raises(AgentConfigError, match="config changed"):
        materialize_native_projection(
            config,
            manifest,
            projection,
            actor="worker",
            entity_token="incarnation-1",
            projection_root=parent / "rejected",
        )

    settings.write_text('model = "v1"\n')
    manifest = config.freeze_manifest()
    projection = build_native_projection(manifest, "codex")
    receipt = materialize_native_projection(
        config,
        manifest,
        projection,
        actor="worker",
        entity_token="incarnation-1",
        projection_root=parent / "accepted",
    )
    assert receipt.source_digest == manifest.digest

    projected_skill = parent / "accepted" / "skills" / "review" / "SKILL.md"
    projected_skill.chmod(0o600)
    projected_skill.write_text("review-v2\n")
    with pytest.raises(AgentConfigError, match="native projection drifted"):
        verify_native_projection(projection, parent / "accepted")
    # Changing the authoritative C after publish is independently detected.
    skill.write_text("review-v3\n")
    with pytest.raises(AgentConfigError, match="config changed"):
        config.verify_manifest(manifest)


def test_t03_auth_refresh_outside_c_does_not_change_personality_revision(
    tmp_path: Path,
) -> None:
    source = tmp_path / "C"
    _write(source, "native/pi/settings.json", "{}\n")
    config = _config(source)
    before = config.freeze_manifest()
    auth = tmp_path / "agent-home" / "secrets" / "native" / "pi" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("P20_SENTINEL_NOT_A_SECRET\n")
    auth.write_text("P20_SENTINEL_REFRESHED_NOT_A_SECRET\n")

    assert config.freeze_manifest() == before


def test_t16_public_projection_exposes_c_but_not_internal_receipt_or_authority(
    tmp_path: Path,
) -> None:
    source = tmp_path / "C"
    _write(source, "native/claude/settings.json", "{}\n")
    config = _config(source)
    manifest = config.freeze_manifest()
    native = build_native_projection(manifest, "claude")
    parent = tmp_path / "projections"
    parent.mkdir()
    receipt = materialize_native_projection(
        config,
        manifest,
        native,
        actor="worker",
        entity_token="internal-incarnation",
        projection_root=parent / "claude",
    )
    public = AgentProjection(
        version=1,
        uri="agent:owner:node:worker",
        actor="worker",
        owner="owner",
        machine="node",
        entity_token="internal-incarnation",
        config=config.to_json(),
    ).to_payload()

    assert public["config"] == config.to_json()
    assert "entityToken" not in public
    assert "projectionReceipt" not in public
    assert "projectionRoot" not in json.dumps(public)
    assert "privateEnv" not in public
    assert set(config.to_json()) == {"sources", "discovery"}
    assert set(receipt.to_json()) == {
        "schemaVersion",
        "actor",
        "entityToken",
        "sourceDigest",
        "harness",
        "mappingVersion",
        "projectionDigest",
        "projectionRoot",
        "items",
    }
    assert {field.name for field in dataclasses.fields(ConfigProjectionReceipt)} == {
        "actor",
        "entity_token",
        "source_digest",
        "harness",
        "mapping_version",
        "projection_digest",
        "projection_root",
        "items",
    }
    assert "P20_SENTINEL" not in json.dumps(receipt.to_json())


def test_agent_projection_detaches_nested_json_from_source_and_readers() -> None:
    config = {"sources": [{"path": "/test/C", "required": True}], "discovery": "none"}
    capabilities = {"nested": {"items": ["one"]}}
    projection = AgentProjection(
        version=1,
        uri="agent:owner:node:worker",
        actor="worker",
        owner="owner",
        machine="node",
        entity_token="incarnation",
        config=config,
        capabilities=tuple(capabilities.items()),
    )
    config["sources"][0]["path"] = "/changed"  # type: ignore[index]
    capabilities["nested"]["items"].append("two")  # type: ignore[index]
    first = projection.to_payload()
    assert first["config"] == {
        "sources": [{"path": "/test/C", "required": True}], "discovery": "none",
    }
    assert first["capabilities"] == {"nested": {"items": ["one"]}}
    first["capabilities"]["nested"]["items"].append("three")  # type: ignore[index]
    assert projection.to_payload()["capabilities"] == {"nested": {"items": ["one"]}}


def test_t16_internal_round_trips_reject_extra_fields(tmp_path: Path) -> None:
    source = tmp_path / "C"
    _write(source, "native/pi/settings.json", "{}\n")
    projection = build_native_projection(_config(source).freeze_manifest(), "pi")
    payload = projection.to_json()
    payload["privateEnv"] = {"TOKEN": "not-allowed"}

    with pytest.raises(AgentConfigError, match="privateEnv is not allowed"):
        NativeConfigProjection.from_json(payload)


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_claude_may_rewrite_its_seeded_dot_claude_json_but_nothing_else(
    tmp_path: Path,
) -> None:
    # 2026-10-06: Claude Code rewrites .claude.json on every interactive
    # start, so the byte check made a seeded agent unstartable after its
    # first run. Only that one file is exempt from the byte check; its
    # presence and private mode, and every other projected file, still are not.
    source = tmp_path / "C"
    _write(source, "native/claude/.claude.json", '{"hasCompletedOnboarding":true}\n')
    _write(source, "native/claude/settings.json", '{"model":"claude"}\n')
    config = _config(source)
    manifest = config.freeze_manifest()
    projection = build_native_projection(manifest, "claude")
    target = tmp_path / "projections" / "claude"
    target.parent.mkdir()
    materialize_native_projection(
        config,
        manifest,
        projection,
        actor="worker",
        entity_token="incarnation-1",
        projection_root=target,
    )
    seeded = target / ".claude.json"
    assert seeded.is_file()

    seeded.write_text('{"hasCompletedOnboarding":true,"numStartups":7}\n')
    verify_native_projection(projection, target)  # the harness's own rewrite

    # Claude Code writes atomically through a sibling temp file and can leave
    # it behind; a stale one must not become permanent drift (e2e, 2026-10-06).
    leftover = target / ".claude.json.tmp.93630.c802c73fb73c"
    leftover.write_text("{}\n")
    leftover.chmod(0o600)
    verify_native_projection(projection, target)
    leftover.unlink()
    for name in ("settings.json.tmp.1.ab", ".claude.json.tmp.x.ab", ".claude.json.bak"):
        # Only the exact atomic-write pattern of the writable file is ignored.
        other = target / name
        other.write_text("{}\n")
        other.chmod(0o600)
        with pytest.raises(AgentConfigError, match=f"drifted after publication: {re.escape(name)}"):
            verify_native_projection(projection, target)
        other.unlink()

    seeded.chmod(0o644)
    with pytest.raises(AgentConfigError, match="mode must be 0600"):
        verify_native_projection(projection, target)
    seeded.chmod(0o600)

    settings = target / "settings.json"
    original = settings.read_text()
    settings.write_text('{"model":"other"}\n')
    with pytest.raises(AgentConfigError, match="drifted after publication: settings.json"):
        verify_native_projection(projection, target)
    settings.write_text(original)

    seeded.unlink()
    with pytest.raises(AgentConfigError, match="drifted after publication: .claude.json"):
        verify_native_projection(projection, target)
