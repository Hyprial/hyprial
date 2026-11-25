# Decision record (contract freeze r3 §8.7; HQ ruling F-6)
# - The frozen contract names the switch `provider_keys.authority`; that is its logical
#   name. It is persisted in H/settings.json as section `providerKeys`, key `authority`
#   (camelCase, like the other settings sections such as workerProxy), read on every
#   use and never cached, like forwarding.mode.
# - A settings file that spells the logical name literally (top-level `provider_keys`,
#   or a dotted `provider_keys.authority` / `providerKeys.authority` key) fails closed
#   with PROVIDER_STORE_UNAVAILABLE instead of being silently ignored: the owner meant
#   to set the switch, so falling back to "legacy" would hide the mistake.
# - Absent home, file, section or key means the default "legacy". A present but
#   unparseable file, a non-object section, or any value other than the two exact
#   strings fails closed with PROVIDER_STORE_UNAVAILABLE: a damaged file must not
#   silently switch authority in either direction.
# - This is the only reader of the switch: launch and the legacy agent.secret.* adapter
#   call authority() here and keep no copy of the rule.
# - No environment override and no writer here: only the home owner changes the
#   switch, on this machine, by editing settings.
"""Provider-key authority gate: "legacy" (default) or "owner-keys"."""
from __future__ import annotations

import json
from os import PathLike
from pathlib import Path
from typing import Literal

from hyprial.identity import ProviderKeys
from hyprial.kernel import ipc_errors

Authority = Literal["legacy", "owner-keys"]
LEGACY: Authority = "legacy"
OWNER_KEYS: Authority = "owner-keys"
SECTION = "providerKeys"
KEY = "authority"
LOGICAL_NAME = "provider_keys.authority"
MISSPELLED_KEYS = frozenset({"provider_keys", LOGICAL_NAME, f"{SECTION}.{KEY}"})
_VALUES: dict[str, Authority] = {LEGACY: LEGACY, OWNER_KEYS: OWNER_KEYS}


def _unavailable() -> ProviderKeys.Error:
    return ProviderKeys.Error(ipc_errors.PROVIDER_STORE_UNAVAILABLE)


def authority(hyprial_home: str | PathLike[str] | None) -> Authority:
    """Return the configured authority of `hyprial_home`; fail closed on a damaged file."""
    if hyprial_home is None:
        return LEGACY
    try:
        text = (Path(hyprial_home) / "settings.json").read_text(encoding="utf-8")
        record = json.loads(text)
    except FileNotFoundError:
        return LEGACY
    except (OSError, ValueError):
        raise _unavailable() from None
    if not isinstance(record, dict) or not MISSPELLED_KEYS.isdisjoint(record):
        raise _unavailable()
    if SECTION not in record:
        return LEGACY
    section = record[SECTION]
    if not isinstance(section, dict):
        raise _unavailable()
    if KEY not in section:
        return LEGACY
    value = section[KEY]
    if not isinstance(value, str) or value not in _VALUES:
        raise _unavailable()
    return _VALUES[value]
