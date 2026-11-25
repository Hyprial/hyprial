# Decision record:
# - A different birth identity at a recorded pid proves the recorded process is
#   gone: birth identities are unique per process start, so the live process at
#   that pid is a later one (typically after a reboot, when pids restart).  For
#   a bootstrap-phase record (``daemon.json`` with ``phase: "bootstrap"`` and
#   ``bootstrap.json``) that proof is enough to reap the stale records under
#   the held admission locks: nothing is adopted or signalled.  Under a service
#   manager, refusing instead meant a restart loop on every reboot (KP-079).
# - The relaxation is opt-in (``reuse_proves_exit``) and never applied to the
#   serving heartbeat, where a reused pid keeps failing closed.  A matching
#   identity (the daemon really runs), an unreadable identity and a probe error
#   still refuse.
# - 2026-11-16: a process that has exited but was never reaped still answers
#   signal 0 and still shows its own birth identity.  That happens to a killed
#   detached daemon wherever PID 1 does not reap orphans (a container whose
#   init is an ordinary command); the home then refused every later start.
#   The OS reporting that exact birth in an exited state is positive proof of
#   exit, so it is accepted for every caller.  Platforms that cannot report
#   it keep refusing.
"""Read-only, fail-closed previous-process check for bootstrap admission.

The caller holds the identity/state locks and supplies an already validated
runtime record. This check never signals termination or deletes that record.
"""

from hyprial.kernel import probe_process, process_birth_identity, process_exited_unreaped


def require_previous_process_exit(
    pid: int, birth_identity: str, *, reuse_proves_exit: bool = False
) -> None:
    """Return only after the OS positively reports that the recorded PID is gone.

    A different birth at a reused PID is refused rather than adopted or killed,
    unless the caller declares ``reuse_proves_exit`` (bootstrap-phase records
    only): then the recorded process is treated as exited and nothing is signalled.
    Missing birth observations and permission errors are not evidence of death.
    This is only one B0 predicate; it does not establish an empty identity/home.
    """
    if type(pid) is not int or pid <= 0:
        raise ValueError("previous process PID must be a positive integer")
    if not isinstance(birth_identity, str) or not birth_identity.strip():
        raise ValueError("previous process birth identity is required")
    try:
        probe_process(pid)
    except ProcessLookupError:
        return
    except OSError as error:
        raise RuntimeError("previous process exit is unverified") from error
    observed = process_birth_identity(pid)
    if observed is None:
        raise RuntimeError("previous process birth identity is unverified")
    if observed != birth_identity:
        if reuse_proves_exit:
            return
        raise RuntimeError("previous process PID has been reused; refusing takeover")
    if process_exited_unreaped(pid, birth_identity):
        return
    raise RuntimeError("previous process has not exited")
