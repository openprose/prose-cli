import { describe, expect, test } from "bun:test";
import { inferOutputMode, parseEntrypoint } from "../src/core/args";
import { readFileSync } from "node:fs";

const invocationAction = "Review the runner syntax with the --help option, place global options before cli, and retry the command.";

function expectInvocationFailure(args: readonly string[], reason: string): void {
  try {
    parseEntrypoint(args);
    throw new Error("expected runner invocation to fail");
  } catch (error) {
    expect(error).toMatchObject({
      code: "INVOCATION_INVALID",
      boundary: "invocation",
      message: "Runner invocation is invalid.",
      action: invocationAction,
      exitCode: 2,
      retryable: false,
      details: { reason },
    });
  }
}

describe("runner-global parsing", () => {
  test("freezes forever at the first language token", () => {
    expect(parseEntrypoint(["--harness", "mock", "run", "--harness", "claude", "--model=x"]))
      .toMatchObject({
        kind: "language",
        global: { harness: "mock" },
        argv: ["prose", "run", "--harness", "claude", "--model=x"],
      });
  });

  test.each([
    ["Unicode and whitespace", ["write", "雪 ❄️", "line\nfeed", "\t"]],
    ["leading dash", ["--future-language-option", "value"]],
    ["shell metacharacters", ["run", "$(touch nope)", "`whoami`", "a;b", "x|y", "*", "'quoted'"]],
  ])("preserves %s as exact opaque argv", (_label, tail) => {
    expect(parseEntrypoint(tail)).toMatchObject({
      kind: "language",
      argv: ["prose", ...tail],
    });
  });

  test("the delimiter forces the reserved cli noun through to the language", () => {
    expect(parseEntrypoint(["--", "cli", "doctor", "--json"])).toEqual({
      kind: "language",
      global: {},
      argv: ["prose", "cli", "doctor", "--json"],
    });
  });

  test("a bare delimiter is rejected instead of inventing a language command", () => {
    expectInvocationFailure(["--"], "`--` must be followed by a language command");
  });

  test("malformed runner syntax uses the invocation boundary", () => {
    for (const [args, reason] of [
      [["--output=machine", "cli", "doctor"], 'invalid output mode "machine"; expected human, json, or jsonl'],
      [["--model", "one", "--model", "two", "cli", "doctor"], "runner option --model was specified more than once"],
      [["cli", "harness", "use", "nope"], "Harness selection must be one of openprose, prime, omp, codex, or claude."],
    ] as const) {
      expectInvocationFailure(args, reason);
    }
  });

  test("unclaimed runner paths are service invocation errors", () => {
    for (const [args, reason] of [
      [["cli", "doctor", "extra"], "missing or invalid arguments for `cli doctor`"],
      [["cli", "frobnicate"], "unknown command `cli frobnicate`. Commands: auth, cleanup, config, doctor, example, harness, job, model, org, package, program, repo, result, run, service, wallet"],
      [["cli", "whoami"], "unknown command `cli whoami`; did you mean `cli auth status`? Commands: auth, cleanup, config, doctor, example, harness, job, model, org, package, program, repo, result, run, service, wallet"],
      // The public client has no environment group and no raw API passthrough.
      [["cli", "environment", "show"], "unknown command `cli environment`. Commands: auth, cleanup, config, doctor, example, harness, job, model, org, package, program, repo, result, run, service, wallet"],
      [["cli", "api", "GET", "/health"], "unknown command `cli api`. Commands: auth, cleanup, config, doctor, example, harness, job, model, org, package, program, repo, result, run, service, wallet"],
    ] as const) {
      const parsed = parseEntrypoint(args);
      if (parsed.kind !== "service" || parsed.command.kind !== "invalid") throw new Error(`service invalid expected for ${args.join(" ")}`);
      expect(parsed.command.invalid.error.details?.reason).toBe(reason);
    }
  });

  test("help words and bare cli print the cli topic", () => {
    for (const args of [["cli"], ["cli", "help"], ["cli", "run", "help"], ["cli", "run", "-h"], ["cli", "run", "list", "-h"]]) {
      const parsed = parseEntrypoint(args);
      if (parsed.kind !== "service" || parsed.command.kind !== "help") throw new Error(`help expected for ${args.join(" ")}`);
    }
    expect(parseEntrypoint(["cli", "doctor", "-h"])).toEqual({ kind: "help", global: {} });
  });

  test("there is no service-selection option: before cli it is an unknown token, after the command an unknown option", () => {
    const flag = ["--service", "environment"].join("-");
    // Before `cli` it is an invocation error that says it was removed (never language input).
    const before = parseEntrypoint([flag, "production", "cli", "run", "list"]);
    if (before.kind !== "service" || before.command.kind !== "invalid") throw new Error("service invalid expected");
    expect(String(before.command.invalid.error.details?.reason)).toBe(`unknown option ${flag} before \`cli\`; the option was removed`);
    expect(before.command.invalid.correction.kind === "argv" ? before.command.invalid.correction.action : "").toContain("public builds always use the OpenProse production service");
    const after = parseEntrypoint(["cli", "run", "list", flag, "production"]);
    if (after.kind !== "service" || after.command.kind !== "invoke") throw new Error("service invocation expected");
    expect(after.command.invocation.error?.details?.reason).toBe(`unknown option ${flag} for \`cli run list\``);
    const all = parseEntrypoint(["cli", "model", "list", "--all"]);
    if (all.kind !== "service" || all.command.kind !== "invoke") throw new Error("service invocation expected");
    expect(all.command.invocation.error?.details?.reason).toBe("unknown option --all for `cli model list`");
  });

  test("service words without cli are redirected with the exact command, globals kept", () => {
    const cases: Array<[string[], string[]]> = [
      [["run", "submit", "absent-program.prose"], ["cli", "run", "submit", "absent-program.prose"]],
      [["--output", "json", "job", "list"], ["--output", "json", "cli", "job", "list"]],
      [["--format", "json", "cli", "run", "list"], ["--output", "json", "cli", "run", "list"]],
      [["login"], ["cli", "auth", "login"]],
      [["models", "--json"], ["cli", "model", "list", "--json"]],
      [["runs"], ["cli", "run", "list"]],
      [["programs", "list"], ["cli", "program", "list"]],
      [["run"], ["cli", "run", "submit", "--help"]],
    ];
    for (const [args, suggested] of cases) {
      const parsed = parseEntrypoint(args);
      expect(parsed.kind).toBe("language");
      if (parsed.kind !== "language") continue;
      expect(parsed.redirect?.argv).toEqual(suggested);
      const command = parsed.redirect?.command;
      expect(command?.kind === "invalid" && command.invalid.correction).toEqual(expect.objectContaining({ kind: "argv", argv: suggested }));
    }
    const status = parseEntrypoint(["status"]);
    expect(status.kind === "language" && status.redirect?.language).toBe(true);
    expect(status.kind === "language" && status.redirect?.argv).toEqual(["cli", "service", "triage"]);
    // A language command word forwards unless the default hosted harness
    // would refuse it; `run FILE` always forwards, with the hint.
    const help = parseEntrypoint(["help"]);
    expect(help.kind === "language" && help.redirect?.language).toBe(true);
    expect(help.kind === "language" && help.redirect?.argv).toEqual(["--help"]);
    const runFile = parseEntrypoint(["run", "absent-program.prose"]);
    expect(runFile.kind === "language" && runFile.redirect?.hintOnly).toBe(true);
    expect(runFile.kind === "language" && runFile.redirect?.argv).toEqual(["cli", "run", "submit", "absent-program.prose", "--preview"]);
    const helped = parseEntrypoint(["help", "cli", "run"]);
    expect(helped.kind === "service" && helped.command.kind).toBe("help");
    for (const args of [["--harness", "mock", "run"], ["serve"], ["--", "job", "list"], ["--env", "production", "cli", "run", "list"]]) {
      const parsed = parseEntrypoint(args);
      expect(parsed.kind === "language" && parsed.redirect).toBeFalsy();
    }
  });

  test("unescaped cli is runner-owned", () => {
    expect(parseEntrypoint(["--harness=mock", "cli", "config", "explain", "--json"]))
      .toMatchObject({ kind: "operation", operation: "config-explain", json: true });
  });

  test("parses auth profile as a runner-global value and output inference skips it", () => {
    expect(parseEntrypoint([
      "--harness", "prime", "--auth-profile=openrouter", "--model", "fixture-model", "run",
    ])).toMatchObject({
      kind: "language",
      global: { harness: "prime", authProfile: "openrouter", model: "fixture-model" },
      argv: ["prose", "run"],
    });
    for (const auth of [["--auth-profile", "openrouter"], ["--auth-profile=openrouter"]]) {
      expect(inferOutputMode([...auth, "--output", "json", "run"])).toBe("json");
    }
  });

  test("rejects empty or missing auth profile values", () => {
    for (const args of [["--auth-profile="], ["--auth-profile"]]) {
      expect(() => parseEntrypoint(args)).toThrow("Runner invocation is invalid.");
    }
  });

  test("parses an explicit user harness selection without crossing the language boundary", () => {
    expect(parseEntrypoint(["--output", "json", "cli", "harness", "use", "claude"]))
      .toEqual({
        kind: "operation",
        global: { output: "json" },
        operation: "harness-use",
        value: "claude",
        json: false,
      });
    expect(() => parseEntrypoint(["cli", "harness", "use", "mock"]))
      .toThrow("Runner invocation is invalid.");
  });

  test("parses harness-selection model and auth flags before or after the runner command", () => {
    expect(parseEntrypoint([
      "cli", "harness", "use", "prime",
      "--model", "openai/gpt-5.4",
      "--auth-profile=prime-harness-login",
      "--json",
    ])).toEqual({
      kind: "operation",
      global: { model: "openai/gpt-5.4", authProfile: "prime-harness-login" },
      operation: "harness-use",
      value: "prime",
      json: true,
    });
    expect(parseEntrypoint([
      "--model=openai/gpt-5.4", "--auth-profile", "prime-harness-login",
      "cli", "harness", "use", "prime",
    ])).toEqual({
      kind: "operation",
      global: { model: "openai/gpt-5.4", authProfile: "prime-harness-login" },
      operation: "harness-use",
      value: "prime",
      json: false,
    });
  });

  test("rejects duplicate or conflicting harness-selection route flags", () => {
    for (const args of [
      ["--model", "openai/first", "cli", "harness", "use", "prime", "--model", "openai/second"],
      ["cli", "harness", "use", "prime", "--model=openai/first", "--model=openai/first"],
      ["--auth-profile", "openai", "cli", "harness", "use", "prime", "--auth-profile", "openrouter"],
      ["cli", "harness", "use", "prime", "--auth-profile=openai", "--auth-profile=openai"],
      ["cli", "harness", "use", "prime", "--json", "--json"],
      ["--model", "openai/first", "--model", "openai/second", "cli", "harness", "use", "prime"],
      ["--auth-profile=openai", "--auth-profile=openai", "cli", "harness", "use", "prime"],
    ]) {
      expect(() => parseEntrypoint(args)).toThrow("Runner invocation is invalid.");
    }
  });

  test.each([
    ["--model", ["cli", "harness", "use", "prime", "--model=openai/first", "--model=openai/second"]],
    ["--auth-profile", ["--auth-profile", "openai", "cli", "harness", "use", "prime", "--auth-profile", "openrouter"]],
  ] as const)("uses the canonical duplicate %s selection error", (option, args) => {
    try {
      parseEntrypoint(args);
      throw new Error("expected duplicate selection option to fail");
    } catch (error) {
      expect(error).toMatchObject({
        code: "INVOCATION_INVALID",
        boundary: "invocation",
        message: "Runner invocation is invalid.",
        action: invocationAction,
        exitCode: 2,
        retryable: false,
        details: { reason: `runner option ${option} was specified more than once` },
      });
    }
  });

  test("infers JSON for a malformed runner command with a trailing local flag", () => {
    expect(inferOutputMode([
      "--model=openai/gpt-5.4",
      "cli", "harness", "use", "prime",
      "--model", "openai/gpt-5.4",
      "--json",
    ])).toBe("json");
    expect(inferOutputMode([
      "--", "cli", "harness", "use", "prime", "--json",
    ])).toBe("human");
    expect(inferOutputMode([
      "run", "cli", "harness", "use", "prime", "--json",
    ])).toBe("human");
  });

  test("parses Prime cleanup only through the global output surface", () => {
    const handle = "prime-v1.openprose-prime-Fixture1.018f47a6-7d2c-7b10-8a2e-1a2b3c4d5e6f";
    expect(parseEntrypoint(["--output", "json", "cli", "cleanup", "prime", handle])).toEqual({
      kind: "operation",
      global: { output: "json" },
      operation: "prime-cleanup",
      value: handle,
      json: false,
    });
    expect(() => parseEntrypoint(["cli", "cleanup", "prime", handle, "--json"]))
      .toThrow("Runner invocation is invalid.");
  });

  test.each([
    ["status", "auth-status"],
    ["login", "auth-login"],
    ["logout", "auth-logout"],
  ] as const)("recognizes the hosted account %s operation", (command, operation) => {
    expect(parseEntrypoint(["--output", "json", "cli", "auth", command]))
      .toEqual({
        kind: "operation",
        global: { output: "json" },
        operation,
        json: false,
      });
  });

  test("`cli --help` is the generated service topic", () => {
    const parsed = parseEntrypoint(["cli", "--help"]);
    expect(parsed.kind).toBe("service");
    if (parsed.kind === "service" && parsed.command.kind === "help") expect(parsed.command.text).toStartWith("Usage: prose [GLOBAL OPTIONS] cli <COMMAND>");
  });

  test("service operations parse through the manifest", () => {
    const parsed = parseEntrypoint(["--output", "json", "cli", "run", "list", "--limit", "2"]);
    expect(parsed.kind).toBe("service");
    expect(parsed.global.output).toBe("json");
    if (parsed.kind === "service" && parsed.command.kind === "invoke") {
      expect(parsed.command.invocation.operation).toBe("run.list");
      expect(parsed.command.invocation.options.get("--limit")).toEqual(["2"]);
    }
    // A misspelled service noun is a service invocation error with a corrected argv.
    const misspelled = parseEntrypoint(["cli", "walet", "balance"]);
    expect(misspelled.kind === "service" && misspelled.command.kind === "invalid" ? misspelled.command.invalid.correction : undefined)
      .toEqual({ kind: "argv", action: "Use `cli wallet balance`: `{command}`", argv: ["cli", "wallet", "balance"] });
  });

  test("known runner groups and commands accept help without configuration", () => {
    for (const args of [
      ["cli", "doctor", "--help"],
      ["cli", "harness", "--help"],
      ["cli", "harness", "list", "--help"],
      ["cli", "harness", "use", "--help"],
      ["cli", "harness", "use", "prime", "--help"],
      ["cli", "cleanup", "--help"],
      ["cli", "cleanup", "prime", "--help"],
      ["cli", "cleanup", "prime", "opaque-handle", "--help"],
      ["cli", "config", "--help"],
      ["cli", "config", "explain", "--help"],
    ]) {
      expect(parseEntrypoint(args)).toEqual({ kind: "help", global: {} });
    }
  });

  test("frozen service groups and verbs print their help.v1.json topic, never the runner help", () => {
    const topics = (JSON.parse(readFileSync(new URL("../../shared/service/help.v1.json", import.meta.url), "utf8")) as { topics: Record<string, string> }).topics;
    for (const [args, topic] of [
      [["cli", "auth", "--help"], "cli auth"],
      [["cli", "auth", "status", "--help"], "cli auth status"],
      [["cli", "auth", "login", "--help"], "cli auth login"],
      [["cli", "auth", "logout", "-h"], "cli auth logout"],
      [["cli", "org", "list", "--help"], "cli org list"],
      [["cli", "package", "withdraw", "--help"], "cli package withdraw"],
    ] as const) {
      const parsed = parseEntrypoint([...args]);
      expect(parsed.kind === "service" && parsed.command.kind === "help" ? parsed.command.text : undefined).toBe(topics[topic]);
      expect(topics[topic]).not.toContain("OpenProse outer runner");
    }
  });

  function suggested(args: readonly string[]): [string, string[]] {
    const parsed = parseEntrypoint(args);
    if (parsed.kind !== "service" || parsed.command.kind !== "invalid") throw new Error(`service invalid expected for ${args.join(" ")}`);
    const correction = parsed.command.invalid.correction;
    if (correction.kind !== "argv") throw new Error(`argv correction expected for ${args.join(" ")}`);
    return [String(parsed.command.invalid.error.details?.reason), correction.argv];
  }

  test("command-local options before cli move after the command path", () => {
    for (const [args, argv] of [
      [["--json", "cli", "run", "list"], ["cli", "run", "list", "--json"]],
      [["-j", "cli", "run", "list"], ["cli", "run", "list", "--json"]],
      [["--output", "json", "--json", "cli", "run", "list"], ["--output", "json", "cli", "run", "list", "--json"]],
      // Settled, because `cli run cancel` rejects the extra argument `x` after `--`.
      [["-y", "--output", "json", "cli", "run", "cancel", "r", "--", "x"], ["--output", "json", "cli", "run", "cancel", "r", "--yes", "--"]],
      [["--limit", "5", "--before=b", "cli", "run", "list"], ["cli", "run", "list", "--limit", "5", "--before=b"]],
    ] as const) {
      const [reason, suggestedArgv] = suggested(args);
      expect(suggestedArgv).toEqual([...argv]);
      expect(reason).toContain("after the command path, not before `cli`");
    }
    expect(suggested(["-j", "cli", "run", "list"])[0]).toBe("`-j` (--json) belongs after the command path, not before `cli`; nothing was forwarded or sent");
    // An unknown option before `cli` and a service command is an invocation
    // error naming it; without `cli` it stays the language's first token.
    expect(suggested(["--jsno", "cli", "run", "list"])).toEqual(["unknown option --jsno before `cli`", ["cli", "run", "list"]]);
    const removed = ["--service", "environment"].join("-");
    expect(suggested([removed, "production", "cli", "service", "status"])).toEqual([`unknown option ${removed} before \`cli\`; the option was removed`, ["cli", "service", "status"]]);
    expect(suggested([`${removed}=production`, "--output", "json", "cli", "service", "status"])).toEqual([`unknown option ${removed} before \`cli\`; the option was removed`, ["--output", "json", "cli", "service", "status"]]);
    expect(parseEntrypoint(["--json", "run.prose"]).kind).toBe("language");
  });

  test("pre-parse global errors are service errors with a fix", () => {
    expect(suggested(["--output", "yaml", "cli", "run", "list"])).toEqual(['invalid output mode "yaml"; expected human, json, or jsonl', ["--output", "json", "cli", "run", "list"]]);
    const [nearReason, nearArgv] = suggested(["--output=JSONL", "cli", "run", "list"]);
    expect(nearReason.endsWith("did you mean `jsonl`?")).toBe(true);
    expect(nearArgv).toEqual(["--output=jsonl", "cli", "run", "list"]);
    // A runner operation keeps the runner renderer.
    expect(() => parseEntrypoint(["--output", "yaml", "cli", "doctor"])).toThrow("Runner invocation is invalid.");
  });

  test("help with --json prints the topic", () => {
    for (const args of [["help", "cli", "--json"], ["help", "cli", "run", "--json"], ["cli", "--help", "--json"], ["cli", "run", "--help", "--json"]]) {
      const parsed = parseEntrypoint(args);
      expect(parsed.kind === "service" ? parsed.command.kind : parsed.kind).toBe("help");
    }
  });

  test("help does not make unknown runner paths valid", () => {
    for (const args of [
      ["cli", "unknown", "--help"],
      ["cli", "harness", "unknown", "--help"],
      ["cli", "doctor", "extra", "--help"],
    ]) {
      let parsed;
      try { parsed = parseEntrypoint(args); } catch (caught) { expect(String(caught)).toContain("Runner invocation is invalid."); continue; }
      // Unknown paths are service invocation errors.
      expect(parsed.kind === "service" && parsed.command.kind === "invalid").toBe(true);
    }
  });
});
test("permission mode is an explicit runner flag",()=>{
 const parsed=parseEntrypoint(["--permission-mode","acceptEdits","run","PROGRAM.md"]);
 expect(parsed.global.permissionMode).toBe("acceptEdits");
});

test("native output is an explicit transport option",()=>{
 expect(parseEntrypoint(["--output-contract","native","run","a.md"])).toMatchObject({global:{outputContract:"native"}});
 expect(()=>parseEntrypoint(["--output-contract","guessed","run"])).toThrow();
});
