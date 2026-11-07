"""Legacy fixture rows cross the real migration boundary before use."""
from hyprial.daemon.impl.desired_state import DesiredState


def migrated_state(*, harnesses=()):
    return DesiredState.from_json({
        "schemaVersion": 1,
        "providers": [spec.to_json() for spec in harnesses],
        "interactiveSessions": [],
    })
