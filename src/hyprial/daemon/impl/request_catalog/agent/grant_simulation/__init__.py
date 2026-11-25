"""Pure what-if grant simulations, independent of transport identity."""

from hyprial.identity import (
    CAPABILITY_ENTRY_POINTS, SEE_ACTORS, SEND_TO, CallerIdentity, CapabilityRecord,
    EnforcementError, GrantRecord, Principal, VisibilityError, check_shared_path,
    explain, explain_target, may_send, visible_targets,
)
from hyprial.kernel import DaemonRequestError, ipc_errors

from .specs import SPECS


def _identity(cls, caller, hosted, unverified, error_code):
    if caller is None:
        return None
    try:
        return cls(caller, hosted=hosted, verified=not unverified)
    except (EnforcementError, VisibilityError) as error:
        raise DaemonRequestError(error_code, str(error)) from error


def check(_app, params, *, trusted_message_origin=None):
    scenario = params['scenario']
    caller = scenario['caller']
    identity = _identity(Principal, caller, scenario['hosted'], scenario['unverified'],
                         ipc_errors.ENFORCEMENT_ERROR)
    try:
        records = [CapabilityRecord(actor=caller, capability=scenario['capability'],
                                    scope=scope, revision=scenario['revision'])
                   for scope in scenario['scope']] if caller is not None else []
        capability = scenario['capability']
        resource = scenario['resource']
        verdict = (check_shared_path(resource, write=scenario['write'], caller=identity, records=records)
                   if capability == 'shared-path' else
                   explain(capability, resource, caller=identity, records=records))
    except EnforcementError as error:
        if error.code != "enforcement_scope_invalid":
            raise
        raise DaemonRequestError(ipc_errors.ENFORCEMENT_SCOPE_INVALID, str(error)) from error
    return {**verdict.as_dict(), 'entryPoints': dict(CAPABILITY_ENTRY_POINTS)}


def visibility(_app, params, *, trusted_message_origin=None):
    scenario = params['scenario']
    caller = scenario['caller']
    identity = _identity(CallerIdentity, caller, scenario['hosted'], scenario['unverified'],
                         ipc_errors.VISIBILITY_ERROR)
    try:
        grants = []
        if caller is not None and scenario['seeActors'] is not None:
            grants.append(GrantRecord(actor=caller, capability=SEE_ACTORS, scope=scenario['seeActors']))
        if caller is not None and scenario['sendTo'] is not None:
            grants.append(GrantRecord(actor=caller, capability=SEND_TO, scope=scenario['sendTo']))
        targets = scenario['targets']
        rows = [{'targetUri': uri, 'targetKind': 'candidate'} for uri in targets]
        decisions = [{'target': uri,
                      'seeActors': explain_target(uri, capability=SEE_ACTORS, caller=identity, grants=grants).as_dict(),
                      'sendTo': may_send(uri, caller=identity, grants=grants).as_dict()} for uri in targets]
        visible = [row['targetUri'] for row in visible_targets(rows, caller=identity, grants=grants)]
    except VisibilityError as error:
        if error.code != "visibility_scope_invalid":
            raise
        raise DaemonRequestError(ipc_errors.VISIBILITY_SCOPE_INVALID, str(error)) from error
    return {'caller': caller, 'hosted': scenario['hosted'], 'verified': not scenario['unverified'],
            'decisions': decisions, 'visible': visible}


BINDINGS = ((SPECS[0], check), (SPECS[1], visibility))
