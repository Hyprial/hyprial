"""Frozen exact native identities, not package/class authority waivers.

Derived from the 129-record migration receipt; lexical manifest owners stay frozen.
Every key is finite. New paths, methods, operations and duplicate definitions fail.
"""

import ast
from collections import Counter
import copy
import hashlib
from importlib.util import resolve_name
from pathlib import Path

SUPERVISION_CROSSWALK = {
    ('src/hyprial/daemon/impl/adapters/lark/runtime/effects.py', 'start_confirm_timeout', 'parameter', '_ProcessEffects.__init__'): ('src/hyprial/daemon/impl/adapters/lark/runtime/runtime.py', 'start_confirm_timeout'),
    ('src/hyprial/daemon/impl/adapters/lark/runtime/runtime.py', 'start_confirm_timeout', 'parameter', 'AdapterRuntime.__init__'): ('src/hyprial/daemon/impl/adapters/lark/runtime/runtime.py', 'start_confirm_timeout'),
    ('src/hyprial/daemon/impl/harnesses/actor/behaviors/reconcile.py', 'wait_ready', 'function', '_ReconcileBehavior._on_wait_ready.wait_ready'): ('src/hyprial/daemon/impl/harnesses/actor/facade.py', 'wait_ready'),
    ('src/hyprial/daemon/impl/harnesses/actor/facade.py', 'wait_ready', 'function', 'HarnessRuntimeFacade.wait_ready'): ('src/hyprial/daemon/impl/harnesses/actor/facade.py', 'wait_ready'),
    ('src/hyprial/shell/impl/cli/commands/common/daemon_start.py', 'ready_timeout', 'parameter', '_launch_daemon_process'): ('src/hyprial/shell/impl/cli/commands/common/daemon_start.py', 'ready_timeout'),
    ('src/hyprial/shell/impl/cli/commands/common/daemon_start.py', 'ready_timeout', 'parameter', '_launch_daemon_process_locked'): ('src/hyprial/shell/impl/cli/commands/common/daemon_start.py', 'ready_timeout'),
    ('src/hyprial/shell/impl/cli/commands/init/init.py', 'ready_timeout', 'parameter', '_complete_initialization'): ('src/hyprial/shell/impl/cli/commands/common/daemon_start.py', 'ready_timeout'),
    ('src/hyprial/shell/impl/cli/commands/init/init.py', 'ready_timeout', 'parameter', 'init'): ('src/hyprial/shell/impl/cli/commands/common/daemon_start.py', 'ready_timeout'),
    ('src/hyprial/shell/impl/cli/commands/login/commands.py', 'ready_timeout', 'parameter', 'login'): ('src/hyprial/shell/impl/cli/commands/common/daemon_start.py', 'ready_timeout'),
}

