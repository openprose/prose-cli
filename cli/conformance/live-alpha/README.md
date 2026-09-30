# Functional-alpha live acceptance

This lane verifies the first downloadable-product journey against an actual
installed harness. While the embedded `echo-v0` image is active, it proves only
that the selected harness was discovered, launched noninteractively, received
the exact opaque task, and returned the image-owned echo terminal envelope.
It does **not** claim that `hello.prose.md` was parsed or executed.

The frozen user journey is:

```sh
PROSE=/absolute/path/to/the/downloaded/prose
"$PROSE" cli harness use codex
"$PROSE" cli doctor
"$PROSE" run cli/conformance/live-alpha/hello.prose.md
```

Use `claude`, `prime`, or `omp` in place of `codex`. Codex and Claude use their
own installed login/subscription state. Prime and OMP require an explicit model
and credential route:

```sh
"$PROSE" cli harness use prime \
  --model openai-codex/gpt-5.4 \
  --auth-profile prime-harness-login
"$PROSE" cli doctor
"$PROSE" run cli/conformance/live-alpha/hello.prose.md
```

The model above is illustrative: replace it with a model available through
your installed Prime harness login. Environment-key profiles are a separate,
explicit route and require their matching key, optionally supplied to the live
driver through `--env-file`.
`PROSE_MODEL` and `PROSE_AUTH_PROFILE` remain environment-based configuration
for individual invocations, but the live driver deliberately supplies its
model/profile as explicit `cli harness use` options and proves that later
doctor/run commands need no override. The live evidence driver additionally
requires the explicit `--acknowledge-provider-cost` switch before it will
validate or start a candidate.

Run one explicit candidate/harness cell through the evidence driver with:

```sh
python3 cli/conformance/live-alpha/run.py \
  --candidate /absolute/path/to/prose \
  --harness codex \
  --model openai/gpt-5.4 \
  --auth-profile cached-chatgpt-login \
  --acknowledge-provider-cost \
  --out /tmp/openprose-live-alpha-codex.json
```

When the candidate is the npm-installed launcher, add `--surface npm`. Direct
Rust and Bun binaries are identified from their runner result; a declared
surface that disagrees with that result is rejected.

Version 5 evidence records the candidate as a closed, ordered custody set. A
direct Rust or Bun surface has one `executable` member. An npm surface has
exactly four members in launch order: `launcher`, `meta-package-json`,
`platform-package-json`, and `native-executable`. The npm metadata also binds
the selected Node command and canonical executable identities, its observed
`process.platform`, `process.arch`, and Linux libc, the derived platform
identifier, exact meta/platform package names and versions, and the declared
native executable path, byte length, and SHA-256 digest. The Node probe is run
through the exact reauthenticated executable selected by the sanitized
`PATH`, with bounded output; Python host-platform guessing is not used. The
driver mirrors the launcher's nested-then-hoisted platform-package resolution,
rejects duplicate selected packages, and requires the meta package's exact
optional-dependency version and the platform manifest's executable integrity
fields to agree with the admitted native bytes. Every candidate member must be
a bounded, nonempty regular file whose raw absolute path and every ancestor
pass exact `lstat`/realpath checks without symlinks or aliases.

Every v5 record is also bound to the platform on which the journey actually
ran. The driver resolves one exact Node executable from its sanitized `PATH`,
authenticates its bytes and route, and uses that executable to report the OS,
architecture, and Linux libc. It reauthenticates that runtime at every
candidate boundary. npm evidence must additionally agree exactly with the
selected platform package and its already-admitted Node identity. This target
probe is evidence-collection infrastructure; it is not a runtime dependency of
the standalone CLI packages.

Every live cell requires an explicit `--model` and `--auth-profile`. The
profile must belong to the selected harness's closed profile set; evidence
retains only its non-secret name and category, never credentials, account IDs,
or provider responses. Pass `--env-file` when the chosen route needs explicit
credentials. The driver never forwards the caller's general environment. Its
closed base is limited to executable lookup, OS/user identity, HOME-backed
cached-login discovery, temporary-directory, shell, locale, and timezone
variables: `PATH`, `PATHEXT`, `HOME`, `USER`, `LOGNAME`, `USERPROFILE`,
`SHELL`, `SystemRoot`, `WINDIR`, `COMSPEC`, `TMPDIR`, `TMP`, `TEMP`, `LANG`,
`LANGUAGE`, `LC_ALL`, `LC_CTYPE`, `TZ`, and `__CF_USER_TEXT_ENCODING` when
present. Ambient provider keys, arbitrary tokens and database URLs, proxy
settings, SSH-agent sockets, build/runtime injection controls, XDG roots, and
all ambient `PROSE_`/`OPENPROSE_` controls are excluded. The only credential
names added are closed allowlisted names actually parsed from `--env-file`;
their values are never printed or retained. The live driver supplies a fresh,
owned `XDG_CONFIG_HOME` and passes the selected model/profile only as explicit
options to `cli harness use`. It creates an isolated user configuration,
persists the selected harness bundle, calls `doctor`, and then calls
`run` through that saved selection. It does not add a harness or transport
override, model, profile, or `PROSE_` setting to the latter two commands. Each
candidate/harness pair is a separate
cost-acknowledged invocation and evidence file. The command shape alone is not
evidence of a completed run.

