"""P0 preserves public bindings and legacy launch/admission authority.

The structural assertions are intentionally RED until extraction. Behavioral
regressions must pass both before and after this no-policy-change slice.

Decision (Z-7, 2026-11-19): the pinned projection of the daemon facade no
longer contains four retired names, removed with their modules by owner
decision: BootstrapHttpHost (ownerless HTTP login host) and CustodyOutcome,
LogoutCustody, take_logout_custody (launcher logout custody). The digest was
re-pinned for exactly that removal; every other binding and the order are
unchanged.

Decision (S2b, 2026-11-21, owner-approved): the pinned projection drops
EnsureSquireRegistryCommand, SquireRegistryResult, SquireSetup and
derive_setup_identity, removed with the dedicated Squire setup surface. The
digest was re-pinned for exactly that removal; every other binding and the
order are unchanged.

Decision (W1D-1, 2026-11-21, owner-approved): the pinned projection drops the
14 retired smolvm runtime/rollback and unreferenced transfer signature/trust
bindings (SignatureError, SignatureFormatError, TrustStore, TrustStoreError,
add_trusted_source_key, downgrade_state, load_trust_store, probe_smolvm,
public_key_id, retire_trusted_source_key, sign_exported_bundle,
smolvm_regular_read, trust_policy_for, trust_store_path). The digest was
re-pinned for exactly that removal; every other binding and the order are
unchanged.

Decision (OC1a, 2026-11-21, owner-approved): the pinned projection adds one
read-only cell cache binding after launch_daemon_lifecycle,
cell_cache_identity_conflict (cells.runtime, for L3). The digest was re-pinned
for exactly that addition on top of W1D-1; every other binding and the order
are unchanged.

Decision (W1D-2, 2026-11-21, owner-approved): the pinned projection drops the
thirteen names retired with the dsh, dsh_tui and tmux harness lines: DshHttpApi,
DSH_TUI_EXIT_CODE_FILENAME, DSH_TUI_IDENTITY_FILENAME, DSH_TUI_PID_FILENAME,
DSH_TUI_SPAWN_ERROR_FILENAME, dsh_mcp_client_row, dsh_tui_process_gone,
harnesses_tmux, process_identity_for_tui, run_dsh_carrier,
session_name_for_actor, write_dsh_tui_plugin and write_launch_state. The digest
was re-pinned for exactly that removal; every other binding and the order are
unchanged.

Decision (T2b, 2026-11-22, owner-approved): the pinned projection drops the
link request and hold/top bindings retired with the link and thread request
catalog. The digest was re-pinned for exactly that removal; every other
binding and the order are unchanged.

Decision (W1B, 2026-11-21, owner-approved): the pinned projection drops the
nine unreferenced context-snapshot bindings removed with their package:
ContextSnapshotBody, ContextSnapshotError, ContextSnapshotExporter,
ContextSnapshotRequest, ContextSnapshotResult, ContextSnapshotSource,
ContextSnapshotTarget, ContextSnapshotWorker, and RecapCaptureState. The
digest was re-pinned for exactly that removal; every other binding and the
order are unchanged.

Decision (OC4, 2026-11-23, owner-approved): the pinned projection drops the
four retired personal-org facade bindings (PersonalOrgOwnerStore,
PersonalOrgSpace, personal_org_for, resolve_personal_org) and adds the
two local owner-store bindings (LocalOwnerStore, OWNER_SCOPE_LOCAL) for
non-OrgFS credential custody. The digest was re-pinned for that exact
replacement on top of T2b/W1B; every other binding and the order are unchanged.

Decision (L3, 2026-11-23, owner-approved): the pinned projection drops the
owner_migration bindings retired with their package (ARCHIVAL_COLUMNS,
MIGRATION_DATABASES, MIGRATION_TEXT_FILES, MigrationPlan,
OwnerMigrationAborted, OwnerMigrationHostedConflict, build_plan,
detect_previous_owner, migrate_owner, migrate_owner_if_needed,
plan_owner_migration). The digest was re-pinned for exactly that removal; every
other binding and the order are unchanged.
"""

import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from hyprial.identity import (
    AgentRuntimeError,
    SharedCredentialBinding,
    preflight_codex_credential,
    resolve_agent_runtime_context,
    validate_shared_credential_binding,
    validate_shared_credential_environment,
)


ROOT = Path(__file__).resolve().parents[4]
SRC = ROOT / "src" / "hyprial"


