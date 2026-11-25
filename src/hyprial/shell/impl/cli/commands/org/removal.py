"""Explicit operator confirmation for persistent tailnet removal operations.

Decision notes:
- ``org.remove``, ``org.leave`` and ``org.delete`` share this one preview ->
  confirm request pair, so each CLI action keeps a single reviewed
  request-fanout site. ``org.delete`` binds only its ``operationId`` (its
  params schema has no ``deviceIds``), describes the preview itself, asks
  its own question, and refuses non-interactive or ``--json`` runs without
  ``--yes`` with ``USER_ACTION_REQUIRED`` and a declined prompt with
  ``DELETE_DECLINED``.
- A preview already in phase ``complete`` (a fresh preview found no new
  targets) is returned as-is; no confirmation is needed.
"""
from collections.abc import Callable
from dataclasses import dataclass

from hyprial.shell.impl.cli.output import confirm, notice


@dataclass(frozen=True, slots=True)
class ConfirmationPrompt:
    """How one confirmed operation describes and refuses itself."""

    question: str
    unconfirmed: tuple[str, str]
    declined: tuple[str, str] | None = None
    describe: Callable[[dict], str] | None = None
    bind_devices: bool = True
    interactive: bool = True


_REMOVAL_PROMPT = ConfirmationPrompt(
    question='Confirm this removal?',
    unconfirmed=('CONFIRMATION_REQUIRED', 'Re-run with --yes to confirm this exact removal.'),
)


def _describe_removal(params, preview) -> None:
    if preview.get('membershipAction') == 'preserved':
        notice(f"Revoke selected devices {preview['deviceIds']}; retain user memberships; "
               f"remaining active devices {preview.get('remainingDeviceIds', [])}. "
               'Affected business connections will close; global propagation is lease-bounded (300 seconds).')
    else:
        notice(f"Cut off owner {params.get('user', 'self')}; devices {preview.get('deviceIds', [])}; "
               f"tailnet {preview.get('tailnetId')}; authority {preview.get('authorityDeviceId')}. "
               'Affected business connections will close; global propagation is lease-bounded (300 seconds).')
    refresh = preview.get('revisionRefresh')
    if isinstance(refresh, dict):
        notice(f"Revision changed {refresh.get('oldRevision')} → {refresh.get('newRevision')}; "
               f"still targeted {refresh.get('remainingDeviceIds', [])}; "
               f"already removed {refresh.get('alreadyRemovedDeviceIds', [])}; "
               f"new devices NOT selected {refresh.get('unselectedDeviceIds', [])}. "
               'Confirm this revised preview; committed OrgFS changes will not repeat.')


def _require_confirmation(services, prompt: ConfirmationPrompt) -> None:
    if not prompt.interactive or not services._stdin_isatty():
        raise services.CliError(*prompt.unconfirmed)
    if not confirm(prompt.question):
        raise services.CliError(*(prompt.declined or prompt.unconfirmed))


def confirmed_removal(services, method, params, *, yes: bool, prompt: ConfirmationPrompt | None = None):
    prompt = prompt or _REMOVAL_PROMPT
    preview = services.runtime.request(method, {**params, 'confirmed': False})
    if (not isinstance(preview, dict) or not isinstance(preview.get('operationId'), str)
            or not preview['operationId'] or not isinstance(preview.get('deviceIds'), list)
            or any(not isinstance(device, str) for device in preview['deviceIds'])):
        raise services.CliError('INVALID_RESPONSE', 'Removal preview must include operationId and deviceIds.')
    if preview.get('phase') == 'complete':
        return preview
    binding = {'operationId': preview['operationId']}
    if prompt.bind_devices:
        binding['deviceIds'] = list(preview['deviceIds'])
    if prompt.describe is None:
        _describe_removal(params, preview)
    else:
        notice(prompt.describe(preview))
    if not yes:
        _require_confirmation(services, prompt)
    result = services.runtime.request(method, {**params, **binding, 'confirmed': True})
    if not isinstance(result, dict):
        raise services.CliError('INVALID_RESPONSE', 'Removal must return an object.')
    if result.get('phase') != 'complete':
        code = 'AUTHORITY_REQUIRED' if result.get('phase') == 'pending-authority' else 'AUTHORITY_UNREACHABLE'
        raise services.CliError(code, 'Removal is pending; execute/retry on the pinned authority.')
    return result
