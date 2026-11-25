"""Compare the fake provider against the real one, and report the differences.

Why this is not a test
----------------------
A test answers "is the product broken".  This answers "has the fake drifted
from the service it imitates" -- a different question with a different owner
and a different urgency.  Run as a pass/fail check it would mostly measure
whether the vendor's API was reachable today, which is the vendor's business,
not ours.  So it emits a difference report and lets a person read it.

"The real call succeeded" is not "the mock matches the real one".  Only a
field-by-field comparison of the two responses can say the second thing, and
that is the thing the E2E line depends on.

Version first
-------------
A mock drifts from its original the way evidence goes stale against the tree
it was gathered on: silently, with nothing in the artifact announcing it.  So
the recorded contract version is compared before any payload is, and a
mismatch is reported as its own finding rather than being left to surface as
a confusing pile of field differences.

⚠️ Deliberately not covered (decided, not overlooked):
  * streaming frame *timing* -- inter-chunk delays are a property of the
    network path, not of the contract, and comparing them would report the
    weather
  * fields whose values are expected to differ every call (ids, timestamps,
    token counts) are compared for presence and type, never for equality
"""

from __future__ import annotations

from typing import Any

# Fields whose values legitimately differ between any two calls.  Comparing
# them for equality would produce a report that is never empty, and a report
# that is never empty is one nobody reads.
VOLATILE_FIELDS = frozenset(
    {"id", "created", "created_at", "usage", "system_fingerprint"}
)


class ContractDrift:
    """One difference between what the fake produces and what the vendor does."""

    __slots__ = ("path", "kind", "real", "mock")

    def __init__(self, path: str, kind: str, real: object, mock: object) -> None:
        self.path = path
        self.kind = kind
        self.real = real
        self.mock = mock

    def __repr__(self) -> str:
        return (
            f"ContractDrift(path={self.path!r}, kind={self.kind!r}, "
            f"real={self.real!r}, mock={self.mock!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ContractDrift):
            return NotImplemented
        return (self.path, self.kind, self.real, self.mock) == (
            other.path,
            other.kind,
            other.real,
            other.mock,
        )


def compare_contract_versions(
    recorded: dict[str, str], live: dict[str, str]
) -> list[ContractDrift]:
    """Report contracts whose live version no longer matches the recorded one.

    Checked before payloads because a version mismatch explains any number of
    field differences at once: reporting those fields first would bury the one
    finding that accounts for them.
    """

    drifts: list[ContractDrift] = []
    for name, recorded_version in sorted(recorded.items()):
        live_version = live.get(name)
        if live_version is None:
            drifts.append(
                ContractDrift(name, "contract-version-unknown", None, recorded_version)
            )
        elif live_version != recorded_version:
            drifts.append(
                ContractDrift(
                    name, "contract-version-changed", live_version, recorded_version
                )
            )
    return drifts


def compare_payloads(
    real: Any, mock: Any, *, path: str = ""
) -> list[ContractDrift]:
    """Field-by-field difference between a real and a faked response.

    Shape is compared, not content: a fake that returns different *text* is
    doing its job, while a fake that omits a key, or returns a list where the
    vendor returns an object, will make the harness behave differently.
    Volatile fields are compared for presence and type only.
    """

    drifts: list[ContractDrift] = []
    if type(real) is not type(mock):
        drifts.append(ContractDrift(path or ".", "type", type(real).__name__,
                                    type(mock).__name__))
        return drifts

    if isinstance(real, dict):
        for key in sorted(set(real) | set(mock)):
            child = f"{path}.{key}" if path else key
            if key not in mock:
                drifts.append(ContractDrift(child, "missing-in-mock", real[key], None))
            elif key not in real:
                drifts.append(ContractDrift(child, "absent-in-real", None, mock[key]))
            elif key in VOLATILE_FIELDS:
                if type(real[key]) is not type(mock[key]):
                    drifts.append(
                        ContractDrift(
                            child,
                            "type",
                            type(real[key]).__name__,
                            type(mock[key]).__name__,
                        )
                    )
            else:
                drifts.extend(compare_payloads(real[key], mock[key], path=child))
        return drifts

    if isinstance(real, list):
        if not real or not mock:
            if bool(real) != bool(mock):
                drifts.append(
                    ContractDrift(path or ".", "emptiness", bool(real), bool(mock))
                )
            return drifts
        # Vendors vary the number of chunks per response, so length is not a
        # contract.  The shape of an element is.
        drifts.extend(compare_payloads(real[0], mock[0], path=f"{path}[]"))
        return drifts

    return drifts


def render_report(
    version_drifts: list[ContractDrift], payload_drifts: list[ContractDrift]
) -> str:
    """Human-readable difference report.

    Deliberately not a verdict.  A reader decides whether a drift matters;
    a boolean would have to decide in advance, and would be wrong for exactly
    the drifts worth knowing about.
    """

    lines: list[str] = []
    if version_drifts:
        lines.append("contract versions:")
        for drift in version_drifts:
            lines.append(
                f"  {drift.path}: recorded={drift.mock!r} live={drift.real!r}"
                f" ({drift.kind})"
            )
    if payload_drifts:
        lines.append("payload shape:")
        for drift in payload_drifts:
            lines.append(
                f"  {drift.path}: real={drift.real!r} mock={drift.mock!r}"
                f" ({drift.kind})"
            )
    if not lines:
        return "no drift: the fake still matches the recorded contract"
    return "\n".join(lines)
