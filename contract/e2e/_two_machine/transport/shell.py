from __future__ import annotations

import subprocess
from typing import Any

def redact_text(ports, text: str | bytes | None) -> str | bytes | None:
    """Same masking for free text: pip/uv/curl echo proxy URLs into their
    output, so a failed build's stdout/stderr tail can carry them too."""

    if text is None:
        return None
    if isinstance(text, bytes):
        return ports._URL_USERINFO_BYTES.sub(rb"\g<scheme>***@", text)
    return ports._URL_USERINFO.sub(r"\g<scheme>***@", text)

def assert_endpoints(ports, 
    label: str, value: dict[str, object], listen: str, connect: str
) -> None:
    zenoh = ports.zenoh_of(value)
    if zenoh.get("listen") != [listen]:
        raise AssertionError(f"{label} listen {zenoh.get('listen')} != [{listen}]")
    if zenoh.get("connect") != [connect]:
        raise AssertionError(f"{label} connect {zenoh.get('connect')} != [{connect}]")

def sh(ports, 
    args: list[str], *, timeout: float = 60.0, check: bool = True
) -> subprocess.CompletedProcess[str]:
    try:
        completed = ports.subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except ports.subprocess.TimeoutExpired as expired:
        raise ports.subprocess.TimeoutExpired(
            ports.redact_argv(args),
            expired.timeout,
            output=ports.redact_text(expired.output),
            stderr=ports.redact_text(expired.stderr),
        ) from None
    if check and completed.returncode != 0:
        raise ports.DockerError(
            f"{' '.join(ports.redact_argv(args))} exited {completed.returncode}: "
            f"stdout={ports.redact_text(completed.stdout[-2000:])!r} "
            f"stderr={ports.redact_text(completed.stderr[-2000:])!r}"
        )
    return completed

def _install_signal_handlers(ports, ) -> dict[int, Any]:
    """Convert SIGINT/SIGTERM into RunnerInterrupted so the finally runs."""

    previous: dict[int, ports.Any] = {}

    def interrupt(_signum: int, _frame: ports.Any) -> None:
        raise ports.RunnerInterrupted(f"interrupted by signal {_signum}")

    if ports.threading.current_thread() is ports.threading.main_thread():
        for signum in (ports.signal.SIGINT, ports.signal.SIGTERM):
            previous[signum] = ports.signal.getsignal(signum)
            ports.signal.signal(signum, interrupt)
    return previous

def docker(ports, args: list[str], *, timeout: float = 60.0) -> str:
    return ports.sh(["docker", *args], timeout=timeout).stdout.strip()


def redact_argv(ports, args: list[str]) -> list[str]:
    """Mask URL userinfo (``user:pass@``) in every argument, by value.

    The E2E-001 build passes the job's proxy as ``--build-arg HTTPS_PROXY=…``;
    a proxy URL may carry credentials, and the argv lands in DockerError /
    TimeoutExpired text, i.e. the CI log.  Redacting by value (any URL with
    userinfo) rather than by key also covers args this function never heard of.
    """

    return [ports._URL_USERINFO.sub(r"\g<scheme>***@", arg) for arg in args]

def wait_for(ports, 
    probe,
    accept,
    *,
    timeout: float,
    interval: float = 0.5,
    what: str = "condition",
):
    """Poll until `accept(probe())` is true or `timeout` expires.

    Probe failures are transient by design (containers boot, docker exec can
    fail for a second), so they are retried rather than raised mid-poll.  The
    last failure is retained separately and re-raised on timeout: the accept
    predicate only ever sees real probe values, and when a poll ultimately
    fails, the ORIGINAL error (e.g. "container is not running") surfaces
    instead of an unrelated AttributeError from inspecting an exception
    object.
    """

    deadline = ports.time.monotonic() + timeout
    last_value = None
    last_error: BaseException | None = None
    while ports.time.monotonic() < deadline:
        try:
            last_value = probe()
        except (ports.DockerError, AssertionError) as error:
            last_error = error
            last_value = None
            ports.time.sleep(interval)
            continue
        last_error = None
        if accept(last_value):
            return last_value
        ports.time.sleep(interval)
    if last_error is not None:
        raise last_error
    raise AssertionError(
        f"{what} did not become true within {timeout}s; last={last_value!r}"
    )
