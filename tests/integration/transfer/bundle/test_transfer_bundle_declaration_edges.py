"""Pure fake review controls: no FIFO open, process, model or sandbox execution."""
from dataclasses import replace
from types import SimpleNamespace
import stat

import pytest
from hyprial.daemon.impl.transfer.archive import bundle as bundle


def exported(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'notes.txt').write_text('fixture')
    target = tmp_path / 'bundle'
    manifest = bundle.export_bundle(source, target, owner='owner', actor='agent', source_machine='source', created_at='2026-10-01')
    return source, target, manifest


def test_conflicting_present_declarations_cannot_hide_a_missing_file(tmp_path):
    _source, target, manifest = exported(tmp_path)
    bundle.write_manifest(target, replace(manifest, dependencies=(
        {'kind': 'config', 'present': False, 'path': 'config/missing.json'},
        {'kind': 'config', 'present': True, 'path': 'config/missing.json'},
    )))
    with pytest.raises(bundle.BundleError):
        bundle.validate_bundle(target)


@pytest.mark.parametrize('phase', ['planning', 'stop_write'])
def test_runtime_fifo_is_skipped_before_binary_content_read(tmp_path, monkeypatch, phase):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'notes.txt').write_text('fixture')

    class FakeRuntimeFifo:
        def is_symlink(self):
            return False
        def stat(self):
            return SimpleNamespace(st_mode=stat.S_IFIFO | 0o600)

    fifo = FakeRuntimeFifo()
    scan = bundle._iter_payload_files
    sniff = bundle._is_platform_binary
    calls = []

    def with_runtime_fifo(root):
        calls.append(root)
        yield from scan(root)
        if len(calls) == (1 if phase == 'planning' else 2):
            yield 'runtime.fifo', fifo

    def forbid_runtime_content_read(path):
        assert path is not fifo, 'binary sniff reached a runtime FIFO before the existing non-regular-file gate'
        return sniff(path)

    monkeypatch.setattr(bundle, '_iter_payload_files', with_runtime_fifo)
    monkeypatch.setattr(bundle, '_is_platform_binary', forbid_runtime_content_read)
    manifest = bundle.export_bundle(source, tmp_path / 'bundle', owner='owner', actor='agent', source_machine='source', created_at='2026-10-01')
    assert [entry.path for entry in manifest.entries] == ['notes.txt']


@pytest.mark.parametrize("declaration", ["envelope", "dependency"])
def test_declared_bundle_root_file_is_not_a_carried_payload_entry(tmp_path, declaration):
    _source, target, manifest = exported(tmp_path)
    (target / "envelope.json").write_text('{"fixture": true}')
    if declaration == "envelope":
        manifest = replace(manifest, credential_envelope="envelope.json")
    else:
        manifest = replace(manifest, dependencies=(
            {"kind": "config", "present": True, "path": "envelope.json"},
        ))
    bundle.write_manifest(target, manifest)
    with pytest.raises(bundle.BundleDeclarationMissing):
        bundle.validate_bundle(target)


def test_identical_present_declarations_remain_idempotent(tmp_path):
    _source, target, manifest = exported(tmp_path)
    declaration = {"kind": "config", "present": True, "path": "notes.txt"}
    bundle.write_manifest(target, replace(manifest, dependencies=(declaration, declaration)))
    loaded = bundle.validate_bundle(target)
    assert loaded.dependencies == (declaration,)
