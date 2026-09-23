"""Ordered model choices from model-matrix.md; availability is never an input.

A model may accept lower-tier work. Its entity tier is its highest declared tier;
future PAC requires.tier consumers must compare minimums, not equality.

Policy (Allen, 2026-09-21): *provider/model availability is the user's own
responsibility*, so dispatch assumes every candidate is usable.  Selection is
the first candidate in the declared tier pool, full stop: no profile mark, no
liveness probe, and no fallback may change who is chosen.  A provider that is
in fact unusable fails at execution time and is reported loudly to the sender
that is waiting for the receipt (see ``hyprial.availability_loud``) -- never
routed around silently.

``diagnose()`` keeps the explicit, human-requested probe as a read-only
inspection of every candidate.  Its readings are returned to the caller and
read by nothing else: they cannot influence ``resolve()``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from hyprial.contracts import ipc_errors

if TYPE_CHECKING:
    from hyprial.squire.profile import UserProfile
    from hyprial.squire.probe import ProbeRunner


class Candidate(NamedTuple):
    harness: str
    provider: str | None
    model: str

    @property
    def probe_cmd(self) -> tuple[str, ...]:
        # Keep CLI import dependency-free. Probe machinery belongs on the
        # dispatch command path, not every ``hyprial`` invocation.
        from hyprial.squire.probe import build_probe_command

        return build_probe_command(self.harness, self.model, self.provider)


def _candidate(harness: str, provider: str | None, model: str) -> Candidate:
    return Candidate(harness, provider, model)


# Claude/Codex subscription paths use their native default provider (None),
# matching Squire's exact (harness, provider, model) runtime-capability key.
TIERS: dict[str, tuple[Candidate, ...]] = {
    "fast": (
        _candidate("codex", None, "gpt-5.6-luna"),
        _candidate("claude", None, "sonnet"),
        _candidate("pi", "deepseek", "deepseek-flash"),
        _candidate("pi", "zai-coding-cn", "glm-5.3-flash"),
    ),
    "strong": (
        _candidate("codex", None, "gpt-5.6-sol"),
        _candidate("pi", "openai-codex", "gpt-5.6-sol"),
        _candidate("claude", None, "fable"),
        _candidate("pi", "openai-codex", "gpt-6-astra"),
        _candidate("claude", None, "opus"),
        _candidate("pi", "kimi-coding", "k3"),
    ),
    "super": (
        _candidate("claude", None, "fable"),
        _candidate("pi", "openai-codex", "gpt-6-astra"),
    ),
}
TIER_RANK = {tier: rank for rank, tier in enumerate(TIERS)}


def tier_for_model(model: str | None) -> str | None:
    """Highest tier for an exact model id; never guess unknown models."""
    return next(
        (tier for tier in reversed(TIERS) if any(c.model == model for c in TIERS[tier])),
        None,
    )


def meets_tier(entity_tier: str | None, required_tier: str) -> bool:
    """Interface for PAC v2 minimum-tier requirements; no node semantics here."""
    return entity_tier in TIER_RANK and TIER_RANK[entity_tier] >= TIER_RANK[required_tier]


@dataclass(frozen=True)
class ProbeReading:
    candidate: Candidate
    status: str
    output: str
    exit_code: int | None = None
    timed_out: bool = False

    #: The owner's diagnostic mark for this exact (harness, provider, model),
    #: when one is recorded.  Kept for diagnosis only: it is reported next to
    #: the reading and is never an input to selection.
    diagnostic_mark: str | None = None

    def to_json(self) -> dict[str, object]:
        return {
            **candidate_json(self.candidate),
            "status": self.status,
            "output": self.output,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "diagnostic_mark": self.diagnostic_mark,
        }


@dataclass(frozen=True)
class LaunchChoice:
    tier: str
    selected: Candidate
    readings: tuple[ProbeReading, ...]
    probed: bool
    considered: tuple[Candidate, ...] | None = None

    @property
    def harness(self) -> str:
        return self.selected.harness

    @property
    def provider(self) -> str | None:
        return self.selected.provider

    @property
    def model(self) -> str:
        return self.selected.model

    def to_json(self) -> dict[str, object]:
        return {
            "tier": self.tier,
            "candidates": [
                candidate_json(candidate)
                for candidate in (self.considered or TIERS[self.tier])
            ],
            "probes": [r.to_json() for r in self.readings],
            "selected": candidate_json(self.selected),
            "probed": self.probed,
        }


class NoCapableHarness(RuntimeError):
    """Retained for the stable IPC error code; selection no longer raises it.

    A static tier pool always yields its first candidate, so "not capable" is
    not a selection outcome any more.  The class (and its code) stay because
    callers and the error-code registry still name it; removing the code would
    be a wire-contract change unrelated to this policy.
    """

    code = ipc_errors.DISPATCH_NO_CAPABLE_HARNESS

    def __init__(
        self,
        tier: str,
        readings: tuple[ProbeReading, ...],
        *,
        candidates: tuple[Candidate, ...] | None = None,
    ):
        super().__init__(f"no capable harness for tier {tier!r}")
        self.data = {
            "tier": tier,
            "candidates": [candidate_json(c) for c in (candidates or TIERS[tier])],
            "probes": [r.to_json() for r in readings],
            "selected": None,
        }


def candidate_json(candidate: Candidate) -> dict[str, object]:
    return {**candidate._asdict(), "probe_cmd": list(candidate.probe_cmd)}


def active_profile() -> UserProfile | None:
    """Read only the active owner's runtime facts; never borrow another pool."""
    from hyprial.daemon.identity import resolve_node_owner
    from hyprial.home import configured_hyprial_home
    from hyprial.squire.profile import UserProfileStore

    configured = os.environ.get("HARNESS_STATE_DIR")
    state = Path(configured).expanduser().resolve() if configured else configured_hyprial_home()[0] / "state"
    store = UserProfileStore(state / "users.json")
    if not store.path.exists():
        return None
    return store.get_by_owner(resolve_node_owner())


