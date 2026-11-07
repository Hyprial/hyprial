"""A deterministic non-person tool profile for tests.

Production resolves each agent's git author at runtime; tests that only need
a stable profile pass this explicit one so no test reads the operator's real
git identity.
"""

from __future__ import annotations

from hyprial.identity import AgentToolProfile

TEST_TOOL_PROFILE = AgentToolProfile(
    profile_id="agent-test-v1",
    git_author_name="Hyprial Test Runner",
    git_author_email="test-runner@agents.invalid",
)
