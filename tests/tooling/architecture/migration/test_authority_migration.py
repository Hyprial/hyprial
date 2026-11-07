"""Finite migration identities cannot become permissive authority exemptions."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import re

import pytest

from tests.support.gates.authority_migration import INBOX_CROSSWALK, SANCTIONED_INBOX_OPERATIONS, SUPERVISION_CROSSWALK, method_ast_sha256
from tests.support.gates.inbox_authority_gate import scan_bypasses, validate_manifest
from tests.support.gates.supervision_boundary_gate import validate_supervision_manifest


def write(root: Path, path: str, source: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source)


def manifest(root: Path, entries: list[dict[str, str]], *, supervision: bool = False) -> Path:
    target = root / 'manifest.json'
    target.write_text(json.dumps({'schema_version': 1,
        'policy': 'shrinking-only' if supervision else 'shrinking-only-until-root-composition',
        'exemptions': entries}))
    return target


def test_all_132_records_keep_exact_nonpath_bytes_types_and_metadata() -> None:
    expected = {
        # PR #1577 (dsh-tui interactive carrier): +3 supervision sites, and
        # #1571 (Pi P2 launch trust): +1 startup readiness bound. The
        # #1699/#1701 compatibility names no longer own supervision policy.
        # 2026-10-27: the retired GUI runtime's _READY_TIMEOUT exemption left
        # with the old lifecycle implementation (shrink-only manifest).
        # #2002 adds four foreground source-mode launcher readiness/polling
        # sites for `hyprial gui`; they are launch-scoped, not the old
        # background lifecycle. Preserve #1571 as well: 70 - 1 + 4 = 73.
        # Combine #2048's bounded post-start readiness observation with
        # 2b7f21e7b/8898eb3e9's Lark request-budget false-positive classification
        # and parent-process custody rationale: 73 + 1 + 1 = 75. Preserve both
        # reviewed entries byte-for-byte; this merge adds no exemption.
        'supervision_exemptions.json': (75, 'a4f7474389ab192bf8fe1adf218cc5753601f21ff60d73ede99e70759ec7ee68'),
        'actor_boundary_exemptions.json': (3, '4c426d223750ef7ee1469e896222d5544a782e98169130c0baa6c85df017ab24'),
        # WP8 (send links): +3 wire-contract evaluator cases WG-032/033/037 that seed an
        # isolated InboxService like WG-001..029; the only growth, flagged in #1457.
        # Merged with dev #1519 (+1 manifest-alarm exemption).
        # #1854 rework re-pinned the batch delivery path; count unchanged at 68.
        # #2088 (release 0.5.5.2) inventories two existing typed fetch_pending
        # calls in runtime/reconcile.py through DeliveryCustodyFacade, each with
        # its own reason; no raw InboxService use is added. 68 + 2 = 70.
        'inbox_authority_exemptions.json': (70, 'fe92290ab0f93dc618850df97d732ed7793e6ec94cfd56818a861803a93c87db'),
    }
    for name, (count, digest) in expected.items():
        data = (Path(__file__).resolve().parents[4] / 'tests' / name).read_bytes()
        assert len(json.loads(data)['exemptions']) == count
        assert hashlib.sha256(re.sub(rb'("path"\s*:\s*)"[^"\n]*"', rb'\1"<PATH>"', data)).hexdigest() == digest


def test_supervision_inverse_targets_only_existing_frozen_records() -> None:
    records = json.loads((Path(__file__).resolve().parents[4] / 'tests/supervision_exemptions.json').read_text())['exemptions']
    keys = {(record['path'], record['symbol']) for record in records}
    assert set(SUPERVISION_CROSSWALK.values()) <= keys
    assert len(SUPERVISION_CROSSWALK) == 9
    assert len(set(SUPERVISION_CROSSWALK.values())) == 3


def test_inbox_inverse_targets_only_existing_frozen_records() -> None:
    records = json.loads((Path(__file__).resolve().parents[4] / 'tests/inbox_authority_exemptions.json').read_text())['exemptions']
    keys = {(record['path'], record['owner'], record['operation']) for record in records}
    assert set(INBOX_CROSSWALK.values()) <= keys
    assert len(INBOX_CROSSWALK) == 26
    assert len(set(INBOX_CROSSWALK.values())) == 23


QUERY = 'src/hyprial/daemon/impl/application/messaging/inbox_surface/queries.py'
SEND = 'src/hyprial/daemon/impl/application/messaging/inbox_surface/send.py'
HOST = 'src/hyprial/daemon/impl/application/__init__.py'


def legal_fixture(root: Path) -> tuple[Path, Path]:
    write(root, QUERY, 'class _MessageQueriesMixin:\n    def _ipc_message_ack(self):\n        self._inbox.ack()\n')
    write(root, SEND, 'class _MessageSendMixin:\n    def _ipc_message_reply(self):\n        self._inbox.ack()\n')
    write(root, HOST,
        'from hyprial.daemon.impl.application.messaging.inbox_surface.queries import _MessageQueriesMixin\n'
        'from hyprial.daemon.impl.application.messaging.inbox_surface.send import _MessageSendMixin\n'
        'class DaemonApplication(_MessageQueriesMixin, _MessageSendMixin):\n    pass\n')
    return root / 'src/hyprial', manifest(root, [{'path': QUERY, 'owner': 'DaemonApplication.handle', 'operation': 'ack'}])


def test_exact_native_fanout_preserves_one_lexical_manifest_identity(tmp_path: Path) -> None:
    src, exemptions = legal_fixture(tmp_path)
    assert validate_manifest(src, exemptions) == []
    assert len(INBOX_CROSSWALK) == len(set(INBOX_CROSSWALK))


def test_method_preflight_allows_import_only_but_not_business_drift(tmp_path: Path) -> None:
    path = tmp_path / 'provider.py'
    source = 'class Owner:\n    def known(self):\n        self._inbox.ack()\n'
    path.write_text(source)
    expected = method_ast_sha256(path, 'Owner.known')
    path.write_text(source.replace('self._inbox', 'from hyprial.daemon import InboxService\n        self._inbox'))
    assert method_ast_sha256(path, 'Owner.known') == expected
    path.write_text(source.replace('.ack()', '.close()'))
    assert method_ast_sha256(path, 'Owner.known') != expected
    path.write_text(source + '    def known(self):\n        self._inbox.ack()\n')
    with pytest.raises(ValueError, match='non-unique method AST pin'):
        method_ast_sha256(path, 'Owner.known')


@pytest.mark.parametrize('mutation', ('new_method', 'operation', 'path', 'duplicate', 'host', 'fake_import',
    'missing_fanout', 'fake_assignment', 'shadowed_module_import'))
def test_inbox_mapping_rejects_every_nonidentical_actual_definition(tmp_path: Path, mutation: str) -> None:
    src, exemptions = legal_fixture(tmp_path)
    query = tmp_path / QUERY
    source = query.read_text()
    if mutation == 'new_method':
        source += '    def newly_added(self):\n        self._inbox.ack()\n'
    elif mutation == 'operation':
        source = source.replace('.ack()', '.close()')
    elif mutation == 'path':
        write(tmp_path, QUERY.replace('queries.py', 'neighbor.py'), source)
        source = ''
    elif mutation == 'duplicate':
        source += '    def _ipc_message_ack(self):\n        self._inbox.ack()\n'
    elif mutation == 'host':
        write(tmp_path, HOST, 'class DaemonApplication:\n    pass\n')
    elif mutation == 'fake_import':
        write(tmp_path, HOST, 'class _MessageQueriesMixin:\n    pass\n'
            'class _MessageSendMixin:\n    pass\n'
            'class DaemonApplication(_MessageQueriesMixin, _MessageSendMixin):\n    pass\n')
    elif mutation in ('fake_assignment', 'shadowed_module_import'):
        original = (tmp_path / HOST).read_text()
        shadow = ('_MessageQueriesMixin = object\n' if mutation == 'fake_assignment'
                  else 'import fake as _MessageQueriesMixin\n')
        write(tmp_path, HOST, original.replace('class DaemonApplication(', shadow + 'class DaemonApplication('))
    else:
        write(tmp_path, SEND, 'class _MessageSendMixin:\n    pass\n')
    query.write_text(source)
    assert validate_manifest(src, exemptions), mutation


@pytest.mark.parametrize('mutation', ('new_method', 'operation', 'path', 'duplicate'))
def test_sanctioned_implementation_is_not_a_file_or_package_waiver(tmp_path: Path, mutation: str) -> None:
    path = 'src/hyprial/daemon/impl/inbox/actor/custody/notices.py'
    klass = 'DeliveryCustodyNoticesMixin'
    source = f'class {klass}:\n    def _stage_expired_sender_notices(self):\n        self._service.receive_system_notice()\n'
    host = 'src/hyprial/daemon/impl/inbox/actor/custody/__init__.py'
    write(tmp_path, host, f'from .notices import {klass}\nclass DeliveryCustody({klass}):\n    pass\n')
    write(tmp_path, path, source)
    src, exemptions = tmp_path / 'src/hyprial', manifest(tmp_path, [])
    assert validate_manifest(src, exemptions) == []
    if mutation == 'new_method':
        source += '    def bypass(self):\n        self._service.receive_system_notice()\n'
    elif mutation == 'operation':
        source = source.replace('receive_system_notice()', 'close()')
    elif mutation == 'path':
        path = path.replace('notices.py', 'neighbor.py')
    else:
        source += '    def _stage_expired_sender_notices(self):\n        self._service.receive_system_notice()\n'
    write(tmp_path, path, source)
    assert validate_manifest(src, exemptions), mutation
    assert all(len(key) == 3 for key in SANCTIONED_INBOX_OPERATIONS)


@pytest.mark.parametrize('mutation', ('new_method', 'kind', 'path', 'duplicate'))
def test_supervision_fanout_is_exact_not_a_symbol_name_allowlist(tmp_path: Path, mutation: str) -> None:
    primary = 'src/hyprial/daemon/impl/adapters/lark/runtime/runtime.py'
    secondary = 'src/hyprial/daemon/impl/adapters/lark/runtime/effects.py'
    source = 'class _ProcessEffects:\n    def __init__(self, start_confirm_timeout):\n        pass\n'
    write(tmp_path, primary, 'class AdapterRuntime:\n    def __init__(self, start_confirm_timeout):\n        pass\n')
    write(tmp_path, secondary, source)
    src = tmp_path / 'src/hyprial'
    exemptions = manifest(tmp_path, [{'path': primary, 'symbol': 'start_confirm_timeout',
        'kind': 'parameter', 'reason': 'fixture', 'owner': 'fixture', 'card': 'fixture'}], supervision=True)
    assert validate_supervision_manifest(src, exemptions) == []
    if mutation == 'new_method':
        source += '    def added(self, start_confirm_timeout):\n        pass\n'
    elif mutation == 'kind':
        source += '    start_confirm_timeout: float\n'
    elif mutation == 'path':
        secondary = secondary.replace('effects.py', 'neighbor.py')
    else:
        source += '    def __init__(self, start_confirm_timeout):\n        pass\n'
    write(tmp_path, secondary, source)
    assert validate_supervision_manifest(src, exemptions), mutation


@pytest.mark.parametrize('provider', ('hyprial.daemon', 'hyprial.daemon.impl.inbox'))
def test_actual_public_inbox_alias_close_is_detected(tmp_path: Path, provider: str) -> None:
    write(tmp_path, 'src/hyprial/consumer.py', f'from {provider} import InboxService as RoutineService\n'
        'def use():\n    service = RoutineService(database)\n    service.close()\n')
    assert {item.operation for item in scan_bypasses(tmp_path / 'src/hyprial')} == {'construct', 'close'}


@pytest.mark.parametrize('mutation', ('none', 'wrong_import', 'rebound', 'field_rebound', 'shadowed',
    'unrelated_operation', 'local_import_shadow', 'unrelated_function_import'))
def test_noninbox_close_requires_real_lexical_type_evidence(tmp_path: Path, mutation: str) -> None:
    source = ('from hyprial.biz import RoutineService\n'
        'from hyprial.daemon import InboxService\n'
        'class RoutineServiceAdapter:\n'
        '    def __init__(self, service: RoutineService):\n'
        '        self.service = service\n'
        '    def close(self):\n        return self.service.close()\n'
        'def factory():\n    service = RoutineService()\n    service.close()\n')
    if mutation == 'wrong_import':
        source = source.replace('from hyprial.biz import RoutineService', 'from fake import RoutineService')
    elif mutation == 'rebound':
        source = source.replace('service = RoutineService()\n', 'service = RoutineService()\n    service = InboxService()\n')
    elif mutation == 'field_rebound':
        source = source.replace('self.service = service', 'self.service = InboxService()')
    elif mutation == 'shadowed':
        source = source.replace('def factory():', 'def factory(RoutineService):')
    elif mutation == 'unrelated_operation':
        source = source.replace('self.service.close()', 'self.service.ack()')
    elif mutation == 'local_import_shadow':
        source = source.replace('def factory():', 'def factory():\n    from fake import RoutineService')
    elif mutation == 'unrelated_function_import':
        source = source.replace('from hyprial.biz import RoutineService\n', '')
        source += 'def unrelated():\n    from hyprial.biz import RoutineService\n'
    write(tmp_path, 'src/hyprial/consumer.py', source)
    findings = scan_bypasses(tmp_path / 'src/hyprial')
    assert bool(findings) is (mutation != 'none'), findings
