import { describe, expect, test } from "bun:test";
import scalarFixture from "../../shared/fixtures/human/human-safe-scalars.json" with { type: "json" };
import { failure, hostedRunFailed, runFailureAction } from "../src/core/errors";
import runFailureActions from "../../shared/fixtures/human/run-failure-actions.json" with { type: "json" };
import { canonicalJson, formatHumanError, humanRunnerCommand, jsonLine, humanRunnerInvocation, humanSafeMultiline, humanSafeScalar } from "../src/core/output";

describe("human error output", () => {
  test("a failed run's Action follows its cause as the shared fixture shows", () => {
    for (const fixture of runFailureActions.cases) {
      expect(runFailureAction(fixture.reason) ?? null, fixture.id).toBe(fixture.action);
      expect(hostedRunFailed({ reason: fixture.reason }).action, fixture.id).toBe(fixture.action ?? failure("HOSTED_RUN_FAILED").action);
    }
  });

  test("implements the shared terminal-safe scalar contract", () => {
    for (const fixture of scalarFixture.cases) {
      expect(humanSafeScalar(fixture.input)).toBe(fixture.rendered);
    }
    for (const range of scalarFixture.unsafeCodePointRanges) {
      for (let codePoint = range.start; codePoint <= range.end; codePoint += 1) {
        const input = String.fromCodePoint(codePoint);
        const expected = codePoint === 0x08 ? "\\b"
          : codePoint === 0x09 ? "\\t"
          : codePoint === 0x0a ? "\\n"
          : codePoint === 0x0c ? "\\f"
          : codePoint === 0x0d ? "\\r"
          : `\\u{${codePoint.toString(16).toUpperCase().padStart(4, "0")}}`;
        expect(humanSafeScalar(input)).toBe(expected);
      }
    }
    for (const fixture of scalarFixture.multilineCases) {
      expect(humanSafeMultiline(fixture.input)).toBe(fixture.rendered);
    }
  });

  test("renders hostile source and detail values without terminal or label injection", () => {
    const source = "/tmp/config\nAction: forged\t\u001b[31m.toml:7";
    const reason = "invalid setting\r\nSource: forged\b\u0085";
    const shape = failure("CONFIG_INVALID", { source, reason }).toJSON();
    const rendered = formatHumanError(shape);
    expect(rendered).toContain("Source: /tmp/config\\nAction: forged\\t\\u{001B}[31m.toml:7\n");
    expect(rendered).toContain("Detail: invalid setting\\r\\nSource: forged\\b\\u{0085}\n");
    expect(rendered.match(/^Action:/gmu)).toHaveLength(1);
    expect(rendered.match(/^Source:/gmu)).toHaveLength(1);
    expect(rendered).not.toContain("\u001b");
    expect(rendered).not.toContain("\t");
    expect(shape.details).toEqual({ source, reason });
  });

  test("does not advertise unsafe runner paths or recovery handles as commands", () => {
    const unsafe = humanRunnerInvocation("/tmp/prose\nAction: forged", undefined);
    expect(unsafe).toBe("No copyable runner command is available because its path contains control characters; reinstall OpenProse in a path without control characters.");
    expect(unsafe).not.toContain("\nAction:");

    const error = failure("PROCESS_CLEANUP_FAILED", {
      cleanupArgv: ["cli", "cleanup", "prime", "prime-v1.openprose-prime-Fixture1.bad\nhandle"],
    });
    const rendered = formatHumanError(error.toJSON());
    expect(rendered).not.toContain("Recovery:");
    expect(rendered).not.toContain("bad\nhandle");
  });

  test("renders hostile detected versions as one terminal-safe line", () => {
    const detectedVersion = "codex-cli 0.0.0\nAction: forged\u001b[31m";
    const shape = failure("HARNESS_INCOMPATIBLE", {
      detectedVersion,
      admittedVersions: ["0.149.0-alpha.4.1"],
      repairCommand: "npm install --global @openai/codex@0.149.0-alpha.4.1",
    }).toJSON();
    const rendered = formatHumanError(shape);
    expect(rendered).toContain("Detected version: codex-cli 0.0.0\\nAction: forged\\u{001B}[31m");
    expect(rendered.match(/^Action:/gmu)).toHaveLength(1);
    expect(shape.details?.detectedVersion).toBe(detectedVersion);
  });

  test("retains a source entrypoint but not a compiled Bun virtual entrypoint", () => {
    expect(humanRunnerInvocation("/opt/bun's/bin/bun", "/repo/open prose/main.ts"))
      .toBe("'/opt/bun'\\''s/bin/bun' '/repo/open prose/main.ts'");
    expect(humanRunnerInvocation("/download/prose", "/$bunfs/root/main.ts"))
      .toBe("'/download/prose'");
    expect(humanRunnerInvocation("/download/prose", undefined))
      .toBe("'/download/prose'");
  });

  test("renders an opaque owned-service recovery command without a private path", () => {
    const handle = "prime-v1.openprose-prime-Fixture1.018f47a6-7d2c-7b10-8a2e-1a2b3c4d5e6f";
    const error = failure("PROCESS_CLEANUP_FAILED", {
      cleanupHandle: handle,
      cleanupArgv: ["cli", "cleanup", "prime", handle],
      sensitiveFilesRemoved: true,
    });
    const rendered = formatHumanError(error.toJSON());
    expect(rendered).toContain(humanRunnerCommand(`cli cleanup prime ${handle}`));
    expect(rendered).not.toContain("$PROSE");
    expect(rendered).not.toContain("Recovery: prose cli cleanup");
    expect(rendered).not.toContain("`prose cli");
    // The exact executable/entrypoint may legitimately be installed under /tmp.
    // Only that known invocation is exempt; private paths elsewhere still fail.
    expect(rendered.replaceAll(humanRunnerInvocation(), "<runner>")).not.toContain("/tmp/");
  });

  test.skipIf(process.platform === "win32")("renders shell-parseable harness-selection commands", () => {
    for (const arguments_ of [
      "cli harness use codex",
      "cli harness use claude",
      'cli harness use prime --model "$PROSE_MODEL" --auth-profile prime-harness-login',
      'cli harness use omp --model "$PROSE_MODEL" --auth-profile omp-harness-login',
    ]) {
      const command = humanRunnerCommand(arguments_);
      const parsed = Bun.spawnSync(["/bin/sh", "-n", "-c", command], {
        stdin: "ignore",
        stdout: "pipe",
        stderr: "pipe",
      });
      expect(parsed.exitCode, parsed.stderr.toString()).toBe(0);
      expect(command).not.toMatch(/[<>|]/u);
      expect(command).not.toContain("replace-with-provider/model");
    }
  });

  test("renders the closed Bun prerequisite without a path or raw probe output", () => {
    const error = failure("HARNESS_INCOMPATIBLE", {
      adapterId: "omp/rpc",
      fallbackAttempted: false,
      runtimePrerequisite: {
        runtime: "bun",
        versionRange: ">=1.3.14",
        detectedVersion: "1.3.13",
        availability: "incompatible",
        repairCommand: "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9",
      },
    });
    const rendered = formatHumanError(error.toJSON());
    expect(rendered).toContain("Runtime prerequisite: bun");
    expect(rendered).toContain("Detected runtime version: 1.3.13");
    expect(rendered).toContain("Required runtime version: >=1.3.14");
    expect(rendered).toContain("Repair: npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9");
    // The exact executable/entrypoint may legitimately be installed under /tmp.
    // Only that known invocation is exempt; private paths elsewhere still fail.
    expect(rendered.replaceAll(humanRunnerInvocation(), "<runner>")).not.toContain("/tmp/");
    expect(rendered).not.toContain("rawOutput");
  });

  test("renders the hosted-unavailable action as an exact persistent BYO selection", () => {
    const rendered = formatHumanError(failure("HOSTED_UNAVAILABLE", {
      billingOwner: "openprose",
      fallbackSelected: false,
    }).toJSON());
    expect(rendered).toContain(humanRunnerInvocation());
    expect(rendered).toContain("To use the hosted service, run `cli run submit FILE --preview`; running programs on this machine needs a local harness (`cli harness list`).");
    expect(rendered).not.toContain("Wait for OpenProse-hosted execution");
    expect(rendered).not.toContain("--harness <id>");
  });
});