INBOX_CROSSWALK = {
    ('src/hyprial/daemon/impl/application/actors/agents_admin/destroys.py', '_AgentDestroyMixin._destroy_agent_messages', 'ack'): ('src/hyprial/daemon/impl/application/actors/agents_admin/destroys.py', 'DaemonApplication._destroy_agent_messages', 'ack'),
    ('src/hyprial/daemon/impl/application/messaging/inbox_surface/queries.py', '_MessageQueriesMixin._ipc_message_ack', 'ack'): ('src/hyprial/daemon/impl/application/messaging/inbox_surface/queries.py', 'DaemonApplication.handle', 'ack'),
    ('src/hyprial/daemon/impl/application/messaging/inbox_surface/queries.py', '_MessageQueriesMixin._ipc_message_ack', 'dismiss_system_notice'): ('src/hyprial/daemon/impl/application/messaging/inbox_surface/queries.py', 'DaemonApplication.handle', 'dismiss_system_notice'),
    ('src/hyprial/daemon/impl/application/messaging/inbox_surface/queries.py', '_MessageQueriesMixin._ipc_message_pending_list', 'fetch_pending'): ('src/hyprial/daemon/impl/application/messaging/inbox_surface/queries.py', 'DaemonApplication.handle', 'fetch_pending'),
    ('src/hyprial/daemon/impl/application/messaging/inbox_surface/queries.py', '_MessageQueriesMixin._ipc_message_read_body', 'fetch_pending'): ('src/hyprial/daemon/impl/application/messaging/inbox_surface/queries.py', 'DaemonApplication.handle', 'fetch_pending'),
    ('src/hyprial/daemon/impl/application/messaging/inbox_surface/queries.py', '_MessageQueriesMixin._ipc_outbox_prune', 'prune_outbox'): ('src/hyprial/daemon/impl/application/messaging/inbox_surface/queries.py', 'DaemonApplication.handle', 'prune_outbox'),
    ('src/hyprial/daemon/impl/application/messaging/inbox_surface/send.py', '_MessageSendMixin._ipc_message_reply', 'ack'): ('src/hyprial/daemon/impl/application/messaging/inbox_surface/queries.py', 'DaemonApplication.handle', 'ack'),
    ('src/hyprial/daemon/impl/application/messaging/inbox_surface/send.py', '_MessageSendMixin._ipc_message_reply', 'submit'): ('src/hyprial/daemon/impl/application/messaging/inbox_surface/send.py', 'DaemonApplication.handle', 'submit'),
    ('src/hyprial/daemon/impl/application/messaging/inbox_surface/send.py', '_MessageSendMixin._ipc_message_send', 'submit'): ('src/hyprial/daemon/impl/application/messaging/inbox_surface/send.py', 'DaemonApplication.handle', 'submit'),
    ('src/hyprial/daemon/impl/application/messaging/maintenance.py', '_MaintenanceMixin._flush_outbox_recipient_wakes', 'wake_outbox_recipient'): ('src/hyprial/daemon/impl/application/messaging/maintenance.py', 'DaemonApplication._flush_outbox_recipient_wakes', 'wake_outbox_recipient'),
    ('src/hyprial/daemon/impl/application/messaging/maintenance.py', '_MaintenanceMixin._raise_harness_failed_alarm', 'alarm_emitter'): ('src/hyprial/daemon/impl/application/messaging/maintenance.py', 'DaemonApplication._raise_harness_failed_alarm', 'alarm_emitter'),
    ('src/hyprial/daemon/impl/application/wiring/services.py', '_WiringServicesMixin._startup_adopt_domain_services', 'alarm_emitter'): ('src/hyprial/daemon/impl/application/wiring/services.py', 'DaemonApplication._start_runtime', 'alarm_emitter'),
    ('src/hyprial/daemon/impl/application/wiring/transport.py', '_WiringTransportMixin._startup_compose_delivery_plane', 'receive'): ('src/hyprial/daemon/impl/application/wiring/transport.py', 'DaemonApplication._start_runtime', 'receive'),
    ('src/hyprial/daemon/impl/runtime/forwards.py', '_BridgeForwardsMixin._confirm_mechanism_answer', 'ack'): ('src/hyprial/daemon/impl/runtime/forwards.py', 'DaemonEventBridge._confirm_mechanism_answer', 'ack'),
    ('src/hyprial/daemon/impl/runtime/forwards.py', '_BridgeForwardsMixin._confirm_mechanism_answer', 'submit'): ('src/hyprial/daemon/impl/runtime/forwards.py', 'DaemonEventBridge._confirm_mechanism_answer', 'submit'),
    ('src/hyprial/daemon/impl/runtime/forwards.py', '_BridgeForwardsMixin._submit_notice_to_inbox', 'submit'): ('src/hyprial/daemon/impl/runtime/forwards.py', 'DaemonEventBridge._submit_loud', 'submit'),
    ('src/hyprial/daemon/impl/runtime/reconcile.py', '_BridgeReconcileMixin._dispatch_actor_deliveries', 'dismiss_system_notice'): ('src/hyprial/daemon/impl/runtime/reconcile.py', 'DaemonEventBridge._dispatch_harness_deliveries', 'dismiss_system_notice'),
    ('src/hyprial/daemon/impl/runtime/reconcile.py', '_BridgeReconcileMixin._drain_dispatch_offer_outcomes', 'dismiss_system_notice'): ('src/hyprial/daemon/impl/runtime/reconcile.py', 'DaemonEventBridge._drain_dispatch_offer_outcomes', 'dismiss_system_notice'),
    ('src/hyprial/daemon/impl/runtime/reconcile.py', '_BridgeReconcileMixin._publish_harness_progress', 'submit_progress_event'): ('src/hyprial/daemon/impl/runtime/reconcile.py', 'DaemonEventBridge._publish_harness_progress', 'submit_progress_event'),
    ('src/hyprial/daemon/impl/runtime/reconcile.py', '_BridgeReconcileMixin._refresh_queued_hold', 'refresh_hold'): ('src/hyprial/daemon/impl/runtime/reconcile.py', 'DaemonEventBridge._refresh_queued_hold', 'refresh_hold'),
    ('src/hyprial/daemon/impl/runtime/reconcile.py', '_BridgeReconcileMixin._retry_pump_run', 'retry_due'): ('src/hyprial/daemon/impl/runtime/reconcile.py', 'DaemonEventBridge._retry_pump_run', 'retry_due'),
    ('src/hyprial/daemon/impl/runtime/results.py', '_BridgeResultsMixin._complete_harness_results', 'ack'): ('src/hyprial/daemon/impl/runtime/results.py', 'DaemonEventBridge._complete_harness_results', 'ack'),
    ('src/hyprial/daemon/impl/runtime/results.py', '_BridgeResultsMixin._complete_harness_results', 'fetch_pending'): ('src/hyprial/daemon/impl/runtime/results.py', 'DaemonEventBridge._complete_harness_results', 'fetch_pending'),
    ('src/hyprial/daemon/impl/runtime/results.py', '_BridgeResultsMixin._settle_claimed_result_member', 'ack'): ('src/hyprial/daemon/impl/runtime/results.py', 'DaemonEventBridge._settle_claimed_result_member', 'ack'),
    ('src/hyprial/daemon/impl/runtime/results.py', '_BridgeResultsMixin._settle_claimed_result_member', 'fetch_pending'): ('src/hyprial/daemon/impl/runtime/results.py', 'DaemonEventBridge._settle_claimed_result_member', 'fetch_pending'),
    ('src/hyprial/daemon/impl/runtime/results.py', '_BridgeResultsMixin._settle_stranded_rows', 'ack'): ('src/hyprial/daemon/impl/runtime/results.py', 'DaemonEventBridge._settle_stranded_rows', 'ack'),
}

