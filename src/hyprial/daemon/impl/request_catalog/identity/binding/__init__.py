"""IPC-only identity introspection: ``identity.whoami``."""

from .specs import WHOAMI


def whoami(app, params, *, trusted_message_origin=None):
    return app._ipc_identity_whoami(dict(params))


BINDINGS = ((WHOAMI, whoami),)
