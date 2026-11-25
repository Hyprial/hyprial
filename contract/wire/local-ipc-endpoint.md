# Local daemon IPC endpoint contract

How a client reaches the owning daemon's local IPC endpoint, `<state>/daemon.sock`,
on every platform. The Python authority is
`src/hyprial/kernel/impl/manysys/ipc/windows_pipe.py` (`PipeListener.__init__` writes
the endpoint file, `connect()` reads and validates it); this document pins the
same convention for every implementation, Python or Node.

## POSIX

`<state>/daemon.sock` IS the channel: a Unix domain socket. A client connects
to that path directly.

## Windows

`<state>/daemon.sock` is a JSON endpoint **file**. The real channel is the
named pipe its `pipe` member names:

```json
{
  "transport": "windows-pipe-v1",
  "pipe": "\\\\.\\pipe\\hyprial-0f1e2d3c4b5a69788796a5b4c3d2e1f0"
}
```

The suffix after `\\.\pipe\hyprial-` is 32 lowercase hexadecimal characters
(a UUID4 hex). The pipe is current-user scoped by the listener's DACL; clients
connect anonymously (no bearer credential travels in the endpoint file).

## Client resolution and validation

Given the endpoint path, a client resolves it before connecting:

* On non-Windows platforms the path is returned unchanged; the file is not
  read. (`platform` is `os.name`/`sys.platform` on the Python side,
  `process.platform` on the Node side.)
* On Windows the endpoint file is read and must satisfy ALL of:
  * the document is at most 4096 bytes (the Python authority reads 4096
    characters of UTF-8 and rejects anything longer);
  * it parses as JSON and is an object (not an array, string, number, or null);
  * `transport` equals `windows-pipe-v1`;
  * `pipe` is a string starting with `\\.\pipe\hyprial-`;
  * the remainder after that prefix is exactly 32 characters, each in
    `0123456789abcdef`.
* Unknown object members are ignored (rolling upgrade headroom).
* Any violation raises/rejects with `invalid local Windows IPC endpoint`; a
  missing or unreadable endpoint file is NOT a format verdict and surfaces as
  the underlying I/O error (the daemon being down is a different failure than
  a malformed endpoint).

On success the client connects to the pipe name, never to the endpoint file
path itself.

## Implementations

* Python: the kernel local-IPC wrapper (listen and connect).
* Node: exactly one shared client module,
  `src/hyprial/daemon/impl/harnesses/pi/resources/pi_local_ipc.ts`
  (`resolveLocalIpcEndpoint` / `connectLocalIpc`). Node only **connects**;
  listening lives in the Python wrapper. `scripts/ci/check_node_net_apis.py`
  keeps `net.createConnection` / `net.connect` / `net.createServer` out of
  every other `.ts` / `.mjs` / `.js` / `.cjs` file under `src/hyprial`.

## Samples

`local-ipc-endpoint-samples.json` carries valid and invalid endpoint-file
contents with per-case expectations; the cross-implementation test suites
drive their client through every case. The invalid rows cover at least:
non-JSON, non-object, wrong/missing `transport`, wrong pipe prefix, suffix of
the wrong length, suffix with uppercase or non-hex characters, and a document
over 4096 bytes.
