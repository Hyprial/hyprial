"""Guard the operator guide and runnable agent-secret acceptance checklist."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
GUIDE = ROOT / "docs" / "guides" / "agent-secrets.md"
OPS_SKILL = ROOT / "src" / "hyprial" / "skills" / "hyprial-ops" / "SKILL.md"

ACCEPTANCE_NODES = {
    "1": (
        "tests/identity/agents/secret_storage/test_secret_store.py::"
        "test_acceptance_1_set_stores_value_but_never_puts_it_in_events",
    ),
    "2": (
        "tests/identity/agents/secret_storage/test_secret_store.py::"
        "test_acceptance_2_update_event_is_aborted_by_append_only_trigger",
        "tests/identity/agents/secret_storage/test_secret_store.py::"
        "test_acceptance_2_delete_event_is_aborted_by_append_only_trigger",
    ),
    "3": (
        "tests/identity/agents/secret_storage/test_secret_store.py::"
        "test_acceptance_3_rotate_changes_current_fingerprint_and_keeps_history",
    ),
    "4": (
        "tests/identity/agents/home/migration/test_sqlite_secret_launch.py::"
        "test_acceptance_4_remove_stops_injection_and_keeps_events",
    ),
    "5": (
        "tests/identity/agents/home/migration/test_sqlite_secret_launch.py::"
        "test_acceptance_5_sql_row_without_write_event_is_rejected",
    ),
    "6": (
        "tests/identity/agents/configuration/test_agent_secret_identity_policy.py::"
        "test_wp1_reserved_name_rejection_carries_the_required_event",
        "tests/identity/agents/home/migration/test_sqlite_secret_launch.py::"
        "test_acceptance_6_reserved_sql_row_is_never_injected",
    ),
    "7": (
        "tests/shell/cli/agents/secret/test_commands.py::"
        "test_acceptance_7_unknown_service_refuses_without_override_and_counts_override",
        "tests/shell/cli/agents/secret/test_commands.py::"
        "test_acceptance_7_validator_is_declarative_shape_only",
    ),
    "8": (
        "tests/shell/cli/agents/secret/test_commands.py::"
        "test_revoke_flow_records_assertion_and_idempotently_resumes",
        "tests/identity/agents/home/migration/test_sqlite_secret_launch.py::"
        "test_acceptance_8_remote_revoke_remains_materializable_until_remove",
    ),
    "8b": (
        "tests/shell/cli/agents/secret/test_commands.py::"
        "test_list_and_history_expose_incomplete_remote_revoke",
        "tests/identity/agents/home/migration/test_sqlite_secret_launch.py::"
        "test_acceptance_8b_event_and_table_reads_agree_remote_only_is_present",
    ),
    "9": (
        "tests/identity/agents/secret_storage/test_secret_store.py::"
        "test_acceptance_9_rotation_and_completed_revoke_leave_no_value_copy",
    ),
}


def _guide() -> str:
    return GUIDE.read_text(encoding="utf-8")


def test_owner_import_is_explicit_one_by_one_and_records_operator_actor() -> None:
    text = _guide()

    assert "不自动扫描" in text
    assert "不自动迁移" in text
    assert "逐条导入" in text
    assert "actor=user:<owner>" in text
    assert "action=set" in text
    assert "导入期间保留旧文件" in text
    assert "确认后自行删除旧文件" in text
    assert "hyprial agent secret set" in text
    assert "hyprial agent secret history" in text
    assert "printf '%s\\n' \"$VALUE\" |" in text


def test_threat_model_literal_is_present_verbatim() -> None:
    text = _guide()
    literal = """防：跨 uid / daemon 代读代写与路径交付 / 命令面留痕
不防：同 uid 直接读、读取不产生事件
不含：独立 uid、keychain ACL、值加密、异地审计"""

    assert literal in text
    assert "同 uid 是已知边界" in text
    assert "防篡改审计" in text


def test_revoke_runbook_keeps_remote_invalidation_and_local_delete_distinct() -> None:
    text = _guide()

    assert "action=revoke_remote" in text
    assert "action=remove" in text
    assert "只有两条事件都存在，撤销才完成" in text
    assert "只出现 `revoke_remote`" in text
    assert "仍可物化" in text
    assert "值不在盘上" in text
    assert "--remote-revoked" in text
    assert "--remote-revoked-at" in text


def test_capability_probe_classes_and_scope_are_documented() -> None:
    text = _guide()

    assert "credential-invalid" in text
    assert "target-missing" in text
    assert "operation-not-permitted" in text
    assert "列表 ≠ 存在" in text
    assert "创建 ≠ 可用" in text
    assert "可解析 ≠ 合法" in text
    assert "凭据可用 ≠ 目标存在" in text
    assert "v1 不实现 `capability_probe`" in text


def test_packaged_ops_skill_points_to_the_migration_and_revoke_runbook() -> None:
    text = OPS_SKILL.read_text(encoding="utf-8")

    assert "docs/guides/agent-secrets.md" in text
    assert "不自动扫描或迁移" in text
    assert "逐条导入" in text
    assert "set/rotate/list/history/remove" in text
    assert "revoke_remote + remove" in text
    assert "同 uid" in text


def test_acceptance_1_through_9_are_mapped_in_order_to_runnable_nodes() -> None:
    text = _guide()
    mapped = re.findall(r"^\| (1|2|3|4|5|6|7|8|8b|9) \|", text, re.MULTILINE)
    assert mapped == ["1", "2", "3", "4", "5", "6", "7", "8", "8b", "9"]

    cursor = -1
    for acceptance in mapped:
        for node in ACCEPTANCE_NODES[acceptance]:
            assert node in text
            assert (ROOT / node.split("::", 1)[0]).is_file()
            position = text.find(node, cursor + 1)
            assert position > cursor
            cursor = position

    assert "uv run pytest -q \\" in text
