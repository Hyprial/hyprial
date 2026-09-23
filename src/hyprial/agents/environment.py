"""Internal complete-child-environment value objects for P1a.

Nothing in this module starts a process.  The types make completeness explicit
so P1b consumers can replace, rather than merge with, an ambient environment.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .secrets import ResolvedSecret

__all__ = [
    "BASE_CHILD_ENVIRONMENT_NAMES",
    "GENERATED_CHILD_ENVIRONMENT_NAMES",
    "GIT_ONE_SHOT_TRIPLE",
    "CompleteChildEnvironment",
    "ChildEnvironmentLaunch",
    "build_complete_child_environment",
    "compose_worker_child_launch",
    "whitelist_replacement_environment",
]

BASE_CHILD_ENVIRONMENT_NAMES = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "HOME",
        "TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "TERM",
        "COLORTERM",
        "TERM_PROGRAM",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "SSH_AUTH_SOCK",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        # Test-infrastructure names (B1 frozen table): consumed only by
        # ``tests/fixtures/fake_pi_rpc.py`` and its env-dump wrapper — the
        # fake pi double the daemon-managed-worker e2e suites spawn.
        # Without them in the base vocabulary the whitelist silently drops
        # them and the fixture loses its log/mode wiring (found live: the
        # fetch-path e2e timed out waiting for a prompt that was logged to
        # a path the child never saw).  Not secrets; behavior control for
        # the test double only.
        "FAKE_PI_MODE",
        "FAKE_PI_LOG",
        "FAKE_PI_ARGV_LOG",
    }
)

GENERATED_CHILD_ENVIRONMENT_NAMES = frozenset(
    {
        "HYPRIAL_HOME",
        "HARNESS_STATE_DIR",
        "HYPRIAL_AGENT_DIR",
        "HYPRIAL_WORKER_ACTOR",
        "HYPRIAL_WORKER_SESSION_REF",
        # P1b B1 (L3): the three daemon-authority variables.  All three are
        # minted by the daemon (owner/node resolution in
        # DaemonApplication.__init__), never passed through from a parent
        # process env, and never derived by splitting a worker-actor URI
        # (that derivation family is refused: pac/context.py's hostname
        # fallback would silently mask a wrong value).
        # ``HYPRIAL_MANAGED_WORKER`` was already injected by
        # WorkerChannel.identity_environment while missing here — the
        # whitelist would have rejected the channel's own injection the
        # moment it became load-bearing (#536 cross-point).
        "HYPRIAL_NODE_ID",
        "HYPRIAL_OWNER",
        "HYPRIAL_MANAGED_WORKER",
    }
)

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_GIT_TRIPLE = frozenset(
    {"GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"}
)

#: Public alias: the one-shot Git override group, exported for daemon-side
#: base construction (B1) so the triple rule has a single spelling.
GIT_ONE_SHOT_TRIPLE = _GIT_TRIPLE


def git_one_shot_is_valid(source: Mapping[str, str]) -> bool:
    """Whether ``source`` carries the exact approved one-shot Git group."""

    return (
        source.get("GIT_CONFIG_COUNT") == "1"
        and str(source.get("GIT_CONFIG_KEY_0", "")).startswith("url.")
        and str(source.get("GIT_CONFIG_KEY_0", "")).endswith(".insteadOf")
        and bool(source.get("GIT_CONFIG_VALUE_0"))
    )


@dataclass(frozen=True, slots=True)
class CompleteChildEnvironment:
    """A complete replacement mapping; values are deliberately not repr'd."""

    _items: tuple[tuple[str, str], ...] = field(repr=False)

    def __post_init__(self) -> None:
        names = [name for name, _value in self._items]
        if len(names) != len(set(names)):
            raise ValueError("complete child environment has duplicate names")
        if any(not _ENV_NAME.fullmatch(name) for name in names):
            raise ValueError("complete child environment has an invalid name")

    def for_exec(self) -> dict[str, str]:
        """Return a detached complete mapping for a future exec consumer."""

        return dict(self._items)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(name for name, _value in self._items)



@dataclass(frozen=True, slots=True)
class ChildEnvironmentLaunch:
    """A complete child environment plus its completion-receipt provenance.

    P1b B1 (design §3.1.5): resolver success is not startup success.  The
    consumer that actually spawns records — after the process exists — the
    identity it bound and the ``(grant_id, revision)`` of every secret the
    environment carries.  The receipt names grants and revisions only;
    secret values never enter it (T07).  This type stays inside the daemon
    process; it is deliberately not part of any persisted projection.
    """

    environment: CompleteChildEnvironment
    actor: str
    grants: tuple[tuple[str, int], ...] = ()


