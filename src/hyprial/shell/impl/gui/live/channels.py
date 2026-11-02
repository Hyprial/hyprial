"""Project App permission, binding and runtime as independent facts."""
from concurrent.futures import ThreadPoolExecutor

from hyprial.kernel import DaemonRequestError
from hyprial.shell.impl.gui.adapter.errors import AppAdapterError, AppAdapterUnavailable


def _text(params, key):
    value = params.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(f'{key} required')
    return value


def _adapters(host):
    response = host.request('adapter.list', {})
    if not isinstance(response, dict) or response.get('ok') is not True:
        raise AppAdapterUnavailable('adapter-list-unconfirmed')
    rows = response.get('adapters')
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise AppAdapterUnavailable('invalid-adapter-list')
    rows = [row for row in rows if row.get("provider") == 'lark']
    if len(rows) > 8 or any(not isinstance(row.get('name'), str) for row in rows):
        raise AppAdapterUnavailable('adapter-list-limit')
    return rows


def _snapshot(host, name):
    status = host.request('adapter.status', {'name': name})
    pins = host.request('adapter.pins', {})
    if (not isinstance(status, dict) or status.get('ok') is not True
            or not isinstance(pins, dict) or pins.get('ok') is not True
            or not isinstance(status.get('adapter'), dict) or not isinstance(pins.get('pins'), dict)):
        raise AppAdapterUnavailable('channel-state-unconfirmed')
    runtime = status['adapter']
    actor = pins['pins'].get(name)
    if runtime.get('name') != name or runtime.get("provider") != 'lark' or (
        actor is not None and (not isinstance(actor, str) or not actor.startswith('agent:'))
    ) or not isinstance(runtime.get('online'), bool):
        raise AppAdapterUnavailable('invalid-channel-state')
    result = {'adapter': name, 'authorization': host.authorization.status(name),
              'binding': {'actor': actor}, 'runtime': {'online': runtime['online']},
              'bindingAvailable': pins.get('supportsExpectedActor') is True}
    operation = host.authorization.current_operation(name)
    if operation is not None:
        result['operation'] = operation
    return result


def channel_operation(host, operation, params):
    if host.authorization is None:
        raise AppAdapterUnavailable('lark-authorization-unavailable')
    if operation == 'lark.authorization.operation':
        return host.authorization.operation(_text(params, 'operationId'))
    if operation == 'lark.authorization.cancel':
        return host.authorization.cancel(_text(params, 'operationId'))
    rows = _adapters(host)
    if operation == 'lark.adapters':
        with ThreadPoolExecutor(max_workers=4) as pool:
            snapshots = list(pool.map(lambda row: _snapshot(host, row['name']), rows))
        return {'adapters': snapshots}
    name = _text(params, 'adapter')
    if name not in {row['name'] for row in rows}:
        raise ValueError('unknown configured Lark adapter')
    if operation == 'lark.authorization.status':
        return host.authorization.status(name)
    if operation == 'lark.authorization.start':
        return host.authorization.start(name)
    if operation != 'lark.binding.set' or set(params) != {'adapter', 'agentId', 'expectedActor'}:
        raise ValueError('binding requires expectedActor')
    actor = _text(params, 'agentId')
    expected = params['expectedActor']
    if expected is not None and (not isinstance(expected, str) or not expected.startswith('agent:')):
        raise ValueError('expectedActor must be null or canonical agent URI')
    targets = host.adapter.targets_list()['targets']
    if actor not in {target['targetUri'] for target in targets}:
        raise ValueError('selected Agent absent from directory')
    auth = host.authorization.status(name)
    if auth.get('authorization') != 'authorized':
        raise AppAdapterError('authorization-required', 'App authorization is not confirmed')
    pins = host.request('adapter.pins', {})
    if not isinstance(pins, dict) or pins.get('ok') is not True or pins.get('supportsExpectedActor') is not True:
        raise AppAdapterUnavailable('conditional-binding-unsupported')
    try:
        result = host.request('adapter.pin', {'name': name, 'actor': actor, 'expectedActor': expected})
    except DaemonRequestError as error:
        if error.code == 'PIN_STATE_CONFLICT':
            raise AppAdapterError('binding-conflict', 'Binding changed; refresh and confirm again') from error
        raise
    if not isinstance(result, dict) or result.get('ok') is not True:
        raise AppAdapterUnavailable('binding-result-unconfirmed')
    # A pin receipt alone cannot establish grant or runtime health.
    return _snapshot(host, name)
