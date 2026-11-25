"""Local operation provenance. This increment never sends a mutation."""

from .schema import decode, encode, exact, fail, operation_id

VERSION = "hyprial.tailnet.credential-journal.v1"
ATTEMPT_FIELDS = {"attemptId", "previousRemoteOutcome", "attemptOutcome", "responseStatus", "responseBody", "responseSha256"}


class Journal:
    def __init__(self, slot):
        self.slot = slot

    def path(self, identifier):
        operation_id(identifier)
        return (*self.slot.state, "journal", identifier + ".json")

    def load(self, identifier):
        raw = self.slot.read(self.path(identifier))
        if raw is None:
            return None
        try:
            value = decode(raw)
            base = {"version", "home", "tailnetId", "operationId", "kind", "phase",
                    "remoteOutcome", "device", "transport", "expectedGeneration",
                    "route", "request", "requestSha256", "staging"}
            if set(value) not in (base, base | ATTEMPT_FIELDS):
                raise ValueError("invalid journal fields")
            exact(value, set(value))
            if (value["version"] != VERSION or value["home"] != self.slot.identity
                    or value["tailnetId"] != self.slot.tailnet_id or value["operationId"] != identifier
                    or value["kind"] != "enroll" or value["phase"] not in ("PREPARING", "PREPARED", "SENDING", "RECEIVED")):
                raise ValueError("journal identity mismatch")
            if value["phase"] in ("PREPARING", "PREPARED"):
                if value["remoteOutcome"] != "NOT_SENT" or set(value) != base:
                    raise ValueError("invalid preparation phase")
            else:
                if set(value) != base | ATTEMPT_FIELDS:
                    raise ValueError("missing attempt state")
                operation_id(value["attemptId"])
                if value["previousRemoteOutcome"] not in ("NOT_SENT", "UNKNOWN"):
                    raise ValueError("invalid previous outcome")
                expected = ("UNKNOWN",) if value["phase"] == "SENDING" else ("SUCCESS", "REJECTED")
                if value["remoteOutcome"] not in expected:
                    raise ValueError("invalid send outcome")
            return value
        except (ValueError, TypeError, UnicodeError, RecursionError):
            fail("TAILNET_JOURNAL_INVALID", "recover")

    def save(self, value):
        self.slot.write(self.path(value["operationId"]), encode(value))

    def pending(self):
        result = []
        for name in self.slot.journals():
            if not name.endswith(".json"):
                fail("TAILNET_JOURNAL_INVALID", "recover")
            value = self.load(name[:-5])
            if value is None:
                fail("TAILNET_JOURNAL_INVALID", "recover")
            result.append(value)
        return result


def observe(value):
    return {key: value[key] for key in ("operationId", "kind", "phase", "remoteOutcome")}
