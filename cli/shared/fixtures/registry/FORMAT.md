# Registry package protocol v1

This independent registry does not change the operator kernel release format.
A publication is one bounded JSON envelope (`prose-package-v1`); no archives,
source evaluation, Markdown interpretation, link following, or dependency solving.
`fixtures/single-file.json` and `directory.json` are normative request examples.
`*.receipt.json` and `hash-vectors.json` provide pinned interoperability examples.

The request has exactly `schema`, `manifest`, and `files`. The manifest has
`organization`, `package`, `version`, `exports`, `dependencies`, and optional
`visibility` (omission means `private`; only `private`/`public` accepted). Names
are lowercase ASCII alphanumeric/hyphen slugs, 1–63 characters with no terminal
hyphen. Versions are exact SemVer 2.0, including prerelease/build metadata. The
complete version string is the immutable version key; there is no range/latest
resolution or build-metadata equivalence. Visibility is immutable per published
version; neither a retry nor a later request changes existing bytes or visibility.

Exports are a nonempty object of `default` and/or named export keys to exact,
case-sensitive included paths. Names match `[A-Za-z][A-Za-z0-9_-]{0,63}` excluding
prototype control names. Dependencies map slug aliases to exact references:
`{organization,package,version,sha256}`. SHA-256 is 64 lowercase hexadecimal
characters. References pin the whole package artifact, not an individual file.
The package contains only explicitly included files; there is no implicit glob,
recursive walk, ignore-file interpretation, or directory discovery.

Each file is exactly `{path,encoding,content}`, with `encoding` `utf8` or canonical
RFC 4648 padded `base64`. UTF-8 rejects unpaired surrogates. Paths are ASCII,
relative slash-separated file paths, at most 240 characters. Absolute paths,
backslashes, empty/dot/dot-dot segments, hidden segments, trailing dots, Windows
reserved device names, duplicate/case-colliding paths, file/directory prefix
collisions, credential/secret names, node_modules, and key/certificate suffixes
are rejected. These conservative rules reduce accidental inclusion; they do not
scan file contents for secrets. Filesystem clients must explicitly select regular
files and reject symlinks (including ancestor directory symlinks) before reading.
The envelope cannot represent symlinks, modes, device files, or filesystem links.

Limits: 2 MiB request UTF-8 JSON, 128 files, 256 KiB decoded per file, 1 MiB decoded
total, 64 exports, and 64 dependencies. Unknown fields fail closed at every schema
level. JSON object member names must be unique at the sender; consumers use the
parsed JSON value (duplicate JSON object members have the platform's usual last
member semantics). Duplicate file entries always fail. Authorization must run
before publication and byte retrieval; these data validators confer no access.

## Canonical bytes and hashes

Normalize omitted visibility to `private`. Sort files by ASCII path. Convert all
file contents to canonical padded base64, preserving decoded bytes exactly (no
newline or Unicode normalization). Recursively sort all JSON object keys by ASCII
(all schema keys and user keys are ASCII); use compact JSON with no whitespace,
JSON standard string escaping and unescaped `/`, and UTF-8 encoding. Append one
LF byte. This yields the canonical envelope saved as `*.canonical.json`.
SHA-256 of these exact bytes is the package artifact identity. It binds manifest,
organization/package/version, dependencies, exports, visibility and every payload
byte. Changing transport encoding, input key order, file order, or explicitly
specifying default private visibility does not change it. No timestamp, credential,
receipt, mutable discovery state, or stable organization UUID enters the artifact.

An inventory is an ASCII-path-sorted array of `{path,size,sha256}`; each file hash
covers exactly its decoded bytes and size is its byte length. Inventory is derived,
not supplied by publishers. `hash-vectors.json` provides artifact byte lengths,
hashes and file hashes for Rust/Bun/Worker/Node implementations to test against;
canonical bytes are checked in, not reconstructed from prose alone.

## Publication receipt and consumption

A receipt is exactly `{schema:"prose-publication-v1",organizationId,reference,
visibility,inventory}`. The registry supplies a stable lowercase UUID
`organizationId`; the manifest organization's slug is an immutable routing name.
The reference has `{organization,package,version,sha256}`. The receipt is metadata,
not an independent claim of authenticity. Clients must obtain it over their trusted
registry connection, download bounded artifact bytes, compare the artifact hash,
validate the package, and require its reference/visibility/inventory to agree with
the receipt before materializing files. Dependency references follow the same rule.
Withdrawal only removes discovery: pinned authorized retrieval remains available.

`contract.mjs` exports `LIMITS`, `parsePublication`, `preparePublication`,
`validateIdentity`, `validateVersion`, `validatePath`, `validateReference`,
`validateReceipt`, `canonicalJSON`, and `sha256`. `preparePublication` returns
`{manifest,inventory,artifact,bytes,sha256,reference}` and is async for Web Crypto.
It performs no I/O or execution. Node 22+, Bun and Workers expose required Web APIs.
