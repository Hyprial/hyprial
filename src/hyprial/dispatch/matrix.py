"""Ordered model choices from model-matrix.md; availability is personal runtime data.

A model may accept lower-tier work. Its entity tier is its highest declared tier;
future PAC requires.tier consumers must compare minimums, not equality.
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
        _candidate("pi", "deepseek", "deepseek-v4-flash"),
        _candidate("pi", "zai-coding-cn", "glm-5.3-flash"),
    ),
    "strong": (
        _candidate("codex", None, "gpt-5.6-sol"),
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

    def to_json(self) -> dict[str, object]:
        return {
            **candidate_json(self.candidate),
            "status": self.status,
            "output": self.output,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
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


def _resolve_candidates(
    tier: str,
    candidates: tuple[Candidate, ...],
    *,
    probe: bool,
    profile: UserProfile | None,
    runner: ProbeRunner | None,
) -> LaunchChoice:
    from hyprial.squire.probe import ProbeRun, RuntimeProber

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
        if observed is not None and observed.status == "unavailable":
            readings.append(
                ProbeReading(
                    candidate,
                    "profile-filtered",
                    observed.detail or observed.reason or "",
                )
            )
            continue
        if not probe:
            return LaunchChoice(
                tier, candidate, tuple(readings), probed=False, considered=candidates
            )
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
            )
        )
        if alive:
            return LaunchChoice(
                tier, candidate, tuple(readings), probed=True, considered=candidates
            )
    raise NoCapableHarness(tier, tuple(readings), candidates=candidates)


def resolve(
    tier: str,
    *,
    probe: bool = True,
    profile: UserProfile | None = None,
    runner: ProbeRunner | None = None,
) -> LaunchChoice:
    """First live candidate in one exact priority pool."""
    if tier not in TIERS:
        raise ValueError(f"tier must be one of {', '.join(TIERS)}")
    return _resolve_candidates(
        tier, TIERS[tier], probe=probe, profile=profile, runner=runner
    )


def resolve_minimum_tier(
    required_tier: str,
    *,
    probe: bool = True,
    profile: UserProfile | None = None,
    runner: ProbeRunner | None = None,
) -> LaunchChoice:
    """Resolve a minimum tier, accepting higher-tier candidates.

    Candidate pools retain their declared tier priority and duplicate model
    rows are probed once. This is the PAC ``requires.tier`` interface; callers
    must not mistake an exact pool for a minimum capability requirement.
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
    return _resolve_candidates(
        required_tier,
        tuple(candidates),
        probe=probe,
        profile=profile,
        runner=runner,
    )
