import { test, expect } from "bun:test";
import { mkdtemp, writeFile, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import fixture from "../../shared/fixtures/adapters/native-output.v1.json";
import { runCli, type CliDependencies } from "../src/cli";
import { sentinelImage } from "../src/assets/sentinel";

// The same frozen controls can also assess an independently compiled Rust CLI.
for (const cell of fixture.recordLimits.cases) {
  test(`record limit black box: ${cell.name}`, async () => {
    const root = await mkdtemp(join(tmpdir(), "prose-record-limit-"));
    try {
      const pidFile = join(root, "native.pid");
      const record = JSON.stringify({ type: "item.completed", item: { type: "command_execution", id: "tool1", command: "fixture", aggregated_output: "", exit_code: 0, status: "completed" } });
      const padding = cell.recordBytes - Buffer.byteLength(record);
      const source = `#!${process.execPath}
const fs = require('node:fs');
if (process.argv.includes('--version')) { console.log('codex-cli 0.149.0-alpha.4.1'); }
else if (process.argv.slice(2).join(' ') === 'login status') { console.log('Logged in using ChatGPT'); }
else {
fs.writeFileSync(${JSON.stringify(pidFile)}, String(process.pid));
console.log(JSON.stringify({type:'thread.started',thread_id:'fixture'}));
console.log(JSON.stringify({type:'turn.started'}));
console.log(${JSON.stringify(record)}.replace('"aggregated_output":""', '"aggregated_output":"' + 'x'.repeat(${padding}) + '"'));
${cell.accepted ? "console.log(JSON.stringify({type:'turn.completed',usage:{input_tokens:1,output_tokens:1,cached_input_tokens:0}}));" : "setInterval(() => {},1000);"}
}
`;
      await writeFile(join(root, "codex"), source, { mode: 0o700 });
      const argv = ["--harness", "codex", "--auth-profile", "cached-chatgpt-login", "--output-contract", "native", "--native-output-bytes", String(cell.aggregateBytes), "--timeout", "5s", "--output", "json", "--", "execute", "fixture.md"];
      let stdout = "";
      const dependencies: CliDependencies = {
        platform: "darwin", arch: "arm64", processCwd: root,
        env: { PATH: root, HOME: root }, userConfigPath: join(root, "absent.toml"),
        imageBundle: sentinelImage,
        clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => performance.now() },
        ids: { invocationId: () => "00000000-0000-7000-8000-000000008989" },
        writeStdout: text => { stdout += text; }, writeStderr: () => {},
      };
      const code = await runCli(argv, dependencies);
      check(JSON.parse(stdout.trim()), code);
      await checkCleanup();
      stdout = "";
      const doctorCode = await runCli(["--harness", "codex", "--auth-profile", "cached-chatgpt-login", "--output-contract", "native", "--native-output-bytes", String(cell.aggregateBytes), "cli", "doctor", "--json"], dependencies);
      expect(doctorCode).toBe(0);
      expect(JSON.parse(stdout.trim()).nativeOutputLimits).toEqual({ maxRecordBytes: fixture.recordLimits.recordLimitBytes, maxAggregateStdoutBytes: cell.aggregateBytes, maxNativeCaptureBytes: cell.aggregateBytes, captureEnabled: false });
      for (const binary of [process.env.PROSE_RECORD_LIMIT_RUST_BINARY, process.env.PROSE_RECORD_LIMIT_BUN_BINARY]) {
        if (binary === undefined) continue;
        const child = Bun.spawn([binary, ...argv], { cwd: root, env: { PATH: root, HOME: root, XDG_CONFIG_HOME: root }, stdout: "pipe", stderr: "pipe" });
        const timer = setTimeout(() => child.kill(), 8000);
        try {
          const [output, _diagnostic, exit] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited]);
          check(JSON.parse(output.trim()), exit);
          await checkCleanup();
        } finally { clearTimeout(timer); }
      }
      async function checkCleanup() {
        const pid = Number(await readFile(pidFile, "utf8"));
        expect(() => process.kill(pid, 0)).toThrow();
      }
      function check(result: any, code: number) {
        expect(result.nativeOutputLimits.maxRecordBytes).toBe(fixture.recordLimits.recordLimitBytes);
        expect(result.nativeOutputLimits.maxAggregateStdoutBytes).toBe(cell.aggregateBytes);
        if (cell.accepted) {
          expect(code).toBe(0);
        } else {
          expect(code).toBe(fixture.recordLimits.error.exitCode);
          for (const [key, value] of Object.entries(fixture.recordLimits.error)) expect(result.error[key]).toEqual(value);
          expect(result.error.details.transportDiagnostic.reason).toBe("record-byte-limit");
          expect(result.error.details.transportDiagnostic.limitBytes).toBe(fixture.recordLimits.recordLimitBytes);
          expect(result.error.details.transportDiagnostic.observedBytes).toBeGreaterThan(fixture.recordLimits.recordLimitBytes);
          expect(result.error.details.terminalEventObserved).toBe(false);
          expect(result.semanticStatus).not.toBe("fulfilled");
        }
      }
    } finally { await rm(root, { recursive: true, force: true }); }
  }, 15000);
}
