"""Typed profile writes and immutable daemon read projections."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.kernel import EffectRequest, EffectCompleted, EffectLane
from hyprial.identity  import UserProfileStore, UserProfile, RuntimeCapability, UserProfileError


@dataclass(frozen=True, slots=True)
class EnsureProfile:
    owner: str
    owner_key: str
    login_name: str
    machine: str
    machine_key: str


@dataclass(frozen=True, slots=True)
class AssociateAgent:
    owner_key: str
    agent: str


@dataclass(frozen=True, slots=True)
class SetSquireChannel:
    owner_key: str
    channel: str


@dataclass(frozen=True, slots=True)
class BindOwnerOpenId:
    owner_key: str
    channel: str
    open_id: str
    now: datetime | None = None


@dataclass(frozen=True, slots=True)
class SetDeliveryAgent:
    owner_key: str
    agent: str


@dataclass(frozen=True, slots=True)
class SetRuntimeCapability:
    owner_key: str
    capability: RuntimeCapability


@dataclass(frozen=True, slots=True)
class RefreshProfiles:
    pass


ProfileMutation = (
    EnsureProfile
    | AssociateAgent
    | SetSquireChannel
    | BindOwnerOpenId
    | SetDeliveryAgent
    | SetRuntimeCapability
    | RefreshProfiles
)


@dataclass(frozen=True, slots=True)
class ProfileCommand:
    operation_id: str
    generation: int
    mutation: ProfileMutation


@dataclass(frozen=True, slots=True)
class ProfileResult:
    profiles: tuple[UserProfile, ...]
    outcome: tuple[UserProfile, tuple[str, ...]] | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ProfileProjection:
    generation: int
    version: int
    profiles: tuple[UserProfile, ...]


@dataclass
class _Reply:
    ready: threading.Event = field(default_factory=threading.Event)
    result: ProfileResult | None = None


class UserProfileAuthority:
    """Protocol-edge writes may wait; reads consume an immutable projection.

    Offline edits are adopted by explicit RefreshProfiles admission. They never
    cause a profile lookup on a timer or native callback to open a file.
    """

    def __init__(self, path: Path, *, capacity: int = 128, timeout: float = 5.0):
        self.path = Path(path)
        self._store = UserProfileStore(self.path)
        self._profiles = self._store.list()  # bootstrap before exposing authority
        self._guard = threading.Lock()
        self._pending: dict[str, _Reply] = {}
        self._closed = False
        self._capacity = capacity
        self._timeout = timeout
        self._generation = 1
        self._version = 0
        self._runtime = ActorRuntime()
        self._handle = self._runtime.start(
            ActorSpec(
                name="user-profiles",
                handler_factory=lambda: self._receive,
                mailbox_capacity=capacity,
            )
        )
        self._effects = EffectLane(
            name="user-profile-storage",
            execute=self._execute,
            complete=lambda event: self._runtime.tell(self._handle, event),
            capacity=capacity,
        )

    def _execute(self, mutation: ProfileMutation) -> ProfileResult:
        store = self._store
        try:
            if isinstance(mutation, EnsureProfile):
                outcome = store.ensure(
                    owner=mutation.owner,
                    owner_key=mutation.owner_key,
                    login_name=mutation.login_name,
                    machine=mutation.machine,
                    machine_key=mutation.machine_key,
                )
            elif isinstance(mutation, AssociateAgent):
                outcome = store.associate_agent(mutation.owner_key, mutation.agent)
            elif isinstance(mutation, SetSquireChannel):
                outcome = store.set_squire_channel(mutation.owner_key, mutation.channel)
            elif isinstance(mutation, BindOwnerOpenId):
                outcome = store.bind_owner_open_id(
                    mutation.owner_key,
                    channel=mutation.channel,
                    open_id=mutation.open_id,
                    now=mutation.now,
                )
            elif isinstance(mutation, SetDeliveryAgent):
                outcome = store.set_delivery_agent(mutation.owner_key, mutation.agent)
            elif isinstance(mutation, SetRuntimeCapability):
                outcome = store.set_runtime_capability(
                    mutation.owner_key, mutation.capability
                )
            elif isinstance(mutation, RefreshProfiles):
                outcome = None
            else:
                raise TypeError("unsupported profile command")
            return ProfileResult(store.list(), outcome)
        except UserProfileError as error:
            return ProfileResult((), error=str(error))

    def _receive(self, command):
        if isinstance(command, ProfileCommand):
            result = self._effects.submit(
                EffectRequest(
                    command.operation_id, command.generation, command.mutation
                )
            )
            if result is not AdmissionResult.ACCEPTED:
                self._settle(
                    command.operation_id,
                    ProfileResult((), error=f"profile storage {result.value}"),
                )
            return
        if not isinstance(command, EffectCompleted):
            raise TypeError("unsupported profile authority message")
        if command.generation == self._generation:
            result = (
                command.result
                if command.error is None
                else ProfileResult((), error=command.error)
            )
            self._settle(command.operation_id, result)
        self._effects.acknowledge(command.operation_id, command.generation)

    def _settle(self, operation_id, result):
        with self._guard:
            reply = self._pending.pop(operation_id, None)
            if reply is None:
                return
            if result.error is None:
                if self._profiles != result.profiles:
                    self._version += 1
                self._profiles = result.profiles
            reply.result = result
            reply.ready.set()

    def _call(self, mutation, *, wait=True):
        operation_id = uuid4().hex
        reply = _Reply()
        with self._guard:
            if self._closed or len(self._pending) >= self._capacity:
                raise UserProfileError("profile authority closed or overloaded")
            self._pending[operation_id] = reply
            result = self._runtime.tell(
                self._handle, ProfileCommand(operation_id, self._generation, mutation)
            )
            if result is not AdmissionResult.ACCEPTED:
                self._pending.pop(operation_id)
                raise UserProfileError(f"profile authority {result.value}")
        if not wait:
            return None
        if not reply.ready.wait(self._timeout):
            raise TimeoutError(
                f"profile operation {operation_id} remains accepted after caller deadline"
            )
        result = reply.result
        if result.error is not None:
            raise UserProfileError(result.error)
        return result.outcome

    def list(self):
        with self._guard:
            return self._profiles

    def projection(self):
        with self._guard:
            return ProfileProjection(self._generation, self._version, self._profiles)

    get = UserProfileStore.get
    get_by_owner = UserProfileStore.get_by_owner
    resolve = UserProfileStore.resolve

    def refresh(self, *, wait=False):
        return self._call(RefreshProfiles(), wait=wait)

    def resolve_current(self, identifier):
        """Fresh authorization/routing read, permitted only on an I/O edge.

        Offline setup can change bindings while the daemon runs. Cache-only
        projections remain suitable for status, but cannot authorize a send
        using a binding that has since been replaced on disk.
        """
        self.refresh(wait=True)
        return self.resolve(identifier)

    def ensure(self, *, owner, owner_key, login_name, machine, machine_key):
        return self._call(
            EnsureProfile(owner, owner_key, login_name, machine, machine_key)
        )

    def associate_agent(self, owner_key, agent):
        return self._call(AssociateAgent(owner_key, agent))

    def set_squire_channel(self, owner_key, channel):
        return self._call(SetSquireChannel(owner_key, channel))

    def bind_owner_open_id(self, owner_key, *, channel, open_id, now=None):
        return self._call(BindOwnerOpenId(owner_key, channel, open_id, now))

    def set_delivery_agent(self, owner_key, agent):
        return self._call(SetDeliveryAgent(owner_key, agent))

    def set_runtime_capability(self, owner_key, capability):
        return self._call(SetRuntimeCapability(owner_key, capability))

    def close(self, timeout=5.0):
        deadline = time.monotonic() + max(0.0, timeout)
        with self._guard:
            self._closed = True
        while True:
            with self._guard:
                pending = bool(self._pending)
            if not pending:
                break
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            return False
        return self._runtime.stop(self._handle, max(0.0, deadline - time.monotonic()))
