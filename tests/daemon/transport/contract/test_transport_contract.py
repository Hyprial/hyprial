from __future__ import annotations

import json
import os
import socket
import time
import unittest

from hyprial.daemon.impl.transport import (
    KeySpace,
    LivelinessDirectory,
    ZenohConfig,
    ZenohTransport,
)


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class KeySpaceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.keys = KeySpace()

    def test_protocol_key_layout_is_versioned_and_identity_safe(self) -> None:
        actor = "agent:alice/mac:worker"
        key = self.keys.inbox(actor, "msg/1")
        self.assertTrue(key.startswith("hyprial/v1/inbox/"))
        self.assertNotIn("agent:alice/mac:worker", key)
        self.assertEqual(self.keys.decode_identity(key.split("/")[3]), actor)

    def test_mailbox_liveliness_uses_role_key(self) -> None:
        self.assertEqual(
            self.keys.mailbox_liveliness("node-a"),
            "hyprial/v1/liveliness/mailbox/node-a",
        )

    def test_system_notice_key_targets_daemon_without_receipt_namespace(self) -> None:
        key = self.keys.system_notice("node/a", "notice/1")
        self.assertTrue(key.startswith("hyprial/v1/system-notice/"))
        self.assertNotIn("node/a", key)
        self.assertEqual(
            self.keys.system_notice_all("node/a"),
            f"hyprial/v1/system-notice/{key.split('/')[3]}/**",
        )

    def test_org_context_keys_are_versioned_and_identity_safe(self) -> None:
        node = "node/with/slashes"
        context_key = self.keys.org_context(node)
        request_key = self.keys.org_request(node)

        self.assertTrue(context_key.startswith("hyprial/v1/org/context/"))
        self.assertTrue(request_key.startswith("hyprial/v1/org/request/"))
        self.assertNotIn(node, context_key)
        self.assertEqual(self.keys.decode_identity(context_key.rsplit("/", 1)[-1]), node)
        self.assertEqual(self.keys.org_context_any(), "hyprial/v1/org/context/*")
        self.assertEqual(self.keys.org_request_any(), "hyprial/v1/org/request/*")

    def test_zenoh_config_is_isolated_and_disables_shared_memory_by_default(
        self,
    ) -> None:
        config = ZenohConfig(
            listen=("tcp/127.0.0.1:7447",),
            connect=("tcp/127.0.0.1:7448",),
        )
        raw = json.loads(config.to_json())
        self.assertFalse(raw["scouting"]["multicast"]["enabled"])
        self.assertFalse(raw["scouting"]["gossip"]["enabled"])
        self.assertFalse(raw["transport"]["shared_memory"]["enabled"])

    def test_zenoh_config_enables_bounded_link_health_and_reconnect(self) -> None:
        raw = json.loads(ZenohConfig().to_json())

        self.assertEqual(
            raw["transport"]["link"]["tx"],
            {"lease": 30_000, "keep_alive": 4},
        )
        self.assertEqual(
            raw["connect"],
            {
                "endpoints": [],
                "exit_on_failure": False,
                "retry": {
                    "period_init_ms": 500,
                    "period_max_ms": 8_000,
                    "period_increase_factor": 2.0,
                },
            },
        )

    def test_zenoh_config_supports_programmatic_health_overrides(self) -> None:
        raw = json.loads(
            ZenohConfig(
                lease_ms=12_000,
                keep_alive=6,
                connect_retry_initial_ms=750,
                connect_retry_max_ms=12_000,
                connect_retry_multiplier=1.5,
            ).to_json()
        )

        self.assertEqual(raw["transport"]["link"]["tx"]["lease"], 12_000)
        self.assertEqual(raw["transport"]["link"]["tx"]["keep_alive"], 6)
        self.assertEqual(raw["connect"]["retry"]["period_init_ms"], 750)
        self.assertEqual(raw["connect"]["retry"]["period_max_ms"], 12_000)
        self.assertEqual(raw["connect"]["retry"]["period_increase_factor"], 1.5)

    def test_zenoh_config_supports_environment_health_overrides(self) -> None:
        names = {
            "HYPRIAL_ZENOH_LEASE_MS": "9000",
            "HYPRIAL_ZENOH_KEEP_ALIVE": "3",
            "HYPRIAL_ZENOH_CONNECT_RETRY_INITIAL_MS": "250",
            "HYPRIAL_ZENOH_CONNECT_RETRY_MAX_MS": "3000",
            "HYPRIAL_ZENOH_CONNECT_RETRY_MULTIPLIER": "1.25",
        }
        previous = {name: os.environ.get(name) for name in names}
        os.environ.update(names)
        try:
            raw = json.loads(ZenohConfig.from_environment().to_json())
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.assertEqual(raw["transport"]["link"]["tx"]["lease"], 9_000)
        self.assertEqual(raw["transport"]["link"]["tx"]["keep_alive"], 3)
        self.assertEqual(raw["connect"]["retry"]["period_init_ms"], 250)
        self.assertEqual(raw["connect"]["retry"]["period_max_ms"], 3_000)
        self.assertEqual(raw["connect"]["retry"]["period_increase_factor"], 1.25)

    def test_zenoh_config_rejects_unsafe_health_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "lease_ms"):
            ZenohConfig(lease_ms=0)
        with self.assertRaisesRegex(ValueError, "keep_alive"):
            ZenohConfig(keep_alive=0)
        with self.assertRaisesRegex(ValueError, "connect_retry_max_ms"):
            ZenohConfig(
                connect_retry_initial_ms=2_000,
                connect_retry_max_ms=1_000,
            )
        with self.assertRaisesRegex(ValueError, "connect_retry_multiplier"):
            ZenohConfig(connect_retry_multiplier=0.5)

    def test_liveliness_directory_tracks_put_and_delete(self) -> None:
        endpoint = f"tcp/127.0.0.1:{_port()}"
        owner = ZenohTransport(ZenohConfig(listen=(endpoint,)))
        observer = ZenohTransport(ZenohConfig(connect=(endpoint,)))
        directory = LivelinessDirectory(observer, self.keys)
        try:
            time.sleep(0.3)
            token = owner.declare_liveliness(self.keys.actor_liveliness("agent:a:m:w"))
            deadline = time.monotonic() + 2
            while (
                not directory.actor_online("agent:a:m:w")
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertTrue(directory.actor_online("agent:a:m:w"))
            token.close()
            deadline = time.monotonic() + 2
            while directory.actor_online("agent:a:m:w") and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(directory.actor_online("agent:a:m:w"))
        finally:
            directory.close()
            observer.close()
            owner.close()


if __name__ == "__main__":
    unittest.main()
