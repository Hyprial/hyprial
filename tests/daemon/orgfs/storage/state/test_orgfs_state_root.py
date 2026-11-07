"""Orgfs selects the canonical state root without importing the CLI."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]


def test_blob_defaults_have_no_shell_import():
    source = ROOT / 'src/hyprial/daemon/impl/orgfs/storage/blobs.py'
    assert not any(isinstance(node, ast.ImportFrom) and node.module == 'hyprial.cli'
                   for node in ast.walk(ast.parse(source.read_text())))


def test_explicit_state_root_precedes_explicit_home(monkeypatch, tmp_path):
    from hyprial.daemon.impl.orgfs.storage.blobs import _state_dir
    state = tmp_path / 'selected-state'
    monkeypatch.setenv('HARNESS_STATE_DIR', str(state))
    monkeypatch.setenv('HYPRIAL_HOME', str(tmp_path / 'different-home'))
    assert _state_dir() == state
    assert not state.exists()


def test_empty_state_override_falls_back_to_explicit_home(monkeypatch, tmp_path):
    from hyprial.daemon.impl.orgfs.storage.blobs import _state_dir
    home = tmp_path / 'selected-home'
    monkeypatch.setenv('HARNESS_STATE_DIR', '')
    monkeypatch.setenv('HYPRIAL_HOME', str(home))
    assert _state_dir() == home / 'state'
    assert not home.exists()
