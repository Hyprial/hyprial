"""The isolated send declaration must export examples the Contract Mock loader accepts."""

import json
from pathlib import Path

from contract_mock import load_contract
import pytest

from hyprial.daemon.impl.inbox.contracts.summary import check_summary
from hyprial.daemon.impl.request_catalog.message.send import (
    MESSAGE_SEND_EXAMPLES,
    MESSAGE_SEND_KEY_LOOKUP_EXAMPLES,
    MESSAGE_SEND_KEY_LOOKUP_SPEC,
    MESSAGE_SEND_SPEC,
)
from hyprial.kernel import DaemonRequestError, export_openapi, ipc_errors


def test_http_openapi_examples_load_and_match_the_handler_error(tmp_path: Path) -> None:
    document = export_openapi(
        (MESSAGE_SEND_SPEC,), example_sets=(MESSAGE_SEND_EXAMPLES,),
    )
    path = tmp_path / "message-send-openapi.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    contract = load_contract(path)
    selected = contract.case("message.send", "summary-too-long")
    with pytest.raises(DaemonRequestError) as actual:
        check_summary(selected["params"]["summary"], required=True)
    assert selected["error"]["code"] == actual.value.code
    assert selected["error"]["data"] == actual.value.data
    assert selected["error"]["code"] == ipc_errors.SUMMARY_REQUIRED
    assert selected["error"]["data"] == {"summaryBytes": 513, "maxBytes": 512}


def test_key_lookup_openapi_preserves_the_unknown_result(tmp_path: Path) -> None:
    document = export_openapi(
        (MESSAGE_SEND_KEY_LOOKUP_SPEC,), example_sets=(MESSAGE_SEND_KEY_LOOKUP_EXAMPLES,),
    )
    path = tmp_path / "message-send-key-lookup-openapi.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    contract = load_contract(path)
    case = contract.case("message.send.key_lookup", "not-found")
    assert case["result"] == {"status": "unknown", "reason": "not-found"}