INBOX_STATE_HOSTS = {
    ('src/hyprial/daemon/impl/application/actors/agents_admin/destroys.py', '_AgentDestroyMixin'): ('src/hyprial/daemon/impl/application/__init__.py', 'DaemonApplication'),
    ('src/hyprial/daemon/impl/application/messaging/inbox_surface/queries.py', '_MessageQueriesMixin'): ('src/hyprial/daemon/impl/application/__init__.py', 'DaemonApplication'),
    ('src/hyprial/daemon/impl/application/messaging/inbox_surface/send.py', '_MessageSendMixin'): ('src/hyprial/daemon/impl/application/__init__.py', 'DaemonApplication'),
    ('src/hyprial/daemon/impl/application/messaging/maintenance.py', '_MaintenanceMixin'): ('src/hyprial/daemon/impl/application/__init__.py', 'DaemonApplication'),
    ('src/hyprial/daemon/impl/application/wiring/services.py', '_WiringServicesMixin'): ('src/hyprial/daemon/impl/application/__init__.py', 'DaemonApplication'),
    ('src/hyprial/daemon/impl/application/wiring/transport.py', '_WiringTransportMixin'): ('src/hyprial/daemon/impl/application/__init__.py', 'DaemonApplication'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py', 'DeliveryCustodyLifecycleMixin'): ('src/hyprial/daemon/impl/inbox/actor/custody/__init__.py', 'DeliveryCustody'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/notices.py', 'DeliveryCustodyNoticesMixin'): ('src/hyprial/daemon/impl/inbox/actor/custody/__init__.py', 'DeliveryCustody'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/progress.py', 'DeliveryCustodyProgressMixin'): ('src/hyprial/daemon/impl/inbox/actor/custody/__init__.py', 'DeliveryCustody'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/retry.py', 'DeliveryCustodyRetryMixin'): ('src/hyprial/daemon/impl/inbox/actor/custody/__init__.py', 'DeliveryCustody'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/submit.py', 'DeliveryCustodySubmitMixin'): ('src/hyprial/daemon/impl/inbox/actor/custody/__init__.py', 'DeliveryCustody'),
    ('src/hyprial/daemon/impl/runtime/forwards.py', '_BridgeForwardsMixin'): ('src/hyprial/daemon/impl/runtime/bridge.py', 'DaemonEventBridge'),
    ('src/hyprial/daemon/impl/runtime/reconcile.py', '_BridgeReconcileMixin'): ('src/hyprial/daemon/impl/runtime/bridge.py', 'DaemonEventBridge'),
    ('src/hyprial/daemon/impl/runtime/results.py', '_BridgeResultsMixin'): ('src/hyprial/daemon/impl/runtime/bridge.py', 'DaemonEventBridge'),
}

SANCTIONED_INBOX_OPERATIONS = frozenset({
    ('src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py', 'DeliveryCustodyLifecycleMixin.__call__', 'close'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py', 'DeliveryCustodyLifecycleMixin.__init__', 'construct'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py', 'DeliveryCustodyLifecycleMixin._handle', 'accept_custody'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py', 'DeliveryCustodyLifecycleMixin._handle', 'ack'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py', 'DeliveryCustodyLifecycleMixin._handle', 'close'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py', 'DeliveryCustodyLifecycleMixin._handle', 'dismiss_system_notice'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py', 'DeliveryCustodyLifecycleMixin._handle', 'drain_system_notices'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py', 'DeliveryCustodyLifecycleMixin._handle', 'fail'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py', 'DeliveryCustodyLifecycleMixin._handle', 'fetch_pending'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py', 'DeliveryCustodyLifecycleMixin._handle', 'prune_outbox'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py', 'DeliveryCustodyLifecycleMixin._handle', 'receive'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py', 'DeliveryCustodyLifecycleMixin._handle', 'receive_progress_event'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py', 'DeliveryCustodyLifecycleMixin._handle', 'receive_system_notice'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py', 'DeliveryCustodyLifecycleMixin._handle', 'refresh_hold'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py', 'DeliveryCustodyLifecycleMixin._handle', 'retire_outbox_receipt'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py', 'DeliveryCustodyLifecycleMixin._handle', 'wake_outbox_recipient'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/lifecycle.py', 'DeliveryCustodyLifecycleMixin.close_storage', 'close'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/notices.py', 'DeliveryCustodyNoticesMixin._stage_expired_sender_notices', 'receive_system_notice'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/progress.py', 'DeliveryCustodyProgressMixin._submit_progress', 'receive_progress_event'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/retry.py', 'DeliveryCustodyRetryMixin._complete_dispatch', 'receive_system_notice'),
    ('src/hyprial/daemon/impl/inbox/actor/custody/submit.py', 'DeliveryCustodySubmitMixin._advance_reply_receipt_locked', 'ack'),
})


def lexical_definitions(tree: ast.Module) -> Counter[str]:
    """Count physical definitions, including shadowed duplicates, not live attributes."""
    counts: Counter[str] = Counter()

    def visit(node: ast.AST, owners: tuple[str, ...]) -> None:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            owners = (*owners, node.name)
            counts['.'.join(owners)] += 1
        for child in ast.iter_child_nodes(node):
            visit(child, owners)

    visit(tree, ())
    return counts


def module_name(path: str) -> str:
    parts = Path(path).with_suffix('').parts[1:]
    if parts[-1] == '__init__':
        parts = parts[:-1]
    return '.'.join(parts)


def method_ast_sha256(path: Path, owner: str) -> str:
    """Pin a unique physical method, tolerating only separately owned import edits."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    matches = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = [node.name]
        parent = parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(parent.name)
            parent = parents.get(parent)
        if '.'.join(reversed(names)) == owner:
            matches.append(node)
    if len(matches) != 1:
        raise ValueError(f'non-unique method AST pin: {path}:{owner} ({len(matches)})')

    class ImportsOnly(ast.NodeTransformer):
        def visit_Import(self, _node):
            return None

        def visit_ImportFrom(self, _node):
            return None

    normalized = ImportsOnly().visit(copy.deepcopy(matches[0]))
    return hashlib.sha256(ast.dump(normalized, include_attributes=False).encode()).hexdigest()


def native_identity_errors(src_root: Path, path: str, owner: str) -> list[str]:
    """Accept only one actual method and its explicitly imported, unique state host."""
    repo = src_root.parents[1]
    source = repo / path
    if not source.is_file():
        return [f'absent authority definition: {path}:{owner}']
    tree = ast.parse(source.read_text(encoding='utf-8'))
    definitions = lexical_definitions(tree)
    if owner != '<module>' and definitions[owner] != 1:
        return [f'non-unique authority definition: {path}:{owner} ({definitions[owner]})']
    native_class = owner.split('.')[0]
    host = INBOX_STATE_HOSTS.get((path, native_class))
    if host is None:
        return []
    host_path, host_name = host
    if not (repo / host_path).is_file():
        return [f'absent authority state host: {host_path}:{host_name}']
    host_tree = ast.parse((repo / host_path).read_text(encoding='utf-8'))
    host_classes = [node for node in host_tree.body
                    if isinstance(node, ast.ClassDef) and node.name == host_name]
    if len(host_classes) != 1 or definitions[native_class] != 1:
        return [f'non-unique authority state host: {path}:{native_class} -> {host_path}:{host_name}']
    package = module_name(host_path)
    if not host_path.endswith('/__init__.py'):
        package = package.rpartition('.')[0]
    aliases: dict[str, list[str]] = {}
    for node in host_tree.body:
        if isinstance(node, ast.ImportFrom):
            module = (resolve_name('.' * node.level + (node.module or ''), package)
                      if node.level else node.module or '')
            for alias in node.names:
                aliases.setdefault(alias.asname or alias.name, []).append(module + '.' + alias.name)
    expected = module_name(path) + '.' + native_class
    bound = [base.id for base in host_classes[0].bases if isinstance(base, ast.Name)
             and aliases.get(base.id) == [expected]]
    if len(bound) != 1:
        return [f'authority state host must inherit exact native provider: {path}:{native_class} -> {host_path}:{host_name}']
    # The exact import must also be the live module binding. Assignments,
    # unrelated module imports, and identically named local classes cannot
    # impersonate the provider. Local method variables do not rebind globals.
    for statement in host_tree.body:
        if isinstance(statement, ast.ImportFrom):
            continue
        if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            shadowed = statement.name in bound
        else:
            shadowed = any(
                isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)) and node.id in bound
                or isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                    (alias.asname or alias.name.split('.')[0]) in bound for alias in node.names)
                for node in ast.walk(statement)
            )
        if shadowed:
            return [f'shadowed authority state-host binding: {host_path}:{bound[0]}']
    return []
