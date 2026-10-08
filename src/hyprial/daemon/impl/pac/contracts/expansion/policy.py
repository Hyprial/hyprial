"""Trusted, read-only policy for PAC expansion."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hyprial.identity import PAC_EXPANSION_INVALID, PAC_EXPANSION_POLICY_UNAVAILABLE, PacError

_POLICY_FILE = "pac-expansion.json"
_LIMIT_KEYS = {"maxNodes", "tiers", "owners", "cwdUnder"}
_DEFAULTS = {
    "maxNodes": 12,
    "tiers": ["fast", "strong"],
    "owners": [],
}


def _invalid(field: str, reason: str, message: str | None = None) -> None:
    raise PacError(PAC_EXPANSION_INVALID, message or f"invalid expansion policy field {field}", {"field": field, "reason": reason})


def _unavailable(path: Path, message: str) -> None:
    raise PacError(PAC_EXPANSION_POLICY_UNAVAILABLE, message, {"field": str(path), "reason": "unavailable"})


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        _invalid(field, "unexpected")
    if len(set(value)) != len(value):
        _invalid(field, "unexpected", f"{field} must contain unique strings")
    return list(value)


def _canonical_path(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        _invalid(field, "cwd")
    return Path(value).resolve(strict=False)


def _contains(root: Path, child: Path) -> bool:
    try:
        child.relative_to(root)
    except ValueError:
        return False
    return True


def _normalise_policy(raw: Any, path: Path) -> dict[str, Any]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        _unavailable(path, "policy must be a string-keyed JSON object")
    unknown = set(raw) - {"version", "workRoot", "limits"}
    if unknown:
        _unavailable(path, f"policy has unknown keys: {', '.join(sorted(unknown))}")
    if type(raw.get("version")) is not int or raw["version"] != 1:
        _unavailable(path, "policy.version must be 1")
    work_root = _canonical_path(raw.get("workRoot"), "workRoot")
    supplied = raw.get("limits", {})
    if not isinstance(supplied, dict) or any(not isinstance(key, str) for key in supplied):
        _unavailable(path, "policy.limits must be a string-keyed object")
    unknown = set(supplied) - _LIMIT_KEYS
    if unknown:
        _unavailable(path, f"policy.limits has unknown keys: {', '.join(sorted(unknown))}")
    max_nodes = supplied.get("maxNodes", _DEFAULTS["maxNodes"])
    if type(max_nodes) is not int or not 1 <= max_nodes <= 100:
        _unavailable(path, "policy.limits.maxNodes must be an integer from 1 through 100")
    tiers = _string_list(supplied.get("tiers", _DEFAULTS["tiers"]), "limits.tiers")
    if any(tier not in {"fast", "strong", "super"} for tier in tiers):
        _unavailable(path, "policy.limits.tiers contains an unknown tier")
    owners = _string_list(supplied.get("owners", _DEFAULTS["owners"]), "limits.owners")
    cwd_under_raw = supplied.get("cwdUnder", [str(work_root)])
    cwd_under = [_canonical_path(item, "limits.cwdUnder") for item in _string_list(cwd_under_raw, "limits.cwdUnder")]
    if any(not _contains(work_root, item) for item in cwd_under):
        _unavailable(path, "every limits.cwdUnder path must be contained by workRoot")
    return {
        "version": 1,
        "workRoot": str(work_root),
        "limits": {
            "maxNodes": max_nodes,
            "tiers": tiers,
            "owners": owners,
            "cwdUnder": [str(item) for item in cwd_under],
        },
    }


def load_expansion_policy(home: Path) -> dict[str, Any]:
    """Load and normalize the daemon-trusted policy without writing anything."""

    path = Path(home) / _POLICY_FILE

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate policy key {key!r}")
            result[key] = value
        return result

    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        _unavailable(path, f"cannot read policy: {error}")
    try:
        return _normalise_policy(raw, path)
    except PacError as error:
        if error.code == PAC_EXPANSION_INVALID:
            raise PacError(
                PAC_EXPANSION_POLICY_UNAVAILABLE,
                str(error),
                error.data,
            ) from error
        raise
    except Exception as error:  # noqa: BLE001 - policy input must fail closed
        _unavailable(path, f"invalid policy: {error}")


def effective_limits(overrides: dict[str, Any] | None, *, policy: dict[str, Any]) -> dict[str, Any]:
    """Return parent limits that are no weaker than the trusted policy."""

    try:
        root = Path(policy["workRoot"]).resolve(strict=False)
        base = policy["limits"]
        if not isinstance(base, dict):
            raise KeyError("limits")
    except (KeyError, TypeError, ValueError) as error:
        raise PacError(PAC_EXPANSION_POLICY_UNAVAILABLE, "trusted expansion policy is unavailable", {"field": "policy", "reason": "unavailable"}) from error
    overrides = {} if overrides is None else overrides
    if not isinstance(overrides, dict) or any(not isinstance(key, str) for key in overrides):
        _invalid("limits", "unexpected")
    unknown = set(overrides) - (_LIMIT_KEYS | {"worktree"})
    if unknown:
        _invalid("limits", "unknown-field")
    result: dict[str, Any] = {
        "workRoot": str(root),
        "maxNodes": base["maxNodes"],
        "tiers": list(base["tiers"]),
        "owners": list(base["owners"]),
        "cwdUnder": list(base["cwdUnder"]),
    }
    if "maxNodes" in overrides:
        value = overrides["maxNodes"]
        if type(value) is not int or not 1 <= value <= result["maxNodes"]:
            _invalid("limits.maxNodes", "count")
        result["maxNodes"] = value
    for key in ("tiers", "owners"):
        if key not in overrides:
            continue
        values = _string_list(overrides[key], f"limits.{key}")
        if not set(values).issubset(set(result[key])):
            _invalid(f"limits.{key}", "tier" if key == "tiers" else "owner")
        result[key] = values
    if "cwdUnder" in overrides:
        values = [_canonical_path(item, "limits.cwdUnder") for item in _string_list(overrides["cwdUnder"], "limits.cwdUnder")]
        allowed = [Path(item).resolve(strict=False) for item in base["cwdUnder"]]
        if any(not any(_contains(parent, item) for parent in allowed) for item in values):
            _invalid("limits.cwdUnder", "cwd")
        result["cwdUnder"] = [str(item) for item in values]
    if "worktree" in overrides:
        worktree = overrides["worktree"]
        if not isinstance(worktree, dict) or set(worktree) != {"repo", "base"}:
            _invalid("limits.worktree", "unexpected")
        repo = _canonical_path(worktree.get("repo"), "limits.worktree.repo")
        base_ref = worktree.get("base")
        if not isinstance(base_ref, str) or not base_ref.strip() or "\0" in base_ref:
            _invalid("limits.worktree.base", "required")
        result["worktree"] = {"repo": str(repo), "base": base_ref}
    return result


__all__ = ["effective_limits", "load_expansion_policy"]
