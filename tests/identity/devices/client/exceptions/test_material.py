"""Safe-reader and material conversion errors expose only the typed surface."""

from types import SimpleNamespace

import pytest

from hyprial.identity.impl.device.client import mtls
from hyprial.identity.interface import TransportCredentialError, load_client_tls_material

from . import support as s


@pytest.mark.parametrize("site", ["credential", "key-typed", "bundle-typed",
                                  "key-oserror", "bundle-oserror", "chain-ascii"])
def test_material_conversion_detaches_raw_credential_context(tmp_path, monkeypatch, site):
    private = s.marker(tmp_path)
    credential = SimpleNamespace(certificate_pem="leaf", chain_pem="chain")

    def read_credential(_home):
        if site == "credential":
            raise TransportCredentialError("TRANSPORT_STORAGE_UNSAFE", private)
        return credential

    def read_file(path, _mode):
        kind = "key" if path.name == "device-key.pem" else "bundle"
        if site == kind + "-typed":
            raise TransportCredentialError("TRANSPORT_STORAGE_UNSAFE", private)
        if site == kind + "-oserror":
            raise OSError(private)
        return b"synthetic"

    if site == "chain-ascii":
        credential.certificate_pem = private + "é"
    with monkeypatch.context() as scoped:
        scoped.setattr(mtls, "read_transport_credential", read_credential)
        scoped.setattr(mtls, "_read_safe_file", read_file)
        result = s.capture(lambda: load_client_tls_material(tmp_path), private)
    code = "TRANSPORT_STORAGE_UNSAFE" if site.endswith("typed") or site == "credential" else "TRANSPORT_CREDENTIAL_ABSENT"
    assert result == s.expected(code)
