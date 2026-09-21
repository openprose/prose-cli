# Contributing to the OpenProse CLI

This guide applies only to the independent CLI implementations under `cli/`.
Read the repository-level `CONTRIBUTING.md` before you change the OpenProse
language, skill, standard library, or examples.

The CLI is an outer runner. It selects and supervises a harness, supplies an
opaque Skill Runtime Image and task argument vector, and reports the result. It
must not parse OpenProse or introduce language semantics. Language-facing
prompts belong in the image or the installed `open-prose` skill. A harness
adapter should remain thin.

## Start here

Required development tools are Python 3.10, Rust 1.87.0 with Clippy
and rustfmt, Bun 1.3.5, Node.js 22.22.3 or newer, and npm 10 or newer. CI uses
Python 3.10.18 and Node.js 24.20.0. Use those exact versions when you need to
reproduce CI or release behavior. The hash-locked test dependencies include
Python 3.10 native wheels; use a Python 3.10 virtual environment for the install
and test commands below. Newer Python versions may select wheels whose hashes
are not in this lock file. Do not bypass hash verification.

The local admission commands below currently require macOS or Linux. Native
Windows admission fails before it starts a child process because the required
Job Object process host is not yet admitted. Windows static and cross-target
checks still run inside the POSIX admission plan and in the dedicated GitHub
workflows.

From the repository root:

```sh
python3 -m pip install --disable-pip-version-check --require-hashes \
  --only-binary=:all: -r cli/ci/requirements-test.txt
rustup toolchain install 1.87.0 --profile minimal --component clippy,rustfmt
export RUSTUP_TOOLCHAIN=1.87.0
cargo fetch --manifest-path cli/rust/Cargo.toml --locked
cargo fetch --manifest-path cli/platform/windows-process-host/Cargo.toml --locked
bun install --cwd cli/bun --frozen-lockfile
python3 cli/ci/check_dependencies.py
python3 cli/ci/run_local.py --quick
```

The exported selector makes the root-run dependency check use the just-installed
Rust toolchain; the nested Rust workspaces also carry matching toolchain files.
It affects only this shell and does not create a persistent rustup override.

The quick run is provider-free. It removes provider credentials and must not
start a real harness. Run a focused gate while iterating:

```sh
python3 cli/ci/run_local.py --list
python3 cli/ci/run_local.py --only differential-conformance
python3 cli/ci/run_local.py --only rust-tests
python3 cli/ci/run_local.py --only bun-tests
```

Before you submit a CLI change, run `python3 cli/ci/run_local.py`. The full
local admission is still mechanical evidence. It does not grant semantic,
portability, benchmark, containment, or publication authority.

## Human and agent workflow

1. Read `cli/AGENTS.md`, `cli/SPEC.md`, and the relevant directory README.
2. If you are a maintainer or agent in a coordinated shared worktree, have the
   lead record an exact path lease in `cli/protocol/OWNERSHIP.md` before any
   edit. This rule includes formatters and generators. If you work in your own
   fork or branch, do not edit `OWNERSHIP.md`; the lead creates the integration
   lease after intake.
3. Add or change the shared black-box case before product-specific behavior.
4. Implement the same observable contract in Rust and Bun. Do not make one
   implementation the oracle for the other.
5. Keep normal tests hermetic. A real-provider run must be explicit,
   cost-acknowledged, bounded, and retained separately from ordinary CI.
6. Report the exact checks that passed and any authority that remains absent.
7. Do not commit generated evidence, credentials, absolute private paths, raw
   provider transcripts, or a benchmark claim that the evidence cannot make.

Agents must not push, publish a package, create a release, or use a person's
GitHub identity without explicit authorization. An agent should leave a
reviewable diff and a concise verification record when that authorization is
absent.

## Report a vulnerability

