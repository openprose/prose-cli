import { describe, expect, test } from "bun:test";
import { mkdtempSync, statSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { readFile } from "node:fs/promises";
import { runCli } from "../src/cli";
import { classify, encodeSegment, requestFor, Transport, type Response } from "../src/core/service/http";
import { Journal } from "../src/core/service/journal";
import { didYouMean, GUIDE_TEXT, guideSlug, MANIFEST_TEXT, misplacedGlobals, operation, parseService, projectFields, unknown, type Correction, type Json, type ServiceCommand } from "../src/core/service/manifest";
import { PRODUCTION, serviceEnvironment } from "../src/core/service/endpoint";
import { commandOperation, runnerArgv, serviceArgv, teach } from "../src/core/service/index";
import { isValueOption } from "../src/core/args";
import { FAILING_JOBS_MAX, human as triageHuman, JOBS_SHOWN, LIVE_WATCH_MAX, RECENT_RUNS } from "../src/core/service/triage";
import { parseProgramRef } from "../src/core/service/program-ref";
import { canonicalJson, localizeHelpFor, patternMatches, redact, sanitizeServiceMessage, shellQuote } from "../src/core/service/render";
import helpProgramName from "../../shared/fixtures/human/help-program-name.json" with { type: "json" };
import { FixtureSource, SseReader, fixtureBytes } from "../src/core/service/sse";
import { FreshDirectory, readSource, writeNewFile } from "../src/core/service/fs";
import { RunnerFailure } from "../src/core/types";

const manifestPath = new URL("../../shared/service/operations.v1.json", import.meta.url);

function response(status: number, body: string): Response {
  return { status, headers: new Map(), body: new TextEncoder().encode(body) };
}

function invocation(args: string[]) {
  const command = parseService(args, args);
  if (command.kind !== "invoke") throw new Error("expected an invocation");
  return command.invocation;
}

async function cli(args: string[], env: Record<string, string> = {}) {
  let stdout = "";
  let stderr = "";
  const home = mkdtempSync(join(tmpdir(), "prose-service-"));
  const code = await runCli(args, {
    env: { HOME: home, XDG_STATE_HOME: join(home, "state"), ...env }, processCwd: home, homeDir: home,
    userConfigPath: join(home, "config", "cli.toml"),
    clock: { now: () => "2026-01-01T00:00:00Z", monotonicMs: () => 0 }, ids: { invocationId: () => "test" },
    writeStdout: (value) => { stdout += value; }, writeStderr: (value) => { stderr += value; },
  });
  return { code, stdout, stderr };
}

describe("Service framework", () => {
  test("help names the invoked program as the shared fixture shows", () => {
    for (const fixture of helpProgramName.cases) {
      expect(localizeHelpFor(fixture.program, fixture.input), fixture.id).toBe(fixture.rendered);
    }
  });

  test("the embedded manifest is byte-identical to the shared file and published without the client's own sections", async () => {
    expect(MANIFEST_TEXT).toBe(await readFile(manifestPath, "utf8"));
    const result = await cli(["--output", "json", "cli", "service", "operations"]);
    expect(result.code).toBe(0);
    expect(result.stdout.split("\n").length).toBe(2);
    const projection = JSON.parse(await readFile(new URL("../../shared/service/operations-public.v1.json", import.meta.url), "utf8")) as { fields: Json; maxBytes: number; forbid: string[] };
    const published = projectFields(JSON.parse(MANIFEST_TEXT) as Json, projection.fields);
    const printed = JSON.stringify((JSON.parse(result.stdout) as { result: unknown }).result);
    expect(printed.length).toBeLessThanOrEqual(projection.maxBytes);
    for (const forbidden of projection.forbid) expect(printed).not.toContain(forbidden);
    const document = JSON.parse(result.stdout) as { schema: string; operation: string; result: unknown };
    expect(document.schema).toBe("openprose.service-operation/1");
    expect(document.operation).toBe("service.operations");
    expect(document.result).toEqual(published);
  });

  test("service operations and capabilities print their view per output mode", async () => {
    const human = await cli(["cli", "service", "operations"]);
    expect(human.code).toBe(0);
    expect(human.stdout.startsWith("OPERATION ")).toBe(true);
    const projection = JSON.parse(await readFile(new URL("../../shared/service/operations-public.v1.json", import.meta.url), "utf8")) as { fields: Json };
    const manifestDocument = projectFields(JSON.parse(MANIFEST_TEXT) as Json, projection.fields) as { operations: unknown[]; exitCodes: unknown };
    const lines = await cli(["--output", "jsonl", "cli", "service", "operations"]);
    const records = lines.stdout.trimEnd().split("\n").map((line) => JSON.parse(line) as { schema: string; record?: unknown });
    expect(records.slice(0, -1).map((line) => line.record)).toEqual(manifestDocument.operations);
    expect(records.at(-1)!.schema).toBe("openprose.service-page/1");
    const capabilities = await cli(["cli", "service", "capabilities", "--json"]);
    expect(capabilities.code).toBe(0);
    expect(capabilities.stdout.split("\n").length).toBe(2);
    const document = JSON.parse(capabilities.stdout) as { schema: string; result: { schema: string; exitCodes: unknown } };
    expect(document.schema).toBe("openprose.service-operation/1");
    expect(document.result.schema).toBe("openprose.service-capabilities/1");
    expect(document.result.exitCodes).toEqual(manifestDocument.exitCodes);
  });

  test("service triage human report stays within 25 lines at its largest", () => {
    const run = { run_id: "run_1", status: "running", model: "m", created_at: "t" };
    const job = { id: "j", type: "schedule", lastError: "x" };
    const command = { why: "w", argv: ["cli", "run", "watch", "run_1"], env: null };
    const text = triageHuman({
      health: { problem: null, status: "ok", default_model: "m", models: ["m"] },
      credential: { variable: "V", source: "environment", state: "valid", problem: null },
      wallet: { problem: null, balance: { available_dollars: "1.00", reserved_dollars: "0.00" }, low: true, lowBelowCents: 500 },
      organization: { problem: null, default: { slug: "o", role: "admin" } },
      runs: { problem: null, recent: Array(RECENT_RUNS).fill(run), live: ["run_1"] },
      jobs: { problem: null, total: 9, max: 10, jobs: Array(JOBS_SHOWN).fill(job) },
      nextCommands: Array(LIVE_WATCH_MAX + 1 + FAILING_JOBS_MAX).fill(command),
    });
    expect(text.split("\n").length - 1).toBeLessThanOrEqual(25);
    expect(text).toContain("Wallet: $1.00 available, $0.00 reserved (low: below $5.00)\n");
  });

  test("service guide prints the guide file and its sections", async () => {
    const human = await cli(["cli", "service", "guide"]);
    expect(human.code).toBe(0);
    expect(human.stdout).toBe(GUIDE_TEXT);
    const machine = await cli(["cli", "service", "guide", "--json"]);
    expect(machine.code).toBe(0);
    const document = JSON.parse(machine.stdout) as { schema: string; operation: string; result: { sections: Array<{ id: string; title: string; body: string }> } };
    expect(document.schema).toBe("openprose.service-operation/1");
    expect(document.operation).toBe("service.guide");
    const sections = document.result.sections;
    expect(sections.map((section) => section.id)).toContain("get-a-runs-answer");
    expect(guideSlug("Exit codes, resume and detach")).toBe("exit-codes-resume-and-detach");
    const rebuilt = sections.map((section) => `## ${section.title}\n\n${section.body}\n`).join("\n");
    expect(`# OpenProse service guide\n\n${rebuilt}`).toBe(GUIDE_TEXT);
  });

  test("parses arguments, options and common flags from the manifest", () => {
    const parsed = invocation(["run", "submit", "prog.md", "--input", "a=1", "--input=b=2", "--detach", "--yes", "--json"]);
    expect(parsed.operation).toBe("run.submit");
    expect(parsed.arguments.get("FILE")).toEqual(["prog.md"]);
    expect(parsed.options.get("--input")).toEqual(["a=1", "b=2"]);
    expect(parsed.flags.has("--detach") && parsed.yes && parsed.json).toBe(true);
    expect(parsed.error).toBeUndefined();
    expect(invocation(["run", "input", "run_1", "--id", "--help"]).options.get("--id")).toEqual(["--help"]);
  });

  test("unknown options and commands name the fix", () => {
    expect(invocation(["run", "cancel", "run_1", "--yess"]).error?.details?.reason).toBe("unknown option --yess for `cli run cancel`; did you mean --yes?");
    const rejected = parseService(["run", "submt"], ["run", "submt"]);
    if (rejected.kind !== "invalid") throw new Error("invalid expected");
    expect(rejected.invalid.error.details?.reason).toContain("did you mean `cli run submit`?");
    // Its arguments are optional and the handler needs a program, so the bare correction is its help.
    expect(rejected.invalid.correction).toEqual({ kind: "argv", action: "Use `cli run submit`; `{command}` shows what it needs", argv: ["run", "submit", "--help"] });
    expect(didYouMean("wallt", ["wallet", "run"])).toBe("wallet");
    expect(didYouMean("ab", ["ac", "ad"])).toBeUndefined();
    // Optimal string alignment, one edit up to four characters, two beyond.
    expect(didYouMean("lsit", ["list", "show"])).toBe("list");
    expect(didYouMean("--jsno", ["--json", "--yes"])).toBe("--json");
    expect(didYouMean("sbmt", ["submit"])).toBeUndefined();
    expect(didYouMean("sbumti", ["submit"])).toBe("submit");
    const synonym = parseService(["wallet", "get"], ["wallet", "get"]);
    if (synonym.kind !== "invalid") throw new Error("invalid expected");
    expect(synonym.invalid.correction).toEqual({ kind: "argv", action: "Use `cli wallet balance`: `{command}`", argv: ["wallet", "balance"] });
    expect(invocation(["wallet", "topup", "-y"]).error?.details?.reason).toBe("unknown option -y for `cli wallet topup`; did you mean --yes?");
  });

  test("suggestions complete to a command that parses", () => {
    const argvOf = (correction: Correction | undefined): string[] => {
      if (correction?.kind === "argv") return correction.argv;
      if (correction?.kind === "command") return correction.words;
      throw new Error(`a command correction expected, got ${JSON.stringify(correction)}`);
    };
    const invalidArgv = (command: ServiceCommand): string[] => {
      if (command.kind !== "invalid") throw new Error("invalid expected");
      return argvOf(command.invalid.correction);
    };
    expect(invalidArgv(unknown(["models"], ["models"]))).toEqual(["model", "list"]);
    expect(invalidArgv(unknown(["organization"], ["organization"]))).toEqual(["org", "--help"]);
    expect(invalidArgv(unknown(["money"], ["money"]))).toEqual(["wallet", "balance"]);
    expect(invalidArgv(unknown(["list", "jobs"], ["list", "jobs"]))).toEqual(["job", "list"]);
    expect(invalidArgv(parseService(["model"], ["model"]))).toEqual(["model", "list"]);
    expect(invalidArgv(parseService(["wallet", "show"], ["wallet", "show"]))).toEqual(["wallet", "balance"]);
    expect(invalidArgv(parseService(["run", "result", "run_1"], ["run", "result", "run_1"]))).toEqual(["run", "show", "run_1", "--file", "outputs/result.json"]);
    // Settled: `cli run show` without RUN_ID is itself rejected, so the suggestion is the listing (settling needs the `cli`).
    expect(invalidArgv(parseService(["run", "shw"], ["cli", "run", "shw"]))).toEqual(["run", "list"]);
    expect(argvOf(invocation(["wallet", "topup", "500"]).correction)).toEqual(["wallet", "topup", "--amount-cents", "500"]);
    expect(argvOf(invocation(["wallet", "topup", "--amount", "5", "--preview"]).correction)).toEqual(["wallet", "topup", "--amount-cents", "500", "--preview"]);
    const redeem = invocation(["wallet", "redeem", "SECRET-CODE", "--yes"]);
    expect(String(redeem.error?.details?.reason)).not.toContain("SECRET-CODE");
    expect(argvOf(redeem.correction)).toEqual(["wallet", "redeem", "--code-file", "-", "--yes"]);
    const update = invocation(["job", "update", "j1", "--name", "x", "--yes"]);
    expect(update.error?.details?.suggestedStdin).toBe('{"name":"x"}\n');
    expect(argvOf(update.correction)).toEqual(["job", "update", "j1", "--spec-file", "-", "--yes"]);
  });

  test("corrections are per cause and keep the output mode", () => {
    const parsed = invocation(["run", "show"]);
    const error = teach(parsed.error!, parsed.correction!, PRODUCTION, "json");
    expect(error.action).toBe("Pass <RUN_ID>; list runs with `prose --output json cli run list`.");
    expect(error.details?.suggestedArgv).toEqual(["--output", "json", "cli", "run", "list"]);
    expect(error.action).not.toContain("place global options");
    const moved = misplacedGlobals(["--output", "json", "run", "list"], ["cli", "--output", "json", "run", "list"]);
    expect(moved?.kind === "invalid" ? moved.invalid.correction : undefined)
      .toEqual({ kind: "argv", action: "Place global options before cli: `{command}`", argv: ["--output", "json", "cli", "run", "list"] });
    expect(misplacedGlobals(["--output", "json", "doctor"], ["cli", "--output", "json", "doctor"])).toBeUndefined();
  });

  test("public builds talk only to production and ignore the developer endpoint variable", () => {
    expect(serviceEnvironment({})).toBe(PRODUCTION);
    expect(serviceEnvironment({ OPENPROSE_API_URL: "https://example.invalid" })).toBe(PRODUCTION);
    expect(PRODUCTION).toEqual({
      name: "production", origin: "https://run-prose-production.openprose.workers.dev", credentialEnv: "OPENPROSE_API_KEY",
      storeService: "org.openprose.cli.production", label: "OpenProse", journal: "production",
    });
  });

  test("classification: body code, then route override, then status", () => {
    const request = requestFor(operation("repo.list")!, 0, "/repos");
    for (const status of [400, 403, 404, 503]) {
      const error = classify("service/1", request, response(status, JSON.stringify({ error: "internal words", code: "feature_disabled", feature: "neutral_feature" })), undefined);
      expect(error.code).toBe("SERVICE_FEATURE_DISABLED");
      // The service's feature name and error text never reach the problem.
      expect(error.details).toEqual({ serviceStatus: status, serviceCode: "feature_disabled" });
      expect(error.message).toBe("This capability is not available for this account.");
    }
    expect(classify("service/1", request, response(401, "{\"error\":\"Reconnect GitHub\"}"), undefined).code).toBe("GITHUB_LINK_REQUIRED");
    expect(classify("service/1", request, response(404, "Not found"), undefined).code).toBe("SERVICE_RESOURCE_NOT_FOUND");
    expect(classify("service/1", request, response(302, ""), undefined).code).toBe("SERVICE_UNAVAILABLE");
    const key = "rr_test_0123456789abcdef0123456789abcdef";
    const rejected = classify("service/1", requestFor(operation("program.save")!, 0, "/programs/x"), response(400, JSON.stringify({ error: `bad\nslug ${key}` })), key);
    expect(rejected.details?.serviceMessage).toBe("bad slug [REDACTED]");
    expect(classify("account/1", request, response(400, "{\"error\":\"x\"}"), undefined).details?.serviceMessage).toBeUndefined();
  });

  test("the fixture transport asserts queries and headers and must be consumed", async () => {
    const environment = PRODUCTION;
    const transport = Transport.withFixture(environment, {
      exchanges: [{ method: "GET", path: "/runs", query: { limit: "2" }, status: 200, body: { runs: [] },
        requestHeaders: { "X-OpenProse-Client": { pattern: "^cli/[0-9.]+\\+(rust|bun)$" }, Authorization: "Bearer t" } }],
    });
    const request = { ...requestFor(operation("run.list")!, 0, "/runs"), query: [["limit", "2"]] as Array<[string, string]> };
    expect((await transport.send(request, "t")).status).toBe(200);
    transport.finish();
    const unused = Transport.withFixture(environment, { exchanges: [{ method: "GET", path: "/runs", status: 200, body: {} }] });
    expect(() => unused.finish()).toThrow(RunnerFailure);
    await expect(unused.send(request, undefined)).rejects.toThrow(RunnerFailure);
  });

  test("a 2xx body over the class limit is SERVICE_RESPONSE_TOO_LARGE", async () => {
    const environment = PRODUCTION;
    const transport = Transport.withFixture(environment, { exchanges: [{ method: "GET", path: "/health", status: 200, bodyText: "x".repeat(1024 * 1024 + 1) }] });
    await expect(transport.send(requestFor(operation("service.status")!, 0, "/health"), undefined)).rejects.toMatchObject({ code: "SERVICE_RESPONSE_TOO_LARGE" });
  });

  test("the SSE reader parses events, ignores heartbeats and bounds events", async () => {
    const signal = new AbortController().signal;
    const reader = new SseReader(new FixtureSource(fixtureBytes([{ comment: "heartbeat" }, { id: "1", event: "status", data: { status: "running" } },
      { raw: "event: text_chunk\r\ndata: a\r\ndata: b\r\n\r\n" }, { raw: "data: partial" }]), "disconnected", () => {}), signal, 1024);
    const first = await reader.next();
    expect(first).toEqual({ kind: "event", event: { id: "1", event: "status", data: "{\"status\":\"running\"}" } });
    expect(await reader.next()).toEqual({ kind: "event", event: { event: "text_chunk", data: "a\nb" } });
    expect(await reader.next()).toEqual({ kind: "end", end: "disconnected" });
    const big = new SseReader(new FixtureSource(fixtureBytes([{ data: "x".repeat(100) }]), "closed", () => {}), signal, 64);
    await expect(big.next()).rejects.toMatchObject({ code: "SERVICE_RESPONSE_TOO_LARGE" });
    const controller = new AbortController();
    const cancelled = new SseReader(new FixtureSource(fixtureBytes([{ data: "1" }]), "idle", () => controller.abort()), controller.signal, 1024);
    expect((await cancelled.next()).kind).toBe("event");
    await expect(cancelled.next()).rejects.toMatchObject({ code: "CANCELLED" });
  });

  test("journal entries are 0600 in 0700 directories, findable and pruned", () => {
    const root = mkdtempSync(join(tmpdir(), "prose-journal-"));
    const journal = Journal.forEnvironment({ XDG_STATE_HOME: root }, undefined, "linux", PRODUCTION);
    expect(journal.directory).toBe(join(root, "openprose", "cli", "production", "runs"));
    const first = { session: "00000000-0000-4000-8000-000000000001", runId: "run_a", createdAt: "2026-09-01T00:00:00.000Z", lastSequence: 0, sourceSha256: null };
    const second = { ...first, session: "00000000-0000-4000-8000-000000000002", createdAt: "2026-09-02T00:00:00.000Z" };
    journal.write(first);
    journal.write(second);
    expect(journal.findRun("run_a")?.session).toBe(second.session);
    const file = join(journal.directory!, `${first.session}.json`);
    expect(statSync(file).mode & 0o777).toBe(0o600);
    expect(statSync(journal.directory!).mode & 0o777).toBe(0o700);
    expect(statSync(join(root, "openprose")).mode & 0o777).toBe(0o700);
    expect(readFileSync(file, "utf8")).toBe(`${canonicalJson(first)}\n`);
    expect(journal.prune("2026-10-01T12:00:00.000Z", 30)).toBe(1);
    expect(journal.get(first.session)).toBeUndefined();
    expect(() => journal.write({ ...first, session: "not-a-uuid" })).toThrow(RunnerFailure);
  });

  test("fresh directories and output files never overwrite", async () => {
    const root = mkdtempSync(join(tmpdir(), "prose-fresh-"));
    const fresh = FreshDirectory.create(root, "out");
    fresh.createFile("a/b.txt");
    expect(() => fresh.createFile("a/b.txt")).toThrow(RunnerFailure);
    for (const bad of ["/etc/passwd", "../x", "a/../../x", "a//b", "a\\b", "", "a/./b"]) expect(() => fresh.createFile(bad)).toThrow(RunnerFailure);
    expect(() => FreshDirectory.create(root, "out")).toThrow(RunnerFailure);
    expect(() => FreshDirectory.create(root, "missing/out")).toThrow(RunnerFailure);
    writeNewFile(root, "file.txt", new TextEncoder().encode("1"));
    expect(() => writeNewFile(root, "file.txt", new TextEncoder().encode("2"))).toThrow(RunnerFailure);
    expect(await readSource(root, "file.txt", 1, "FILE")).toEqual(new TextEncoder().encode("1"));
    await expect(readSource(root, "file.txt", 0, "FILE")).rejects.toThrow(RunnerFailure);
    writeFileSync(join(root, "x"), "");
  });

  test("program references parse pinned and latest forms", () => {
    expect(parseProgramRef("OpenProse/hello-world", false)).toEqual({ owner: "OpenProse", slug: "hello-world" });
    expect(parseProgramRef("openprose/hello@0123456789abcdef", true).rev).toBe("0123456789abcdef");
    for (const bad of ["hello", "a/B", "a/b@123", "-a/b", "a/b@0123456789ABCDEF", "a/b/c"]) expect(() => parseProgramRef(bad, false)).toThrow(RunnerFailure);
    expect(() => parseProgramRef("a/b", true)).toThrow(RunnerFailure);
  });

  test("render helpers match the Rust product", () => {
    expect(encodeSegment("a b/c")).toBe("a%20b%2Fc");
    expect(redact("key rr_test_abc123 end")).toBe("key [REDACTED] end");
    expect(sanitizeServiceMessage("\u0007\n")).toBeUndefined();
    expect(Array.from(sanitizeServiceMessage("é".repeat(600))!).length).toBe(512);
    expect(shellQuote("it's")).toBe("'it'\\''s'");
    expect(canonicalJson({ b: 1, a: [true, null, "x"] })).toBe("{\"a\":[true,null,\"x\"],\"b\":1}");
    expect(patternMatches("^cli/[0-9]+\\.[0-9]+\\.[0-9]+\\+(rust|bun)$", "cli/0.1.0+bun")).toBe(true);
    expect(patternMatches("(?=x)", "x")).toBeUndefined();
  });

  test("an oversize 2xx body through the product is SERVICE_RESPONSE_TOO_LARGE", async () => {
    // Generated at runtime (the Rust twin is tests/service_framework.rs): a > 1 MiB
    // corpus file would exceed the repository's new-file limit.
    const directory = mkdtempSync(join(tmpdir(), "prose-oversize-"));
    const fixture = join(directory, "fixture.json");
    writeFileSync(fixture, JSON.stringify({ environment: "production", exchanges: [{ method: "GET", path: "/health", status: 200, bodyText: "x".repeat(1024 * 1024 + 1) }] }));
    const result = await cli(["--output", "json", "cli", "service", "status"], { PROSE_TEST_SERVICE_FIXTURE: fixture });
    expect(result.code).toBe(10);
    expect(JSON.parse(result.stdout)).toMatchObject({ operation: "service.status", problem: { code: "SERVICE_RESPONSE_TOO_LARGE" } });
  });

  test("confirmation needs neither a credential nor the network", async () => {
    const result = await cli(["--output", "json", "cli", "program", "delete", "demo"]);
    expect(result.code).toBe(2);
    expect(JSON.parse(result.stdout).problem.code).toBe("CONFIRMATION_REQUIRED");
  });
});

// The envelope of a rejected argv names the operation the argv
// names, and only service command lines take the envelope. Mirrors Rust
// `service::tests::rejected_argvs_name_their_operation_and_shape`.
test("rejected argvs name their operation and shape", async () => {
  const named = (argv: string[]) => commandOperation(argv)?.id;
  expect(named(["--json", "cli", "run", "list"])).toBe("run.list");
  expect(named(["cli", "org", "member", "list", "acme"])).toBe("org.member.list");
  expect(named(["cli", "api", "GET", "/runs"])).toBeUndefined();
  expect(named(["cli", "models"])).toBeUndefined();
  expect(named(["cli", "org", "member", "rm", "acme"])).toBeUndefined();
  expect(named(["models", "--json"])).toBeUndefined();
  expect(named(["--", "cli", "run", "list"])).toBeUndefined();
  expect(serviceArgv(["--output", "json", "cli", "package", "frob"], isValueOption)).toBe(true);
  expect(serviceArgv(["--output", "json", "cli", "doctor", "--bogus"], isValueOption)).toBe(false);
  expect(serviceArgv(["--cwd", "cli", "run", "list"], isValueOption)).toBe(false);
  expect(serviceArgv(["run.prose", "cli", "run"], isValueOption)).toBe(false);
  expect(runnerArgv(["--output", "json", "cli", "harness", "lst"])).toBe(true);
  expect(runnerArgv(["cli", "run", "list"])).toBe(false);
  let stdout = "";
  const code = await runCli(["cli", "models", "--json"], {
    env: {}, processCwd: tmpdir(), homeDir: mkdtempSync(join(tmpdir(), "service-framework-")),
    clock: { now: () => "2026-09-24T00:00:00.000Z", monotonicMs: () => 0 }, ids: { invocationId: () => "inv_framework" },
    writeStdout: (text) => { stdout += text; }, writeStderr: () => {},
  });
  const document = JSON.parse(stdout);
  expect(code).toBe(2);
  expect(document.schema).toBe("openprose.service-operation/1");
  expect(document.operation).toBe("cli");
  expect(document).not.toHaveProperty("environment");
  expect(document.result).toBeNull();
  expect(document.problem.code).toBe("INVOCATION_INVALID");
  // The one-verb group completes (`cli model list`).
  expect(document.problem.details.suggestedArgv).toEqual(["cli", "model", "list", "--json"]);
});

test("journal times match the shared vectors", async () => {
  const { rfc3339 } = await import("../src/core/service/journal");
  const { readFileSync } = await import("node:fs");
  const { join } = await import("node:path");
  const fixture = JSON.parse(readFileSync(join(import.meta.dir, "../../shared/fixtures/journal-times.json"), "utf8"));
  for (const item of fixture.cases) {
    const millis = rfc3339(item.value);
    expect({ id: item.id, millis: millis === undefined ? null : Math.floor(millis) }).toEqual({ id: item.id, millis: item.millis });
  }
});
