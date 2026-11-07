"""Cache immutable births without trusting stale PIDs or changing old markers."""

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from hyprial.daemon.impl.mcp.channel import ownership

from .support import REFERENCE, probes as probes


def test_released_055_markers_match_and_ps_runs_once_per_birth(probes):
    assert REFERENCE['head'] == '90d5c535b719567661ab74125ff7648fba1d0440'
    for row in probes.markers:
        for _ in range(4):
            observed = ownership._read_process_identity(row['pid'])
            assert observed == row['processIdentity']
            assert probes.status(row['pid'], row['processIdentity']) is ownership._OwnerProcessStatus.ALIVE
    assert len(probes.calls) == len(probes.markers)


def test_concurrent_first_read_collapses_to_one_ps(probes):
    row = probes.markers[0]
    probes.delay = 0.01
    with ThreadPoolExecutor(max_workers=16) as executor:
        markers = list(executor.map(ownership._read_process_identity, [row['pid']] * 16))
    assert markers == [row['processIdentity']] * 16
    assert probes.calls == [row['pid']]


def test_concurrent_failed_probe_is_shared_without_caching_failure(probes):
    row = probes.markers[0]
    probes.delay = 0.05
    barrier = threading.Barrier(16)

    def failed_ps(_pid):
        raise OSError('transient concurrent ps failure')

    def read(_index):
        barrier.wait(timeout=2)
        return ownership._read_process_identity(row['pid'])

    probes.on_ps = failed_ps
    with ThreadPoolExecutor(max_workers=16) as executor:
        markers = list(executor.map(read, range(16)))
    assert all(marker and 'ps-lstart:' not in marker for marker in markers)
    # A call arriving after a failed flight may legitimately retry it; the
    # concurrently waiting cohort must not serialize sixteen new ps probes.
    assert len(probes.calls) <= 2
    previous = len(probes.calls)
    probes.on_ps = None
    probes.delay = 0
    assert ownership._read_process_identity(row['pid']) == row['processIdentity']
    assert len(probes.calls) == previous + 1


@pytest.mark.parametrize('same_second', [False, True])
def test_pid_reuse_refreshes_cache_and_fences_the_old_marker(probes, same_second):
    row = probes.markers[0]
    pid = row['pid']
    original = ownership._read_process_identity(pid)
    assert ownership._read_process_identity(pid) == original
    seconds, micros = probes.births[pid]
    probes.births[pid] = (seconds if same_second else seconds + 10, micros + 1)
    if not same_second:
        probes.ps_text[pid] = 'a different process birth'
    observed = ownership._read_process_identity(pid)
    assert observed != original
    assert probes.status(pid, original) is ownership._OwnerProcessStatus.IDENTITY_MISMATCH
    assert len(probes.calls) == 2


def test_native_failure_never_reuses_cached_native_evidence(probes):
    row = probes.markers[0]
    pid = row['pid']
    assert ownership._read_process_identity(pid) == row['processIdentity']
    probes.births[pid] = None
    assert ownership._read_process_identity(pid) == 'ps-lstart:' + row['psText']
    assert len(probes.calls) == 2
    probes.ps_text[pid] = 'another birth'
    assert probes.status(pid, row['processIdentity']) is ownership._OwnerProcessStatus.IDENTITY_MISMATCH
    probes.fail = True
    assert probes.status(pid, row['processIdentity']) is ownership._OwnerProcessStatus.UNKNOWN


def test_failed_ps_is_retried_and_never_frozen_in_the_cache(probes):
    row = probes.markers[0]
    probes.fail = True
    degraded = ownership._read_process_identity(row['pid'])
    assert degraded is not None and 'ps-lstart:' not in degraded
    probes.fail = False
    assert ownership._read_process_identity(row['pid']) == row['processIdentity']
    assert ownership._read_process_identity(row['pid']) == row['processIdentity']
    assert len(probes.calls) == 2


def test_cache_evicts_safely_at_its_bound(probes, monkeypatch):
    monkeypatch.setattr(ownership, '_DARWIN_IDENTITY_CACHE_LIMIT', 2, raising=False)
    first, second, third = [row['pid'] for row in probes.markers[:3]]
    for pid in (first, second, first):
        ownership._read_process_identity(pid)
    assert len(probes.calls) == 2
    ownership._read_process_identity(third)
    ownership._read_process_identity(second)
    assert len(probes.calls) == 4
    assert len(ownership._DARWIN_IDENTITY_CACHE) <= 2


def test_birth_change_during_ps_does_not_publish_a_hybrid_marker(probes):
    row = probes.markers[0]
    pid = row['pid']
    old = probes.births[pid]
    new = (old[0] + 10, old[1])

    def replace_process(observed_pid):
        probes.births[observed_pid] = new
        probes.ps_text[observed_pid] = 'new process birth'
        probes.on_ps = None

    probes.on_ps = replace_process
    marker = ownership._read_process_identity(pid)
    assert marker.startswith(f'darwin-starttime:{new[0]}:{new[1]};')
    assert marker.endswith('ps-lstart:new process birth')
    assert probes.status(pid, row['processIdentity']) is ownership._OwnerProcessStatus.IDENTITY_MISMATCH
    assert len(probes.calls) == 2


def test_repeated_birth_changes_return_unknown_without_caching(probes):
    pid = probes.markers[0]['pid']

    def change_birth(observed_pid):
        seconds, micros = probes.births[observed_pid]
        probes.births[observed_pid] = seconds, micros + 1

    probes.on_ps = change_birth
    assert ownership._read_process_identity(pid) is None
    assert len(probes.calls) == 2


def test_native_confirmation_failure_does_not_cache_a_hybrid(probes):
    row = probes.markers[0]
    pid = row['pid']
    probes.on_ps = lambda observed: probes.births.__setitem__(observed, None)
    assert ownership._read_process_identity(pid) == 'ps-lstart:' + row['psText']
    probes.births[pid] = tuple(row['start'])
    probes.on_ps = None
    assert ownership._read_process_identity(pid) == row['processIdentity']
    assert len(probes.calls) == 2


def test_node_coarse_and_legacy_ps_markers_remain_compatible(probes):
    row = probes.markers[0]
    observed = ownership._read_process_identity(row['pid'])
    for expected in ('ps-lstart:' + row['psText'], f"darwin-startsec:{row['start'][0] - 1}"):
        assert ownership._process_identities_match(expected, observed)