Do not file a public issue or harness/model request, or a benchmark proposal,
for a suspected security vulnerability. Use the repository's
[private vulnerability report](https://github.com/openprose/prose-cli/security/advisories/new)
and include only the information needed to reproduce and assess the issue. Do
not include live credentials, account identifiers, or unrelated private data.

In this guide, a **surface** is one execution or distribution path, such as a
Rust archive, Bun archive, npm installation, or direct-skill session. A
benchmark **cell** is one frozen combination of surface, harness, model,
program, and platform. Evidence **authority** states which facts a measurement
can establish and which facts remain unknown.

## Choose the correct change surface

| Change | Primary location | Required checks |
| --- | --- | --- |
| Shared CLI behavior or error | `shared/` and `conformance/cases/` | Schema/fixture tests and Rust/Bun differential conformance |
| Installed harness adapter | `shared/capabilities/adapters/` | Recipe oracle, fake wire fixtures, both products, and adversarial adapter tests |
| Rust implementation | `rust/` | Formatting, strict Clippy, unit/integration tests, and shared conformance |
| Bun or npm implementation | `bun/` | Typecheck, Bun tests, package tests, and shared conformance |
| Package or release behavior | `ci/`, `release/`, and workflows | Local packaging, installed-package admission, reproducibility, and workflow policy |
| Benchmark policy or evidence | `benchmarks/` | Frozen policy/profile, write-once evidence, deterministic reanalysis, and trust review |
| Language meaning or prompt | Language-owned skill/image source, not an adapter | Language conformance and image build owned by that layer |

## Propose a harness, model, or admitted version

Use the [OpenProse CLI harness or model request](https://github.com/openprose/prose-cli/issues/new?template=openprose-cli-harness-model.yml)
issue form for one of these requests:

- a new external-process harness adapter;
- a new exact admitted version of an existing harness; or
- an exact model or authentication route for an identified harness.

Do not use this form to propose a benchmark profile or cell. The form asks for
the exact harness, adapter, transport, model, provider, and authentication
identities that apply. It also asks for the official distribution and version
evidence, supported platforms, executable discovery and repair behavior,
noninteractive protocol and lifecycle, prompt and project isolation,
authentication and billing boundaries, exact reproduction, and upstream
sources. Write `not applicable` when a field does not apply; do not guess.

The external harness remains outside this repository. Only its thin adapter,
closed capability facts, fixtures, and tests belong here. Never put a
credential, token, account identifier, private path, or raw provider transcript
in the request.

## Propose a benchmark profile or cell

Use the [OpenProse CLI benchmark profile or cell proposal](https://github.com/openprose/prose-cli/issues/new?template=openprose-cli-benchmark-profile.yml)
issue form for a new frozen benchmark profile, one exact cell in a frozen
profile, or a reviewed amendment. Do not use this form to request adapter
admission or a new admitted version.

A benchmark profile must freeze the research question, cohort, programs,
validator, cell schema, artifact and image identity rules, ordering, cache and
warmup policy, repetitions, randomized-order policy and fixed seed, timeouts,
spend stops, retries, cost observation channel, failed-attempt retention,
redaction, provenance, reanalysis, reporting rules, and nonclaims. A cell must
identify the exact surface, admitted adapter and harness version, model and
provider route, authentication profile, target artifact, Skill Runtime Image,
corpus, validator, platform, architecture, and hardware controls.

A benchmark proposal does not authorize live collection. Provider-free
validation must pass first, and a separately reviewed profile must be frozen
before an explicit, bounded, cost-acknowledged run. Never put a credential,
token, account identifier, private path, or raw provider transcript in the
proposal.

## Keep acceptance decisions separate

- Adapter admission establishes only that one exact harness adapter, version,
  platform, and lifecycle passed its stated admission gates.
- Acceptance into the adapter inventory is not acceptance into a benchmark.
  Benchmark inclusion establishes only that one exact cell is eligible under
  one frozen profile.
- Semantic conformance requires a frozen semantic corpus and validator. A
  successful adapter or benchmark trial does not make the CLI an OpenProse
  interpreter or establish Prose Completeness.
- Portability requires evidence from each claimed execution surface and target
  platform using the exact admitted artifacts.
- Public ranking requires a precommitted comparison policy, comparable
  surfaces, retained failed attempts, and explicit retry, timeout, cost, and
  uncertainty accounting.

Acceptance into a benchmark is not evidence that a model is better, portable,
semantically conformant, or suitable for public ranking. Each later claim needs
its own authority and review.

## Implement a harness adapter

Start with the shared contract. Do not add product-local behavior first.

1. Add or revise the closed recipe under
   `cli/shared/capabilities/adapters/recipes/`. Validate it against
   `cli/shared/schemas/adapter-admission-recipe.schema.json` and update
   `oracle.v1.json` only with upstream-supported facts.
2. Change `functional-alpha.v1.json` only when the exact version, platform, and
   repair route have earned that narrower admission.
3. Add generated wire scenarios under `cli/shared/fixtures/adapters/` and a
   shared black-box case under `cli/conformance/cases/adapters/`.
4. Implement the Rust adapter in `cli/rust/crates/prose-runner-core/src/`
   (`installed_adapters.rs`, configuration/invocation IDs, and runner
   inventory) and the Bun adapter in `cli/bun/src/adapters/` plus its
   configuration, invocation, and inventory surfaces.
5. Run the focused provider-free gates:

   ```sh
   python3 cli/ci/run_local.py --only shared-contracts
   python3 cli/ci/run_local.py --only adapter-oracle
   python3 cli/ci/run_local.py --only adapter-product-adversary
   python3 cli/ci/run_local.py --only rust-tests
   python3 cli/ci/run_local.py --only bun-tests
   python3 cli/ci/run_local.py --only differential-conformance
   ```

6. Complete installed discovery and explicit live admission only after the
   provider-free lifecycle and parity gates pass.

## Adapter acceptance sequence

An adapter normally advances through these gates:

1. **Proposal:** upstream facts, version, protocol, platform, authentication,
   billing, and isolation boundaries are reviewable.
2. **Recipe:** the shared schema and capability oracle admit a closed recipe.
3. **Provider-free implementation:** both products pass the same generated
   wire fixtures and adversarial lifecycle tests.
4. **Installed probe:** exact executable and version discovery succeeds
   without a provider call.
5. **Opt-in live smoke:** one exact, cost-acknowledged route proves only the
   claims measured by its runtime image and evidence schema.
6. **Benchmark inclusion:** a separately reviewed policy freezes the cohort,
   programs, order, repetitions, failures, retries, timeouts, cost authority,
   and reporting rules before collection.

Do not skip a failed gate by loosening a version allowlist, changing evidence
in place, dropping failed trials, retrying without recording the attempt, or
using one product's output as the expected result for the other.

## Pull request checklist

- The change stays inside the CLI/language boundary.
- In a coordinated shared worktree, the lead's path lease was active before
  each write. Fork contributors did not edit the ownership registry.
- Shared observable behavior has a black-box case.
- Rust and Bun agree where the shared contract requires parity.
- npm and standalone installation behavior is tested when affected.
- Provider-free tests make no network or model calls.
- Any real run was explicit, bounded, and reported with retries and failures.
- Documentation uses exact versions and commands and states its platform and
  authentication assumptions.
- Evidence and release claims do not exceed the authority actually observed.
- No secret, private path, or raw provider response is present in the diff.

For the current implementation status and known blockers, read
`cli/protocol/STATUS.md`. For release construction and local package rehearsal,
read `cli/release/README.md`.
