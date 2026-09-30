# Installed-adapter product adversary

This suite exercises the locally built Rust and Bun `prose` artifacts as black
boxes. It never invokes an installed provider harness or makes a model/network
request.

The checks derive adapter IDs, transport IDs, executable names, environment
policy, billing ownership, and launch recipes from the shared adapter oracle.
They do not maintain a product-specific copy of those facts.

Run after building both products:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 \
  cli/conformance/adversarial/adapter-products/test_adapter_products.py --build
```

`OPENPROSE_RUST_BIN` and `OPENPROSE_BUN_BIN` may point at alternate installed
artifacts. Both are required by default. During a deliberately partial local
build, `OPENPROSE_ADVERSARY_ALLOW_MISSING_PRODUCTS=1` permits testing only the
artifact that exists. Admission and CI must not use that override.

The functional-alpha success path places four provider-free fake installed
harnesses on an isolated `PATH`; it does not use a product test seam. It checks
both products' exact argv, global `--auth-profile` selection, image/task wire
bytes, environment selection, terminal recovery, normalized events, and result
evidence. Prime and OMP use the explicit `prime-harness-login` and
`omp-harness-login` routes in this path. The suite verifies that these empty
credential groups preserve the HOME-backed harness store, strip provider
credential and store-override variables, remain `unknown` in readiness, and
produce identical Rust/Bun dry-run and missing-profile JSON. The `echo-v0`
Environment-key Prime/OMP profiles are tested separately: each receives only
its selected provider credential group plus a fresh mode-0700 config directory
under the runner-owned transport directory. The suite proves the path exists
through harness execution, changes on every run, is removed after settlement,
does not inherit either ambient config override, and never triggers a preflight
model-list/help authentication probe. Actual execution is the first auth
authority, with no credential-route fallback. OMP cells additionally prove
cross-product parity for the final private retry- and MCP-provider-disabled config, exact
`--no-tools`, `--no-lsp`, correlated empty-tool state gating before prompt delivery,
hostile/reordered/duplicate control responses, interactive UI refusal, and
fail-closed nonterminal settlement. Control IDs and state candidate canaries
are absent from product output. Prime cleanup failure is exercised across
success, protocol, postprocessing, timeout, cancellation, and child-failure
exits. Every case requires the entire owned root to be gone before product
exit. The long-running timeout and cancellation cases additionally place a
symlink to an external canary inside the live owned root and require the target
to remain untouched. Cleanup-failure evidence stays pathless and exactly equal
across Rust and Bun. The `echo-v0` terminal authorizes only
`semantic.status=not-applicable`; this suite cannot prove OpenProse language
semantics or strict-wrapper admission.

The build flag creates dedicated Rust/Bun fixed-image echo candidates with test
seams disabled, a scrubbed environment and locked offline dependencies. It does
not overwrite ordinary published-kernel candidates. Ordinary startup is qualified
separately by the current kernel candidate workflow; these fake-harness checks
must not fetch a published runtime or make provider calls.
