import { describe, expect, test } from "bun:test";
import scalarFixture from "../../shared/fixtures/human/human-safe-scalars.json" with { type: "json" };
import { failure } from "../src/core/errors";
import { formatHumanError, humanRunnerCommand, humanRunnerInvocation, humanSafeMultiline, humanSafeScalar } from "../src/core/output";

describe("human error output", () => {
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
    expect(rendered).toContain("Select an available BYO harness with the `cli harness use <id>` runner operation, then invoke the `cli doctor` runner operation.");
    expect(rendered).not.toContain("Wait for OpenProse-hosted execution");
    expect(rendered).not.toContain("--harness <id>");
  });
});
