# OS credential store

`prose cli auth login` saves your OpenProse API key in the operating system's
credential store. Later commands read it from there when `OPENPROSE_API_KEY` is
unset or empty; a non-empty `OPENPROSE_API_KEY` always wins over the stored key.
`prose cli auth logout` removes it.

There is no plaintext fallback. If the store cannot be used, the command fails
with `CREDENTIAL_STORE_UNAVAILABLE`. Set the environment variable instead: both
`details.reason` and the Action name that variable, for service commands and
for `cli auth status`, `cli auth login` and `cli org list`.
`cli auth logout` keeps the plain store Action, because it only removes a
stored key.

## One item shared by both products

The Rust and Bun builds read and write the **same** item, so logging in with
either one works for both.

| Platform | Rust | Bun |
| --- | --- | --- |
| macOS | `/usr/bin/security` (login keychain) | `/usr/bin/security` (login keychain) |
| Linux | `secret-tool` (libsecret, Secret Service over D-Bus) | `Bun.secrets` (libsecret) |
| Windows | unavailable (use the variable) | unavailable (use the variable) |

The table is also `cli/shared/capabilities/credential-stores.v1.json`. On
Linux the Rust build needs the `secret-tool` program (the distribution's
libsecret tools package, see below); Bun links libsecret itself. Neither build
uses the Windows Credential Manager: on Windows, set `OPENPROSE_API_KEY`.

## macOS: `/usr/bin/security`, in both builds

Both builds run `/usr/bin/security -i` with an empty environment and send
fixed commands on standard input, one session per step. The commands, their
order, the error classification and the reason texts are the shared fixture
`cli/shared/fixtures/credentials/macos-security.v1.json`, which the tests of
both builds replay.

- **The item** is a generic password in the login keychain: service
  `org.openprose.cli.production`, account `api-key`, comment
  `openprose-cli-v1`. The `security` tool creates it, so the item's access
  list trusts `/usr/bin/security` and any build of either product reads it
  without a keychain prompt, also after an upgrade or a rebuild.
- **Reading** (`get`): `find-generic-password -s <service> -a api-key` looks
  at the attributes only (never a prompt), then
  `find-generic-password -s <service> -a api-key -w` reads the key. The value
  is trimmed of surrounding spaces, tabs and line breaks and otherwise
  returned unchanged; callers validate it as on Linux.
- **Saving** (`set`, by `cli auth login`): `delete-generic-password`, then
  `add-generic-password -U -s <service> -a api-key -j openprose-cli-v1 -w <key>`.
  Only a key matching `rr_test_[0-9a-f]{32}` is ever sent.
- **Removing** (`delete`, by `cli auth logout`): `delete-generic-password`.
  A missing item is not an error.
- **Results.** Standard error decides first: "could not be found" means no
  item (signed out); "User canceled" or a denied prompt is `CANCELLED`
  (exit 24); "User interaction is not allowed" is
  `CREDENTIAL_STORE_UNAVAILABLE` with the keychain-prompt reason below; any
  other failure (another `security` error, a non-zero exit, a signal, the
  10-second bound, more than 8 KiB of output, a missing program) is
  `CREDENTIAL_STORE_UNAVAILABLE`.
- **Ctrl-C** kills `security` and reports `CANCELLED`. While a key is being
  saved, the result is `CREDENTIAL_STORE_UNAVAILABLE` instead, with a reason
  saying that whether the key was stored is unknown.

### Keys saved by earlier builds

Earlier Bun builds saved the key through `Bun.secrets`, so the item trusted
only that one executable: every rebuild or upgrade asked for approval in a
macOS prompt. The first read by a current build sees that the item lacks the
`openprose-cli-v1` comment. After one successful read (you may be asked once
to allow access), it re-creates the item through `security`, and later builds
never prompt. When no prompt can be shown (an SSH session, for example), the
read fails with `CREDENTIAL_STORE_UNAVAILABLE`, the reason says the keychain
needs approval in a prompt, and the Action says to run the command once in a
desktop session or to set `OPENPROSE_API_KEY`.

### Manual acceptance on a Mac

These steps use the real login keychain, so they are never part of the
automated tests. Each status must finish without a keychain prompt.

1. Log in with the Bun build: `prose cli auth login`.
2. Rebuild the Bun build (`bun run build` in `cli/bun`), then
   `prose cli auth status`.
3. `prose cli auth status` with the Rust build.
4. `prose cli auth logout`, then `prose cli auth login`, with the Rust build.
5. `prose cli auth status` with the Bun build.

On Linux the item is:

| Field | Value |
| --- | --- |
| `xdg:schema` attribute | `com.oven-sh.bun.Secret` |
| `service` attribute | `org.openprose.cli.production` |
| `account` attribute | `api-key` |
| label | `<service>/api-key` |
| secret | the key, with no trailing newline |

`Bun.secrets` only matches items that carry the `xdg:schema` attribute. An item
stored by hand without it is invisible to both products.

To inspect it by hand (this prints the length only, never the key):

```sh
secret-tool lookup xdg:schema com.oven-sh.bun.Secret service org.openprose.cli.production account api-key | wc -c
```

This command returns `40` when a key is stored (`secret-tool` adds no newline
when its output is piped) and `0` when there is none.

## How the Rust build calls `secret-tool` on Linux

