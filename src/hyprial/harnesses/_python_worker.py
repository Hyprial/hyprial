"""Packaged TypeSafe worker for the ``python_worker`` harness mechanism.

The parent process owns admission and inbox custody.  This module owns only
the v1 JSONL wire and a bounded executor; it never acknowledges, retries, or
discards an inbox row.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 8 * 1024 * 1024
DEFAULT_MODEL = "jev-latest"
CONCURRENCY = 10
EFFECTIVE_CONFIG = {
    "concurrency": CONCURRENCY,
    "mode": "pool",
    "model": DEFAULT_MODEL,
    "protocolVersion": PROTOCOL_VERSION,
}
_HEX40 = set("0123456789abcdef")


def canonical_startup_json(value: object) -> bytes:
    """Canonical UTF-8 JSON used for the startup hash golden."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def startup_hash(value: object) -> str:
    return hashlib.sha256(canonical_startup_json(value)).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> object:
    raise ValueError(f"non-finite JSON number: {value}")


class WorkerError(ValueError):
    """A sanitized, terminal worker error."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(message[:500])


CREDENTIAL_ENVIRONMENT_NAME = "TYPESAFE_API_KEY"
#: The one file the user-side ``jev`` CLI tells people to ``source``; relative
#: to HOME.  Deliberately a constant: accepting a path from the environment
#: would turn "read the credential" into "read any file".
CREDENTIAL_FILE_RELATIVE = Path(".config") / "typesafe" / "env"
CREDENTIAL_FILE_DISPLAY = "~/.config/typesafe/env"

#: Where the credential came from, as reported in the ready frame.  Never the
#: value, never the path.
CREDENTIAL_SOURCES = ("environment", "file")


class CredentialLookup:
    """Closed set of the stable codes a credential lookup can stop at.

    Environment first, then the file, then fail.  ``ENV_ABSENT`` is where the
    lookup stops when there is no environment value AND no HOME to find the
    file in; the file-side codes say which half of the file lookup failed; the
    last is the TypeSafe service refusing a credential we did find.  Kept as an
    explicit tuple, never a pattern over message text: a free-text classifier
    silently changes meaning when someone rewords an error.
    """

    ENV_ABSENT = "TYPESAFE_CREDENTIAL_ENV_ABSENT"
    FILE_ABSENT = "TYPESAFE_CREDENTIAL_FILE_ABSENT"
    KEY_ABSENT = "TYPESAFE_CREDENTIAL_KEY_ABSENT"
    PROVIDER_REJECTED = "PROVIDER_AUTHENTICATION_FAILED"
    ALL = (ENV_ABSENT, FILE_ABSENT, KEY_ABSENT, PROVIDER_REJECTED)


_CREDENTIAL_HINT = (
    f"set {CREDENTIAL_ENVIRONMENT_NAME} for this agent (hyprial agent secret), or put "
    f"`export {CREDENTIAL_ENVIRONMENT_NAME}=...` in {CREDENTIAL_FILE_DISPLAY}"
)


def _unquote(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _file_credential(home: str) -> tuple[str | None, str | None]:
    """Read only ``TYPESAFE_API_KEY`` from the credential file, never executing it.

    Returns ``(value, None)`` or ``(None, lookup_code)``.  The file is parsed, not
    sourced: optional ``export``, single/double quotes or bare, comments and blank
    lines skipped, every other key ignored, the last assignment wins (as it would
    under ``source``).  An empty value counts as absent.
    """

    path = Path(home) / CREDENTIAL_FILE_RELATIVE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, CredentialLookup.FILE_ABSENT
    found: str | None = None
    prefix = f"{CREDENTIAL_ENVIRONMENT_NAME}="
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        if stripped.startswith(prefix):
            found = _unquote(stripped[len(prefix) :])
    if not found:
        return None, CredentialLookup.KEY_ABSENT
    return found, None


def resolve_credential(
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Return ``(value, source, lookup_code)``; exactly one of value/lookup_code is set.

    An explicitly injected environment value (``hyprial agent secret``) always
    wins over the file, so the two paths coexist without either changing the other.
    """

    env = os.environ if environ is None else environ
    value = env.get(CREDENTIAL_ENVIRONMENT_NAME)
    if value:
        return value, "environment", None
    home = env.get("HOME")
    if not home:
        return None, None, CredentialLookup.ENV_ABSENT
    value, code = _file_credential(home)
    if value:
        return value, "file", None
    return None, None, code


