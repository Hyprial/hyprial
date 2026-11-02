"""Public shell entry for the owned GUI child, never a business CLI."""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from hyprial.daemon import create_lark_authorization_service, resolve_node_owner
from hyprial.kernel import (
    canonical_agent_uri,
    canonical_user_uri,
    configured_harness_state_dir,
    require_initialized_hyprial_home,
)
from hyprial.shell.impl.gui.adapter.chat import AppChatAdapter, SessionBinding
from hyprial.shell.impl.gui.adapter.host import daemon_request_callable
from hyprial.shell.impl.gui.live.backend import LiveBackend, failure
from hyprial.shell.impl.gui.live.journal import IntentJournal


def _personal_squire_uri(request, owner):
    status = request('ps', {})
    daemon = status.get('daemon') if isinstance(status, dict) else None
    if (not isinstance(daemon, dict) or daemon.get('running') is not True
            or daemon.get('owner') != owner):
        raise ValueError('GUI daemon owner cannot be confirmed')
    node_id = daemon.get('nodeId')
    if not isinstance(node_id, str) or not node_id:
        raise ValueError('GUI daemon node cannot be confirmed')
    return canonical_agent_uri(owner, node_id, 'squire')


def backend_from_environment():
    if os.environ.get('HYPRIAL_GUI_BACKEND_DRIVER') != 'daemon':
        raise ValueError('explicit daemon driver required')
    if not os.environ.get('HYPRIAL_HOME'):
        raise ValueError('explicit HYPRIAL_HOME required')
    home = require_initialized_hyprial_home()
    owner = resolve_node_owner(hyprial_home=home)
    principal = os.environ.get('HYPRIAL_GUI_PRINCIPAL')
    if principal != canonical_user_uri(owner):
        raise ValueError('GUI principal must match the trusted node owner')
    daemon_state = configured_harness_state_dir()
    state_dir = daemon_state / 'gui-live'
    request = daemon_request_callable()
    journal = IntentJournal(state_dir, principal)
    sessions = {row['session']: SessionBinding((row['target'],), row['conversation'])
                for row in journal.pending()}
    adapter = AppChatAdapter(request=request, sender=principal, sessions=sessions)
    return LiveBackend(adapter=adapter, request=request, principal=principal,
                       state_dir=state_dir,
                       personal_squire_uri=_personal_squire_uri(request, owner),
                       authorization=create_lark_authorization_service(home, daemon_state))


def run_gui_backend():
    """One bounded JSON frame per line; process lifetime owned by Node."""
    # Public composition is imported lazily, so old packages fail before any
    # write when the matching backend facade has not been installed.
    backend = backend_from_environment()
    writer = threading.Lock()
    slots = threading.BoundedSemaphore(16)
    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix='gui-operation')

    def respond(identifier, response):
        with writer:
            sys.stdout.write(json.dumps({'id': identifier, **response}, ensure_ascii=False) + '\n')
            sys.stdout.flush()

    def perform(identifier, request):
        try:
            respond(identifier, backend.handle(request))
        finally:
            slots.release()

    def stop(_signal, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    try:
        respond('ready', {'ok': True, 'result': {'driver': 'daemon'}})
        while line := sys.stdin.buffer.readline(262145):
            if len(line) > 262144 or not line.endswith(b'\n'):
                break
            try:
                frame = json.loads(line)
                if not isinstance(frame, dict) or set(frame) != {'id', 'operation', 'params'}:
                    break
                identifier = frame.pop('id')
                if not isinstance(identifier, str) or len(identifier) > 128:
                    break
            except (ValueError, UnicodeDecodeError):
                break
            if not slots.acquire(blocking=False):
                respond(identifier, failure('busy', '服务繁忙，结果尚未确认。'))
                continue
            executor.submit(perform, identifier, frame)
    finally:
        backend.close()
        executor.shutdown(wait=False, cancel_futures=True)
