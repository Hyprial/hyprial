import hashlib
from pathlib import Path

import pytest

from hyprial.daemon.impl.terminals.acquisition.download import download_hc_archive
from hyprial.daemon.impl.terminals.acquisition.lock import HcTargetSpec


class Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self.body
        body, self.body = self.body[:size], self.body[size:]
        return body


def spec(body: bytes) -> HcTargetSpec:
    return HcTargetSpec(
        url="https://example.test/hc.tar.gz",
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
    )


def test_download_checks_size_hash_and_user_agent(tmp_path: Path) -> None:
    body = b"verified archive"
    requests: list[object] = []

    def opener(request: object, timeout: float | None = None, **_kwargs: object) -> Response:
        requests.append(request)
        assert timeout == 30.0
        return Response(body)

    destination = tmp_path / "archive.bin"
    result = download_hc_archive(spec(body), destination, opener=opener)
    assert result == destination
    assert destination.read_bytes() == body
    assert requests[0].get_header("User-agent").startswith("hyprial-daemon/")


def test_download_retries_transient_failure(tmp_path: Path) -> None:
    body = b"retry archive"
    calls = 0
    sleeps: list[float] = []

    def opener(_request: object, _timeout: float | None = None, **_kwargs: object) -> Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("temporary")
        return Response(body)

    destination = tmp_path / "archive.bin"
    download_hc_archive(spec(body), destination, opener=opener, sleep=sleeps.append)
    assert calls == 2
    assert sleeps == [0.5]


@pytest.mark.parametrize("body", [b"short", b"exact"])
def test_download_removes_failed_file(tmp_path: Path, body: bytes) -> None:
    expected = HcTargetSpec(
        url="https://example.test/hc.tar.gz",
        sha256=hashlib.sha256(b"different").hexdigest(),
        size=len(body) + 1,
    )
    destination = tmp_path / "archive.bin"

    def opener(_request: object, _timeout: float | None = None, **_kwargs: object) -> Response:
        return Response(body)

    with pytest.raises(Exception):
        download_hc_archive(expected, destination, opener=opener)
    assert not destination.exists()
    assert not tuple(tmp_path.glob(".download-*"))


def test_download_rejects_hash_mismatch_before_return(tmp_path: Path) -> None:
    body = b"same size"
    expected = HcTargetSpec(
        url="https://example.test/hc.tar.gz",
        sha256=hashlib.sha256(b"different").hexdigest(),
        size=len(body),
    )
    with pytest.raises(Exception):
        download_hc_archive(expected, tmp_path / "archive.bin", opener=lambda *_: Response(body))
