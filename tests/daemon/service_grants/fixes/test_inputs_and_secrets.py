"""Grant inputs: secret reading through identity, names, notes and identifiers.

Decision (service grant S3 fix, 2026-11-26, critic S4, S6, N4 and S7 items 9
and 10): the daemon reads a user-provider value only through identity's public
``read_user_provider_value`` and keeps no record parser of its own. ``name``
follows the service-connect service-name rule, ``note`` is at most 512
characters without control characters, and owner and device identifiers follow
the S2 rule before any secret is read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import hyprial.identity as identity
from hyprial.daemon.impl.service_grants import ServiceGrantService
from hyprial.daemon.impl.service_grants import models as grant_models
from hyprial.daemon.impl.service_grants import service as grant_service
from hyprial.daemon.impl.service_connect import models as connect_models
from hyprial.kernel import DaemonRequestError, ipc_errors, write_private_file_atomic

from tests.daemon.service_grants.test_service_grants_core import (
    ORG,
    SYNTHETIC_SECRET,
    _write_user_provider,
    harness,
)

_FIXTURES = (harness,)


def _write_raw_provider(home: Path, entry_id: str, document: object) -> None:
    _write_user_provider(home, "placeholder", {"K": "v"})
    write_private_file_atomic(
        home / "secrets" / "providers" / f"{entry_id}.json",
        json.dumps(document).encode("utf-8"),
    )


def _refused(service: ServiceGrantService, **kwargs: Any) -> DaemonRequestError:
    with pytest.raises(DaemonRequestError) as caught:
        service.grant(org=ORG, port=8447, **kwargs)
    return caught.value


def test_identity_exports_one_user_provider_reader_and_daemon_has_no_parser() -> None:
    """S4: the reader is identity's public function; the daemon copy is gone."""

    assert callable(identity.read_user_provider_value)
    assert "read_user_provider_value" in identity.__all__
    for name in ("_read_user_provider_secret", "_extract_provider_values", "_validate_provider_ancestors"):
        assert not hasattr(grant_service, name)


def test_identity_reader_selects_one_field_with_identity_rules(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    _write_user_provider(home, "gateway", {"API_KEY": SYNTHETIC_SECRET, "OTHER": "x"})
    assert identity.read_user_provider_value(home, "gateway", "API_KEY") == SYNTHETIC_SECRET
    with pytest.raises(identity.SecretResolutionError) as missing:
        identity.read_user_provider_value(home, "absent", "API_KEY")
    assert SYNTHETIC_SECRET not in str(missing.value)


@pytest.mark.parametrize(
    "field_name",
    [pytest.param(None, id="no-field"), pytest.param("K", id="field")],
)
def test_configured_record_with_invalid_routes_is_refused(
    harness: dict[str, Any], field_name: str | None
) -> None:
    """S4 divergence 1 and S7 item 10: identity's record validation applies."""

    _write_raw_provider(
        harness["home"],
        "configured",
        {"$schema": "hyprial-model-secret/v2", "values": {"K": SYNTHETIC_SECRET}, "routes": {"K": {"bogus": True}}},
    )
    credential = {"entryId": "configured"} if field_name is None else {"entryId": "configured", "fieldName": field_name}
    error = _refused(harness["service"], device="bob/device-b", credential=credential)
    assert error.code == ipc_errors.AGENT_SECRET_RESOLUTION_FAILED
    assert SYNTHETIC_SECRET not in str(error)


def test_configured_record_without_field_name_requires_one(harness: dict[str, Any]) -> None:
    """S7 item 10: a configured record is ambiguous without ``fieldName``."""

    _write_raw_provider(
        harness["home"],
        "configured",
        {
            "$schema": "hyprial-model-secret/v1",
            "values": {"K": SYNTHETIC_SECRET},
            "routes": {"K": {"endpoint": "https://models.example.invalid/v1", "protocol": "openai-completions", "vendorId": "deepseek"}},
        },
    )
    error = _refused(harness["service"], device="bob/device-b", credential={"entryId": "configured"})
    assert error.code == ipc_errors.AGENT_SECRET_RESOLUTION_FAILED
    assert "field-required" in str(error)
    assert SYNTHETIC_SECRET not in str(error)


def test_value_with_lone_surrogate_is_a_request_error(harness: dict[str, Any]) -> None:
    """S4 divergence 2: no ``UnicodeEncodeError`` escapes and no value is echoed."""

    _write_raw_provider(harness["home"], "surrogate", {"K": "\ud800" + SYNTHETIC_SECRET})
    error = _refused(harness["service"], device="bob/device-b", credential={"entryId": "surrogate", "fieldName": "K"})
    assert error.code == ipc_errors.AGENT_SECRET_RESOLUTION_FAILED
    assert SYNTHETIC_SECRET not in str(error)


def test_service_name_reuses_the_service_connect_rule(harness: dict[str, Any]) -> None:
    """S6: one service-name rule, shared with service-connect."""

    assert grant_models.SERVICE_NAME_PATTERN is connect_models._IDENTIFIER
    service: ServiceGrantService = harness["service"]
    for bad in ("../x\ny" + "z" * 5000, "Gateway", "a" * 64, "-gw", "gw.local"):
        assert _refused(service, device="@members", name=bad).code == ipc_errors.INVALID_ARGUMENT
    accepted = service.grant(org=ORG, port=8447, device="@members", name="a" * 63)
    assert accepted["grant"]["service"]["name"] == "a" * 63


def test_note_is_capped_at_512_characters_without_control_characters(harness: dict[str, Any]) -> None:
    service: ServiceGrantService = harness["service"]
    for bad in ("n" * 513, "n" * 200000, "line\nbreak", "bell\x07", "c1\x85"):
        assert _refused(service, device="@members", note=bad).code == ipc_errors.INVALID_ARGUMENT
    accepted = service.grant(org=ORG, port=8447, device="@members", note="é" * 512)
    assert accepted["grant"]["service"]["note"] == "é" * 512


@pytest.mark.parametrize(
    "device",
    [
        pytest.param("bob/@device-b", id="selector-device"),
        pytest.param("bob/" + "d" * 257, id="device-257"),
        pytest.param("bob/dev\ud800", id="surrogate-device"),
    ],
)
def test_identifiers_follow_the_s2_rule_before_any_secret_is_read(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch, device: str
) -> None:
    """N4: an identifier S2 would refuse fails as INVALID_ARGUMENT before the secret is read."""

    def forbidden(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("secret read before identifier validation")

    monkeypatch.setattr(grant_service, "read_user_provider_value", forbidden, raising=False)
    _write_user_provider(harness["home"], "gateway", {"API_KEY": SYNTHETIC_SECRET})
    error = _refused(harness["service"], device=device, credential={"entryId": "gateway", "fieldName": "API_KEY"})
    assert error.code == ipc_errors.INVALID_ARGUMENT
