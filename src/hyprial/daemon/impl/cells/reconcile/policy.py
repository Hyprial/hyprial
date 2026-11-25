"""Cell member launch policy: resolution, launch options, ack check, lookup (I6-5a).

Decision notes (cell model slice I6-5a, PLAN-I5 §I6-5, ruling §13.1.5):

- ``allow``/``deny`` are read from the cell document only (``cell.members.<m>``
  via ``parse_structure``).  A member that declares neither has no policy;
  this module never supplies a default.
- The harness-neutral policy maps to launch options per harness.  Only
  ``claude`` has a mapping (``allowedTools``/``disallowedTools``); a member of
  another harness that declares a policy is refused by the caller (member
  failed, cell degraded), never launched with the policy silently dropped.
- The digest is the one the isolated Agent SDK worker recomputes over the
  launch options it received (``harnesses/agent_sdk/authority_model.py``).
  It is imported lazily because the harness packages import cells.
- ``MemberLaunchPolicies`` is a standing lookup keyed by ``(harness, actor,
  member)``, not a one-shot handoff: a crash restart or a boot recovery launch
  goes through the same worker-channel factory outside any reconcile effect
  and must get the same policy.  It also keeps the last launched process per
  key so the reconcile effect reads the worker's raw ready frame, and the last
  verified entry so a failed apply can relaunch the previous policy.
  Publishing ``None`` is an entry ("no policy"), distinct from nothing
  published: a rollback to a policy-less spec must not fall back to the cell
  document, which already holds the rules that just failed.
- I6-5a (last step): a launch that reconcile did not publish a policy for
  (``lifecycle.start``, restore, crash restart) reads the member's rules from
  the cell document itself (``document_launch_policy``).  A document with
  structure problems, no such member, no declaration or a harness without a
  mapping yields no policy; the caller launches with channel wiring only.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from hyprial.daemon.impl.cells.model import Cell
from hyprial.daemon.impl.cells.structure import parse_structure

POLICY_HARNESSES = frozenset({"claude"})
REASON_POLICY_UNSUPPORTED = "policy-unsupported"


class PolicyHandshakeError(RuntimeError):
    """The launched harness did not acknowledge the policy it was given."""


def policy_digest(
    *, harness: str, headless: bool, allow: Iterable[str], deny: Iterable[str]
) -> str:
    """The canonical launch-policy digest shared with the worker handshake."""

    from hyprial.daemon.impl.harnesses.agent_sdk.authority_model import (
        policy_digest as shared_digest,
    )

    return shared_digest(harness=harness, headless=headless, allow=allow, deny=deny)


def protocol_version() -> int:
    from hyprial.daemon.impl.harnesses.agent_sdk.authority_model import (
        AUTHORITY_PROTOCOL_VERSION,
    )

    return AUTHORITY_PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class MemberPolicy:
    """One member's resolved launch policy and its digest."""

    harness: str
    allow: tuple[str, ...]
    deny: tuple[str, ...]
    digest: str

    def launch_options(self) -> Mapping[str, object]:
        """The harness launch options carrying this policy."""

        options: dict[str, object] = {"allowedTools": list(self.allow)}
        if self.deny:
            options["disallowedTools"] = list(self.deny)
        return MappingProxyType(options)


def supports_policy(harness: str) -> bool:
    return harness in POLICY_HARNESSES


def resolve_member_policy(
    harness: str,
    allow: tuple[str, ...] | None,
    deny: tuple[str, ...] | None,
) -> MemberPolicy | None:
    """The member's policy, or None when the document declares none."""

    if allow is None and deny is None:
        return None
    allow_rules = tuple(allow or ())
    deny_rules = tuple(deny or ())
    return MemberPolicy(
        harness=harness,
        allow=allow_rules,
        deny=deny_rules,
        digest=policy_digest(
            harness=harness, headless=True, allow=allow_rules, deny=deny_rules
        ),
    )


def require_policy_ack(
    ready: Mapping[str, object] | None, *, digest: str, version: int
) -> None:
    """Require the ready frame to acknowledge exactly this policy and version."""

    authority = ready.get("authority") if isinstance(ready, Mapping) else None
    if not isinstance(authority, Mapping):
        raise PolicyHandshakeError("harness ready frame has no authority acknowledgement")
    if authority.get("enabled") is not True:
        raise PolicyHandshakeError("harness did not enable authority mode")
    protocol = authority.get("protocol")
    if type(protocol) is not int or protocol != protocol_version():
        raise PolicyHandshakeError("harness authority protocol version mismatch")
    if authority.get("policyDigest") != digest:
        raise PolicyHandshakeError("harness acknowledged a different policy digest")
    ack_version = authority.get("version")
    if type(ack_version) is not int or ack_version != version:
        raise PolicyHandshakeError("harness acknowledged a different policy version")


@dataclass(frozen=True, slots=True)
class LaunchPolicy:
    """The launch option bundle one managed worker receives."""

    launch_options: Mapping[str, object]
    version: int
    digest: str


PolicyKey = tuple[str, str, str]

REASON_POLICY_DOCUMENT_INVALID = "policy-document-invalid"
REASON_POLICY_NO_MEMBER = "policy-no-member"


