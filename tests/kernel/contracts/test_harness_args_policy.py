"""Adapter-owned persistent harness argument policy contracts."""

from __future__ import annotations

import hashlib
import json

import pytest


def _policy():
    from hyprial.kernel import (
        DangerousArg,
        HarnessArgIssue,
        HarnessArgValidationError,
        RedactedHarnessArgs,
        classify_dangerous_launch_args,
        normalize_harness_args,
        redact_harness_args,
        validate_harness_launch_args,
    )

    return {
        "DangerousArg": DangerousArg,
        "HarnessArgIssue": HarnessArgIssue,
        "HarnessArgValidationError": HarnessArgValidationError,
        "RedactedHarnessArgs": RedactedHarnessArgs,
        "classify": classify_dangerous_launch_args,
        "normalize": normalize_harness_args,
        "redact": redact_harness_args,
        "validate": validate_harness_launch_args,
    }


def _issues(error: pytest.ExceptionInfo[ValueError]) -> tuple[object, ...]:
    return error.value.issues


def test_policy_types_and_functions_are_public_kernel_contracts():
    policy = _policy()

    assert issubclass(policy["HarnessArgValidationError"], ValueError)
    assert policy["HarnessArgIssue"]("code", "message").code == "code"
    assert policy["DangerousArg"]("--flag", "category", "reason").category == "category"
    assert policy["RedactedHarnessArgs"]


def test_normalize_drops_empty_groups_sorts_keys_and_preserves_argv_order():
    normalize = _policy()["normalize"]
    normalized = normalize(
        {
            "pi": ["--offline", "--approve"],
            "codex": [],
            "claude": ("--model", "sonnet", "--verbose"),
        }
    )

    assert list(normalized) == ["claude", "pi"]
    assert normalized == {
        "claude": ("--model", "sonnet", "--verbose"),
        "pi": ("--offline", "--approve"),
    }


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ([], "invalid-map"),
        ({"unknown": []}, "unsupported-harness"),
        ({"claude": "--verbose"}, "invalid-args"),
        ({"claude": ["--model", 7]}, "invalid-arg"),
        ({"claude": ["bad\x00arg"]}, "nul-byte"),
        ({"claude": ["--verbose"] * 257}, "too-many-args"),
        ({"claude": ["x" * 65_537]}, "args-too-large"),
    ],
)
def test_normalize_rejects_invalid_structure_and_group_limits(raw, code):
    with pytest.raises(_policy()["HarnessArgValidationError"]) as error:
        _policy()["normalize"](raw)

    assert {issue.code for issue in _issues(error)} == {code}


def test_normalize_accepts_exact_group_limits():
    normalized = _policy()["normalize"]({"claude": ["x" * 256] * 256})

    assert len(normalized["claude"]) == 256


@pytest.mark.parametrize(
    "args",
    [
        ("--model", "sonnet"),
        ("--permission-mode=plan",),
        ("--allowedTools", "Bash(git *)"),
        ("--mcp-config", "{}"),
        ("--settings={}",),
        ("--dangerously-skip-permissions",),
        ("--no-chrome",),
    ],
)
def test_claude_managed_allowlist_accepts_declared_options(args):
    assert _policy()["validate"]("claude", args) == args


@pytest.mark.parametrize(
    ("args", "code", "token"),
    [
        (("--json",), "unsupported-option", "--json"),
        (("--permission-mode", "manual"), "invalid-option-value", "--permission-mode"),
        (("--unknown",), "unsupported-option", "--unknown"),
        (("--model",), "missing-option-value", "--model"),
        (("prompt",), "positional-not-supported", "<positional>"),
    ],
)
def test_claude_policy_rejects_unsafe_or_unmanaged_arguments(args, code, token):
    with pytest.raises(_policy()["HarnessArgValidationError"]) as error:
        _policy()["validate"]("claude", args)

    assert [(issue.code, issue.token) for issue in _issues(error)] == [(code, token)]


@pytest.mark.parametrize(
    ("args", "token"),
    [
        (("--resume", "session"), "--resume"),
        (("--session-id=abc",), "--session-id"),
        (("--continue",), "--continue"),
        (("--fork-session",), "--fork-session"),
    ],
)
def test_claude_policy_rejects_connector_owned_lifecycle_arguments(args, token):
    with pytest.raises(_policy()["HarnessArgValidationError"]) as error:
        _policy()["validate"]("claude", args)

    assert [(issue.code, issue.token) for issue in _issues(error)] == [
        ("managed-lifecycle-conflict", token)
    ]


