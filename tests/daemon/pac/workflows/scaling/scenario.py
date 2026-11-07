"""A deterministic decision transcript captured from the released tick code."""

from __future__ import annotations

from collections import Counter
import json

import yaml

from hyprial.daemon.impl.inbox.links.io import InboxIoDeferred
from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService

from .support import USER, WORKER, Sender


class TransientSender(Sender):
    def __init__(self):
        super().__init__()
        self.fail_deadline = True

    def send(self, **kwargs):
        result = super().send(**kwargs)
        if kwargs['recipient'] == 'user:deadline' and self.fail_deadline:
            self.fail_deadline = False
            raise InboxIoDeferred('one transient alert delivery failure')
        return result


def transcript(path):
    sender, clock = TransientSender(), [1000]
    service = GraphWorkflowService(
        state_dir=path, owner='tick-test', machine='machine', sender=sender,
        clock_ms=lambda: clock[0], start_thread=False,
    )
    labels = {}
    snapshots = []

    def create(label, nodes, policy='terminate', escalation=None):
        specification = {'version': 2, 'name': label, 'nodes': nodes, 'on_failure': policy}
        if escalation:
            specification['escalate_to'] = escalation
        graph = service.start(yaml_text=yaml.safe_dump(specification), sender=USER,
                              operation_key=label)['graphId']
        labels[graph] = label
        return graph

    def snapshot(name, at):
        clock[0] = at
        service._tick(at)
        store = PacGraphStore(service.database, read_only=True)
        try:
            graphs = []
            for graph_id, label in labels.items():
                graph = store.graph(graph_id)
                meta = store._db.execute('SELECT state,reason_ref FROM workflow_graphs WHERE graph_id=?', (graph_id,)).fetchone()
                rows = store._db.execute(
                    'SELECT node_id,state,reason_ref,deadline_ms,generation,request_id IS NOT NULL AS requested '
                    'FROM workflow_nodes WHERE graph_id=? ORDER BY node_id', (graph_id,),
                ).fetchall()
                graphs.append({'label': label, 'version': graph['version'], 'state': meta['state'],
                               'reason': meta['reason_ref'], 'closedAt': graph['closed_at'],
                               'nodes': [dict(row) for row in rows]})
            notifications = []
            for row in store._db.execute('SELECT * FROM notifications'):
                graph_id = json.loads(row['plan_json'])['graphId']
                notifications.append({'graph': labels[graph_id], 'kind': row['kind'],
                                      'recipient': row['recipient'], 'at': row['at'],
                                      'delivered': row['message_id'] is not None,
                                      'failedAt': row['failed_at'], 'failureCode': row['failure_code']})
            snapshots.append({'name': name, 'graphs': sorted(graphs, key=lambda item: item['label']),
                              'notifications': sorted(notifications, key=lambda item: json.dumps(item, sort_keys=True)),
                              'sendAttemptsByRecipient': dict(sorted(Counter(item['recipient'] for item in sender.sent).items()))})
        finally:
            store.close()

    def flag(graph_id, node_id, at, reset=False):
        clock[0] = at
        store = PacGraphStore(service.database)
        try:
            reactor = PacReactor(store, clock=lambda: at)
            if reset:
                reactor.reset_flag(graph_id, node_id, actor=WORKER, reason_ref='test:withdraw')
            else:
                request = next(item['requestId'] for item in service.status(run_id=graph_id)['nodes'] if item['nodeId'] == node_id)
                reactor.set_flag(graph_id, node_id, actor=WORKER, expected_request=request,
                                 reason_ref='test:accepted', output_text='accepted result')
        finally:
            store.close()

    try:
        task = {'id': 'work', 'task': 'work', 'owner': WORKER, 'timeout': '1h'}
        create('deadline', [{**task, 'timeout': '1s'}], escalation='user:deadline')
        create('held', [{**task, 'timeout': '1s'}], policy='hold', escalation='user:held')
        manual = create('manual-close', [task])
        inputs = create('inputs', [{**task, 'id': 'first'}, {**task, 'id': 'second', 'after': ['first']}])
        restarted = create('restart-input', [task])
        snapshot('requested', 1000)
        snapshot('stable', 1500)
        flag(inputs, 'first', 1600)
        snapshot('downstream-requested', 1610)
        flag(inputs, 'first', 1700, reset=True)
        snapshot('withdrawn-and-reissued', 1710)
        snapshot('stable-after-reset', 1720)
        flag(inputs, 'first', 1800)
        snapshot('new-downstream-request', 1810)
        flag(inputs, 'second', 1900)
        snapshot('accepted-close', 1910)
        store = PacGraphStore(service.database)
        try:
            # Legacy close and journal-less restart are real committed DB inputs.
            with store.write():
                store._db.execute('UPDATE graphs SET closed_at=1950,closed_by=? WHERE graph_id=?', (USER, manual))
        finally:
            store.close()
        snapshot('external-close', 1960)
        snapshot('deadline-equality', 2000)
        snapshot('deadline-expired-alert-pending', 2001)
        snapshot('closed-alert-retried', 2100)
        snapshot('stable-held', 2200)
        store = PacGraphStore(service.database)
        try:
            with store.write():
                store._db.execute("UPDATE workflow_nodes SET state='pending',request_id=NULL,input_token=NULL,reason_ref=NULL,output_text=NULL WHERE graph_id=?", (restarted,))
        finally:
            store.close()
        snapshot('journal-less-restart', 2301)
        snapshot('stable-after-restart', 2400)
        return snapshots
    finally:
        assert service.close()
