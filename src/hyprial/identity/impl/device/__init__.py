"""Local device key locations and the publishable device record (§4.1)."""

from hyprial.identity.impl.device.machine_binding import (
    MachineBinding,
    consume_rotation_event,
    machine_binding_path,
    machine_fingerprint,
    mark_first_published,
    read_machine_binding,
    write_machine_binding,
)
from hyprial.identity.impl.device.models import DeviceRecord
from hyprial.identity.impl.device.store import (
    ADDRESS_RELPATH,
    DEVICE_KEY_RELPATH,
    DEVICE_RECORD_RELPATH,
    address_path,
    device_key_path,
    device_record_path,
    read_device_record,
    write_device_record,
)

__all__ = [
    "ADDRESS_RELPATH",
    "DEVICE_KEY_RELPATH",
    "DEVICE_RECORD_RELPATH",
    "DeviceRecord",
    "MachineBinding",
    "address_path",
    "consume_rotation_event",
    "device_key_path",
    "device_record_path",
    "machine_binding_path",
    "machine_fingerprint",
    "mark_first_published",
    "read_device_record",
    "read_machine_binding",
    "write_device_record",
    "write_machine_binding",
]
