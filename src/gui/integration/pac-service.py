#!/usr/bin/env python3
"""DSH adapter over installed PAC v2. JSON stdin/stdout, no shell or model launch.

Only managed graphs are exposed. Role bindings come from operator-owned config;
actor/session come from the JS bridge, never from native tool arguments.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from hyprial.pac.context import node_context
from hyprial.pac.errors import PacError
from hyprial.pac.graph import create_graph, add_node, add_edge, activate_graph, close_graph
from hyprial.pac.reactor import PacReactor
from hyprial.pac.store import PacGraphStore
from hyprial.pac.subscription import snapshot


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def fail(code, message):
    raise PacError(code, message)


def atomic(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.pac-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class GuardedStore(PacGraphStore):
    """Check expected work while holding the SAME SQLite writer lock as _flag.

    No PAC schema changes. Deliberately small adapter to the installed store's
    write() seam; contract tests cover stale writes and external-writer races.
    """
    guard = None

    def write(self):
        db = super().write()
        guard, self.guard = self.guard, None
        try:
            if guard:
                guard()
        except BaseException:
            db.rollback()
            raise
        return db


class Service:
    def __init__(self, state):
        self.state = Path(state)
        self.root = self.state / 'dsh-pac'
        self.config = json.loads((self.root / 'config.json').read_text())
        if self.config.get('version') != 1 or self.config.get('enabled') is not True:
            fail('PAC_DISABLED', 'PAC GUI automation is disabled')
        self.database = self.state / 'pac-graph.sqlite3'
        self.store = GuardedStore(self.database)
        # Delivery is the persistent PAC assignment itself, consumed directly by
        # the Host. No chat send and no fabricated transport delivery receipt.
        self.reactor = PacReactor(self.store)

    def manifests(self):
        return [(p, json.loads(p.read_text())) for p in sorted(self.root.glob('task-*.json'))]

    def managed(self, gid):
        for path, doc in self.manifests():
            if doc.get('graphId') == gid:
                return path, doc
        fail('PAC_NOT_MANAGED', 'graph is not managed by this GUI adapter')

    def member(self, doc, request, role=None):
        candidates = [doc['roles'][role]] if role else doc['roles'].values()
        if not any(r['actor'] == request.get('actor') and r['sessionId'] == request.get('sessionId') for r in candidates):
            fail('PAC_NOT_OWNER', 'this session does not hold the required task role')

    def token(self, gid, state=None):
        state = state if state is not None else snapshot(self.database, gid)
        # Cursor/delivery timestamps are not work revisions. Include request IDs
        # and completed facts: activationId alone can survive withdraw/reissue.
        assignments = [{k: item[k] for k in ('nodeId', 'owner', 'activationId')} | {
            'requests': sorted((r['eventId'], r['edge']) for r in item['requests'])
        } for item in state['assignments']]
        return digest({'journalId': state['journalId'], 'version': state['version'], 'closed': state['closed'],
                       'flags': state['flags'], 'assignments': assignments})

    def context(self, doc, node):
        # Both work context and token are derived from ONE public snapshot.
        state = snapshot(self.database, doc['graphId'])
        nodes = {n['nodeId']: n for n in state['structure']['nodes']}
        if node not in nodes:
            fail('PAC_NODE_NOT_FOUND', 'unknown task node')
        definition = nodes[node]
        predecessors = sorted(e['from'] for e in state['structure']['edges'] if e['to'] == node and e['kind'] == 'forward')
        context = {'graphId': doc['graphId'], 'nodeId': node, 'version': state['version'],
                   'journalId': state['journalId'], 'cursor': state['cursor'],
                   'owner': definition['owner'], 'kind': definition['kind'], 'briefRef': definition['briefRef'],
                   **state['flags'][node], 'active': state['active'], 'closed': state['closed'],
                   'currentActivation': next((a for a in state['assignments'] if a['nodeId'] == node), None),
                   'predecessors': [{'nodeId': p, 'owner': nodes[p]['owner'], 'briefRef': nodes[p]['briefRef'], **state['flags'][p]} for p in predecessors]}
        return {**context, 'expectedToken': self.token(doc['graphId'], state), 'taskKey': doc['taskKey'],
                'title': doc['title'], 'brief': doc['brief'], 'roles': doc['roles'],
                'wakes': doc.get('wakes', {}), 'cancellation': doc.get('cancellation'), 'deliveryMode': 'dsh-host-assignment'}

    def expect(self, gid, token):
        if not token or self.token(gid) != token:
            fail('PAC_STALE_WORK', 'work changed; reread context and do not apply an old result')

    def create(self, request):
        self.member(self.config, request, 'coordinator')
        args = request['args']
        for key, limit in [('taskKey', 200), ('title', 200), ('brief', 64000)]:
            if not isinstance(args.get(key), str) or not args[key].strip() or len(args[key]) > limit:
                fail('PAC_INVALID_TASK', f'invalid {key}')
        source = request.get('source', 'local')
        key = 'dsh-pac:' + digest([request['actor'], source, args['taskKey']])
        path = self.root / ('task-' + digest(key) + '.json')
        spec = {'taskKey': args['taskKey'], 'title': args['title'], 'brief': args['brief'],
                'roles': self.config['roles'], 'source': source, 'operationKey': key}
        if path.exists():
            doc = json.loads(path.read_text())
            if any(doc.get(k) != v for k, v in spec.items()):
                fail('PAC_TASK_KEY_CONFLICT', 'taskKey already names a different task; inspect existing task')
        else:
            doc = {**spec, 'wakes': {}}
            atomic(path, doc)
        graph = create_graph(self.store, name=args['title'], created_by=request['actor'], operation_key=key)
        gid = graph['graphId']
        doc['graphId'] = gid
        atomic(path, doc)
        if self.store.graph(gid)['closed_at'] is not None:
            return {'ok': True, 'graphId': gid, 'existing': True}
        owners = {k: v['actor'] for k, v in doc['roles'].items()}
        nodes = [('dispatch', 'coordinator', 'task'), ('implement', 'worker', 'task'),
                 ('review', 'verifier', 'task'), ('rework', 'verifier', 'task'), ('finish', 'coordinator', 'end')]
        for node, role, kind in nodes:
            existing = self.store.node(gid, node)
            if not existing:
                add_node(self.store, self.state, graph_id=gid, node_id=node, owner=owners[role], kind=kind,
                         brief_ref=str(path) + '#' + node, expect_version=self.store.graph(gid)['version'])
            elif existing.owner != owners[role] or existing.kind != kind:
                fail('PAC_GRAPH_CONFLICT', 'managed graph structure has changed')
        for a, b, kind in [('dispatch', 'implement', 'forward'), ('implement', 'review', 'forward'),
                           ('rework', 'implement', 'back'), ('review', 'finish', 'forward')]:
            if not any(e.from_node == a and e.to_node == b and e.kind == kind for e in self.store.edges(gid)):
                add_edge(self.store, graph_id=gid, from_node=a, to_node=b, kind=kind, expect_version=self.store.graph(gid)['version'])
        if not self.store.graph(gid)['activated_at']:
            activate_graph(self.store, gid, actor=owners['coordinator'])
        if not self.store.node(gid, 'dispatch').flag:
            self.reactor.set_flag(gid, 'dispatch', actor=owners['coordinator'], reason_ref=str(path))
        return {'ok': True, 'graphId': gid, 'context': self.context(doc, 'implement')}

    def current(self, gid, node):
        context = node_context(self.database, gid, node)
        if context['closed'] or not context['active'] or not context['currentActivation']:
            fail('PAC_NO_CURRENT_WORK', 'no active request for this node')
        if node == 'review' and node_context(self.database, gid, 'implement')['currentActivation']:
            fail('PAC_REWORK_PENDING', 'worker has pending rework; this review cannot complete')
        return context

    def mutate(self, request, doc):
        args, gid = request['args'], doc['graphId']
        action = request['tool']
        node = 'review' if action == 'rework' else args['nodeId']
        role = {'implement': 'worker', 'review': 'verifier', 'finish': 'coordinator'}.get(node)
        if not role:
            fail('PAC_INVALID_NODE', 'only implement/review/finish are work nodes')
        self.member(doc, request, role)
        self.expect(gid, args['expectedToken'])
        current = self.current(gid, node)
        actor = request['actor']
        token = args['expectedToken']
        def guard():
            self.expect(gid, token)
            self.current(gid, node)
        if action == 'begin':
            if current['flag']:
                self.store.guard = guard
                self.reactor.reset_flag(gid, node, actor=actor, reason_ref='dsh-pac:begin-current-activation')
        elif action == 'complete':
            if not isinstance(args.get('evidenceRef'), str) or not args['evidenceRef'].strip() or len(args['evidenceRef']) > 4096:
                fail('PAC_EVIDENCE_REQUIRED', 'provide an evidence reference')
            self.store.guard = guard
            self.reactor.set_flag(gid, node, actor=actor, reason_ref=args['evidenceRef'])
        elif action == 'rework':
            if not isinstance(args.get('evidenceRef'), str) or not args['evidenceRef'].strip() or len(args['evidenceRef']) > 4096:
                fail('PAC_EVIDENCE_REQUIRED', 'provide rejection evidence')
            self.store.guard = guard
            if self.store.node(gid, 'rework').flag:
                self.reactor.reset_flag(gid, 'rework', actor=actor, reason_ref=args['evidenceRef'])
                # Preserve the review request identity across our own reset.
                fresh = self.current(gid, 'review')
                if fresh['currentActivation'] != current['currentActivation'] or fresh['predecessors'] != current['predecessors']:
                    fail('PAC_STALE_WORK', 'review changed during rejection')
                token = self.token(gid)
            self.store.guard = guard
            self.reactor.set_flag(gid, 'rework', actor=actor, reason_ref=args['evidenceRef'])
        return {'ok': True, 'context': self.context(doc, node)}

    def jobs(self):
        jobs = []
        for _, doc in self.manifests():
            gid = doc.get('graphId')
            if not gid:
                continue
            state = snapshot(self.database, gid)
            if state['closed'] or not state['active']:
                continue
            for assignment in state['assignments']:
                node = assignment['nodeId']
                if node not in ('implement', 'review', 'finish'):
                    continue
                # Do not wake an old reviewer while a rejection awaits worker begin.
                if node == 'review' and any(a['nodeId'] == 'implement' for a in state['assignments']):
                    continue
                role = {'implement': 'worker', 'review': 'verifier', 'finish': 'coordinator'}[node]
                identity = doc['roles'][role]
                message_id = 'dsh-pac-' + digest([gid, node, sorted(r['eventId'] for r in assignment['requests'])])
                wake = doc.get('wakes', {}).get(message_id, {})
                prompt = ('【PAC 任务通知；不是聊天派单】\n' + json.dumps({'graphId': gid, 'nodeId': node, 'title': doc['title']}, ensure_ascii=False)
                          + '\n先调用 h2b_pac_context 核对当前请求。implement 开始前调用 h2b_pac_begin（返工会撤回旧事实），然后实施和验证；review 独立审核，通过用 h2b_pac_complete，拒绝用 h2b_pac_rework；finish 由协调者验收后 complete。'
                          + '\n完成必须提供实际证据引用和本轮开始工作时取得的 expectedToken。若过期则重新核对任务，不得把旧结果套用新 token。遇到阻塞在本会话明确说明，不虚报完成。不要用 h2b_session_reply/send 发送收到、待命或派工回声。')
                jobs.append({**identity, 'graphId': gid, 'nodeId': node, 'messageId': message_id,
                             'reserved': bool(wake), 'received': wake.get('state') == 'received', 'expectedToken': self.token(gid, state), 'prompt': prompt})
        return {'ok': True, 'jobs': jobs, 'sessionIds': [r['sessionId'] for r in self.config['roles'].values()]}

    def handle(self, request):
        operation = request['operation']
        if operation == 'poll':
            return self.jobs()
        if operation in ('reserve', 'received'):
            path, doc = self.managed(request['graphId'])
            self.member(doc, request)
            job = next((j for j in self.jobs()['jobs'] if j['messageId'] == request['messageId'] and j['sessionId'] == request['sessionId'] and j['nodeId'] == request['nodeId']), None)
            if not job:
                return {'ok': True, 'accepted': False}
            if operation == 'reserve':
                self.expect(doc['graphId'], request['expectedToken'])
                if job['reserved']:
                    return {'ok': True, 'accepted': False}
            doc.setdefault('wakes', {})[job['messageId']] = {'state': 'reserved' if operation == 'reserve' else 'received', 'nodeId': job['nodeId']}
            atomic(path, doc)
            return {'ok': True, 'accepted': True}
        tool = request['tool']
        if tool == 'create':
            return self.create(request)
        if tool == 'list':
            tasks = []
            for _, doc in self.manifests():
                try:
                    self.member(doc, request)
                except PacError:
                    continue
                if doc.get('graphId'):
                    state = snapshot(self.database, doc['graphId'])
                    tasks.append({'graphId': doc['graphId'], 'taskKey': doc['taskKey'], 'title': doc['title'], 'closed': state['closed'], 'assignments': [{'nodeId': a['nodeId'], 'owner': a['owner']} for a in state['assignments']]})
            return {'ok': True, 'tasks': tasks, 'roles': self.config['roles']}
        path, doc = self.managed(request['args']['graphId'])
        self.member(doc, request)
        if tool == 'inspect':
            # All task state is folded by the public API in ONE read transaction.
            # Do not join per-node context reads or derive progress from chat/wakes.
            return {'ok': True, 'task': {
                'taskKey': doc['taskKey'], 'title': doc['title'], 'brief': doc['brief'],
                'snapshot': snapshot(self.database, doc['graphId']),
            }}
        if tool == 'cancel':
            self.member(doc, request, 'coordinator')
            args = request['args']
            if not isinstance(args.get('evidenceRef'), str) or not args['evidenceRef'].strip():
                fail('PAC_EVIDENCE_REQUIRED', 'provide a cancellation reason reference')
            self.store.guard = lambda: self.expect(doc['graphId'], args['expectedToken'])
            close_graph(self.store, doc['graphId'], actor=request['actor'])
            doc['cancellation'] = {'actor': request['actor'], 'evidenceRef': args['evidenceRef']}
            atomic(path, doc)
            return {'ok': True, 'context': self.context(doc, 'finish')}
        if tool == 'context':
            return {'ok': True, 'context': self.context(doc, request['args']['nodeId'])}
        if tool not in ('begin', 'complete', 'rework'):
            fail('PAC_INVALID_OPERATION', 'unsupported PAC tool')
        return self.mutate(request, doc)


def main():
    request = json.loads(sys.stdin.read(1024 * 1024))
    state = Path(os.environ.get('HARNESS_STATE_DIR') or (Path(os.environ.get('HYPRIAL_HOME', str(Path.home() / '.hyprial'))) / 'state'))
    root = state / 'dsh-pac'
    if not (root / 'config.json').is_file():
        fail('PAC_NOT_CONFIGURED', 'configure PAC roles before enabling automation')
    with (root / 'service.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        service = Service(state)
        try:
            result = service.handle(request)
        finally:
            service.reactor.close()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (PacError, ValueError, KeyError) as error:
        print(json.dumps({'ok': False, 'error': {'code': getattr(error, 'code', 'PAC_INVALID_ARGUMENT'), 'message': str(error)}}))
        sys.exit(1)
