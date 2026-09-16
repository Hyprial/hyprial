"""Runtime capability probing for personal Squire profiles.

A probe runs one minimal real invocation of a harness + provider + model
combination and records the observed fact: available or not, why not, and
when it was checked.  Quota belongs to the harness + provider pair, not to
the model, so every combination is probed through its exact command line.
Tiering or dispatch judgement does not live here; this module records facts.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from .profile import RuntimeCapability, UserProfile, UserProfileError, UserProfileStore

REASON_USAGE_LIMIT = "usage-limit"
REASON_MISSING_CREDENTIAL = "missing-credential"
REASON_COMMAND_NOT_FOUND = "command-not-found"
REASON_PROBE_TIMEOUT = "probe-timeout"
REASON_PROBE_ERROR = "probe-error"

DEFAULT_TIMEOUT_SECONDS = 180.0
_DETAIL_LIMIT = 200

ComboSpec = tuple[str, str | None, str]  # (harness, provider, model)


@dataclass(frozen=True, slots=True)
class ProbeRun:
    """Raw outcome of one probe invocation."""

    exit_code: int | None  # None when the command could not be started
    output: str
    timed_out: bool = False


ProbeRunner = Callable[[tuple[str, ...], float], ProbeRun]


def build_probe_command(
    harness: str, model: str, provider: str | None = None
) -> tuple[str, ...]:
    """The exact minimal invocation used to probe one combination.

    Deliberately real executions only.  Listing-oriented probes such as
    ``pi --list-models`` are not evidence: they enumerate fuzzily instead of
    resolving a concrete model, and a model name can resolve to an
    unintended provider.  ``codex exec`` requires ``--skip-git-repo-check``
    so the trusted-directory check does not masquerade as a quota failure.
    """

    if harness == "pi":
        if not provider:
            raise ValueError(
                "pi probes require an explicit provider; without --provider the "
                "model resolves ambiguously"
            )
        return (
            "pi",
            "-p",
            "--provider",
            provider,
            "--model",
            model,
            "--thinking",
            "high",
            "hello",
        )
    if harness == "claude":
        return ("claude", "-p", "--model", model, "hello")
    if harness == "codex":
        return ("codex", "exec", "--skip-git-repo-check", "--model", model, "hello")
    raise ValueError(f"unsupported harness {harness!r}")


_USAGE_LIMIT_MARKERS = (
    "out of extra usage",
    "usage limit",
    "rate limit",
    "rate_limit",
    "insufficient_quota",
    "quota exceeded",
    "too many requests",
    "429",
)
_CREDENTIAL_MARKERS = (
    "missing credential",
    "no credentials",
    "invalid api key",
    "api key",
    "unauthorized",
    "401",
    "authentication",
    "not logged in",
    "login required",
)
_RETRY_AT = re.compile(r"try again at\s+([^\n.;]+)", re.IGNORECASE)


def classify_run(run: ProbeRun) -> tuple[str, str | None, str | None]:
    """Classify one run into (status, reason, retry_at)."""

    if run.timed_out:
        return "unavailable", REASON_PROBE_TIMEOUT, None
    if run.exit_code is None:
        return "unavailable", REASON_COMMAND_NOT_FOUND, None
    if run.exit_code == 0:
        return "available", None, None
    retry_at = _extract_retry_at(run.output)
    lowered = run.output.lower()
    if any(marker in lowered for marker in _USAGE_LIMIT_MARKERS):
        return "unavailable", REASON_USAGE_LIMIT, retry_at
    if any(marker in lowered for marker in _CREDENTIAL_MARKERS):
        return "unavailable", REASON_MISSING_CREDENTIAL, retry_at
    return "unavailable", REASON_PROBE_ERROR, retry_at


def _extract_retry_at(output: str) -> str | None:
    """Capture recovery timestamps such as codex's "try again at ..."."""

    match = _RETRY_AT.search(output)
    if match is None:
        return None
    raw = match.group(1).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _combine_output(*parts: str | bytes | None) -> str:
    decoded = []
    for part in parts:
        if part is None:
            continue
        decoded.append(
            part.decode("utf-8", errors="replace") if isinstance(part, bytes) else part
        )
    return "\n".join(piece for piece in decoded if piece)


def _detail(output: str) -> str | None:
    collapsed = " ".join(output.split())
    if not collapsed:
        return None
    return collapsed[:_DETAIL_LIMIT]


def _subprocess_runner(command: tuple[str, ...], timeout: float) -> ProbeRun:
    if shutil.which(command[0]) is None:
        return ProbeRun(None, f"command not found: {command[0]}")
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return ProbeRun(None, f"command not found: {command[0]}")
    except subprocess.TimeoutExpired as error:
        return ProbeRun(
            None,
            _combine_output(error.stdout, error.stderr),
            timed_out=True,
        )
    return ProbeRun(
        completed.returncode,
        _combine_output(completed.stdout, completed.stderr),
    )


class RuntimeProber:
    """Probes combinations through real harness invocations."""

    def __init__(
        self,
        *,
        runner: ProbeRunner | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("probe timeout must be positive")
        self.runner = runner or _subprocess_runner
        self.timeout = timeout
        self._now = now or (lambda: datetime.now(UTC))

    def probe(
        self, harness: str, model: str, provider: str | None = None
    ) -> RuntimeCapability:
        command = build_probe_command(harness, model, provider)
        started = self._now()
        run = self.runner(command, self.timeout)
        status, reason, retry_at = classify_run(run)
        detail = None if status == "available" else _detail(run.output)
        return RuntimeCapability(
            harness=harness,
            provider=provider,
            model=model,
            status=status,
            reason=reason,
            detail=detail,
            retry_at=retry_at,
            probed_at=started.isoformat().replace("+00:00", "Z"),
        )


def probe_combinations(
    store: UserProfileStore,
    owner_key: str,
    combos: Iterable[ComboSpec],
    *,
    prober: RuntimeProber,
    dry_run: bool = False,
) -> tuple[tuple[RuntimeCapability, ...], tuple[str, ...], UserProfile]:
    """Probe each combination and persist results unless ``dry_run``."""

    profile = store.get(owner_key)
    if profile is None:
        raise UserProfileError(f"user profile {owner_key!r} was not registered")
    results: list[RuntimeCapability] = []
    changed: list[str] = []
    for harness, provider, model in combos:
        result = prober.probe(harness, model, provider)
        results.append(result)
        if not dry_run:
            profile, fields = store.set_runtime_capability(owner_key, result)
            changed.extend(fields)
    return tuple(results), tuple(changed), profile
