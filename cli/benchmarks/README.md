# OpenProse provider-free benchmark rig

This directory owns measurement and analysis. It does not parse OpenProse or
call providers. Sentinel and shell fixtures do not establish language semantics,
product performance, installed-harness admission or release approval.

The former checked `local-smoke-example` is not shipped in this repository. Its
mechanically migrated records are historical material, available in the
[earlier source history](https://github.com/openprose/prose-cli/tree/6d7e8eb2ced840d443489593d29ca5f058090bd7/cli/benchmarks).
Current tests qualify snapshot custody, atomic evidence publication, deterministic
analysis and privacy using explicitly created fixture processes. They do not
invent replacement historical observations.

Before collection, an owner freezes the policy, profile, exact target and fixture
bytes. `verify` rejects changed identities. Collection copies admitted bytes into
private read-only snapshots, executes those snapshots and reauthenticates them
before writing evidence. This is same-user hardening and detection, not a
privilege boundary. Existing evidence directories are immutable; a new generation
is published atomically to an absent destination without following symlinks.

The analysis retains failures, timeouts and unrun trials, separates warmups from
measurements, uses only successful pairs for latency, and records exclusions.
Cost, retry and span facts require runner-owned observations; candidate stdout
cannot supply those authorities. Direct-skill and CLI wrapper observations cannot
be relabeled as one another. Process-group settlement does not claim containment
of detached descendants. No composite winner or public performance claim follows
from this rig's mechanical checks.

Run from `cli/benchmarks/`:

```sh
PYTHONPATH=. python3 -m unittest discover -s tests -v
PYTHONPATH=. python3 -m runner.cli verify --profile /path/to/reviewed-profile.json
PYTHONPATH=. python3 -m runner.cli run \
  --profile /path/to/reviewed-profile.json --output-dir evidence/new-generation
PYTHONPATH=. python3 -m runner.cli analyze \
  --raw evidence/new-generation/raw.json --policy /path/to/frozen-policy.json \
  --summary /tmp/reproduced-summary.json
```

The bundled development profile retains its declared frozen identities; it is
expected to refuse replacement build bytes. A new source requires a new reviewed
profile and evidence generation. Current installation and distribution rehearsal
belong to [installed benchmarks](installed/) and the
[release path](../release/README.md). Live language/reliability qualification,
protected inputs, publication and native platform trust remain separate gates.
