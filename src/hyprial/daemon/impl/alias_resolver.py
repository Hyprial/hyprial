"""Exact immutable-id alias resolution at daemon input boundaries.

P2 deliberately validates without rewriting.  The caller keeps the address it
received; this module only answers whether each alias is current, unknown in a
domain this daemon owns, ambiguous, or outside the daemon's present knowledge.
P3 will consume the resolved identifiers when storage changes in one coordinated
install.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from hyprial.identity import read_device_record
from hyprial.kernel import ipc_errors, parse_agent_uri


class AliasKind(str, Enum):
    USER = "user"
    DEVICE = "device"
    AGENT = "agent"


class AliasStatus(str, Enum):
    RESOLVED = "resolved"
    UNKNOWN = "unknown"
    AMBIGUOUS = "ambiguous"
    OUTSIDE_DOMAIN = "outside-domain"


class AliasSurface(str, Enum):
    ADAPTER = "adapter"
    CLI = "cli"
    DAEMON = "daemon"
    MCP = "mcp"
    PAC = "pac"
    ROUTINE = "routine"


_ID_PREFIX = {
    AliasKind.USER: "u.",
    AliasKind.DEVICE: "d.",
    AliasKind.AGENT: "a.",
}


@dataclass(frozen=True, slots=True)
class AliasRecord:
    kind: AliasKind
    alias: str
    identifier: str

    def __post_init__(self) -> None:
        if not self.alias or not self.identifier.startswith(_ID_PREFIX[self.kind]):
            raise ValueError(f"invalid {self.kind.value} alias record")


@dataclass(frozen=True, slots=True)
class AliasResolution:
    status: AliasStatus
    identifier: str | None = None
    candidates: tuple[str, ...] = ()


class AliasSource(Protocol):
    def records(self, kind: AliasKind) -> Iterable[AliasRecord]: ...


class LocalAliasSource(AliasSource, Protocol):
    def local_record(self, kind: AliasKind) -> AliasRecord | None: ...

    def is_local_domain(self, kind: AliasKind, value: str) -> bool: ...

    def knows_local_agent(self, value: str) -> bool: ...


class AliasValidationError(ValueError):
    """A within-domain or ambiguous alias cannot cross an input boundary."""

    def __init__(
        self,
        *,
        status: AliasStatus,
        kind: AliasKind,
        alias: str,
        candidates: tuple[str, ...] = (),
    ) -> None:
        self.status = status
        self.kind = kind
        self.alias = alias
        self.candidates = candidates
        if status is AliasStatus.AMBIGUOUS:
            detail = (
                f"{kind.value} alias {alias!r} is ambiguous; candidate ids: "
                + ", ".join(candidates)
            )
        else:
            detail = f"unknown {kind.value} alias {alias!r} in this node's resolvable domain"
        super().__init__(detail)

    @property
    def code(self) -> str:
        return (
            ipc_errors.AMBIGUOUS_TARGET
            if self.status is AliasStatus.AMBIGUOUS
            else ipc_errors.TARGET_UNRESOLVED
        )

    @property
    def data(self) -> dict[str, object]:
        data: dict[str, object] = {"alias": self.alias, "kind": self.kind.value}
        if self.candidates:
            data["candidates"] = list(self.candidates)
        return data


class StaticAliasSource:
    """Mutable fixture/source adapter; records are still read on every query."""

    def __init__(
        self,
        records: Iterable[AliasRecord] = (),
        *,
        local: Mapping[AliasKind, AliasRecord] | None = None,
        domain: Mapping[AliasKind, str] | None = None,
    ) -> None:
        self._records = tuple(records)
        self._local = dict(local or {})
        self._domain = dict(domain or {})

    def replace(self, *records: AliasRecord) -> None:
        self._records = tuple(records)

    def records(self, kind: AliasKind) -> tuple[AliasRecord, ...]:
        return tuple(record for record in self._records if record.kind is kind)

    def local_record(self, kind: AliasKind) -> AliasRecord | None:
        return self._local.get(kind)

    def is_local_domain(self, kind: AliasKind, value: str) -> bool:
        configured = self._domain.get(kind)
        if configured is not None:
            return value == configured
        record = self.local_record(kind)
        return record is not None and value in (record.alias, record.identifier)

    def knows_local_agent(self, value: str) -> bool:
        return any(
            record.kind is AliasKind.AGENT
            and value in (record.alias, record.identifier)
            for record in self._records
        )


@dataclass(frozen=True, slots=True)
class VerifiedPeopleBinding:
    """The non-secret identity verdict returned by a people-row verifier."""

    username: str
    user_id: str


PeopleRowVerifier = Callable[[Mapping[str, object]], VerifiedPeopleBinding | None]


class PeopleDirectoryAliasSource:
    """Other-user aliases sourced only from already verified people rows.

    There is intentionally no default verifier.  Production does not have the
    Casdoor proof reader yet, so production composition does not instantiate
    this source.  Tests supply fixture rows and a fixture verifier through this
    narrow seam; a verifier refusal or a row/claim name mismatch is ignored.
    """

    def __init__(
        self,
        rows: Callable[[], Iterable[Mapping[str, object]]],
        *,
        verify: PeopleRowVerifier,
    ) -> None:
        self._rows = rows
        self._verify = verify

    def records(self, kind: AliasKind) -> tuple[AliasRecord, ...]:
        if kind is not AliasKind.USER:
            return ()
        records: list[AliasRecord] = []
        for row in self._rows():
            verdict = self._verify(row)
            if verdict is None or row.get("user") != verdict.username:
                continue
            user_id = _typed_id(AliasKind.USER, verdict.user_id)
            if verdict.username and user_id is not None:
                records.append(AliasRecord(kind, verdict.username, user_id))
        return tuple(records)


class _DaemonAliasSource:
    """Live local settings/device/registry source used by the daemon."""

    def __init__(
        self,
        home: Path,
        agents: Any,
        *,
        owner: str,
        node_id: str,
        known_agent: Callable[[str], bool],
    ) -> None:
        self._home = Path(home)
        self._agents = agents
        self._owner = owner
        self._node_id = node_id
        self._known_agent = known_agent

    def _user(self) -> AliasRecord | None:
        try:
            document = json.loads((self._home / "settings.json").read_text("utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return None
        if not isinstance(document, dict):
            return None
        username = document.get("username")
        identifier = _typed_id(AliasKind.USER, document.get("userId"))
        if not isinstance(username, str) or not username or identifier is None:
            return None
        return AliasRecord(AliasKind.USER, username, identifier)

    def _device(self) -> AliasRecord | None:
        try:
            record = read_device_record(self._home)
        except ValueError:
            return None
        if record is None or record.name is None:
            return None
        identifier = _typed_id(AliasKind.DEVICE, record.device_uid)
        return None if identifier is None else AliasRecord(AliasKind.DEVICE, record.name, identifier)

    def local_record(self, kind: AliasKind) -> AliasRecord | None:
        if kind is AliasKind.USER:
            return self._user()
        if kind is AliasKind.DEVICE:
            return self._device()
        return None

    def is_local_domain(self, kind: AliasKind, value: str) -> bool:
        if kind is AliasKind.USER:
            return value == self._owner
        if kind is AliasKind.DEVICE:
            return value == self._node_id
        return False

    def knows_local_agent(self, value: str) -> bool:
        return self._known_agent(value)

    def records(self, kind: AliasKind) -> tuple[AliasRecord, ...]:
        if kind in (AliasKind.USER, AliasKind.DEVICE):
            record = self.local_record(kind)
            return () if record is None else (record,)
        records: list[AliasRecord] = []
        for agent in self._agents.list():
            records.append(AliasRecord(AliasKind.AGENT, agent.actor, agent.agent_id))
        return tuple(records)


def _typed_id(kind: AliasKind, value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    prefix = _ID_PREFIX[kind]
    return value if value.startswith(prefix) else f"{prefix}{value}"


class AliasResolver:
    """One exact daemon resolver for all three identity segment kinds."""

    def __init__(
        self,
        local_source: LocalAliasSource,
        *,
        sources: Iterable[AliasSource] = (),
        log: Callable[..., None] | None = None,
    ) -> None:
        self._local_source = local_source
        self._sources = (local_source, *tuple(sources))
        self._log = log
        self._logged_outside: OrderedDict[tuple[str, str, str], None] = OrderedDict()

    def _records(self, kind: AliasKind) -> tuple[AliasRecord, ...]:
        return tuple(record for source in self._sources for record in source.records(kind))

    def resolve(
        self, kind: AliasKind, value: str, *, within_domain: bool
    ) -> AliasResolution:
        prefix = _ID_PREFIX[kind]
        if value.startswith(prefix):
            candidates = {record.identifier for record in self._records(kind) if record.identifier == value}
        else:
            candidates = {record.identifier for record in self._records(kind) if record.alias == value}
        ordered = tuple(sorted(candidates))
        if len(ordered) == 1:
            return AliasResolution(AliasStatus.RESOLVED, ordered[0], ordered)
        if len(ordered) > 1:
            return AliasResolution(AliasStatus.AMBIGUOUS, candidates=ordered)
        return AliasResolution(
            AliasStatus.UNKNOWN if within_domain else AliasStatus.OUTSIDE_DOMAIN
        )

    def _is_local(self, kind: AliasKind, value: str) -> bool:
        return self._local_source.is_local_domain(kind, value)

    def _checked(
        self,
        kind: AliasKind,
        alias: str,
        *,
        within_domain: bool,
        surface: AliasSurface,
        preserve_unknown: bool = False,
    ) -> AliasResolution:
        answer = self.resolve(kind, alias, within_domain=within_domain)
        if answer.status is AliasStatus.AMBIGUOUS:
            raise AliasValidationError(
                status=answer.status,
                kind=kind,
                alias=alias,
                candidates=answer.candidates,
            )
        if answer.status is AliasStatus.UNKNOWN and not preserve_unknown and not (
            kind is AliasKind.AGENT
            and self._local_source.knows_local_agent(alias)
        ):
            raise AliasValidationError(status=answer.status, kind=kind, alias=alias)
        if answer.status is AliasStatus.OUTSIDE_DOMAIN:
            key = (
                kind.value,
                surface.value,
                sha256(alias.encode("utf-8")).hexdigest(),
            )
            if key not in self._logged_outside:
                self._logged_outside[key] = None
                if len(self._logged_outside) > 1024:
                    self._logged_outside.popitem(last=False)
                if self._log is not None:
                    self._log(
                        "alias.unresolved",
                        kind=kind.value,
                        surface=surface.value,
                    )
            else:
                self._logged_outside.move_to_end(key)
        return answer

    def validate_target(
        self,
        target: str,
        *,
        surface: AliasSurface,
        preserve_unknown: bool = False,
    ) -> str:
        """Validate one input address and return its byte-identical spelling."""

        if ":" not in target:
            self._checked(
                AliasKind.AGENT,
                target,
                within_domain=True,
                surface=surface,
                preserve_unknown=preserve_unknown,
            )
            return target
        if target.startswith("user:") and target.count(":") == 1:
            alias = target.removeprefix("user:")
            if self._is_local(AliasKind.USER, alias):
                return target
            self._checked(
                AliasKind.USER,
                alias,
                within_domain=False,
                surface=surface,
                preserve_unknown=preserve_unknown,
            )
            return target
        parsed = parse_agent_uri(target)
        if parsed is None:
            return target
        owner, machine, agent = parsed
        if not self._is_local(AliasKind.USER, owner):
            self._checked(
                AliasKind.USER,
                owner,
                within_domain=False,
                surface=surface,
                preserve_unknown=preserve_unknown,
            )
            return target
        if not self._is_local(AliasKind.DEVICE, machine):
            self._checked(
                AliasKind.DEVICE,
                machine,
                within_domain=False,
                surface=surface,
                preserve_unknown=preserve_unknown,
            )
            return target
        self._checked(
            AliasKind.AGENT,
            agent,
            within_domain=True,
            surface=surface,
            preserve_unknown=preserve_unknown,
        )
        return target


def daemon_alias_resolver(
    *,
    home: Path,
    agents: Any,
    owner: str,
    node_id: str,
    known_agent: Callable[[str], bool],
    log: Callable[..., None] | None = None,
) -> AliasResolver:
    """Compose the production resolver; peer sources stay disabled in P2."""

    return AliasResolver(
        _DaemonAliasSource(
            home,
            agents,
            owner=owner,
            node_id=node_id,
            known_agent=known_agent,
        ),
        log=log,
    )


__all__ = [
    "AliasKind",
    "AliasRecord",
    "AliasResolution",
    "AliasResolver",
    "AliasStatus",
    "AliasSurface",
    "AliasValidationError",
    "PeopleDirectoryAliasSource",
    "StaticAliasSource",
    "VerifiedPeopleBinding",
    "daemon_alias_resolver",
]
