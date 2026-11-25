"""Consistency gates over trusted projections, without lease or roster storage."""

from functools import wraps

from ..store import read_device_record
from ..transport_credential import TransportCredentialError, _read_local_device_file
from ..client import (
    DeviceAuthorityConfig,
    TransportClientError,
    load_client_tls_material,
)
from .ports import (
    TailnetAuthorityBinding,
    TailnetAuthorityPin,
    TailnetCredentialAssociation,
    TailnetCredentialError,
    TailnetMember,
    TailnetMembership,
)


def fail(code, phase, outcome="NOT_SENT"):
    raise TailnetCredentialError(code, phase=phase, remote_outcome=outcome)


def fresh(home, tailnet_id, context, *, phase, op=None):
    outcome = op.outcome if op else "NOT_SENT"
    try:
        record = read_device_record(
            home, file_reader=lambda path: _read_local_device_file(home, path)
        )
        s = context.membership(tailnet_id)
        pin = s.authority_pin
        valid_pin = (
            isinstance(pin, TailnetAuthorityPin)
            and all(
                isinstance(v, str) and v
                for v in (
                    pin.tailnet_id,
                    pin.owner,
                    pin.device_id,
                    pin.master_public,
                    pin.server_public,
                    pin.address,
                )
            )
            and type(pin.control_port) is int
            and 1 <= pin.control_port <= 65535
        )
        valid = (
            isinstance(s, TailnetMembership)
            and valid_pin
            and record is not None
            and record.device_uid is not None
            and s.tailnet_id == tailnet_id == pin.tailnet_id
            and type(s.revision) is int
            and 1 <= s.revision <= 2**53 - 1
            and s.member_ready is True
            and s.code is None
            and s.authority_device_id == pin.device_id
            and isinstance(s.local_member, TailnetMember)
            and s.local_member.state == "active"
            and s.local_member.owner == record.owner
            and s.local_member.device_id == record.device_id
        )
        authority_local = (
            s.role == "authority"
            and pin.device_id == record.device_id
            and pin.owner == record.owner
        )
        lease = s.lease_remaining_seconds
        valid = valid and (
            (authority_local and lease is None)
            or (s.role == "member" and type(lease) is int and 0 < lease <= 300)
        )
        binding = context.authority(tailnet_id, pin)
        valid = (
            valid
            and isinstance(binding, TailnetAuthorityBinding)
            and binding.authority_pin == pin
        )
        valid = (
            valid
            and isinstance(binding.config, DeviceAuthorityConfig)
            and isinstance(binding.policy_issuer, str)
            and bool(binding.policy_issuer)
            and isinstance(binding.trust_bundle_id, str)
            and bool(binding.trust_bundle_id)
        )
        if op is not None:
            valid = (
                valid
                and record.as_record() == op.record.as_record()
                and record.device_uid == op.record.device_uid
                and binding == op.binding
            )
    except Exception:
        fail("TAILNET_CONTEXT_UNAVAILABLE", phase, outcome)
    if not valid:
        fail("TAILNET_CONTEXT_MISMATCH", phase, outcome)
    return record, binding


def association(context, tailnet_id, record, binding, *, phase, outcome="NOT_SENT"):
    try:
        return context.association(tailnet_id, record.device_id, binding.authority_pin)
    except Exception:
        fail("TAILNET_ASSOCIATION_UNAVAILABLE", phase, outcome)


def matches(value, tailnet_id, record, binding, credential):
    return (
        isinstance(value, TailnetCredentialAssociation)
        and value.authority_pin == binding.authority_pin
        and value.tailnet_id == tailnet_id
        and value.device_id == record.device_id
        and value.owner == record.owner == credential.owner
        and value.device_uid == record.device_uid == credential.device_uid
        and value.binding_id == credential.binding_id
        and type(value.credential_generation) is int
        and value.credential_generation == credential.credential_generation
        and value.spki_sha256 == credential.spki_sha256
        and value.owner_user_id == credential.owner_user_id
        and value.policy_issuer == credential.policy_issuer == binding.policy_issuer
        and value.trust_bundle_id
        == credential.trust_bundle_id
        == binding.trust_bundle_id
        and value.state == "active"
    )


def require_association(
    context, tailnet_id, record, binding, credential, *, phase, outcome="NOT_SENT"
):
    value = association(
        context, tailnet_id, record, binding, phase=phase, outcome=outcome
    )
    if not matches(value, tailnet_id, record, binding, credential):
        fail("TAILNET_ASSOCIATION_MISMATCH", phase, outcome)


def public_gate(function):
    """Detach known typed workflow failures without replacing their identity."""

    @wraps(function)
    def detached(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (
            TailnetCredentialError,
            TransportClientError,
            TransportCredentialError,
        ) as error:
            error.__context__ = None
            error.__cause__ = None
            raise

    return detached


def require_ready_tls(home, binding, *, phase):
    try:
        expected = load_client_tls_material(home)
        valid = binding.config.client_tls == expected
    except Exception:
        fail("TAILNET_TLS_MISMATCH", phase)
    if not valid:
        fail("TAILNET_TLS_MISMATCH", phase)
