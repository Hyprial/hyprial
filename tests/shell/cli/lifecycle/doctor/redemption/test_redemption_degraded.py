"""Doctor visibility for a degraded pending invite redemption.

The daemon serves while the redemption retries against an unreachable
authority; ``hyprial doctor`` must say so with the exact reason, without a
running daemon (the degraded state is a durable local record).
"""
from __future__ import annotations

from typing import Any

import pytest

import hyprial.shell.impl.cli.commands.doctor.checks.redemption as redemption_module
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_no_record_means_no_check(tmp_path) -> None:
    assert redemption_module.redemption_doctor_check(tmp_path) is None


def test_degraded_redemption_warns_with_the_exact_reason(tmp_path) -> None:
    from hyprial.daemon.impl.forwarding.membership.migration import record_degraded_redemption

    record_degraded_redemption(
        tmp_path, tailnet_id='space-1', invite_id='i1',
        code='AUTHORITY_UNREACHABLE', error='Tailnet membership command failed.',
    )
    check = redemption_module.redemption_doctor_check(tmp_path)
    assert check is not None
    assert check['name'] == 'membership-redemption'
    assert check['status'] == 'warn'
    assert 'AUTHORITY_UNREACHABLE' in check['detail']
    assert 'Tailnet membership command failed.' in check['detail']
    assert 'space-1' in check['detail']
    assert check['metrics']['attempts'] == 1
    assert check['action']['command'] == 'hyprial org migration-status'


def test_permanently_refused_invite_asks_for_a_fresh_invite(tmp_path) -> None:
    from hyprial.daemon.impl.forwarding.membership.migration import record_degraded_redemption

    record_degraded_redemption(
        tmp_path, tailnet_id='space-1', invite_id='old', code='INVITE_EXPIRED',
        error='The recorded invite expired (stage=invite-redemption).',
    )
    check = redemption_module.redemption_doctor_check(tmp_path)
    assert check is not None and check['status'] == 'warn'
    assert 'INVITE_EXPIRED' in check['detail'] and 'stage=invite-redemption' in check['detail']
    assert 'does not replay' in check['detail'] and 'retries' not in check['detail']
    assert check['metrics']['retryable'] is False
    assert check['action']['command'] == 'hyprial org join -'


def test_check_survives_a_corrupt_record(tmp_path) -> None:
    (tmp_path / 'tailnet').mkdir()
    (tmp_path / 'tailnet' / 'redemption-degraded.json').write_text('{not json')
    check = redemption_module.redemption_doctor_check(tmp_path)
    assert check is None


def test_doctor_report_carries_the_degraded_redemption_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any,
) -> None:
    from hyprial.daemon.impl.forwarding.membership.migration import (
        record_degraded_redemption,
    )
    from hyprial.shell.impl.cli.commands.common.services import (
        get_services as _cli_get_services,
    )
    from tests.shell.cli.lifecycle.doctor.doctor_support import (
        checks as _checks,
        doctor as _doctor,
        running_ps as _running_ps,
    )

    record_degraded_redemption(
        tmp_path, tailnet_id='space-1', invite_id='i1',
        code='AUTHORITY_UNREACHABLE', error='Tailnet membership command failed.',
    )
    services = _cli_get_services()
    monkeypatch.setattr(services, '_hyprial_home', lambda: tmp_path)
    monkeypatch.setattr(services, '_state_dir', lambda: tmp_path / 'state')
    result = _doctor(monkeypatch, _running_ps({"listen": [], "connect": []}))
    check = _checks(result)['membership-redemption']
    assert check['status'] == 'warn'
    assert 'AUTHORITY_UNREACHABLE' in str(check['detail'])
