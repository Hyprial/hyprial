import json
from pathlib import Path

import pytest

from hyprial.daemon.impl.pac.contracts.expansion.policy import (
    effective_limits,
    load_expansion_policy,
)
from hyprial.identity import PAC_EXPANSION_INVALID, PAC_EXPANSION_POLICY_UNAVAILABLE, PacError


def write_policy(home: Path, raw: str) -> None:
    (home / "pac-expansion.json").write_text(raw, encoding="utf-8")


def test_policy_rejects_duplicate_json_keys_and_noncanonical_roots(tmp_path):
    write_policy(tmp_path, '{"version":1,"version":1,"workRoot":"/tmp"}')
    with pytest.raises(PacError) as duplicate:
        load_expansion_policy(tmp_path)
    assert duplicate.value.code == PAC_EXPANSION_POLICY_UNAVAILABLE

    write_policy(tmp_path, json.dumps({"version": 1, "workRoot": "relative"}))
    with pytest.raises(PacError) as relative:
        load_expansion_policy(tmp_path)
    assert relative.value.code == PAC_EXPANSION_POLICY_UNAVAILABLE


def test_policy_paths_are_symlink_safe_and_worktree_is_absolute(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "escape"
    link.symlink_to(outside, target_is_directory=True)
    write_policy(
        root,
        json.dumps(
            {
                "version": 1,
                "workRoot": str(root),
                "limits": {"cwdUnder": [str(link)]},
            }
        ),
    )
    with pytest.raises(PacError) as escaped:
        load_expansion_policy(root)
    assert escaped.value.code == PAC_EXPANSION_POLICY_UNAVAILABLE

    safe = load_expansion_policy(
        _write_valid(root, {"version": 1, "workRoot": str(root)})
    )
    with pytest.raises(PacError) as bad_repo:
        effective_limits(
            {"worktree": {"repo": "relative", "base": "main"}},
            policy=safe,
        )
    assert bad_repo.value.code == PAC_EXPANSION_INVALID
    assert effective_limits(
        {"worktree": {"repo": str(outside), "base": "refs/heads/main"}},
        policy=safe,
    )["worktree"]["repo"] == str(outside)


def _write_valid(home: Path, payload: dict) -> Path:
    write_policy(home, json.dumps(payload))
    return home


def test_explicit_empty_trusted_lists_remain_empty(tmp_path):
    write_policy(
        tmp_path,
        json.dumps(
            {
                "version": 1,
                "workRoot": str(tmp_path),
                "limits": {"tiers": [], "owners": [], "cwdUnder": []},
            }
        ),
    )
    loaded = load_expansion_policy(tmp_path)
    assert loaded["limits"]["tiers"] == []
    assert effective_limits(None, policy=loaded)["cwdUnder"] == []