def build_complete_child_environment(
    *,
    base: Mapping[str, str],
    generated: Mapping[str, str],
    secrets: Iterable[ResolvedSecret] = (),
) -> CompleteChildEnvironment:
    """Construct from approved base, daemon-generated, and granted values only.

    There is intentionally no default and no read of ``os.environ``.  The Git
    one-shot override is accepted only as the exact approved three-key group,
    with count one and an ``insteadOf`` key; broader ``GIT_CONFIG_*`` input is
    outside the vocabulary.
    """

    if any(name not in BASE_CHILD_ENVIRONMENT_NAMES for name in base):
        raise ValueError("base child environment contains an unapproved name")
    if any(name not in GENERATED_CHILD_ENVIRONMENT_NAMES for name in generated):
        raise ValueError("generated child environment contains an unapproved name")
    git_names = _GIT_TRIPLE.intersection(base)
    if git_names and git_names != _GIT_TRIPLE:
        raise ValueError("Git one-shot environment must contain the complete triple")
    if git_names:
        if base["GIT_CONFIG_COUNT"] != "1":
            raise ValueError("Git one-shot environment count must be one")
        key = base["GIT_CONFIG_KEY_0"]
        if not key.startswith("url.") or not key.endswith(".insteadOf"):
            raise ValueError("Git one-shot environment key is not approved")
        if not base["GIT_CONFIG_VALUE_0"]:
            raise ValueError("Git one-shot environment source must not be blank")

    combined: dict[str, str] = {}
    for mapping in (base, generated):
        for name, value in mapping.items():
            if not isinstance(value, str):
                raise TypeError("child environment values must be strings")
            if name in combined:
                raise ValueError("child environment name is mapped twice")
            combined[name] = value
    for resolved in secrets:
        for name, value in resolved.environment().items():
            if name in combined:
                raise ValueError("secret environment name conflicts with another mapping")
            combined[name] = value
    return CompleteChildEnvironment(tuple(sorted(combined.items())))


def compose_worker_child_launch(
    *,
    registry: Any,
    hyprial_home: Path,
    channel: Any,
    environ: Mapping[str, str],
    agent_name: str,
) -> ChildEnvironmentLaunch:
    """The daemon-side composition of one worker's complete environment (B1).

    One implementation shared by the daemon wiring and the tests that pin
    it: generated values from the worker channel's daemon-authority
    identity (now including node/owner) plus the agent's home directory;
    secret values only from the agent's explicit grants via the P1a
    resolver; BASE from the daemon's own current values for the approved
    names ("P1 保留现用真实 HOME/PATH", T16).  The Git one-shot triple
    passes only as a complete valid group; a partial group is dropped so
    the global gitconfig path stays available.  Any resolution failure
    propagates — the worker start fails loudly; there is no ambient
    fallback and no silent secretless continuation.
    """

    from .home import AgentHomeError
    from .secrets import SecretResolver

    resolver = SecretResolver(Path(hyprial_home), registry)
    resolved = []
    for grant in registry.secret_inventory(agent_name):
        resolved.append(resolver.resolve(agent_name, grant.grant_id))
    generated = dict(channel.identity_environment())
    try:
        generated["HYPRIAL_AGENT_DIR"] = str(
            registry.home_receipt(agent_name).path
        )
    except AgentHomeError:
        # A home that has not been provisioned yet (a legacy agent whose
        # first start under P1a has not run the actor-path ensure_home) is
        # an honest ABSENCE of optional discovery info, not a start
        # failure: with no home there can be no grants (the write path
        # requires the receipt), so nothing is being silently skipped.
        # Every other home error still propagates loudly.
        if resolved:
            raise
    base = {
        name: environ[name]
        for name in BASE_CHILD_ENVIRONMENT_NAMES
        if name in environ and name not in GIT_ONE_SHOT_TRIPLE
    }
    if (
        GIT_ONE_SHOT_TRIPLE.intersection(environ) == GIT_ONE_SHOT_TRIPLE
        and git_one_shot_is_valid(environ)
    ):
        for name in GIT_ONE_SHOT_TRIPLE:
            base[name] = environ[name]
    return ChildEnvironmentLaunch(
        environment=build_complete_child_environment(
            base=base, generated=generated, secrets=resolved
        ),
        actor=channel.actor,
        grants=tuple(
            (secret.grant.grant_id, secret.grant.revision)
            for secret in resolved
        ),
    )


def whitelist_replacement_environment(
    environ: Mapping[str, str],
    *deltas: Mapping[str, str],
) -> dict[str, str]:
    """The B2 spawn-site replacement for ``{**os.environ, **delta}``.

    Ambient inheritance is restricted to the frozen table's vocabulary
    (BASE + GENERATED names; the Git one-shot triple passes only as a
    complete valid group, a partial group drops so the global gitconfig
    path stays available).  Everything else the caller hands in ``deltas``
    — provider mappings, worker identity, harness handoff payloads — is
    explicit input, overlaid in order after the filtered ambient base.
    Provider keys are deliberately NOT ambient: an exported
    ``DEEPSEEK_API_KEY`` does not ride the environment into a child; it
    arrives via a delta (the resolver for daemon workers, the provider
    configuration for interactive launches) or not at all.
    """

    combined: dict[str, str] = {}
    for name in BASE_CHILD_ENVIRONMENT_NAMES:
        if name in environ and name not in GIT_ONE_SHOT_TRIPLE:
            combined[name] = environ[name]
    for name in GENERATED_CHILD_ENVIRONMENT_NAMES:
        if name in environ:
            combined[name] = environ[name]
    if (
        GIT_ONE_SHOT_TRIPLE.intersection(environ) == GIT_ONE_SHOT_TRIPLE
        and git_one_shot_is_valid(environ)
    ):
        for name in GIT_ONE_SHOT_TRIPLE:
            combined[name] = environ[name]
    for delta in deltas:
        for name, value in delta.items():
            if not isinstance(value, str):
                raise TypeError("environment values must be strings")
            combined[name] = value
    return combined
