"""agent_home_p2_migration: ownership scenarios."""

from __future__ import annotations
import json
from pathlib import Path
import pytest
from hyprial.identity.impl.agents import (
    AgentConfig,
)
from hyprial.identity.impl.agents.migration._base import (
    AgentHomeMigrationError,
    MigrationEntry,
    SupportKey,
    SupportMatrix,
    SupportRow,
    SupportStatus,
)

from .support import (
    _Bindings,
    _authorization,
    _coordinator,
    _isolated_environment as _isolated_environment,
    _plan,
    _registry,
    _source,
    _support,
    _support_key,
)


def test_t15_target_preoccupation_is_never_overwritten(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")
        target = Path(registry.home_receipt("worker").path) / "config"
        (target / "incumbent.txt").write_text("keep", encoding="utf-8")
        source = _source(tmp_path)
        coordinator = _coordinator(registry)
        with pytest.raises(AgentHomeMigrationError) as refused:
            coordinator.preflight(
                _authorization(registry, "worker"),
                (MigrationEntry(str(source), "config", False),),
                bindings=_Bindings(registry),
            )
        assert refused.value.category == "target-occupied"
        assert (target / "incumbent.txt").read_text() == "keep"
        assert source.exists()
    finally:
        registry.close()


def test_t15_native_target_parent_requires_private_owner_mode(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")
        root = Path(registry.home_receipt("worker").path)
        (root / "secrets").mkdir(mode=0o700)
        native_parent = root / "secrets" / "native"
        native_parent.mkdir(mode=0o700)
        native_parent.chmod(0o755)
        source = _source(tmp_path)
        key = _support_key("codex")
        matrix = _support(SupportRow(key, SupportStatus.PASS, "isolated test fixture"))
        with pytest.raises(AgentHomeMigrationError) as refused:
            _coordinator(registry, matrix).preflight(
                _authorization(registry, "worker"),
                (MigrationEntry(str(source), "secrets/native/codex", True),),
                required_support=(key,),
                bindings=_Bindings(registry),
            )
        assert refused.value.category == "unsafe-target"
        assert source.exists()
    finally:
        registry.close()


def test_t15_existing_identity_config_cannot_be_replaced_by_another_source(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    try:
        current = _source(tmp_path / "current")
        candidate = _source(tmp_path / "candidate")
        registry.create("worker", config=AgentConfig(str(current)))
        with pytest.raises(AgentHomeMigrationError) as refused:
            _coordinator(registry).preflight(
                _authorization(registry, "worker"),
                (MigrationEntry(str(candidate), "config", False),),
                bindings=_Bindings(registry),
            )
        assert refused.value.category == "config-source-mismatch"
        assert registry.require("worker").config == AgentConfig(str(current))
        assert candidate.exists()
    finally:
        registry.close()


def test_t16_config_candidate_rejects_native_auth_before_staging(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")
        source = _source(tmp_path)
        auth = source / "native" / "codex" / "auth.json"
        auth.write_text("NON_PRODUCTION_SENTINEL\n", encoding="utf-8")
        auth.chmod(0o600)
        with pytest.raises(AgentHomeMigrationError) as refused:
            _coordinator(registry).preflight(
                _authorization(registry, "worker"),
                (MigrationEntry(str(source), "config", False),),
                bindings=_Bindings(registry),
            )
        assert refused.value.category == "invalid-config-source"
        assert source.exists()
        assert not any(
            (Path(registry.home_receipt("worker").path) / "state").glob(
                "migrations/*"
            )
        )
    finally:
        registry.close()


def test_t15_overlapping_sources_are_refused_before_staging(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")
        source = _source(tmp_path)
        nested = source / "shared"
        key = _support_key("codex")
        matrix = _support(SupportRow(key, SupportStatus.PASS, "isolated test fixture"))
        with pytest.raises(AgentHomeMigrationError) as refused:
            _coordinator(registry, matrix).preflight(
                _authorization(registry, "worker"),
                (
                    MigrationEntry(str(source), "config", False),
                    MigrationEntry(str(nested), "secrets/native/codex", True),
                ),
                required_support=(key,),
                bindings=_Bindings(registry),
            )
        assert refused.value.category == "overlapping-source"
        assert source.exists()
    finally:
        registry.close()


def test_t15_identity_config_revision_is_rechecked_before_execute(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    try:
        current = _source(tmp_path / "current")
        replacement = _source(tmp_path / "replacement")
        registry.create("worker", config=AgentConfig(str(current)))
        bindings = _Bindings(registry)
        coordinator = _coordinator(registry)
        plan = coordinator.preflight(
            _authorization(registry, "worker"),
            (MigrationEntry(str(current), "config", False),),
            bindings=bindings,
        )
        registry.update("worker", config=AgentConfig(str(replacement)))

        with pytest.raises(AgentHomeMigrationError) as refused:
            coordinator.execute(plan, bindings=bindings)

        assert refused.value.category == "config-revision-drift"
        assert current.exists()
        assert replacement.exists()
        assert bindings.events == ["preflight"]
    finally:
        registry.close()


def test_t16_secret_preflight_serializes_metadata_not_value_or_value_digest(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")
        secret = "NON_PRODUCTION_SENTINEL_SECRET_VALUE"
        source = _source(tmp_path, secret=secret)
        key = _support_key("codex")
        coordinator = _coordinator(
            registry,
            _support(SupportRow(key, SupportStatus.PASS, "isolated test fixture")),
        )
        plan = coordinator.preflight(
            _authorization(registry, "worker"),
            (MigrationEntry(str(source), "secrets/native/codex", True),),
            required_support=(key,),
            bindings=_Bindings(registry),
        )
        encoded = json.dumps(plan.to_json(), sort_keys=True)
        assert secret not in encoded
        value_digest = __import__("hashlib").sha256(secret.encode()).hexdigest()
        file_digest = __import__("hashlib").sha256(
            (source / "native" / "codex" / "settings.json").read_bytes()
        ).hexdigest()
        assert value_digest not in encoded
        assert file_digest not in encoded
        assert str(int(file_digest, 16)) not in encoded
        assert "settings.json" in encoded
        assert "secrets/native/codex" in encoded
    finally:
        registry.close()


def test_t16_sensitive_source_permissions_fail_closed_before_inventory(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")
        source = _source(tmp_path)
        (source / "native" / "codex" / "settings.json").chmod(0o644)
        coordinator = _coordinator(registry)
        with pytest.raises(AgentHomeMigrationError) as refused:
            coordinator.preflight(
                _authorization(registry, "worker"),
                (MigrationEntry(str(source), "secrets/tools", True),),
                bindings=_Bindings(registry),
            )
        assert refused.value.category == "unsafe-source-mode"
        assert not (Path(registry.home_receipt("worker").path) / "secrets" / "tools").exists()
    finally:
        registry.close()


def test_t16_support_table_round_trip_preserves_exact_dimensions() -> None:
    key = _support_key("pi")
    matrix = _support(SupportRow(key, SupportStatus.PASS, "P20 isolated fixture"))
    assert SupportMatrix.from_json(matrix.to_json()) == matrix
    unsupported = SupportRow(key, SupportStatus.UNSUPPORTED, "measured limitation")
    assert unsupported.to_json()["status"] == "UNSUPPORTED"
    assert matrix.status_for(
        SupportKey("pi", "other-version", "darwin", "headless", "file")
    ) is SupportStatus.NOT_RUN


def test_t16_checked_in_support_table_matches_integrated_p23_rows() -> None:
    path = (
        Path(__file__).resolve().parents[6] / 'docs/notes/evidence/agent-home-p2-p24-20260923/support-matrix.json'
    )
    matrix = SupportMatrix.from_json(json.loads(path.read_text(encoding="utf-8")))
    assert len(matrix.rows) == 18
    passed = {row.key for row in matrix.rows if row.status is SupportStatus.PASS}
    assert passed == {
        SupportKey(
            "claude",
            "claude-code-2.1.280+sdk-0.2.125",
            "Darwin 25.5.0 arm64",
            "sdk",
            "api-key",
        ),
        SupportKey(
            "codex", "0.155.1", "Darwin 25.5.0 arm64", "app-server", "file"
        ),
        SupportKey(
            "codex",
            "0.155.1",
            "Darwin 25.5.0 arm64",
            "app-server",
            "ephemeral-custom",
        ),
        SupportKey(
            "codex", "0.155.1", "Darwin 25.5.0 arm64", "tui", "file"
        ),
        SupportKey(
            "pi",
            "0.85.1",
            "Darwin 25.5.0 arm64",
            "controlled-sdk-headless",
            "api-key",
        ),
        SupportKey(
            "pi",
            "0.85.1",
            "Darwin 25.5.0 arm64",
            "controlled-sdk-tui",
            "api-key",
        ),
    }
    assert matrix.status_for(
        SupportKey(
            "pi", "0.85.1", "Darwin 25.5.0 arm64", "bare-rpc", "api-key"
        )
    ) is SupportStatus.UNSUPPORTED
    assert matrix.status_for(
        SupportKey(
            "codex", "0.155.1", "Darwin 25.5.0 arm64", "app-server", "oauth"
        )
    ) is SupportStatus.NOT_RUN
    for harness, version in (
        ("claude", "claude-code-2.1.280+sdk-0.2.125"),
        ("codex", "0.155.1"),
        ("pi", "0.85.1"),
    ):
        assert matrix.status_for(
            SupportKey(
                harness,
                version,
                "Darwin 25.5.0 arm64",
                "cross-host-transfer",
                "all-p2",
            )
        ) is SupportStatus.UNSUPPORTED


def test_t16_packaged_support_table_pins_claude_2_1_283_observations() -> None:
    path = (
        Path(__file__).resolve().parents[6] / 'src/hyprial/identity/impl/agents/migration/migration-support-matrix.json'
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    keys = [
        (
            row["harness"], row["version"], row["operatingSystem"],
            row["entrypoint"], row["authMode"],
        )
        for row in raw["rows"]
    ]
    assert keys == sorted(keys)
    pi_130 = "1.3.0"
    assert matrix_pi_130_headless_pass(raw, pi_130, "Darwin 25.5.0 arm64")
    assert matrix_pi_130_transfer_not_run(raw, pi_130, "Darwin 25.5.0 arm64")
    matrix = SupportMatrix.from_json(raw)
    # No row-count pin (Allen 2026-10-18): adding a row must not require
    # editing this test.  What has to stay true is that every
    # (harness, version) group the table claims to cover is actually
    # accounted for -- at least one PASS or NOT_RUN row -- so a group cannot
    # silently vanish, nor arrive as UNSUPPORTED-only and still look covered.
    assert matrix.rows, "the packaged support table must not be empty"
    for harness, version in {(row["harness"], row["version"]) for row in raw["rows"]}:
        statuses = {
            matrix.status_for(
                SupportKey(
                    row["harness"],
                    row["version"],
                    row["operatingSystem"],
                    row["entrypoint"],
                    row["authMode"],
                )
            )
            for row in raw["rows"]
            if (row["harness"], row["version"]) == (harness, version)
        }
        assert statuses & {SupportStatus.PASS, SupportStatus.NOT_RUN}, (
            f"{harness} {version} is listed but neither proven nor pending"
        )
    version = "claude-code-2.1.283+sdk-0.2.125"
    os_name = "Darwin 25.5.0 arm64"
    for entrypoint, auth_mode, status in (
        ("sdk", "api-key", SupportStatus.PASS),
        ("sdk", "oauth-token", SupportStatus.PASS),
        ("tui", "oauth-token", SupportStatus.NOT_RUN),
        # C3-S2-C proved the chain on two real daemons with the repository's
        # claude protocol double; the row opens with that run's reading.
        ("cross-host-transfer", "all-p2", SupportStatus.PASS),
    ):
        assert matrix.status_for(
            SupportKey("claude", version, os_name, entrypoint, auth_mode)
        ) is status
    assert matrix.status_for(
        SupportKey(
            "claude", "claude-code-2.1.280+sdk-0.2.125",
            os_name, "sdk", "api-key",
        )
    ) is SupportStatus.PASS


def test_t16_support_table_never_promotes_missing_failed_or_unsupported_rows(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")
        key = _support_key()
        for status in (SupportStatus.FAILED, SupportStatus.UNSUPPORTED, SupportStatus.NOT_RUN):
            matrix = _support(SupportRow(key, status, "isolated non-secret observation"))
            source = _source(tmp_path / status.value)
            coordinator = _coordinator(registry, matrix)
            with pytest.raises(AgentHomeMigrationError) as refused:
                coordinator.preflight(
                    _authorization(registry, "worker"),
                    (MigrationEntry(str(source), "config", False),),
                    required_support=(key,),
                    bindings=_Bindings(registry),
                )
            assert refused.value.category == f"support-{status.value.lower()}"

        absent = SupportKey("claude", "unmeasured", "darwin", "tui", "oauth")
        assert _support().status_for(absent) is SupportStatus.NOT_RUN

        source = _source(tmp_path / "undeclared-native")
        with pytest.raises(AgentHomeMigrationError) as undeclared:
            _coordinator(registry).preflight(
                _authorization(registry, "worker"),
                (MigrationEntry(str(source), "secrets/native/codex", True),),
                bindings=_Bindings(registry),
            )
        assert undeclared.value.category == "support-not-declared"
    finally:
        registry.close()


def test_t16_unrecorded_codex_version_uses_recorded_structural_result(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")
        recorded = SupportKey("codex", "0.155.1", "darwin", "app-server", "file")
        unrecorded = SupportKey("codex", "9.9.9", "darwin", "app-server", "file")
        matrix = _support(
            SupportRow(recorded, SupportStatus.PASS, "isolated structural fixture")
        )
        source = _source(tmp_path / "unrecorded-codex")

        plan = _coordinator(registry, matrix).preflight(
            _authorization(registry, "worker"),
            (MigrationEntry(str(source), "config", False),),
            required_support=(unrecorded,),
            bindings=_Bindings(registry),
        )

        assert plan.support == (unrecorded,)
        assert "codex.version.unrecorded" in caplog.text
        assert "9.9.9" in caplog.text

        blocked_recorded = SupportKey(
            "codex", "0.155.1", "darwin", "cross-host-transfer", "all-p2"
        )
        blocked_unrecorded = SupportKey(
            "codex", "9.9.9", "darwin", "cross-host-transfer", "all-p2"
        )
        blocked = _support(
            SupportRow(
                blocked_recorded,
                SupportStatus.UNSUPPORTED,
                "isolated structural refusal",
            )
        )
        with pytest.raises(AgentHomeMigrationError) as refused:
            _coordinator(registry, blocked).preflight(
                _authorization(registry, "worker"),
                (MigrationEntry(str(source), "config", False),),
                required_support=(blocked_unrecorded,),
                bindings=_Bindings(registry),
            )
        assert refused.value.category == "support-unsupported"
    finally:
        registry.close()


def test_t17_missing_binding_facade_is_a_named_preflight_failure(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")
        source = _source(tmp_path)
        coordinator = _coordinator(registry)
        with pytest.raises(AgentHomeMigrationError) as refused:
            coordinator.preflight(
                _authorization(registry, "worker"),
                (MigrationEntry(str(source), "config", False),),
                bindings=object(),  # type: ignore[arg-type]
            )
        assert refused.value.category == "binding-facade-incomplete"
        assert refused.value.phase == "preflight-bindings"
    finally:
        registry.close()


def test_t18_incarnation_rotation_fences_plan_before_any_copy(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")
        bindings = _Bindings(registry)
        coordinator, plan, source = _plan(tmp_path, registry, bindings)
        rotated = registry.rotate_secret_authority("worker")

        with pytest.raises(AgentHomeMigrationError) as refused:
            coordinator.execute(plan, bindings=bindings)

        assert refused.value.category == "incarnation-mismatch"
        assert rotated.entity_token != plan.authorization.entity_token
        assert source.exists()
        assert bindings.events == ["preflight"]
    finally:
        registry.close()


def test_t18_destroy_cleans_native_and_tool_custody_before_recreate(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    try:
        original = registry.create("worker")
        receipt = registry.home_receipt("worker")
        root = Path(receipt.path)
        native = root / "secrets" / "native" / "codex"
        tools = root / "secrets" / "tools"
        (root / "secrets").mkdir(mode=0o700)
        native.mkdir(parents=True, mode=0o700)
        tools.mkdir(mode=0o700)
        (native / "auth.json").write_text("NON_PRODUCTION_SENTINEL\n", encoding="utf-8")
        (tools / "login.json").write_text("NON_PRODUCTION_SENTINEL\n", encoding="utf-8")

        assert registry.destroy("worker")
        assert not root.exists()
        with pytest.raises(Exception, match="cleanup-fenced"):
            registry.cleanup_home("worker", expected_token="wrong-token")

        rebuilt = registry.create("worker")
        assert rebuilt.entity_token != original.entity_token
        rebuilt_root = Path(registry.home_receipt("worker").path)
        assert not (rebuilt_root / "secrets" / "native").exists()
        assert not (rebuilt_root / "secrets" / "tools").exists()
    finally:
        registry.close()


def test_t17_rollback_drift_is_checked_before_binding_is_changed(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")
        bindings = _Bindings(registry)
        coordinator, plan, _source_path = _plan(tmp_path, registry, bindings)
        coordinator.execute(plan, bindings=bindings)
        target = (
            Path(registry.home_receipt("worker").path)
            / "config"
            / "native"
            / "codex"
            / "settings.json"
        )
        target.write_text('{"changed":"after-migration"}\n', encoding="utf-8")

        with pytest.raises(AgentHomeMigrationError) as refused:
            coordinator.rollback(
                plan,
                authorization=_authorization(registry, "worker"),
                bindings=bindings,
            )

        assert refused.value.category == "rollback-drift"
        assert bindings.events == ["preflight", "activate", "source-check"]
        assert registry.require("worker").config is not None
        record = json.loads(
            (
                Path(registry.home_receipt("worker").path)
                / "state"
                / "migrations"
                / plan.migration_id
                / "record.json"
            ).read_text(encoding="utf-8")
        )
        assert record["phase"] == "rollback-failed"
        assert record["failurePhase"] == "rollback"
        assert record["failureCategory"] == "rollback-drift"
    finally:
        registry.close()


def test_t17_post_filesystem_config_mismatch_is_durable_rollback_failure(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    try:
        registry.create("worker")

        class WrongConfigRollback(_Bindings):
            def rollback(self, plan) -> None:  # noqa: ANN001
                self.events.append("rollback")
                wrong = tmp_path / "wrong-config"
                wrong.mkdir()
                self.registry.update(
                    plan.authorization.actor,
                    config=AgentConfig(str(wrong)),
                )

        bindings = WrongConfigRollback(registry)
        coordinator, plan, source = _plan(tmp_path, registry, bindings)
        coordinator.execute(plan, bindings=bindings)

        with pytest.raises(AgentHomeMigrationError) as failed:
            coordinator.rollback(
                plan,
                authorization=_authorization(registry, "worker"),
                bindings=bindings,
            )

        root = Path(registry.home_receipt("worker").path)
        record = json.loads(
            (
                root
                / "state"
                / "migrations"
                / plan.migration_id
                / "record.json"
            ).read_text(encoding="utf-8")
        )
        assert failed.value.category == "rollback-config-mismatch"
        assert source.exists()
        assert record["phase"] == "rollback-failed"
        assert record["failurePhase"] == "rollback"
        assert record["failureCategory"] == "rollback-config-mismatch"
    finally:
        registry.close()


def matrix_pi_130_headless_pass(
    raw: dict, version: str, os_name: str
) -> bool:
    """The 1.3.0 headless row carries the P23 re-run's PASS, nothing looser."""

    matrix = SupportMatrix.from_json(raw)
    return matrix.status_for(
        SupportKey("pi", version, os_name, "controlled-sdk-headless", "api-key")
    ) is SupportStatus.PASS


def matrix_pi_130_transfer_not_run(raw: dict, version: str, os_name: str) -> bool:
    """The 1.3.0 transfer row stays NOT_RUN: only the protocol double ran."""

    matrix = SupportMatrix.from_json(raw)
    return matrix.status_for(
        SupportKey("pi", version, os_name, "cross-host-transfer", "all-p2")
    ) is SupportStatus.NOT_RUN