def minimum_pool(required_tier: str) -> tuple[Candidate, ...]:
    """Declared candidate order for a minimum-tier requirement.

    Tiers are walked in declaration order (``fast``, ``strong``, ``super``)
    and a candidate appearing in more than one pool is kept at its first
    position.  Pure configuration: no runtime fact participates.
    """
    if required_tier not in TIERS:
        raise ValueError(f"tier must be one of {', '.join(TIERS)}")
    candidates: list[Candidate] = []
    for tier in TIERS:
        if not meets_tier(tier, required_tier):
            continue
        for candidate in TIERS[tier]:
            if candidate not in candidates:
                candidates.append(candidate)
    return tuple(candidates)


def _static_choice(tier: str, candidates: tuple[Candidate, ...]) -> LaunchChoice:
    """First declared candidate, chosen without reading any runtime fact."""
    return LaunchChoice(
        tier, candidates[0], (), probed=False, considered=candidates
    )


def resolve(tier: str) -> LaunchChoice:
    """First candidate in one exact priority pool; availability is not read.

    No profile mark, no probe, no fallback: changing every runtime
    availability fact must leave this answer identical.
    """
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {', '.join(TIERS)}")
    return _static_choice(tier, TIERS[tier])


def resolve_minimum_tier(required_tier: str) -> LaunchChoice:
    """Resolve a minimum tier, accepting higher-tier candidates.

    Candidate pools retain their declared tier priority. This is the PAC
    ``requires.tier`` interface; callers must not mistake an exact pool for a
    minimum capability requirement.  Like :func:`resolve`, selection ignores
    runtime availability entirely.
    """
    return _static_choice(required_tier, minimum_pool(required_tier))


def diagnose(
    tier: str,
    *,
    minimum: bool = False,
    profile: UserProfile | None = None,
    runner: ProbeRunner | None = None,
) -> tuple[ProbeReading, ...]:
    """Probe every candidate and return the raw readings; never select.

    This is the only remaining caller of the probe machinery, and it exists
    for the explicit human request ``hyprial dispatch matrix --probe``.  Its
    result is returned to that caller and read by nothing else: it is not
    cached, not written to the profile, and never consulted by :func:`resolve`
    or :func:`resolve_minimum_tier`.  A provider that answers badly here
    changes a report, not a route.

    The owner's diagnostic mark is attached to each reading (when one exists)
    so the report can show it beside the probe result; the mark neither
    suppresses a probe nor reorders the candidates.
    """
    from hyprial.squire.probe import ProbeRun, RuntimeProber

    if tier not in TIERS:
        raise ValueError(f"tier must be one of {', '.join(TIERS)}")
    candidates = minimum_pool(tier) if minimum else TIERS[tier]
    profile = profile if profile is not None else active_profile()
    prober = RuntimeProber(runner=runner)
    readings: list[ProbeReading] = []
    for candidate in candidates:
        observed = (
            profile.runtime_capability(
                candidate.harness, candidate.model, candidate.provider
            )
            if profile is not None
            else None
        )
        mark = None
        if observed is not None:
            mark = observed.status if not observed.reason else f"{observed.status}:{observed.reason}"
        try:
            run = prober.runner(candidate.probe_cmd, prober.timeout)
        except OSError as error:
            run = ProbeRun(None, str(error))
        alive = run.exit_code == 0 and not run.timed_out and bool(run.output.strip())
        readings.append(
            ProbeReading(
                candidate,
                "available" if alive else "unavailable",
                run.output,
                run.exit_code,
                run.timed_out,
                diagnostic_mark=mark,
            )
        )
    return tuple(readings)
