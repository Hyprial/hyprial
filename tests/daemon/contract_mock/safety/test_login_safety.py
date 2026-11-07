"""Mock credential writes accept only documented synthetic material."""

import pytest
from contract_mock.contract import load_contract
from contract_mock.envelope import Envelope
from contract_mock.sensitive import SENTINEL, SENTINEL_B64
from contract_mock.service import MockService


@pytest.mark.parametrize("credential", [
    {"setupToken": "ordinary-secret"}, {"setupToken": None},
    {"files": [{"filename": "auth.json", "contentBase64": "e30="}]},
    {"files": []}, {},
    {"setupToken": SENTINEL, "files": [{"filename": "auth.json", "contentBase64": "e30="}]},
])
def test_mock_refuses_non_synthetic_login_without_echo(credential):
    contract = load_contract()
    service = MockService(contract, Envelope(contract), scenario="success", token="test")
    response = service.handle("agent.provider.login", {"actor": "example-agent", "provider": "claude", **credential}, "Bearer test")
    assert response.status == 400
    assert response.payload["dispatch"] == "not-dispatched"
    assert "ordinary-secret" not in str(response.payload)


@pytest.mark.parametrize("credential", [
    {"setupToken": SENTINEL},
    {"files": [{"filename": "auth.json", "contentBase64": SENTINEL_B64}]},
])
def test_mock_accepts_documented_sentinel(credential):
    contract = load_contract()
    service = MockService(contract, Envelope(contract), scenario="success", token="test")
    assert service.handle("agent.provider.login", {"actor": "example-agent", "provider": "claude", **credential}, "Bearer test").status == 200
