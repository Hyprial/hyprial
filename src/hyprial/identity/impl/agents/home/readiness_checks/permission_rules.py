"""Static models of declared Claude Code Bash permission rules.

Only the syntax forms this preflight can reason about without executing the
harness are modeled: exact matches, ``:*`` prefix rules, and a plain ``*``
glob.  Character classes, escapes and shell constructs stay UNKNOWN rather
than being guessed into a grant.  Env-wrapper rules are parsed here only so
they can be audited separately; a prefix on ``env`` is never evidence that the
wrapped executable is allowed.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

_BASH_RULE = re.compile(r"^Bash\((.*)\)$")
_ENV_ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_ENV_UNSAFE = re.compile(r"[*?\s\x00-\x1f\x7f;&|<>`$(){}]")
# Only a bare '*' is modeled.  Character classes, escapes and shell
# constructs stay UNKNOWN instead of being guessed into a grant.
_BASH_UNMODELED = re.compile(r"[?\[\]{}\\\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class EnvGrant:
    env_keys: tuple[str, ...]
    wrapped_tool: str
    arguments: tuple[str, ...]


def bash_spec(rule: str) -> str | None:
    match = _BASH_RULE.match(rule)
    return None if match is None else match.group(1).strip()


def env_spec(spec: str) -> bool:
    # An env wrapper is audited by the env permission check.  It is never
    # evidence that the wrapped executable itself is allowed.
    return spec == "env" or spec.startswith("env:") or spec.startswith("env ")


def bash_rule_state(rule: str, command: str) -> tuple[str, str | None]:
    """Classify one Bash rule against the command form being checked.

    States: ``match``, ``no-match``, ``unknown`` (syntax this static check
    does not model), and ``skip`` (not a Bash rule, or an env-wrapper rule
    audited separately).  Exact rules match the whole invocation; ``:*`` is
    the supported prefix form; a bare ``*`` glob matches any characters.
    Nothing here executes Claude Code's enforcement path.
    """

    spec = bash_spec(rule)
    if spec is None:
        return "skip", None
    if env_spec(spec):
        return "skip", None
    if not spec:
        return "unknown", "an empty Bash rule is not modeled"
    if _BASH_UNMODELED.search(spec):
        return "unknown", "it uses wildcard/escape syntax this check does not model"
    if spec == "*":
        return "match", None
    if spec.endswith(":*") and spec.count("*") == 1:
        prefix = spec[:-2]
        if not prefix:
            return "unknown", "a prefix rule without a command is not modeled"
        return (
            "match"
            if command == prefix or command.startswith(prefix + " ")
            else "no-match"
        ), None
    if "*" in spec:
        pattern = re.escape(spec).replace(r"\*", ".*")
        return ("match" if re.fullmatch(pattern, command) else "no-match"), None
    return ("match" if spec == command else "no-match"), None


def bash_rule_matches(rule: str, command: str) -> bool:
    state, _ = bash_rule_state(rule, command)
    return state == "match"


def specific_invocation_rule(rule: str, command: str) -> str | None:
    """A known rule for a subcommand of ``command``, not for the whole tool."""

    spec = bash_spec(rule)
    if spec is None or env_spec(spec) or _BASH_UNMODELED.search(spec):
        return None
    if spec.startswith(command + " "):
        return rule
    return None


def unknown_rule_may_target(
    rule: str, command: str, *, state: str
) -> bool:
    """Whether an unmodeled rule plausibly covers ``command``.

    The literal prefix before the first wildcard is enough to avoid turning an
    unrelated rule (for example a deny on ``rm``) into UNKNOWN for every tool.
    A rule that starts with a wildcard still may target anything and stays
    conservative.
    """

    if state != "unknown":
        return False
    spec = bash_spec(rule)
    if spec is None or env_spec(spec):
        return False
    literal = re.split(r"[*?\[\]{}\\]", spec, maxsplit=1)[0]
    if not literal:
        return True
    return (
        literal == command
        or literal.startswith(command + " ")
        or command.startswith(literal)
    )


def env_grant(rule: str) -> tuple[bool, EnvGrant | None, str | None]:
    """Parse an env-wrapper allow rule without ever treating it as tool grant.

    The only accepted form is ``Bash(env KEY=VALUE... /exact/tool ARGS:*)``.
    Keys must later appear in the role's exact env-key allowlist.  Wildcards,
    shell metacharacters, and values are rejected; values are never reported.
    """

    match = _BASH_RULE.match(rule)
    if match is None:
        return False, None, None
    spec = match.group(1).strip()
    if not (
        spec == "env"
        or spec.startswith("env:")
        or spec.startswith("env ")
    ):
        return False, None, None
    if not spec.endswith(":*"):
        return True, None, "it is not an exact prefix grant ending in ':*'"
    try:
        tokens = shlex.split(spec[:-2].strip())
    except ValueError:
        return True, None, "its env-wrapper declaration cannot be tokenized"
    if not tokens or tokens[0] != "env":
        return True, None, "it does not start with the env command"
    env_keys: list[str] = []
    index = 1
    while index < len(tokens):
        assignment = _ENV_ASSIGNMENT.match(tokens[index])
        if assignment is None:
            break
        key, value = assignment.groups()
        if key in env_keys:
            return True, None, "it repeats an env key"
        if _ENV_UNSAFE.search(value):
            return True, None, "it contains a wildcard or unsafe env value"
        env_keys.append(key)
        index += 1
    if index >= len(tokens):
        return True, None, "it has no exact wrapped executable"
    wrapped_tool = tokens[index]
    arguments = tuple(tokens[index + 1 :])
    if "=" in wrapped_tool:
        return True, None, "it declares an env assignment with an invalid key"
    if any(
        not token or _ENV_UNSAFE.search(token)
        for token in (wrapped_tool, *arguments)
    ):
        return True, None, (
            "its wrapped executable or arguments contain a wildcard or unsafe "
            "character"
        )
    return (
        True,
        EnvGrant(
            env_keys=tuple(env_keys),
            wrapped_tool=wrapped_tool,
            arguments=arguments,
        ),
        None,
    )


def env_deny_matches(rule: str, grant: EnvGrant) -> bool:
    """A broad or exact env deny always wins over an env allow."""

    is_env, denied, error = env_grant(rule)
    if not is_env:
        return False
    if error is not None or denied is None:
        return True
    return denied.wrapped_tool == grant.wrapped_tool