- **Program lookup.** `secret-tool` is taken from the fixed system
  directories first (`/usr/bin`, `/bin`, `/run/current-system/sw/bin`,
  `/usr/local/bin`), and only then from absolute `PATH` entries. A program
  earlier on `PATH`, such as a package manager's `node_modules/.bin`, therefore
  cannot shadow the system tool. Every candidate is followed through symlinks
  and must be an executable regular file owned by root or the current user.
  It must not be group- or world-writable, and neither may its directory.
  Residual risk: someone who can already write a trusted directory or replace
  `prose` itself is out of scope.
- **Arguments.** The argument vectors are fixed, with no shell:
  - `lookup <attributes>`
  - `store --label=<service>/api-key <attributes>`
  - `clear <attributes>`
- **The key.** Only a key matching `rr_test_[0-9a-f]{32}` is ever written. It
  goes over standard input and never appears in argv.
- **Reading is transport only.** `get` returns the stored value exactly as
  stored: no newline stripping and no format check, the same as
  `Bun.secrets.get`. Every caller validates it, so both products classify a
  malformed stored value identically:
  - Service commands (for example `cli wallet balance`) report
    `SERVICE_AUTH_REQUIRED` with a `details.reason` naming the variable, and
    send no request.
  - `cli auth status` and `cli org list` report the same
    `SERVICE_AUTH_REQUIRED` (`credentialProblem` `malformed`,
    `credentialSource` `store`).
  - `cli auth logout` and `cli auth login` still work, so you can recover by
    logging out and logging in again.
- **Environment.** The environment is cleared except for
  `DBUS_SESSION_BUS_ADDRESS` and `XDG_RUNTIME_DIR`. When neither is set,
  nothing is spawned and the result is `CREDENTIAL_STORE_UNAVAILABLE`.
- **Time and output bounds.** The tool runs in its own process group. Each
  call is bounded to 10 seconds, including the time to collect its output. A
  locked keyring that shows an unlock prompt is killed at the bound, together
  with any descendant still holding the output pipes. Each output stream is
  capped at 8 KiB. Ctrl-C kills the group and reports `CANCELLED` promptly.
- **Results.**
  - Exit 0 is success.
  - Exit 1 with no output means "no such item": `get` finds nothing, and a
    `delete` of a missing item succeeds, so logout is idempotent.
  - Anything else is `CREDENTIAL_STORE_UNAVAILABLE`: any diagnostic on stderr,
    another exit code, a signal, the timeout, or a missing program.

## Troubleshooting `CREDENTIAL_STORE_UNAVAILABLE` (Rust build, Linux)

The error code is the same for every cause. Service commands add a
`details.reason` naming the environment variable. The fastest way around any
cause is to set `OPENPROSE_API_KEY` for the command.

| Cause | Check | Fix |
| --- | --- | --- |
| `secret-tool` not installed | `command -v secret-tool` | install the libsecret tools (below) |
| no D-Bus session (SSH, container, CI) | `echo "$DBUS_SESSION_BUS_ADDRESS$XDG_RUNTIME_DIR"` is empty | run inside a desktop session, or set the variable |
| no Secret Service provider, or a locked keyring (killed at 10 s) | `secret-tool search --all service org.openprose.cli.production` | start or unlock GNOME Keyring or KeePassXC |
| untrusted `secret-tool` (group- or world-writable, foreign owner) | `ls -lL "$(command -v secret-tool)"` | fix the permissions, or use the distribution package |

Install the tool with your distribution's libsecret tools package, for example
`libsecret-tools` (Debian/Ubuntu) or `libsecret` (Arch/Fedora). You also need a
running Secret Service provider, such as GNOME Keyring or KeePassXC.

## Tests

- `cargo test -p prose-runner-core --features test-seams credential_store`: unit
  tests against a fake `secret-tool`. They cover the round trip, the argv, stdin
  and environment contract, a missing tool, missing D-Bus, D-Bus failure,
  token-alphabet injection, stored values returned unchanged, system-directory
  preference and program trust checks, the output bound, signals, the timeout,
  cancellation, and a descendant that keeps the pipes open.
- `cargo test -p prose-cli --features test-seams --test credential_store`: tests
  the compiled `prose` binary with a fake `secret-tool` on `PATH`. It covers
  `auth status` and `auth logout`, and the unavailable cases naming
  `OPENPROSE_API_KEY`. It checks that a malformed stored value
  is rejected by the callers exactly as Bun rejects it, that a system tool wins
  over a `PATH` shadow. Test builds
  replace the system directory list through `PROSE_TEST_SECRET_TOOL_SYSTEM_DIRS`
  (test-seams only, which release builds reject).
- macOS, both builds: `cargo test -p prose-runner-core --features test-seams macos_store`
  and `bun test test/credential-store-macos.test.ts` replay every scenario of
  the shared fixture and check the process contract (standard input, empty
  environment, bound, output cap, cancellation) against a fake `security`.
  `cargo test -p prose-cli --features test-seams --test credential_store_macos`
  and `bun test test/credential-store-macos-cli.test.ts` run the command line
  with a fake `security` through `PROSE_TEST_MACOS_SECURITY` (test-seam
  builds only).
- `PROSE_TEST_REAL_KEYRING=1 bun test test/credential-store-interop.test.ts`
  (from `cli/bun`): an opt-in check against the machine's real keyring, under
  the throwaway service `org.openprose.cli.test`. It checks four directions:
  - Rust writes and Bun reads.
  - Bun writes and Rust reads.
  - Rust replaces Bun's item.
  - Each side's delete is seen by the other.

  The test removes the item afterwards.