def _credential_unavailable(code: str | None) -> WorkerError:
    # The outer code stays PROVIDER_AUTHENTICATION_FAILED: the daemon lists it as
    # a permanent failure (daemon/api.py), so the turn is not redelivered.  The
    # lookup code leads the message as a fixed token so a reader knows WHICH half
    # was missing -- it is for people deciding where to configure, not for a
    # classifier.
    return WorkerError(
        CredentialLookup.PROVIDER_REJECTED,
        f"{code}: TypeSafe credential is unavailable; {_CREDENTIAL_HINT}",
    )


def _json_content(value: object, *, allow_null: bool = True) -> bool:
    if value is None:
        return allow_null
    if isinstance(value, str):
        return True
    if isinstance(value, list):
        return all(_json_content(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_content(item) for key, item in value.items())
    return False


def _require_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise WorkerError("PROVIDER_INVALID_REQUEST", f"{label} must be an object")
    return value


def _only_keys(value: dict[str, object], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise WorkerError(
            "PROVIDER_INVALID_REQUEST",
            f"{label} has unknown fields: {', '.join(sorted(unknown))}",
        )


def _parse_questions(raw: object) -> tuple[dict[str, object], dict[str, object]]:
    questions = _require_object(raw, "questions")
    if not questions:
        raise WorkerError("PROVIDER_INVALID_REQUEST", "questions must not be empty")
    parsed: dict[str, object] = {}
    for name, raw_question in questions.items():
        if not isinstance(name, str) or not name:
            raise WorkerError("PROVIDER_INVALID_REQUEST", "question names must be non-empty strings")
        question = _require_object(raw_question, f"questions.{name}")
        _only_keys(question, {"type", "instructions", "criteria"}, f"questions.{name}")
        kind = question.get("type")
        if kind not in {"noul", "choice", "score"}:
            raise WorkerError("PROVIDER_INVALID_REQUEST", f"questions.{name}.type is invalid")
        if "instructions" in question and not _json_content(question["instructions"]):
            raise WorkerError("PROVIDER_INVALID_REQUEST", f"questions.{name}.instructions is invalid")
        criteria = question.get("criteria")
        if kind == "noul":
            if criteria is not None:
                criteria_obj = _require_object(criteria, f"questions.{name}.criteria")
                _only_keys(criteria_obj, {"true", "false"}, f"questions.{name}.criteria")
                if any(not _json_content(item) for item in criteria_obj.values()):
                    raise WorkerError("PROVIDER_INVALID_REQUEST", f"questions.{name}.criteria is invalid")
        elif kind == "choice":
            criteria_obj = _require_object(criteria, f"questions.{name}.criteria")
            if not criteria_obj or any(not isinstance(key, str) or not key for key in criteria_obj):
                raise WorkerError("PROVIDER_INVALID_REQUEST", f"questions.{name}.criteria must be non-empty")
            if any(not _json_content(item) for item in criteria_obj.values()):
                raise WorkerError("PROVIDER_INVALID_REQUEST", f"questions.{name}.criteria is invalid")
        else:
            if not isinstance(criteria, list) or not criteria or any(not _json_content(item, allow_null=False) for item in criteria):
                raise WorkerError("PROVIDER_INVALID_REQUEST", f"questions.{name}.criteria must be a non-empty array")
        parsed[name] = question
    return parsed, questions


def parse_payload(value: object) -> tuple[object, dict[str, object], str]:
    payload = _require_object(value, "payload")
    _only_keys(payload, {"state", "questions", "model"}, "payload")
    state = payload.get("state")
    if not _json_content(state, allow_null=False) or not isinstance(state, (str, dict, list)):
        raise WorkerError("PROVIDER_INVALID_REQUEST", "state must be a string, object, or array")
    questions, original = _parse_questions(payload.get("questions"))
    model = payload.get("model", DEFAULT_MODEL)
    if not isinstance(model, str) or not model:
        raise WorkerError("PROVIDER_INVALID_REQUEST", "model must be a non-empty string")
    return state, questions, model


def _sdk_questions(raw: dict[str, object]) -> dict[str, object]:
    from typesafe_sdk import Choice, Noul, Score

    result: dict[str, object] = {}
    for name, question in raw.items():
        assert isinstance(question, dict)
        kwargs: dict[str, object] = {}
        if "instructions" in question:
            kwargs["instructions"] = question["instructions"]
        kind = question["type"]
        if "criteria" in question:
            kwargs["criteria"] = question["criteria"]
        if kind == "noul":
            result[name] = Noul(**kwargs)
        elif kind == "choice":
            result[name] = Choice(**kwargs)
        else:
            result[name] = Score(**kwargs)
    return result


def _response_output(
    response: object, model: str, question_names: set[str]
) -> dict[str, object]:
    dump = getattr(response, "model_dump", None)
    value = dump(mode="json") if callable(dump) else None
    if not isinstance(value, dict):
        raise WorkerError("PROVIDER_TRANSIENT_FAILURE", "TypeSafe response was not an object", retryable=True)
    usage = value.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    answers = value.get("answers")
    if not isinstance(answers, dict) or set(answers) != question_names:
        raise WorkerError("PROVIDER_TRANSIENT_FAILURE", "TypeSafe response had no answers", retryable=True)
    return {
        "model": value.get("model") if isinstance(value.get("model"), str) else model,
        "answers": answers,
        "usage": {
            "inputTokens": usage.get("input_tokens"),
            "outputTokens": usage.get("output_tokens"),
        },
    }


def _vendor_error(error: BaseException) -> WorkerError:
    try:
        import typesafe_sdk as sdk

        if isinstance(error, (sdk.TypeSafeAuthenticationError, sdk.TypeSafePermissionDeniedError)):
            return WorkerError("PROVIDER_AUTHENTICATION_FAILED", "TypeSafe credential is unavailable")
        if isinstance(error, (sdk.TypeSafeBadRequestError, sdk.TypeSafeUnprocessableEntityError)):
            return WorkerError("PROVIDER_INVALID_REQUEST", "TypeSafe rejected the request")
        if isinstance(error, sdk.TypeSafeRateLimitError):
            return WorkerError("PROVIDER_TRANSIENT_FAILURE", "TypeSafe rate limit", retryable=True)
        if isinstance(error, (sdk.TypeSafeAPITimeoutError, sdk.TypeSafeAPIConnectionError, sdk.TypeSafeInternalServerError)):
            return WorkerError("PROVIDER_TRANSIENT_FAILURE", "TypeSafe service is temporarily unavailable", retryable=True)
    except (AttributeError, ImportError):
        pass
    return WorkerError("PROVIDER_TRANSIENT_FAILURE", "TypeSafe call failed", retryable=True)


def _resolve_commit() -> str:
    candidates: list[object] = [os.environ.get("HYPRIAL_SOURCE_COMMIT")]
    try:
        from hyprial import updates

        candidates.append(updates.read_installation("hyprial").commit)
    except Exception:
        pass
    package_root = Path(__file__).resolve()
    for parent in (package_root, *package_root.parents):
        if (parent / ".git").exists():
            try:
                candidates.append(
                    subprocess.check_output(
                        ["git", "-C", str(parent), "rev-parse", "HEAD"],
                        text=True,
                        stderr=subprocess.DEVNULL,
                        timeout=2,
                    ).strip()
                )
            except (OSError, subprocess.SubprocessError):
                pass
            break
    for value in candidates:
        if isinstance(value, str) and len(value) == 40 and set(value) <= _HEX40:
            return value
    raise WorkerError("HARNESS_START_FAILED", "Hyprial commit is unavailable")


def _startup_metadata(kind: str) -> dict[str, object]:
    try:
        sdk_version = importlib.metadata.version("typesafe-sdk")
    except importlib.metadata.PackageNotFoundError as error:
        raise WorkerError("HARNESS_START_FAILED", "typesafe-sdk is unavailable") from error
    if sdk_version != "0.7.0":
        raise WorkerError("HARNESS_START_FAILED", "typesafe-sdk version mismatch")
    commit = _resolve_commit()
    script_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    values = {
        "hyprialCommit": commit,
        "kind": kind,
        "effectiveConfig": EFFECTIVE_CONFIG,
        "typesafeSdkVersion": sdk_version,
        "scriptSha256": script_hash,
    }
    startup_hash = startup_hash_for_values(values)
    return {
        **values,
        "startupHash": startup_hash,
        "credentialSource": resolve_credential()[1],
    }


def startup_hash_for_values(values: dict[str, object]) -> str:
    return startup_hash(values)


def _emit(lock: threading.Lock, value: dict[str, object]) -> None:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    with lock:
        sys.stdout.write(encoded + "\n")
        sys.stdout.flush()


def _error_frame(
    lock: threading.Lock,
    request_id: str,
    *,
    stage: str,
    code: str,
    message: str,
    retryable: bool,
) -> None:
    _emit(
        lock,
        {
            "v": PROTOCOL_VERSION,
            "type": "error",
            "id": request_id,
            "ok": False,
            "stage": stage,
            "code": code,
            "message": message[:500],
            "retryable": retryable,
        },
    )


def _run_call(
    lock: threading.Lock,
    active: list[int],
    active_lock: threading.Lock,
    request_id: str,
    state: object,
    questions: dict[str, object],
    model: str,
) -> None:
    with active_lock:
        active[0] += 1
        current = active[0]
    started = time.monotonic()
    try:
        api_key, _source, lookup = resolve_credential()
        if not api_key:
            raise _credential_unavailable(lookup)
        from typesafe_sdk import TypeSafeClient

        client = TypeSafeClient(api_key=api_key, model=model)
        try:
            response = client.system_one(state, _sdk_questions(questions), model=model)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
        output = _response_output(response, model, set(questions))
        _emit(
            lock,
            {
                "v": PROTOCOL_VERSION,
                "type": "metric",
                "id": request_id,
                "segment": "call",
                "durationMs": max(0, int((time.monotonic() - started) * 1000)),
                "ok": True,
                "inFlight": current,
                "queueDepth": 0,
            },
        )
        _emit(lock, {"v": PROTOCOL_VERSION, "type": "result", "id": request_id, "ok": True, "output": output})
    except WorkerError as error:
        _emit(
            lock,
            {
                "v": PROTOCOL_VERSION,
                "type": "metric",
                "id": request_id,
                "segment": "call",
                "durationMs": max(0, int((time.monotonic() - started) * 1000)),
                "ok": False,
                "inFlight": current,
                "queueDepth": 0,
            },
        )
        _error_frame(lock, request_id, stage="call", code=error.code, message=str(error), retryable=error.retryable)
    except Exception as error:  # noqa: BLE001 - vendor boundary is sanitized
        mapped = _vendor_error(error)
        _emit(
            lock,
            {
                "v": PROTOCOL_VERSION,
                "type": "metric",
                "id": request_id,
                "segment": "call",
                "durationMs": max(0, int((time.monotonic() - started) * 1000)),
                "ok": False,
                "inFlight": current,
                "queueDepth": 0,
            },
        )
        _error_frame(lock, request_id, stage="call", code=mapped.code, message=str(mapped), retryable=mapped.retryable)
    finally:
        with active_lock:
            active[0] -= 1


def run(kind: str) -> int:
    if kind != "jev":
        print("python worker accepts only --kind jev", file=sys.stderr)
        return 2
    # Import/config validation happens before ready.  Importing here also keeps
    # the parent process free of the SDK dependency's network/client state.
    try:
        from typesafe_sdk import Choice, Noul, Score, TypeSafeClient  # noqa: F401

        metadata = _startup_metadata(kind)
    except WorkerError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - startup is fail-closed
        print(f"worker startup failed: {type(error).__name__}", file=sys.stderr)
        return 1

    lock = threading.Lock()
    _emit(lock, {"v": PROTOCOL_VERSION, "type": "ready", "kind": kind, "pid": os.getpid(), **metadata})
    executor = ThreadPoolExecutor(max_workers=CONCURRENCY, thread_name_prefix="hyprial-jev")
    active = [0]
    active_lock = threading.Lock()
    futures: set[Future[object]] = set()
    seen_ids: set[str] = set()
    accepting = True
    try:
        while True:
            line = sys.stdin.buffer.readline(MAX_LINE_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n") or line.endswith(b"\r\n"):
                print("protocol frame is too large or not LF-delimited", file=sys.stderr)
                return 1
            decode_started = time.monotonic()
            try:
                frame = json.loads(
                    line.decode("utf-8"),
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=_reject_non_finite,
                )
                if not isinstance(frame, dict):
                    raise ValueError("frame must be an object")
                if frame.get("v") != PROTOCOL_VERSION or frame.get("op") not in {"call", "stop"}:
                    raise ValueError("unsupported protocol frame")
                if frame["op"] == "stop":
                    if set(frame) != {"v", "op"}:
                        raise ValueError("stop has unknown fields")
                    accepting = False
                    break
                if set(frame) != {"v", "op", "id", "payload"}:
                    raise ValueError("call has unknown fields")
                request_id = frame["id"]
                if not isinstance(request_id, str) or not request_id:
                    raise ValueError("call id must be non-empty")
                if request_id in seen_ids:
                    raise ValueError("call id is already live")
                seen_ids.add(request_id)
                state, questions, model = parse_payload(frame["payload"])
                _emit(
                    lock,
                    {
                        "v": PROTOCOL_VERSION,
                        "type": "metric",
                        "id": request_id,
                        "segment": "decode",
                        "durationMs": max(0, int((time.monotonic() - decode_started) * 1000)),
                        "ok": True,
                        "inFlight": active[0],
                        "queueDepth": 0,
                    },
                )
            except WorkerError as error:
                request_id = frame.get("id", "") if isinstance(locals().get("frame"), dict) else ""
                if isinstance(request_id, str) and request_id:
                    _emit(lock, {"v": PROTOCOL_VERSION, "type": "metric", "id": request_id, "segment": "decode", "durationMs": max(0, int((time.monotonic() - decode_started) * 1000)), "ok": False, "inFlight": active[0], "queueDepth": 0})
                    _error_frame(lock, request_id, stage="decode", code=error.code, message=str(error), retryable=error.retryable)
                else:
                    print(str(error), file=sys.stderr)
                    return 1
                continue
            except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
                print(f"protocol frame rejected: {type(error).__name__}", file=sys.stderr)
                return 1
            if len(futures) >= CONCURRENCY:
                _error_frame(
                    lock,
                    request_id,
                    stage="protocol",
                    code="HARNESS_TRANSIENT_FAILURE",
                    message="worker concurrency bound exceeded",
                    retryable=True,
                )
                return 1
            future = executor.submit(_run_call, lock, active, active_lock, request_id, state, questions, model)
            futures.add(future)
            futures = {item for item in futures if not item.done()}
    finally:
        if accepting:
            # EOF is equivalent to stop for a child that has no more parent.
            accepting = False
        for future in tuple(futures):
            future.result()
        executor.shutdown(wait=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--kind")
    args, unknown = parser.parse_known_args(argv)
    if unknown or args.kind is None:
        print("usage: python -m hyprial.harnesses._python_worker --kind jev", file=sys.stderr)
        return 2
    return run(args.kind)


if __name__ == "__main__":
    raise SystemExit(main())