@pytest.mark.parametrize(
    "args",
    [
        ("--model", "gpt-5"),
        ("-a=never",),
        ("--sandbox", "workspace-write"),
        ("--approve-for-me",),
        ("--dangerously-bypass-approvals-and-sandbox",),
        ("-c", 'model_reasoning_effort="high"'),
    ],
)
def test_codex_policy_matches_app_server_accepted_execution_arguments(args):
    assert _policy()["validate"]("codex", args) == args


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (("--ask-for-approval", "always"), "invalid-option-value"),
        (("--sandbox", "host"), "invalid-option-value"),
        (("--model",), "missing-option-value"),
        (("--approve-for-me", "--ask-for-approval", "never"), "conflicting-options"),
        (("--dangerously-bypass-approvals-and-sandbox", "--sandbox", "read-only"), "conflicting-options"),
        (("--yolo",), "unsupported-option"),
        (("--search",), "unsupported-option"),
        (("-c", "features.web_search=true"), "unsupported-option"),
        (("prompt",), "positional-not-supported"),
    ],
)
def test_codex_policy_rejects_app_server_invalid_arguments(args, code):
    with pytest.raises(_policy()["HarnessArgValidationError"]) as error:
        _policy()["validate"]("codex", args)

    assert _issues(error)[0].code == code


@pytest.mark.parametrize(
    "args",
    [
        ("--provider", "anthropic"),
        ("--model=claude-sonnet-4-5",),
        ("--skill", "/tmp/e2e013-skill"),
        ("--approve",),
        ("--offline",),
    ],
)
def test_pi_policy_accepts_non_lifecycle_options_from_the_known_table(args):
    assert _policy()["validate"]("pi", args) == args


def test_pi_policy_accepts_the_e2e_013_skill_launch_arguments():
    args = ("--skill", "/tmp/e2e013-skill", "--approve")

    assert _policy()["validate"]("pi", args) == args


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (("--unknown",), "unsupported-option"),
        (("--provider",), "missing-option-value"),
        (("--skill",), "missing-option-value"),
        (("prompt",), "positional-not-supported"),
    ],
)
def test_pi_policy_rejects_unknown_or_positional_arguments(args, code):
    with pytest.raises(_policy()["HarnessArgValidationError"]) as error:
        _policy()["validate"]("pi", args)

    assert _issues(error)[0].code == code


@pytest.mark.parametrize(
    "args",
    [
        ("--session-id", "abc"),
        ("--resume",),
        ("--print",),
        ("--fork", "abc"),
    ],
)
def test_pi_policy_rejects_connector_owned_options(args):
    with pytest.raises(_policy()["HarnessArgValidationError"]) as error:
        _policy()["validate"]("pi", args)

    assert _issues(error)[0].code == "managed-lifecycle-conflict"


@pytest.mark.parametrize("harness", ["jev", "user-proxy", "unknown"])
def test_validate_rejects_non_phase_one_harnesses(harness):
    with pytest.raises(_policy()["HarnessArgValidationError"]) as error:
        _policy()["validate"](harness, ())

    assert _issues(error)[0].code == "unsupported-harness"


@pytest.mark.parametrize(
    ("harness", "args", "category"),
    [
        ("claude", ("--dangerously-skip-permissions",), "privilege-escalation"),
        ("codex", ("--ask-for-approval", "never"), "approval-bypass"),
        ("codex", ("--sandbox", "danger-full-access"), "sandbox-bypass"),
        ("claude", ("--settings", "{}"), "code-execution"),
        ("claude", ("--dangerously-unregistered",), "unknown-dangerous-pattern"),
    ],
)
def test_dangerous_classification_has_every_declared_category(harness, args, category):
    dangerous = _policy()["classify"](harness, args)

    assert category in {item.category for item in dangerous}


@pytest.mark.parametrize(
    "category",
    [
        "privilege-escalation",
        "approval-bypass",
        "sandbox-bypass",
        "code-execution",
        "unknown-dangerous-pattern",
    ],
)
def test_safe_model_selection_is_a_negative_control_for_each_category(category):
    dangerous = _policy()["classify"]("claude", ("--model", "sonnet"))

    assert category not in {item.category for item in dangerous}


