"""Pure provider-key primitives. No daemon signing activation or network I/O."""
from .envelope import open_envelope, reseal, seal, unwrap_check, verify_envelope
from .grants import sign_grant, verify_grant
from .models import Error, OwnerKey, Recipient, SigningKey
from .policy import check_access
from .ports import KeyLookup, OwnerKeyPort, RecipientPort, StoragePort
from .records import (
    agent_uuid, check_envelope_matches, check_transition, new_key_id, record_digest,
    sign_record, verify_record,
)


class ProviderKeys:
    Error = Error
    OwnerKey = OwnerKey
    Recipient = Recipient
    SigningKey = SigningKey
    KeyLookup = KeyLookup
    OwnerKeyPort = OwnerKeyPort
    RecipientPort = RecipientPort
    StoragePort = StoragePort
    sign_grant = staticmethod(sign_grant)
    verify_grant = staticmethod(verify_grant)
    check_access = staticmethod(check_access)
    seal = staticmethod(seal)
    open_envelope = staticmethod(open_envelope)
    reseal = staticmethod(reseal)
    verify_envelope = staticmethod(verify_envelope)
    unwrap_check = staticmethod(unwrap_check)
    sign_record = staticmethod(sign_record)
    verify_record = staticmethod(verify_record)
    record_digest = staticmethod(record_digest)
    new_key_id = staticmethod(new_key_id)
    agent_uuid = staticmethod(agent_uuid)
    check_transition = staticmethod(check_transition)
    check_envelope_matches = staticmethod(check_envelope_matches)
