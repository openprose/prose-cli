# OpenProse CLI repository guidance

**For agents.** These instructions apply throughout this repository, including
root packaging, distribution and harness integrations. Follow narrower
instructions where applicable. For changes beneath `cli/`, read
[implementation rules](cli/AGENTS.md) and [contributor guidance](cli/CONTRIBUTING.md).
This file does not change the CLI's runtime defaults or the OpenProse language.

## Prefer established operational conventions

Before implementing conventions for packages, installation, distribution,
updates, configuration, credentials, cache/state/log locations or diagnostics,
inspect the current official documentation or source of comparable established
tools. Relevant peers include Codex, Claude Code, OpenCode and Prime Agent.
Record the comparison in the task, design note or PR before implementation:
sources, areas of agreement, meaningful differences and the selected approach.

Where peers agree, prefer their established convention. Do not introduce a
custom alternative without first documenting the concrete requirement that
existing conventions cannot satisfy, its user impact and any migration cost.
Where peers differ, report that variation and select an established approach
suited to the product; do not manufacture consensus or require unanimity.
Explicit user decisions take precedence over this general selection guidance.

Keep user preferences independent of whether the executable comes from npm,
npx, Homebrew or a standalone archive. Distinguish installation artifacts,
user configuration, credentials and reconstructible caches. Preserve explicit
user overrides during upgrades. Missing configuration should inherit built-in
defaults; do not write those defaults as user overrides automatically.

This constraint governs operational plumbing. It does not authorize copying
another tool's language semantics, bypassing local controls, broadening
permissions or claiming capabilities that have not been implemented and tested.