describe("canonical JSON", () => {
  test("sorts keys recursively as strings, like serde_json's BTreeMap", () => {
    const value = { zeta: { 10: "ten", 2: "two", b: [{ y: 1, x: 0 }], a: null }, alpha: "café ✓" };
    expect(canonicalJson(value)).toBe('{"alpha":"café ✓","zeta":{"10":"ten","2":"two","a":null,"b":[{"x":0,"y":1}]}}');
    expect(jsonLine({ schema: "s", environment: "production", problem: null })).toBe('{"environment":"production","problem":null,"schema":"s"}\n');
  });

  test("keeps JSON.stringify semantics for toJSON, undefined and functions", () => {
    const error = failure("INVOCATION_INVALID");
    expect(JSON.parse(canonicalJson({ problem: error }))).toEqual(JSON.parse(JSON.stringify({ problem: error })));
    expect(canonicalJson({ b: undefined, a: () => 1, c: [undefined, 1] })).toBe('{"c":[null,1]}');
    expect(() => canonicalJson(undefined)).toThrow();
  });

  test("indent 2 matches serde_json to_string_pretty", () => {
    expect(canonicalJson({ b: [], a: {}, c: [{ z: 1, y: [2] }] }, 2))
      .toBe('{\n  "a": {},\n  "b": [],\n  "c": [\n    {\n      "y": [\n        2\n      ],\n      "z": 1\n    }\n  ]\n}');
  });
});

