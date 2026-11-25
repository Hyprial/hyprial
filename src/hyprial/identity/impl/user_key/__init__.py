"""Local user signing-key core, exported through identity.UserKey."""
from .binding import (
    unavailable_binding as unavailable_binding, verify_binding as verify_binding,
    verify_revocations as verify_revocations, valid_key_id as valid_key_id,
)
from .crypto import canonical_bytes as canonical_bytes, digest as digest, sign as sign, verify as verify
from .models import Error as Error, Record as Record, Trust as Trust
from .purposes import PURPOSES as PURPOSES, Purpose as Purpose
from .store import Store as Store


class UserKey:
    Store = Store
    Error = Error
    Record = Record
    Trust = Trust
    Purpose = Purpose
    PURPOSES = PURPOSES
    canonical_bytes = staticmethod(canonical_bytes)
    digest = staticmethod(digest)
    sign = staticmethod(sign)
    verify = staticmethod(verify)
    unavailable_binding = staticmethod(unavailable_binding)
    verify_binding = staticmethod(verify_binding)
    verify_revocations = staticmethod(verify_revocations)
    valid_key_id = staticmethod(valid_key_id)
