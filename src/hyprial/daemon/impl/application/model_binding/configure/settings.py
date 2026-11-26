"""Offline operation capacity uses the existing private atomic settings writer."""

import json
from pathlib import Path

from hyprial.kernel import atomic_json_write
from hyprial.daemon.impl.desired_state.bindings.models import MODEL_BINDING_OPERATION_LIMIT


def _record(home):
    path = Path(home) / "settings.json"
    if path.is_symlink():
        raise ValueError("settings symlink is not supported")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        value = {}
    if not isinstance(value, dict) or "modelBinding.operationLimit" in value:
        raise ValueError("invalid model binding settings")
    section = value.get("modelBinding", {})
    if not isinstance(section, dict):
        raise ValueError("invalid model binding settings")
    return path, value, section


def _limit(value):
    if type(value) is not int or not MODEL_BINDING_OPERATION_LIMIT <= value <= 2**31 - 1:
        raise ValueError("operation limit must be an integer from 10000 to 2147483647")
    return value


def operation_limit(home):
    try:
        _, _, section = _record(home)
        return _limit(section.get("operationLimit", MODEL_BINDING_OPERATION_LIMIT))
    except (OSError, ValueError):
        return None


def write_model_binding_operation_limit(value, home):
    checked = _limit(value)
    path, document, section = _record(home)
    document["modelBinding"] = {**section, "operationLimit": checked}
    atomic_json_write(path, document)
    return path
