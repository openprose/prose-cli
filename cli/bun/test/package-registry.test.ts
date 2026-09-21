import { expect, test } from "bun:test";
import { mkdir, mkdtemp, readFile, readdir, rm, symlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { runCli } from "../src/cli";
import { preparePackage, receipt, matchReceipt, parsePackageJSON, packagePath, version } from "../src/core/package-format";
import { parsePackageCommand } from "../src/core/package-args";
const fixtures = new URL("../../shared/fixtures/registry/", import.meta.url);
const token = `rr_test_${"1".repeat(32)}`;
const base = "/registry/v1/organizations/example/packages";
const data = async (name: string) => JSON.parse(await readFile(new URL(name, fixtures), "utf8"));
const raw = async (name: string) => readFile(new URL(name, fixtures), "utf8");
async function workspace(fn: (root: string, invoke: (args: string[], fixture?: unknown, env?: Record<string, string>) => Promise<{ code: number; stdout: string; stderr: string; report: any }>) => Promise<void>) {
  const root = await mkdtemp("/private/tmp/prose-registry-test-");
  const invoke = async (args: string[], fixture: unknown = { credential: null, storeAvailable: true, exchanges: [] }, env: Record<string, string> = {}) => {
    const path = join(root, "transport.json"); await writeFile(path, JSON.stringify(fixture));
    let stdout = "", stderr = "";
    const code = await runCli(args, { processCwd: root, userConfigPath: join(root, "cli.toml"), env: { PROSE_TEST_SERVICE_FIXTURE: path, ...env }, clock: { now: () => "2026-01-01T00:00:00Z", monotonicMs: () => 0 }, ids: { invocationId: () => "package-test" }, writeStdout: value => { stdout += value; }, writeStderr: value => { stderr += value; }, observeMockInvocation: () => { throw new Error("Harness must never start"); } });
    expect(stdout + stderr).not.toContain(token);
    return { code, stdout, stderr, report: stdout.startsWith("{") ? JSON.parse(stdout) : null };
  };
  try { await fn(root, invoke); } finally { await rm(root, { recursive: true, force: true }); }
}
const transport = (exchanges: unknown[], extra: Record<string, unknown> = {}) => ({ credential: token, storeAvailable: true, exchanges, ...extra });
const publishArgs = ["cli", "package", "publish", "hello.md", "--organization", "example", "--name", "hello", "--version", "1.0.0", "--json"];
const fetchArgs = ["cli", "package", "fetch", "example/hello@1.0.0", "--output-dir", "download", "--json"];
async function fetchExchanges(artifact?: string) { return [{ method: "GET", path: `${base}/hello/versions/1.0.0`, status: 200, body: await data("single-file.receipt.json") }, { method: "GET", path: `${base}/hello/versions/1.0.0/artifact`, status: 200, body: artifact ?? await raw("single-file.canonical.json") }]; }
test("Bun independently reproduces normative canonical byte/hash vectors", async () => {
  for (const vector of await data("hash-vectors.json")) {
    const prepared = preparePackage(await data(vector.fixture));
    expect(Buffer.from(prepared.bytes).toString()).toBe(await raw(vector.canonical));
    expect(prepared.bytes.length).toBe(vector.artifactBytes);
    expect(prepared.reference.sha256).toBe(vector.sha256);
    expect(prepared.inventory).toEqual(vector.inventory);
    matchReceipt(receipt(await data(vector.fixture.replace(".json", ".receipt.json"))), prepared);
  }
});
test("strict format rejects collisions, unsafe paths, encodings, unknown fields and unpinned metadata", async () => {
  for (const path of ["../file", "/file", "a\\file", ".env", "a/.key", "a/secret.txt", "a/node_modules/x", "CON.txt", "a/key.pem", "a."]) expect(() => packagePath(path)).toThrow();
  for (const bad of ["^1.0.0", "1.0.0-01", "1.0", "1.0.0\n"]) expect(() => version(bad)).toThrow();
  const source = await data("single-file.json");
  for (const mutate of [
    (value: any) => { value.extra = true; },
    (value: any) => { value.files.push({ ...value.files[0], path: "HELLO.md" }); },
    (value: any) => { value.files.push({ ...value.files[0], path: "hello.md/a" }); },
    (value: any) => { value.files[0].content = "\ud800"; },
    (value: any) => { value.files[0].encoding = "base64"; value.files[0].content = "Zh=="; },
    (value: any) => { value.manifest.exports.default = "missing.md"; },
    (value: any) => { value.manifest.dependencies.x = { organization: "example", package: "x", version: "^1.0.0", sha256: "a".repeat(64) }; },
  ]) { const changed = structuredClone(source); mutate(changed); expect(() => preparePackage(changed)).toThrow(); }
});
test("package grammar rejects unknown duplicate and misplaced flags", () => {
  for (const args of [["list", "example", "--public"], ["withdraw", "example/a@1.0.0", "--cursor", "x"], ["fetch", "example/a@1.0.0"], ["publish", "a", "--organization", "example", "--organization", "example"], ["list", "example", "--cursor", "x", "--cursor", "y"]]) expect(() => parsePackageCommand(args)).toThrow();
});
test("single publish uploads exact canonical bytes and hash to production account origin", async () => workspace(async (root, invoke) => {
  await writeFile(join(root, "hello.md"), "# Hello\n");
  const result = await invoke(publishArgs, transport([{ method: "POST", path: `${base}/hello/versions`, origin: "https://run-prose-production.openprose.workers.dev", status: 201, expectedBody: await raw("single-file.canonical.json"), expectedSha256: (await data("hash-vectors.json"))[0].sha256, body: await data("single-file.receipt.json") }]));
  expect(result.code).toBe(0); expect(result.report.environment).toBe("production"); expect(result.stderr).toBe("");
}));
test("directory publish explicitly selects bytes and retains exact build metadata", async () => workspace(async (root, invoke) => {
  const source = await data("directory.json");
  await mkdir(join(root, "pkg", "docs"), { recursive: true });
  await writeFile(join(root, "pkg", "prose-package.json"), JSON.stringify({ schema: "prose-package-directory-v1", files: source.files.map((f: any) => f.path), exports: source.manifest.exports, dependencies: source.manifest.dependencies }));
  for (const file of source.files) await writeFile(join(root, "pkg", file.path), file.encoding === "base64" ? Buffer.from(file.content, "base64") : file.content);
  await writeFile(join(root, "pkg", ".env"), "unlisted-must-not-read");
  const result = await invoke(["cli", "package", "publish", "pkg", "--organization", "example", "--name", "kit", "--version", "2.0.0-beta.1+build.4", "--public", "--json"], transport([{ method: "POST", path: `${base}/kit/versions`, status: 200, expectedBody: await raw("directory.canonical.json"), body: await data("directory.receipt.json") }]));
  expect(result.code).toBe(0);
  const withdrawn = await invoke(["cli", "package", "withdraw", "example/kit@2.0.0-beta.1+build.4", "--json"], transport([{ method: "POST", path: `${base}/kit/versions/2.0.0-beta.1+build.4/withdraw`, status: 200, body: { receipt: await data("directory.receipt.json"), withdrawn: true } }]));
  expect(withdrawn.code).toBe(0);
}));
test("fetch verifies all bytes then installs fresh exact files and receipt", async () => workspace(async (root, invoke) => {
  const result = await invoke(fetchArgs, transport(await fetchExchanges()));
  expect(result.code).toBe(0); expect(await readFile(join(root, "download", "hello.md"), "utf8")).toBe("# Hello\n");
  expect(JSON.parse(await readFile(join(root, "download", ".prose-package-receipt.json"), "utf8"))).toEqual(await data("single-file.receipt.json"));
  expect((await readdir(root)).some(name => name.startsWith(".prose-package-"))).toBe(false);
  const existing = await invoke(fetchArgs, transport(await fetchExchanges()));
  expect(existing.report.problem.code).toBe("CONFIG_INVALID"); expect(await readFile(join(root, "download", "hello.md"), "utf8")).toBe("# Hello\n");
}));
test("tampered/noncanonical artifacts and mismatched receipts never create destination", async () => workspace(async (root, invoke) => {
  for (const artifact of [`${await raw("single-file.canonical.json")} `, await raw("single-file.json")]) {
    const result = await invoke(fetchArgs, transport(await fetchExchanges(artifact)));
    expect(result.report.problem.code).toBe("SERVICE_PROTOCOL_INVALID"); expect(await readdir(root)).not.toContain("download");
  }
  const result = await invoke([...fetchArgs.slice(0, -1), "--sha256", "0".repeat(64), "--json"], transport(await fetchExchanges()));
  expect(result.report.problem.code).toBe("SERVICE_PROTOCOL_INVALID"); expect(await readdir(root)).not.toContain("download");
}));
test("source and destination symlinks fail closed without overwriting targets", async () => workspace(async (root, invoke) => {
  await mkdir(join(root, "actual")); await writeFile(join(root, "actual", "hello.md"), "# Hello\n");
  await symlink(join(root, "actual", "hello.md"), join(root, "hello.md"));
  expect((await invoke(publishArgs)).report.problem.code).toBe("CONFIG_INVALID");
  await symlink(join(root, "actual"), join(root, "alias"));
  const args = [...publishArgs]; args[3] = "alias/hello.md";
  expect((await invoke(args)).report.problem.code).toBe("CONFIG_INVALID");
  await symlink(join(root, "actual"), join(root, "download"));
  expect((await invoke(fetchArgs, transport(await fetchExchanges()))).report.problem.code).toBe("CONFIG_INVALID");
  expect(await readdir(join(root, "actual"))).toEqual(["hello.md"]);
}));
test("pagination allows public receipts only and reports opaque cursor", async () => workspace(async (_root, invoke) => {
  const result = await invoke(["cli", "package", "list", "example", "--cursor", "public:kit:2.0.0+build.4", "--json"], transport([{ method: "GET", path: `${base}?cursor=public%3Akit%3A2.0.0%2Bbuild.4`, status: 200, body: { packages: [await data("directory.receipt.json")], nextCursor: "public:kit:2.0.0+build.4" } }]));
  expect(result.code).toBe(0); expect(result.report.result.nextCursor).toBe("public:kit:2.0.0+build.4");
  const bad = await invoke(["cli", "package", "list", "example", "--json"], transport([{ method: "GET", path: base, status: 200, body: { packages: [await data("single-file.receipt.json")], nextCursor: null } }]));
  expect(bad.report.problem.code).toBe("SERVICE_PROTOCOL_INVALID");
}));
test("anonymous read allowed on unavailable store; write requires credentials; staging persists", async () => workspace(async (root, invoke) => {
  const list = [{ method: "GET", path: base, status: 200, body: { packages: [], nextCursor: null } }];
  expect((await invoke(["cli", "package", "list", "example", "--json"], transport(list, { storeAvailable: false }))).code).toBe(0);
  await writeFile(join(root, "hello.md"), "# Hello\n");
  expect((await invoke(publishArgs)).report.problem.code).toBe("SERVICE_AUTH_REQUIRED");
  await invoke(["cli", "environment", "use", "staging", "--json"]);
  const result = await invoke(["cli", "package", "list", "example", "--json"], transport(list, { environment: "staging", credentials: { staging: token, production: null } }), { OPENPROSE_API_KEY: "malformed" });
  expect(result.report.environment).toBe("staging"); expect(result.code).toBe(0); expect(result.stderr).toBe("");
  const human = await invoke(["cli", "package", "list", "example"], transport(list, { environment: "staging" })); expect(human.stderr).toContain("OpenProse staging");
}));
test("selected malformed credential and exhausted fixture fail closed without response reflection", async () => workspace(async (_root, invoke) => {
  const result = await invoke(["cli", "package", "list", "example", "--json"], transport([]), { OPENPROSE_API_KEY: "bad-secret-value" });
  expect(result.report.problem.code).toBe("SERVICE_PROTOCOL_INVALID"); expect(result.stdout).not.toContain("bad-secret-value");
  expect((await invoke(["cli", "package", "list", "example", "--json"], transport([]))).report.problem.code).toBe("SERVICE_PROTOCOL_INVALID");
}));

test("local directory manifests reject duplicate and escaped-equivalent keys", async () => workspace(async (root, invoke) => {
  await mkdir(join(root, "pkg"));
  await writeFile(join(root, "pkg", "hello.md"), "# Hello\n");
  for (const contents of [
    '{"schema":"prose-package-directory-v1","files":["hello.md"],"files":["hello.md"],"exports":{"default":"hello.md"},"dependencies":{}}',
    '{"schema":"prose-package-directory-v1","files":["hello.md"],"exports":{"default":"hello.md","default":"hello.md"},"dependencies":{}}',
    '{"schema":"prose-package-directory-v1","files":["hello.md"],"exports":{"default":"hello.md","\\u0064efault":"hello.md"},"dependencies":{}}',
  ]) {
    await writeFile(join(root, "pkg", "prose-package.json"), contents);
    const args = [...publishArgs]; args[3] = "pkg";
    expect((await invoke(args)).report.problem.code).toBe("CONFIG_INVALID");
  }
}));
