#!/usr/bin/env python3
"""Isolated PAC v2 integration scenario; real store/reactor, recorded delivery port.

Run with the Python interpreter that has hyprial installed. No daemon, model,
external inbox, production home, or legacy workflow database is used.
"""
import argparse
import json
import os
from pathlib import Path

from hyprial.home import reject_retired_environment
from importlib.metadata import version


def main():
    reject_retired_environment()
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True, help='new evidence directory')
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    for key in list(os.environ):
        if key.startswith(('HARNESS_', 'HYPRIAL_')):
            del os.environ[key]
    os.environ['HYPRIAL_HOME'] = str(root / 'home')
    os.environ['HYPRIAL_OWNER'] = 'pac-test'
    os.environ['HARNESS_STATE_DIR'] = str(root / 'state')
    state = root / 'state'
    state.mkdir()
    (root / 'home').mkdir()

    from hyprial.pac.context import node_context
    from hyprial.pac.errors import PacError
    from hyprial.pac.graph import create_graph, add_node, add_edge, activate_graph
    from hyprial.pac.reactor import NullSender, PacReactor
    from hyprial.pac.store import PacGraphStore
    from hyprial.pac.subscription import snapshot

    database = state / 'pac-graph.sqlite3'
    store = PacGraphStore(database)
    class RecordingSender(NullSender):
        def __init__(self):
            super().__init__()
            self.fail_once = True
            self.attempts = []

        def send(self, **kwargs):
            self.attempts.append(dict(kwargs))
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError('simulated delivery outage')
            return super().send(**kwargs)

    sender = RecordingSender()
    reactor = PacReactor(store, sender=sender)
    checks, contexts = [], {}

    def check(name, condition):
        assert condition, name
        checks.append(name)

    def rejects(name, code, action):
        try:
            action()
        except PacError as error:
            check(name, error.code == code)
        else:
            raise AssertionError(name + ': mutation unexpectedly accepted')

    graph = create_graph(store, name='dsh-message-termination', created_by='agent:pac-test:local:coordinator', operation_key='dsh-terminal-test-v1')
    gid, revision = graph['graphId'], graph['version']
    check('keyed creation does not create another task', create_graph(store, name='dsh-message-termination', created_by='agent:pac-test:local:coordinator', operation_key='dsh-terminal-test-v1')['graphId'] == gid)
    nodes = [('dispatch', 'coordinator', 'task'), ('implement', 'worker', 'task'),
             ('review', 'verifier', 'task'), ('rework', 'verifier', 'task'),
             ('finish', 'coordinator', 'end')]
    for name, owner, kind in nodes:
        result = add_node(store, state, graph_id=gid, node_id=name, owner='agent:pac-test:local:' + owner, kind=kind,
                          brief_ref=f'docs/pac/message-termination.md#{name}', expect_version=revision)
        revision = result['version']
    rejects('stale structure edit rejected', 'PAC_GRAPH_VERSION_CONFLICT', lambda: add_edge(store, graph_id=gid, from_node='dispatch', to_node='implement', expect_version=1))
    for a, b, kind in [('dispatch', 'implement', 'forward'), ('implement', 'review', 'forward'),
                       ('rework', 'implement', 'back'), ('review', 'finish', 'forward')]:
        revision = add_edge(store, graph_id=gid, from_node=a, to_node=b, kind=kind, expect_version=revision)['version']
    activate_graph(store, gid, actor='agent:pac-test:local:coordinator')
    dispatch = reactor.set_flag(gid, 'dispatch', actor='agent:pac-test:local:coordinator', reason_ref='evidence/dispatch.json')
    check('failed notification remains durably undelivered', len(dispatch.undelivered) == 1 and not sender.sent)
    reactor.close()
    store = PacGraphStore(database)
    reactor = PacReactor(store, sender=sender)
    reactor.resend_undelivered(gid)
    check('restart retries exact notification identity and payload', len(sender.sent) == 1 and sender.attempts[0] == sender.attempts[1])
    first = node_context(database, gid, 'implement')
    contexts['firstImplementation'] = first
    check('dispatch creates worker activation', first['currentActivation'] is not None and first['owner'] == 'agent:pac-test:local:worker')
    rejects('verifier cannot complete worker task', 'PAC_FLAG_NOT_OWNER', lambda: reactor.set_flag(gid, 'implement', actor='agent:pac-test:local:verifier'))
    before = snapshot(database, gid)
    # Neither chat text nor an ACK is passed to the PAC reactor.
    transcript = [{'intent': 'reply', 'message': '完成，无动作，待命', 'acknowledged': True}]
    (root / 'chat-fixture.json').write_text(json.dumps(transcript, ensure_ascii=False))
    check('chat result and ACK do not advance task state', snapshot(database, gid) == before)
    reactor.set_flag(gid, 'implement', actor='agent:pac-test:local:worker', reason_ref='evidence/head-a-tests.json')
    old_review = node_context(database, gid, 'review')
    contexts['reviewBeforeRework'] = old_review
    check('implementation activates verifier', old_review['currentActivation'] is not None)
    count = len(sender.sent)
    rejects('duplicate completion cannot fan out again', 'PAC_FLAG_ALREADY_SET', lambda: reactor.set_flag(gid, 'implement', actor='agent:pac-test:local:worker'))
    reactor.resend_undelivered(gid)
    check('delivered notifications are not resent', len(sender.sent) == count)

    # Conditional choice belongs to the reviewer, not automatic graph branching:
    # rejection completes rework, approval completes review, never both.
    reactor.set_flag(gid, 'rework', actor='agent:pac-test:local:verifier', reason_ref='evidence/review-head-a-rejected.json')
    second = node_context(database, gid, 'implement')
    contexts['implementationReentered'] = second
    check('back edge creates a new activation despite historical completed flag', second['flag'] and second['currentActivation'] is not None and second['currentActivation']['activationId'] != first['currentActivation']['activationId'])
    reactor.reset_flag(gid, 'implement', actor='agent:pac-test:local:worker', reason_ref='evidence/rework-head-b.json')
    withdrawn = node_context(database, gid, 'review')
    check('withdrawn old review notification is not current work', withdrawn['currentActivation'] is None)
    reactor.set_flag(gid, 'implement', actor='agent:pac-test:local:worker', reason_ref='evidence/head-b-tests.json')
    current_review = node_context(database, gid, 'review')
    contexts['reviewAfterRework'] = current_review
    old_requests = {r['eventId'] for r in old_review['currentActivation']['requests']}
    new_requests = {r['eventId'] for r in current_review['currentActivation']['requests']}
    check('withdraw/reissue may reuse activation id', current_review['currentActivation']['activationId'] == old_review['currentActivation']['activationId'])
    check('stale review notification is excluded from current requests', old_requests.isdisjoint(new_requests))
    check('current review references latest implementation evidence', current_review['predecessors'][0]['reasonRef'] == 'evidence/head-b-tests.json')
    check('context revision stays structural while cursor advances', current_review['version'] == old_review['version'] and current_review['cursor'] > old_review['cursor'])
    reactor.set_flag(gid, 'review', actor='agent:pac-test:local:verifier', reason_ref='evidence/review-head-b-approved.json')
    reactor.set_flag(gid, 'finish', actor='agent:pac-test:local:coordinator', reason_ref='evidence/closed.json')
    final = snapshot(database, gid)
    check('end node closes graph', final['closed'] is not None)
    count = len(sender.sent)
    rejects('closed task cannot be reopened by old work', 'PAC_GRAPH_CLOSED', lambda: reactor.reset_flag(gid, 'implement', actor='agent:pac-test:local:worker'))
    check('terminal state produces no further notifications', len(sender.sent) == count)
    report = {'ok': True, 'mode': 'real-pac-engine/local-recording-port/simulated-roles',
              'hyprialVersion': version('hyprial'), 'graphId': gid, 'database': str(database),
              'checks': checks, 'contexts': contexts, 'final': final, 'notifications': sender.sent,
              'limitation': 'No independent agent review, live daemon delivery, GUI migration or production deployment.'}
    (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    reactor.close()
    print(json.dumps({'ok': True, 'checks': len(checks), 'graphId': gid, 'report': str(root / 'report.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
