---
name: e2e-resident-fixture
description: Minimal real skill directory for the E2E-006 resident-provider
  gate. Exists so the CI-controlled gate can point HYPRIAL_RESIDENT_SKILL at a
  repository-owned path instead of a developer-machine workspace. Apply this
  skill only when a message explicitly invokes it.
---

# E2E resident fixture skill

This skill is a launch fixture for `run-resident-providers.py`: the scenario
passes it to the pi resident via `--skill` to prove a real skill directory
loads from the configured path. It carries no behavioral probe (E2E-013's
runner-generated CFGPROBE skill owns the load-witness assertion), so it only
needs to be a valid, loadable skill.

When a message does not mention this skill, ignore it entirely.
