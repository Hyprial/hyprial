"""Item E: the sidecar's error code reaches the receipt; the message stays secret-free."""
import json
from datetime import datetime

import pytest

from hyprial.daemon.impl.forwarding.membership.migration import initialize_once, receipt_path
from hyprial.daemon.impl.forwarding.policy import ForwardingSidecarError

PARAMS = {'operationId': 'init', 'tailnetId': 'space', 'authority': {},
          'migration': {'source': 'directory-0.5.5', 'sourceDigest': 'digest', 'completed': True},
          'members': []}


def test_failed_init_records_last_error_code_and_time_in_the_receipt(tmp_path):
    def refusing(op, params):
        raise ForwardingSidecarError('Tailnet membership command failed.', code='MEMBER_CONFLICT')
    with pytest.raises(ForwardingSidecarError) as caught:
        initialize_once(tmp_path, PARAMS, refusing, initialized=False)
    assert caught.value.code == 'MEMBER_CONFLICT'
    receipt = json.loads(receipt_path(tmp_path, 'space').read_text())
    assert receipt['state'] == 'pending'
    assert receipt['lastError']['code'] == 'MEMBER_CONFLICT'
    datetime.fromisoformat(receipt['lastError']['at'])  # ISO-8601 timestamp
    assert set(receipt['lastError']) == {'code', 'at'}


def test_successful_replay_clears_last_error(tmp_path):
    def refusing(op, params):
        raise ForwardingSidecarError('Tailnet membership command failed.', code='AUTHORITY_UNREACHABLE')
    with pytest.raises(ForwardingSidecarError):
        initialize_once(tmp_path, PARAMS, refusing, initialized=False)
    initialize_once(tmp_path, PARAMS, lambda op, params: {}, initialized=False)
    receipt = json.loads(receipt_path(tmp_path, 'space').read_text())
    assert receipt['state'] == 'complete' and receipt.get('lastError') is None
