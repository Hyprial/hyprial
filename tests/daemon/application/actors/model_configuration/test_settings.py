"""The offline capacity switch preserves unrelated identity and receipts."""

import json
import stat

import pytest

from hyprial.daemon.impl.application.model_binding.configure.settings import (
    operation_limit, write_model_binding_operation_limit,
)


def test_missing_and_invalid_settings_are_distinct(tmp_path):
    assert operation_limit(tmp_path) == 10000
    path = tmp_path / "settings.json"
    for content in ("{", "[]", '{"modelBinding":[]}', '{"modelBinding":{"operationLimit":true}}',
                    '{"modelBinding":{"operationLimit":9999}}', '{"modelBinding.operationLimit":10000}'):
        path.write_text(content)
        assert operation_limit(tmp_path) is None


def test_writer_preserves_identity_other_sections_and_uses_private_atomic_file(tmp_path):
    path = tmp_path / "settings.json"
    original = {"identity": {"sub": "synthetic"}, "other": [1, 2], "modelBinding": {"future": "keep"}}
    path.write_text(json.dumps(original))
    assert write_model_binding_operation_limit(20000, tmp_path) == path
    assert json.loads(path.read_text()) == {**original, "modelBinding": {"future": "keep", "operationLimit": 20000}}
    assert operation_limit(tmp_path) == 20000
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    before = path.read_bytes()
    for value in (True, "10000", 10000.0, 9999, 2**31):
        with pytest.raises(ValueError):
            write_model_binding_operation_limit(value, tmp_path)
        assert path.read_bytes() == before


def test_corrupt_or_symlink_settings_not_clobbered(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{")
    with pytest.raises(ValueError):
        write_model_binding_operation_limit(20000, tmp_path)
    assert path.read_text() == "{"
    path.unlink()
    target = tmp_path / "target"
    target.write_text("{}")
    path.symlink_to(target)
    with pytest.raises(ValueError):
        write_model_binding_operation_limit(20000, tmp_path)
    assert target.read_text() == "{}"
