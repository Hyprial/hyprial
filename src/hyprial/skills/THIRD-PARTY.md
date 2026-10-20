# Third-party skills

These skills are vendored from upstream projects. Do not edit them locally without
recording the divergence here.

## `stc/` — Simplified Technical Chinese (STC)

- Source: https://github.com/daimonia-ai/STC-Simplified-Technical-Chinese
- Version: skill `0.2.3` (`node .agents/skills/stc/tools/cli.mjs --version`).
- License: rules and dictionary under CC BY 4.0; code (CLI, skill tooling) under MIT.
- Purpose: Chinese writing rules for agent output — answer first, drop filler, keep
  terms consistent, keep facts and stated uncertainty; three-part task receipt
  (done / need-you / fyi).
- Scope: generation, review and rewriting of Chinese text. It does not decide facts,
  and `stc check` only reports issues its rules can decide mechanically.

## `ste100/` — ASD-STE100 Simplified Technical English

- Source: https://github.com/danyuchn/asd-ste100-skill
- Purpose: English counterpart. Two modes: strict for procedures, error messages,
  tool docs, agent-facing instructions and safety text; STE-flavored for README,
  PR descriptions and changelog prose (structure rules apply, vocabulary rules relaxed).
- Scope: English output only.

Both skills are advisory text rules. They do not gate messages, permissions or
admission; enforcement belongs to the components that own those decisions.
