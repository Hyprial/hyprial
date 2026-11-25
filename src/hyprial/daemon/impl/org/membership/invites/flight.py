"""Share one acceptance result across IPC threads and fresh service instances."""

from concurrent.futures import Future, TimeoutError
from threading import Lock

from hyprial.daemon.impl.orgfs.api import OrgFsError

_LOCK = Lock()
_FLIGHTS: dict[tuple[object, str], Future] = {}
ACCEPT_WAIT_SECONDS = 120.0


def single_flight(key, run):
    with _LOCK:
        flight = _FLIGHTS.get(key)
        first = flight is None
        if first:
            flight = Future()
            _FLIGHTS[key] = flight
    if not first:
        try:
            return flight.result(timeout=ACCEPT_WAIT_SECONDS)
        except TimeoutError as error:
            raise OrgFsError("no-holder-online", {
                "status": "partial", "missingStep": "accept-in-flight",
                "inviteId": key[1],
            }) from error
    try:
        result = run()
        flight.set_result(result)
        return result
    except BaseException as error:
        flight.set_exception(error)
        raise
    finally:
        with _LOCK:
            _FLIGHTS.pop(key, None)
