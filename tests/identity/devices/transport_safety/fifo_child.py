"""Owned subprocess entrypoint for real FIFO stage rejection tests."""

from __future__ import annotations

import sys
from pathlib import Path

from hyprial.identity import TransportCredentialError, stage_transport_credential


def main() -> None:
    print("ENTERING_STAGE", flush=True)
    try:
        stage_transport_credential(
            Path(sys.argv[1]),
            operation_id=sys.argv[2],
            owner="alice@example.test",
            device_uid=sys.argv[3],
            expected_credential_generation=0,
        )
    except TransportCredentialError as error:
        print(error.code, flush=True)
    else:
        print("RETURNED", flush=True)


if __name__ == "__main__":
    main()