OMP admission is pinned to the ordinary upstream
`@oh-my-pi/pi-coding-agent@18.0.9` package and exact `omp/18.0.9` version
output. The CLI does not extrapolate that audit to arbitrary 18.x releases.
The v5 evidence driver independently rechecks every observed harness version
against the recipe's exact, nonempty `support.admittedVersions` list after the
recipe-owned `versionPattern` extracts one version. It binds that exact list,
the exact recipe `repairCommand`, and the recipe digest; requires the doctor
and run probes to agree; and rejects adjacent versions or admission/repair
drift. `support.versionRange`, when present in a recipe, is descriptive only
and is never an admission authority in this lane. It also
records the recipe-bound prompt placement and isolation guarantee; advisory or
unsupported harness isolation is never promoted into a stronger claim merely
because the driver's own CLI-selection config root was isolated.
The complete candidate closure is authenticated immediately before selection,
after selection, after doctor, after run, and once more at final settlement.
For npm, those boundaries also reauthenticate the exact Node executable bytes
and the `PATH` route that selected it.
The program bytes are likewise reauthenticated after each candidate phase and
at final settlement. Replacement, symlinking, truncation, or mutation at those
boundaries is rejected. These same-user checks are evidence custody, not a
privilege boundary against an actor able to replace and restore a file entirely
between checks.

Version 5 also independently resolves the exact harness command selected from
the sanitized `PATH`. It captures a pathless, bounded byte identity for the
entrypoint, persistent symlink route, launcher interpreter, resolved Node or
Bun runtime and version, and the installed root package plus its recursively
resolved declared runtime dependencies. Package-tree digests cover sorted
package-relative file identities while excluding nested `node_modules`; a
separate graph digest binds resolution edges and missing optional/peer
dependencies. Missing dependencies are rechecked as negative observations at
every lifecycle boundary. Native distributions such as the Claude binary or a
native Codex/Prime build are represented distinctly and do not claim a package
graph. OMP 18.0.9 package evidence requires and records Bun 1.3.14 or newer.

This custody detects persistent same-user mutation between the driver's
checks. It does not prove what a malicious candidate launched and deliberately
does not claim undeclared dynamic imports, OS/dynamic-loader libraries,
interpreter resource files, ambient harness config/plugins/skills, cached
provider or account state, or provider-side model routing. Those authorities
remain explicitly external and unbound.

The first command persists an explicit user default. `doctor` must identify the
selected harness, executable version, authentication category, billing owner,
prompt placement, and any actionable blocker without starting a model. The
third command must start exactly that harness with no shell, outer PTY, or
fallback and must exit successfully only after recovering an exact echo of its
task argv.

## Historical v5 live matrix

The exact packaged candidate from source commit
`31d81c55c8c90a7358b1cd8c5a0ccba631290a83`, version
`0.15.0-alpha.1`, completed a no-retry valid-route collection on Darwin ARM64
on 2026-08-31. Rust standalone, Bun standalone, and the installed npm launcher
each passed Prime, OMP, Codex, and Claude. Matrix v4 admitted all 12 cells and
bound their exact target, pathless candidate closure, runner, program/task/image
and terminal identities, declared harness package/runtime custody, harness
version and route, model identifier, and auth-route category.

The aggregate and private source records from that earlier workspace were not
carried into this repository. Current provider-free admission generates synthetic
records to verify matrix schema, custody and privacy rules. Those tests do not
establish a fresh live result; a release candidate needs its own explicit
cost-acknowledged collection. Each exact-route invocation ran
once with no retry. The matrix is therefore one passing record per valid cell,
not a reliability sample.

This remains candidate-reported `echo-v0` transport smoke. OpenProse was not
parsed or executed, semantic status is `not-applicable`, provider charging is
unverified, and ambient harness/provider state and dynamic resources remain
external and unbound. The single target matrix provides no portability,
strict-wrapper, release, publication, or ranking authority.

## Historical v2 live matrix

All twelve distribution-surface-by-harness cells passed the hardened v2
functional-alpha journey in one no-retry collection: Rust standalone, Bun
standalone, and the npm-installed launcher each completed Prime, OMP, Codex,
and Claude. The aggregate verifier admitted the exact shared build identity,
program, task, image, terminal schema, and terminal envelope. Every cell
reported task digest `3045752...dd` and terminal-envelope digest
`fb410f...4e83`.

