"""Tailnet-scoped credential runtime public facade."""

from ..ports import TailnetCredentialError
from .ports import (
    Descriptor, DialTarget, FrozenTransport, MemberProjection, MembershipSource, MtlsReference,
)
from .schema import decode, descriptor_record, encode, fail, parse_descriptor, public_gate, text
from .slot import Slot
from .workflow import Runtime


class TailnetCredentials:
    DialTarget = DialTarget
    Descriptor = Descriptor
    MtlsReference = MtlsReference
    FrozenTransport = FrozenTransport
    MemberProjection = MemberProjection
    MembershipSource = MembershipSource
    Runtime = Runtime

    def __init__(self, home, *, membership, dial):
        if not callable(dial) or not callable(getattr(membership, "membership", None)) or not callable(getattr(membership, "members", None)):
            raise ValueError("credential composition requires membership and socket dial")
        self._home = home
        self._membership = membership
        self._dial = dial

    @public_gate
    def runtime(self, *, tailnet_id):
        return Runtime._create(self, Slot(self._home, tailnet_id))

    @public_gate
    def bootstrap(self, *, tailnet_id, remote_port=7449):
        from ..authority.http import bootstrap
        runtime = self.runtime(tailnet_id=tailnet_id)
        with runtime._slot.locked():
            _, observation = runtime._fresh()
            target = DialTarget(observation.authority_pin, remote_port)
            if observation.role == "authority":
                from ..authority.issuer import ensure
                descriptor, _, _ = ensure(runtime._slot, target.authority, remote_port)
            else:
                descriptor = bootstrap(self._dial, target, tailnet_id)
            # Freshness must still hold after the bounded socket exchange.
            _, after = runtime._fresh()
            if after.authority_pin != target.authority:
                fail("TAILNET_AUTHORITY_DESCRIPTOR_CONFLICT", "bootstrap")
            path = (*runtime._slot.state, "descriptor.json")
            existing = runtime._slot.read(path)
            if existing is not None:
                try:
                    previous = parse_descriptor(decode(existing))
                except (ValueError, TypeError, UnicodeError):
                    fail("TAILNET_AUTHORITY_DESCRIPTOR_CONFLICT", "bootstrap")
                if previous != descriptor:
                    fail("TAILNET_AUTHORITY_DESCRIPTOR_CONFLICT", "bootstrap")
            else:
                runtime._slot.write(path, encode(descriptor_record(descriptor)), exclusive=True)
            return descriptor

    @public_gate
    def inspect_journal(self, *, tailnet_id, operation_id):
        return self.runtime(tailnet_id=tailnet_id).status(operation_id)

    def peer_member(self, connection, *, tailnet_id):
        text(tailnet_id)
        # The future adapter's unforgeable connection capability is not present.
        raise TailnetCredentialError("TAILNET_CONNECTION_UNTRUSTED", phase="peer")

    def handle(self, *, connection, tailnet_id, route, request_body):
        text(tailnet_id)
        raise TailnetCredentialError("TAILNET_CONNECTION_UNTRUSTED", phase="handle")

    def query_current_key(self, *, tailnet_id, device_id):
        text(tailnet_id)
        text(device_id)
        raise TailnetCredentialError("TAILNET_RUNTIME_NOT_READY", phase="query")
