"""Every stdio record of a stamped process names its time and its instance.

A service-managed daemon's stdout/stderr is one file that spans restarts, so a
line that says only ``{"ok":false,"code":"DAEMON_OWNERSHIP_BUSY"}`` proves an
error happened but not *which* restart produced it (M-hq-adjutant-0002).  The
stamper adds ``t`` (UTC ISO-8601, milliseconds) and the instance -- ``epoch``
once the runtime minted one, ``pid`` always -- without changing what a reader
parses: a JSON object gains fields, everything else gains a prefix.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys

import pytest

from hyprial.kernel import LogStamp, stamp_std_streams

ISO_MILLISECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _stamp(epoch: str | None = None) -> LogStamp:
    stamp = LogStamp()
    if epoch is not None:
        stamp.bind_epoch(epoch)
    return stamp


def _write(stamp: LogStamp, text: str) -> tuple[str, str]:
    """Write ``text`` to a stamped stdout; return (stdout, stderr) captures."""

    out, err = io.StringIO(), io.StringIO()
    with stamp_std_streams(stamp, stdout=out, stderr=err):
        sys.stdout.write(text)
        sys.stdout.flush()
    return out.getvalue(), err.getvalue()


def test_json_envelope_gains_time_and_pid_fields() -> None:
    line = '{"ok":false,"code":"DAEMON_OWNERSHIP_BUSY","error":"busy"}\n'

    captured, _ = _write(_stamp(), line)

    assert captured.endswith("\n")
    payload = json.loads(captured)
    # The envelope a consumer already parses is unchanged.
    assert payload["ok"] is False
    assert payload["code"] == "DAEMON_OWNERSHIP_BUSY"
    assert payload["error"] == "busy"
    # ... and the stamp is part of the same record.
    assert ISO_MILLISECONDS.match(payload["t"]), payload["t"]
    assert payload["pid"] == os.getpid()
    assert "epoch" not in payload


def test_epoch_names_the_daemon_instance_once_it_exists() -> None:
    captured, _ = _write(_stamp("epoch-a7"), '{"ok":true,"stopped":true}\n')

    payload = json.loads(captured)
    assert payload["epoch"] == "epoch-a7"
    assert payload["pid"] == os.getpid()
    assert ISO_MILLISECONDS.match(payload["t"]), payload["t"]


def test_empty_json_object_stays_valid_json() -> None:
    captured, _ = _write(_stamp(), "{}\n")

    payload = json.loads(captured)
    assert ISO_MILLISECONDS.match(payload["t"]), payload["t"]
    assert payload["pid"] == os.getpid()


def test_plain_text_lines_carry_a_prefix() -> None:
    captured, _ = _write(_stamp("epoch-b1"), "daemon shutting down\n")

    assert captured.startswith("t="), captured
    assert ISO_MILLISECONDS.match(captured[2:].split(" ")[0])
    assert " pid=" in captured
    assert " epoch=epoch-b1 " in captured
    assert captured.endswith(" daemon shutting down\n")


def test_each_line_of_a_multi_line_write_is_stamped() -> None:
    captured, _ = _write(_stamp(), "first\nsecond\n")

    lines = captured.splitlines()
    assert len(lines) == 2
    assert all(line.startswith("t=") for line in lines), lines
    assert lines[0].endswith(" first")
    assert lines[1].endswith(" second")


def test_stderr_is_stamped_with_its_own_time() -> None:
    out, err = io.StringIO(), io.StringIO()
    with stamp_std_streams(_stamp("epoch-c2"), stdout=out, stderr=err):
        sys.stderr.write('{"event":"daemon.start.failed","phase":"ipc"}\n')
        sys.stderr.flush()

    payload = json.loads(err.getvalue())
    assert payload["event"] == "daemon.start.failed"
    assert payload["epoch"] == "epoch-c2"
    assert ISO_MILLISECONDS.match(payload["t"]), payload["t"]
    assert out.getvalue() == ""


def test_a_split_json_record_is_never_corrupted_by_a_prefix() -> None:
    out = io.StringIO()
    with stamp_std_streams(_stamp(), stdout=out, stderr=io.StringIO()):
        sys.stdout.write('{"ok":')
        sys.stdout.write("false}\n")

    payload = json.loads(out.getvalue())
    assert payload["ok"] is False
    assert ISO_MILLISECONDS.match(payload["t"])
    assert payload["pid"] == os.getpid()


def test_a_pretty_printed_json_block_is_stamped_without_corruption() -> None:
    text = '{\n  "ok": false,\n  "code": "X"\n}\n'

    captured, _ = _write(_stamp(), text)

    payload = json.loads(captured)
    assert payload["code"] == "X"
    assert ISO_MILLISECONDS.match(payload["t"])
    assert payload["pid"] == os.getpid()


def test_json_dump_chunks_and_flush_keep_nested_json_valid() -> None:
    out = io.StringIO()
    with stamp_std_streams(_stamp("epoch-d3"), stdout=out, stderr=io.StringIO()):
        for chunk in json.JSONEncoder(indent=2).iterencode({"nested": {"ok": False}}):
            sys.stdout.write(chunk)
            sys.stdout.flush()
        sys.stdout.write("\nnext line\n")

    decoder = json.JSONDecoder()
    payload, end = decoder.raw_decode(out.getvalue())
    assert payload["nested"] == {"ok": False}
    assert payload["epoch"] == "epoch-d3"
    assert out.getvalue()[end:].strip().startswith("t=")


def test_existing_stamp_keys_do_not_override_current_instance() -> None:
    captured, _ = _write(_stamp("current"), '{"t":"old","pid":1,"epoch":"old"}\n')
    payload = json.loads(captured)
    assert ISO_MILLISECONDS.match(payload["t"])
    assert payload["pid"] == os.getpid()
    assert payload["epoch"] == "current"
    assert len(json.loads(captured, object_pairs_hook=list)) == 3


def test_braced_non_json_does_not_hide_later_diagnostics() -> None:
    out = io.StringIO()
    with stamp_std_streams(_stamp(), stdout=out, stderr=io.StringIO()):
        sys.stdout.write("{not JSON}\nnext error\n")
        sys.stdout.flush()
        lines = out.getvalue().splitlines()
        assert len(lines) == 2
        assert all(line.startswith("t=") for line in lines)
        assert lines[-1].endswith("next error")


def test_the_original_streams_are_restored_after_the_context() -> None:
    before_out, before_err = sys.stdout, sys.stderr

    with stamp_std_streams(_stamp(), stdout=io.StringIO(), stderr=io.StringIO()):
        assert sys.stdout is not before_out
        assert sys.stderr is not before_err

    assert sys.stdout is before_out
    assert sys.stderr is before_err


def test_a_non_text_write_is_rejected() -> None:
    with stamp_std_streams(_stamp(), stdout=io.StringIO(), stderr=io.StringIO()):
        with pytest.raises(TypeError):
            sys.stdout.write(b"bytes")  # type: ignore[arg-type]


def test_flush_reaches_the_underlying_stream() -> None:
    class _Recorder(io.StringIO):
        flushed = 0

        def flush(self) -> None:
            type(self).flushed += 1
            super().flush()

    recorder = _Recorder()
    with stamp_std_streams(_stamp(), stdout=recorder, stderr=io.StringIO()):
        sys.stdout.write("x\n")
        sys.stdout.flush()

    assert recorder.flushed >= 1
    assert recorder.getvalue().endswith(" x\n")