That collection observed Prime `0.7.0`, exact OMP `18.0.9`, Codex
`0.150.0-alpha.8`, and Claude Code `2.1.243`. The npm cells exercised the
installed launcher and its selected platform package rather than treating the
Bun standalone binary as a proxy for npm installation.

That operational observation is retained as historical v2 evidence only. The
v2 record hashed the npm launcher but did not retain the selected platform
manifest and executable as candidate members, so it cannot establish v5
candidate custody and is intentionally rejected by the current verifier. The
historical v5 collection above was recollected from the exact candidate; the old
evidence was not relabeled or upgraded in place.

The active evidence-v5 and matrix-v4 formats were still unshipped when exact
list admission was added. Draft files created before this change, which retain
only `versionRange`, are intentionally rejected and must be recollected from
the original candidate. Historical v2 evidence remains historical and is not
reformatted or relabeled.

Prime additionally passed a freshly rebuilt stability sample of 5/5 Rust runs
and 5/5 Bun runs. Every stability run used its own private daemon socket, so it
did not depend on or mutate the user's default Prime daemon. OMP used an exact,
disposable upstream `18.0.9` installation for these cells; it was not inferred
from or installed into a user's global toolchain.

One preceding v2 collection observed a single Bun/Claude
`PROTOCOL_MALFORMED` failure, after which an explicit retry passed. The later
hardened 12-cell collection above had no failures or retries. Both facts are
retained here because the acceptance matrix is not a reliability benchmark;
future reliability measurements must freeze a repetition policy and retain
every failed and successful attempt.

This is candidate-reported live transport evidence. Provider spend remains
unverified, `semantic.status` is `not-applicable`, and `echo-v0` remains a
deliberately nonsemantic image. The evidence does not establish OpenProse
execution, portability, strict wrapper admission, release eligibility, or
publication authority, and no artifact from this implementation has been
published. Provider calls remain explicit and opt-in; the retained summary
contains no run-specific credentials, paths, prompts, or raw transcripts.
Candidate stdout and stderr are never copied into diagnostics or retained
evidence; failures report only the fixed phase, exit/timeout class, and custody
status needed to retry safely. On a Prime `PROTOCOL_MALFORMED` or
`PROTOCOL_TRUNCATED` run failure, the driver may additionally print the
candidate-reported parser stage, closed lifecycle phase, and four bounded
counters from `openprose.adapter-diagnostic/1`. That projection is accepted
only for the Prime run phase and renders no raw frame, output, identifier,
reason, or candidate-supplied string. It is a troubleshooting hint rather than
retained evidence: the failed invocation never creates an evidence file.
The closed first-content phase is `await-thinking-or-text-start`, matching the
source-audited Prime 0.8.1 text-only index-zero branch without exposing which
candidate record was rejected.

## Mechanical matrix verification

Individual v5 files can be admitted as one closed target-specific matrix:

```sh
python3 cli/conformance/live-alpha/matrix.py \
  /tmp/openprose-live-alpha/*.json \
  --target-platform darwin-arm64 \
  --out /tmp/openprose-live-alpha-matrix.json
```

The version 4 matrix report requires the exact product of the requested
surfaces and the target-supported harness set. By default that is twelve cells
on `darwin-arm64`, three on `darwin-x64`, six on `linux-x64-gnu`, and three on
`linux-arm64-gnu`. A deliberately narrower observation can pass explicit
`--surfaces` and `--harnesses` subsets; the report records those exact subsets
and never presents them as exhaustive target coverage. The verifier rejects
unsupported target/harness combinations, cross-target pooling, target-probe or
npm-platform drift, missing or duplicate cells,
missing, duplicate, or reordered candidate-closure members, candidate path or
byte drift within a surface, npm-native drift from the direct Bun executable,
runner build drift, harness-version drift, harness-byte-custody drift,
model or auth-route drift across distribution surfaces,
exact admitted-version-list or repair-command drift, adapter-recipe drift,
configuration-isolation gaps, or differences in the
program, task, image, terminal schema, or terminal envelope. Its report binds
each source evidence file by digest. Every matrix cell carries the complete
pathless candidate closure, while exact candidate and program paths remain
confined to source evidence.

This is equality of candidate-reported transport fingerprints and closed
settlement facts. It is not independent proof of provider billing, prompt
semantics, or OpenProse execution, and the report states those limits as
machine-readable claims. It is also a pass-admission matrix, not a reliability
sample: it consumes one passing record per cell and explicitly marks failed
attempt retention as outside the report. Benchmark runs must retain every
attempt rather than using this matrix to discard failures or retries.

Replacing `echo-v0` with the language-owned runtime image must require no launch
adapter changes. At that point this same file becomes the smallest semantic
smoke, but semantic success remains unavailable until the new image supplies
and owns that terminal contract.
