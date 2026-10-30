"""Read-only view of the sidecar's durable membership store (never written here)."""
from __future__ import annotations

import json
from pathlib import Path

from ...policy import ForwardingSidecarError
from ..settings import tailnet_settings


def store_tailnets(home: Path) -> set[str]:
    """Tailnet IDs the sidecar store already holds; an absent store holds none."""
    path = Path(tailnet_settings(home)['membershipStore'])
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return set()
    except (OSError, ValueError) as error:
        raise ForwardingSidecarError('Membership store is unreadable.', code='MEMBERSHIP_STORE_INVALID') from error
    tailnets = data.get('tailnets') if isinstance(data, dict) else None
    return set(tailnets) if isinstance(tailnets, dict) else set()
