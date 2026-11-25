"""The daemon exposes no logout custody entry for a supervisor coordinator."""

import importlib

import pytest


def test_logout_custody_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("hyprial.daemon.impl.onboarding.operations.custody")


@pytest.mark.parametrize("name", ["CustodyOutcome", "LogoutCustody", "take_logout_custody"])
def test_daemon_public_face_has_no_custody_names(name):
    import hyprial.daemon as daemon

    assert name not in daemon.__all__
    assert not hasattr(daemon, name)
