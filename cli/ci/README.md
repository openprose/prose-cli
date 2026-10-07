# Current CLI admission

Run full provider-free admission from the repository root after preparing the
pinned tools in [the contributor guide](../CONTRIBUTING.md):

```sh
python3 -m pip install --require-hashes --only-binary=:all: -r cli/ci/requirements-test.txt
python3 cli/ci/run_local.py
```

`run_local.py` strips provider credentials, build overrides and ambient hooks,
poisons ordinary proxy-based network access, launches commands without a shell,
and stops at the first failure. Gates have absolute deadlines and bounded
output. POSIX cleanup tracks the direct child and original process group; it
does not contain a detached descendant. The final success line states that limit.

Use `--list`, repeatable `--only GATE`, or `--quick` for focused development.
A quick or selected pass does not replace complete admission. Tests use fresh
roots, fixed inputs and fake harnesses/transports. Actual provider runs remain
explicit, opt-in and cost acknowledged. The frozen direct legacy-skill experiment
is retired; its historical source does not qualify either current CLI.

## Covered boundaries

The current gate graph covers shared schemas and opaque image delivery,
architecture, adapter oracle and adversarial products, version/authentication
admission, process settlement, strict Rust format/Clippy and developer builds,
Bun type checking and compiled products, shared differential behavior, hosted
operations and registry fixtures, public surfaces, package lifecycle, fresh
installations, inventories, release reproducibility and current publication
contracts. `--list` is the authority for gate names.

The installed corpus contains 50 cases. Rust and Bun implement it independently;
packaging rehearsal also runs it through npm. Synthetic capture and matrix
records verify schema, custody and privacy without claiming live success.
Non-native Windows checks do not promote native runtime admission.

`check_dependencies.py` checks exact Rust, Cargo, Bun and Python package versions
plus supported Python/Node/npm floors. `dependency_evidence.py` inventories
resolved locked graphs without network access. Packaging binds that inventory;
it does not supply independent vulnerability or signing authority.

`check_architecture.py` checks structural language and process boundaries.
Products transport opaque image/task bytes, spawn argument arrays without an
outer shell or PTY, and do not infer OpenProse semantics or install ambient
skills. Black-box tests supply behavioral evidence alongside static checks.

## Packaged SDK checks

Mac SDK construction uses PyInstaller's directory layout. Admission binds every
physical support file, directory and framework alias, complete COLLECT membership,
actual Mach-O architecture/signature targets and final hashes. Archive consumers,
fresh installation, upgrade, Homebrew and relocation verify the complete tree.
Linux retains the single-file layout and pinned native supplier/ELF closure checks.

Provider-free adapter-oracle fixtures substitute an inert helper. Their recorded
production source identity and fixture substitution remain distinct from actual
packaged-helper execution. Synthetic tree and poison controls establish rejection
behavior; native jobs must separately prove real payload size, cold startup and
installation. The helper's five-second version-probe deadline remains unchanged.

## Remote checks and release

[Source admission](../../.github/workflows/cli-ci.yml) runs the full command on
Linux x64 and macOS ARM64. Its Bash pipeline retains logs with pipefail enabled.
[Distribution rehearsal](../../.github/workflows/cli-distribution-check.yml) and
[unsigned kernel candidate construction](../../.github/workflows/cli-kernel-rc.yml)
cover Linux and macOS on x64 and ARM64. All run on pull requests and main pushes.
Jobs retain failure diagnostics and cannot publish or read provider secrets.

`check_workflows.py` parses YAML structurally, rejects duplicate keys and admits
only these three check workflows plus the protected manual publisher. It checks
immutable action pins, locked tools/dependencies, platform coverage, deadlines,
read-only build permissions and unconditional qualification. It also checks
publisher plan guards, exact artifact verification, serialized main-only
operation and explicit bootstrap credentials.

Optional workflow syntax validation uses the current inventory:

```sh
actionlint .github/workflows/cli-ci.yml \
  .github/workflows/cli-distribution-check.yml \
  .github/workflows/cli-kernel-rc.yml \
  .github/workflows/cli-publish.yml
```

Follow [the maintained release guide](../release/README.md) for development
packaging, exact release bytes and publication prerequisites. A successful test
run or PR merge does not authorize package publication. Historical results stay
bound to their measured source; the repair record lives in
[docs/ci-roll-forward.md](../../docs/ci-roll-forward.md).