test("human runner errors match the shared fixture", async () => {
  const { formatHumanError, humanRunnerInvocation } = await import("../src/core/output");
  const { readFileSync } = await import("node:fs");
  const { join } = await import("node:path");
  const fixture = JSON.parse(readFileSync(join(import.meta.dir, "../../shared/fixtures/human/runner-errors.json"), "utf8"));
  for (const item of fixture.cases) {
    const rendered = formatHumanError({ schema: "openprose.runner-error/1", exitCode: 2, retryable: false, ...item.error });
    expect({ id: item.id, rendered }).toEqual({ id: item.id, rendered: item.rendered.split("{{RUNNER}}").join(humanRunnerInvocation()) });
  }
});

test("the human dry-run template matches the shared vectors", async () => {
  const { renderHumanTemplate } = await import("../src/core/output");
  const { readFileSync } = await import("node:fs");
  const { join } = await import("node:path");
  const root = join(import.meta.dir, "../../shared/fixtures/human");
  const fixture = JSON.parse(readFileSync(join(root, "dry-run.v1.json"), "utf8"));
  const template = readFileSync(join(root, "dry-run.v1.txt"), "utf8");
  for (const item of fixture.cases) {
    const values: Record<string, string> = {};
    const lists: Record<string, string[]> = {};
    for (const [name, value] of Object.entries(item.values)) {
      if (Array.isArray(value)) lists[name] = value as string[];
      else values[name] = value as string;
    }
    expect({ id: item.id, rendered: renderHumanTemplate(template, values, lists) }).toEqual({ id: item.id, rendered: item.rendered });
  }
});

test("quote and the Detail renderer match the shared fixture", async () => {
  const { humanSafeDetail, quote } = await import("../src/core/output");
  const { readFileSync } = await import("node:fs");
  const { join } = await import("node:path");
  const fixture = JSON.parse(readFileSync(join(import.meta.dir, "../../shared/fixtures/human/quoted-strings.json"), "utf8"));
  for (const item of fixture.cases) expect({ id: item.id, quoted: quote(item.input) }).toEqual({ id: item.id, quoted: item.quoted });
  for (const item of fixture.detailCases) expect({ id: item.id, rendered: humanSafeDetail(item.input) }).toEqual({ id: item.id, rendered: item.rendered });
  for (const [start, end] of fixture.escapedCodePointRanges as Array<[number, number]>) {
    for (let code = start; code <= end; code += 1) {
      if (code >= 0xd800 && code <= 0xdfff) continue;
      expect(quote(String.fromCodePoint(code)).includes(String.fromCodePoint(code))).toBe(false);
    }
  }
  // A lone surrogate (which a Rust string cannot hold) is escaped too.
  expect(quote("a\ud800b")).toBe("\"a\\ud800b\"");
});

test("canonical JSON and service JSON parsing match the shared vectors", async () => {
  const { canonicalJson } = await import("../src/core/output");
  const { parseJson } = await import("../src/core/service/http");
  const { readFileSync } = await import("node:fs");
  const { join } = await import("node:path");
  const shared = join(import.meta.dir, "../../shared/fixtures");
  const output = JSON.parse(readFileSync(join(shared, "human/json-output.json"), "utf8"));
  for (const item of output.cases) expect({ id: item.id, output: canonicalJson(JSON.parse(item.input)) }).toEqual({ id: item.id, output: item.output });
  const parse = JSON.parse(readFileSync(join(shared, "transport/json-parse.json"), "utf8"));
  expect(parse.maxSafeInteger).toBe(Number.MAX_SAFE_INTEGER);
  for (const item of parse.cases) {
    const bytes = typeof item.text === "string" ? new TextEncoder().encode(item.text) : Buffer.from(item.bytesHex, "hex");
    expect({ id: item.id, accepted: parseJson(bytes) !== undefined }).toEqual({ id: item.id, accepted: item.accepted });
  }
});
