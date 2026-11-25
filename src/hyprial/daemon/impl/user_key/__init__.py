"""Daemon user-key API, exported as one public namespace."""
from hyprial.daemon.impl.identity.jwks import JwksSource as JwksSource
from .publication import publish as publish
from .policy import Caller as Caller
from .service import Signer as Signer
from .verifier import OwnerKeyPort as OwnerKeyPort


class UserKey:
    Caller = Caller
    Signer = Signer
    OwnerKeyPort = OwnerKeyPort
    JwksSource = JwksSource
    publish = staticmethod(publish)