def test_daemon_exports_keep_names_order_targets_and_lazy_loading():
    source = (SRC / "daemon/interface.py").read_text()
    values = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }
    assert values["_FACADE_EXPORTS"].pop("UserKey") == (
        "hyprial.daemon.impl.user_key", "UserKey"
    )
    values["__all__"].remove("UserKey")
    assert values["_FACADE_EXPORTS"].pop("BootstrapWireHost") == (
        "hyprial.daemon.impl.application.onboarding.bootstrap.wire",
        "BootstrapWireHost",
    )
    values["__all__"].remove("BootstrapWireHost")
    ownerless = "hyprial.daemon.impl.application.onboarding.lifecycle.ownerless"
    for name, module in (
        ("BootstrapLaunchError", ownerless),
        ("STOP_LOGOUT", "hyprial.daemon.impl.application.onboarding.lifecycle.stop_reason"),
        ("check_native_engine", ownerless),
        ("launch_daemon_lifecycle", ownerless),
        ("leave_interpreter_after_loop", "hyprial.daemon.impl.bootstrap.exit_independence"),
    ):
        assert values["_FACADE_EXPORTS"].pop(name) == (module, name)
        values["__all__"].remove(name)
    encoded = json.dumps(
        [values["__all__"], values["_FACADE_EXPORTS"]], sort_keys=True
    ).encode()
    assert hashlib.sha256(encoded).hexdigest() == (
        "b262c657cd5d3e8f71c2a9d7d9f53b1de1db19952be82e2c26fadcc743765c0d"
    )
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
import hyprial.daemon as daemon
from hyprial.daemon import interface
assert 'hyprial.daemon.impl.application' not in sys.modules
assert set(dir(interface)) == set(interface.__all__)
assert daemon.ServiceConnectError is interface.ServiceConnectError
assert interface.ServiceConnectError is interface.ServiceConnectError
assert 'hyprial.daemon.impl.application' not in sys.modules
try:
    interface.not_a_public_name
except AttributeError:
    pass
else:
    raise AssertionError('unknown export accepted')
"""], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_interface_and_context_have_room_for_following_slices():
    for relative, budget in (
        ("daemon/interface.py", 950),
        ("identity/impl/agents/runtime/context.py", 850),
    ):
        assert len((SRC / relative).read_text().splitlines()) <= budget, relative


def test_shared_helpers_are_delegated_without_replacing_public_functions():
    source = ast.parse((SRC / "identity/impl/agents/runtime/context.py").read_text())
    functions = {node.name: node for node in source.body if isinstance(node, ast.FunctionDef)}
    for name in ("validate_shared_credential_binding", "shared_credential_status"):
        assert functions[name].end_lineno - functions[name].lineno < 12
    helper = SRC / "identity/impl/agents/runtime/context_parts/shared.py"
    assert helper.is_file()


def test_no_new_provider_port_keeps_existing_native_root(provider_registry, tmp_path):
    context = resolve_agent_runtime_context(
        registry=provider_registry, agent_name="test-agent", harness="codex",
        cwd=str(tmp_path / "workspace"),
    )
    assert context is not None
    assert context.shared_credential is None
    assert context.roots.native_root == (
        Path(provider_registry.home_receipt("test-agent").path) / "secrets/native/codex"
    )
    assert context.environment()["CODEX_HOME"] == str(context.roots.native_root)
    assert context.public_projection()["auth"] is None
    # No grant/provider port: there is still no fabricated native credential.
    with pytest.raises(AgentRuntimeError) as raised:
        preflight_codex_credential(
            native_root=context.roots.native_root, actor=context.actor, model_provider=None,
        )
    assert raised.value.code == "CODEX_CREDENTIAL_MISSING"
    assert raised.value.data["reason"] == "missing"
    validate_shared_credential_environment(context, ["TEST_API_KEY"])


def test_shared_binding_keeps_exact_target_and_environment_exclusivity(tmp_path):
    target = tmp_path / "synthetic-auth"
    target.write_text("SYNTHETIC-NOT-A-PROVIDER-CREDENTIAL")
    target.chmod(0o600)
    native = tmp_path / "auth.json"
    native.symlink_to(target)
    binding = SharedCredentialBinding("test-agent", "codex", native, target, None)
    validate_shared_credential_binding(binding)
    validate_shared_credential_environment(binding, [])
    with pytest.raises(AgentRuntimeError) as raised:
        validate_shared_credential_environment(binding, ["TEST_API_KEY"])
    assert raised.value.code == "SHARED_CREDENTIAL_CONFLICT"
    native.unlink()
    native.write_text("SYNTHETIC-DIVERGED-COPY")
    with pytest.raises(AgentRuntimeError) as raised:
        validate_shared_credential_binding(binding)
    assert raised.value.code == "SHARED_CREDENTIAL_DIVERGED"
