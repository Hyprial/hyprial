"""Local X25519 core. Trusted declaration/publication remains unavailable."""
from .models import signer_kind_allowed
from .store import Store


class EncryptionKey:
    Store = Store
    signer_kind_allowed = staticmethod(signer_kind_allowed)