def document_launch_policy(
    cell: Cell, member: str, harness: str
) -> tuple[LaunchPolicy | None, str | None]:
    """The launch policy *cell* declares for *member*, and why there is none.

    The reason is None when the policy resolved or the member declares no
    rules; otherwise it names why a declared policy cannot be used.
    """

    structure = parse_structure(cell)
    if structure.problems:
        return None, REASON_POLICY_DOCUMENT_INVALID
    declared = next((item for item in structure.members if item.name == member), None)
    if declared is None:
        return None, REASON_POLICY_NO_MEMBER
    policy = resolve_member_policy(harness, declared.allow, declared.deny)
    if policy is None:
        return None, None
    if not supports_policy(harness):
        return None, REASON_POLICY_UNSUPPORTED
    return (
        LaunchPolicy(
            launch_options=policy.launch_options(),
            version=max(1, cell.revision),
            digest=policy.digest,
        ),
        None,
    )


def launch_key(spec: object) -> PolicyKey | None:
    """The ``(harness, actor, member)`` key of a launch spec (member defaults to the name)."""

    harness = getattr(spec, "harness", None)
    name = getattr(spec, "name", None)
    member = getattr(spec, "member", None) or name
    if not (
        isinstance(harness, str) and harness
        and isinstance(name, str) and name
        and isinstance(member, str) and member
    ):
        return None
    return (harness, name, member)


class MemberLaunchPolicies:
    """Process-local standing lookup from a launch spec to its member policy."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: dict[PolicyKey, LaunchPolicy | None] = {}
        self._verified: dict[PolicyKey, LaunchPolicy | None] = {}
        self._processes: dict[PolicyKey, object] = {}
        self._launched: dict[PolicyKey, LaunchPolicy | None] = {}

    def publish(
        self, key: PolicyKey, policy: MemberPolicy | None, *, version: int
    ) -> LaunchPolicy | None:
        """Make *policy* the one every later launch of *key* receives.

        ``None`` is published too: it means "no policy" for this key, which a
        launch honours without reading the cell document.
        """

        if policy is None:
            with self._lock:
                self._current[key] = None
            return None
        launch = LaunchPolicy(
            launch_options=policy.launch_options(),
            version=max(1, int(version)),
            digest=policy.digest,
        )
        with self._lock:
            self._current[key] = launch
        return launch

    def mark_verified(self, key: PolicyKey) -> None:
        with self._lock:
            self._verified[key] = self._current.get(key)

    def restore_verified(self, key: PolicyKey) -> LaunchPolicy | None:
        """Republish the last verified entry of *key* (None: no policy)."""

        with self._lock:
            previous = self._verified.get(key)
            self._current[key] = previous
            return previous

    def forget(self, key: PolicyKey) -> None:
        with self._lock:
            self._current.pop(key, None)
            self._verified.pop(key, None)
            self._processes.pop(key, None)
            self._launched.pop(key, None)

    def pin(self, key: PolicyKey, launch: LaunchPolicy | None) -> None:
        """Make *launch* exactly what the next launch of *key* receives."""

        with self._lock:
            self._current[key] = launch

    def release(self, key: PolicyKey) -> None:
        """Drop the published entry of *key*: later launches read the document again."""

        with self._lock:
            self._current.pop(key, None)

    def note_launch(self, spec: object, launch: LaunchPolicy | None) -> None:
        """Record the policy a launch of *spec* received."""

        key = launch_key(spec)
        if key is None:
            return
        with self._lock:
            self._launched[key] = launch

    def launched(self, key: PolicyKey) -> tuple[bool, LaunchPolicy | None]:
        """Whether a launch of *key* was recorded, and the policy it received."""

        with self._lock:
            return key in self._launched, self._launched.get(key)

    def policy_for(self, spec: object) -> LaunchPolicy | None:
        """The policy a launch of *spec* receives; never consumed."""

        return self.lookup(spec)[1]

    def lookup(self, spec: object) -> tuple[bool, LaunchPolicy | None]:
        """Whether reconcile published an entry for *spec*'s key, and the entry."""

        key = launch_key(spec)
        if key is None:
            return False, None
        with self._lock:
            return key in self._current, self._current.get(key)

    def policy_for_key(self, key: PolicyKey) -> LaunchPolicy | None:
        with self._lock:
            return self._current.get(key)

    def record_launch_process(self, spec: object, process: object) -> None:
        key = launch_key(spec)
        if key is None:
            return
        with self._lock:
            self._processes[key] = process

    def discard_process(self, key: PolicyKey) -> None:
        with self._lock:
            self._processes.pop(key, None)

    def ready_frame(self, key: PolicyKey) -> Mapping[str, object] | None:
        with self._lock:
            process = self._processes.get(key)
        ready = getattr(process, "last_ready", None)
        return dict(ready) if isinstance(ready, Mapping) else None


__all__ = [
    "LaunchPolicy",
    "MemberLaunchPolicies",
    "MemberPolicy",
    "POLICY_HARNESSES",
    "REASON_POLICY_DOCUMENT_INVALID",
    "REASON_POLICY_NO_MEMBER",
    "PolicyHandshakeError",
    "PolicyKey",
    "REASON_POLICY_UNSUPPORTED",
    "policy_digest",
    "document_launch_policy",
    "launch_key",
    "protocol_version",
    "require_policy_ack",
    "resolve_member_policy",
    "supports_policy",
]