@pytest.mark.parametrize(
    ("harness", "args", "category"),
    [
        ("claude", ("--permission-mode=bypassPermissions",), "privilege-escalation"),
        ("codex", ("--approve-for-me",), "approval-bypass"),
        ("pi", ("--approve",), "approval-bypass"),
        ("claude", ("--mcp-config={}",), "code-execution"),
        ("codex", ("--yolo",), "privilege-escalation"),
        ("claude", ("--allow-everything",), "unknown-dangerous-pattern"),
        ("codex", ("--no-super-sandbox",), "unknown-dangerous-pattern"),
    ],
)
def test_dangerous_classification_handles_aliases_and_patterns(harness, args, category):
    dangerous = _policy()["classify"](harness, args)

    assert category in {item.category for item in dangerous}


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (("--verbose",) * 257, "too-many-args"),
        (("x" * 65_537,), "args-too-large"),
    ],
)
def test_validation_applies_common_limits_before_adapter_semantics(args, code):
    with pytest.raises(_policy()["HarnessArgValidationError"]) as error:
        _policy()["validate"]("claude", args)

    assert _issues(error)[0].code == code


def test_redaction_covers_sensitive_forms_and_hashes_the_raw_canonical_map():
    raw = {
        "pi": ["--header", "Authorization: Bearer secret-header"],
        "claude": [
            "--settings",
            '{"token":"secret-settings"}',
            "--mcp-config=secret-mcp",
            "--token=secret-token",
            "--model",
            "sonnet",
        ],
    }
    result = _policy()["redact"](raw)
    canonical = json.dumps(
        {"claude": raw["claude"], "pi": raw["pi"]},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    assert result.digest == hashlib.sha256(canonical).hexdigest()
    assert result.harness_args == {
        "claude": (
            "--settings",
            "<redacted>",
            "--mcp-config=<redacted>",
            "--token=<redacted>",
            "--model",
            "sonnet",
        ),
        "pi": ("--header", "<redacted>"),
    }
    assert json.loads(result.json) == {
        key: list(value) for key, value in result.harness_args.items()
    }
    assert "secret" not in result.json
    assert result.truncated is False


def test_redaction_truncates_values_and_oversized_json_without_leaking_secrets():
    secret = "sensitive-" + "x" * 20_000
    result = _policy()["redact"](
        {
            "claude": [
                "--settings",
                secret,
                "--model",
                "m" * 600,
                *["--verbose"] * 250,
            ]
        }
    )

    assert len(result.json.encode("utf-8")) <= 16 * 1024
    assert secret not in result.json
    assert "sensitive-" not in result.json
    assert result.truncated is True
    assert json.loads(result.json)


def test_validation_errors_never_echo_sensitive_values():
    secret = "sensitive-token-value"
    with pytest.raises(_policy()["HarnessArgValidationError"]) as error:
        _policy()["validate"]("claude", (f"--token={secret}",))

    assert secret not in str(error.value)
    assert secret not in repr(error.value.issues)


@pytest.mark.parametrize(
    "args",
    [
        ("--model", "gemini-3.8-flash"),
        ("--mode", "default"),
        ("--mode=accept-edits",),
        ("--effort", "medium"),
        ("--dangerously-skip-permissions",),
        ("--disable-telemetry",),
        ("--add-dir", "/tmp/extra"),
        ("--agent", "default"),
        ("--log-file", "/tmp/agy.log"),
    ],
)
def test_agy_policy_accepts_declared_acp_options(args):
    assert _policy()["validate"]("agy", args) == args


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (("--mode", "auto"), "invalid-option-value"),
        (("--mode=yolo",), "invalid-option-value"),
        (("--effort", "xhigh"), "invalid-option-value"),
        (("--gemini-dir", "/tmp/override"), "managed-lifecycle-conflict"),
        (("--gemini-dir=/tmp/override",), "managed-lifecycle-conflict"),
        (("--resume", "s1"), "managed-lifecycle-conflict"),
        (("--stdio",), "managed-lifecycle-conflict"),
        (("--unknown",), "unsupported-option"),
        (("prompt",), "positional-not-supported"),
    ],
)
def test_agy_policy_rejects_unsafe_or_lifecycle_arguments(args, code):
    with pytest.raises(_policy()["HarnessArgValidationError"]) as error:
        _policy()["validate"]("agy", args)

    assert _issues(error)[0].code == code


def test_agy_dangerous_classification_flags_skip_permissions_and_mode_auto():
    dangerous = _policy()["classify"]("agy", ("--dangerously-skip-permissions",))
    assert {item.category for item in dangerous} == {"privilege-escalation"}
    mode_auto = _policy()["classify"]("agy", ("--mode", "auto"))
    assert {item.category for item in mode_auto} == {"approval-bypass"}
